"""ASR 模型清单、本地查找、完整性校验与下载核心。

本模块只依赖 Python 标准库，**不导入 sherpa_onnx**，因此可以被
``scripts/download_models.py``、测试和 Bridge 启动路径安全复用。

模型分层
--------
- partial（流式）：sherpa-onnx 流式 zipformer，中英双语，16 kHz，chunk ≈ 320 ms。
- final（定稿）：SenseVoice（sherpa-onnx offline）整段复判，内置 ITN。

模型目录解析优先级（``resolve_models_dir``）::

    显式参数  >  环境变量 DSH_PHONE_MODELS_DIR  >  <repo>/models

模型不提交 git（``models/*`` 已在 .gitignore）；下载见
``python scripts/download_models.py``。

错误约定
--------
所有模型相关异常都继承 :class:`ModelError`，并带 ``code = 2002`` /
``retryable = True`` 属性，方便上层 Bridge 直接映射成
``error{code:2002,"retryable":true}``（PROTOCOL §10.1）。
"""

from __future__ import annotations

import dataclasses
import hashlib
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence

__all__ = [
    "MODELS",
    "DEFAULT_STREAMING_MODEL",
    "DEFAULT_FINAL_MODEL",
    "ModelError",
    "ModelNotFoundError",
    "UnknownModelError",
    "ModelIntegrityError",
    "DownloadError",
    "ModelFile",
    "ModelSpec",
    "ModelCheck",
    "DownloadItem",
    "repo_root",
    "default_models_dir",
    "resolve_models_dir",
    "get_spec",
    "model_dir",
    "missing_files",
    "check_model",
    "require_model",
    "verify_model",
    "download_plan",
    "download_file",
    "download_model",
    "format_bytes",
    "MODEL_ALIASES",
]

# --------------------------------------------------------------------------
# 异常
# --------------------------------------------------------------------------


class ModelError(RuntimeError):
    """模型不可用的基类。上层应转成 ``error{code:2002}``。"""

    code = 2002
    retryable = True


class UnknownModelError(ModelError):
    """清单里没有这个模型名。"""


class ModelNotFoundError(ModelError):
    """模型文件缺失（目录不存在或缺少部分文件）。"""


class ModelIntegrityError(ModelError):
    """模型文件存在但大小/sha256 不匹配（下载损坏或版本不符）。"""


class DownloadError(ModelError):
    """下载失败（网络、HTTP 状态、写盘）。"""


# --------------------------------------------------------------------------
# 数据模型
# --------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class ModelFile:
    """模型目录内的一个文件。

    ``rel_path`` 相对模型根目录（允许子目录），``size`` 为字节数，
    ``sha256`` 为小写十六进制摘要（空串表示该文件不做哈希校验）。
    """

    rel_path: str
    size: int
    sha256: str = ""

    def __post_init__(self) -> None:
        if not self.rel_path or self.rel_path.startswith(("/", "\\")):
            raise ValueError(f"非法模型文件路径: {self.rel_path!r}")
        if ".." in Path(self.rel_path).parts:
            raise ValueError(f"模型文件路径不允许包含 '..': {self.rel_path!r}")
        if self.size < 0:
            raise ValueError(f"非法模型文件大小: {self.size!r}")
        if self.sha256 and len(self.sha256) != 64:
            raise ValueError(f"sha256 必须是 64 位十六进制: {self.sha256!r}")


@dataclasses.dataclass(frozen=True)
class ModelSpec:
    """一个模型的清单条目。

    ``hf_repo`` 为 HuggingFace 仓库 id，文件 URL 由
    ``hf_repo + resolve/main + rel_path`` 拼接；``alt_bases`` 是额外的
    下载基地址（镜像）。任何基地址都可以被 CLI 的 ``--base-url`` 覆盖。
    """

    name: str
    kind: str  # "streaming" | "offline"
    description: str
    files: tuple[ModelFile, ...]
    hf_repo: str = ""
    url_bases: tuple[str, ...] = ()
    license: str = ""
    notes: str = ""

    def __post_init__(self) -> None:
        if self.kind not in ("streaming", "offline"):
            raise ValueError(f"{self.name}: kind 必须是 streaming/offline，收到 {self.kind!r}")
        if not self.files:
            raise ValueError(f"{self.name}: 清单不能为空")

    @property
    def total_size(self) -> int:
        return sum(f.size for f in self.files)

    def file(self, rel_path: str) -> ModelFile:
        for f in self.files:
            if f.rel_path == rel_path:
                return f
        raise KeyError(rel_path)

    def hf_url_bases(self) -> tuple[str, ...]:
        """默认下载基地址：HF 官方 + hf-mirror（国内可达）。"""
        bases: list[str] = []
        if self.hf_repo:
            bases.append(f"https://huggingface.co/{self.hf_repo}/resolve/main")
            bases.append(f"https://hf-mirror.com/{self.hf_repo}/resolve/main")
        bases.extend(self.url_bases)
        # 去重保序
        seen: set[str] = set()
        out: list[str] = []
        for b in bases:
            b = b.rstrip("/")
            if b and b not in seen:
                seen.add(b)
                out.append(b)
        return tuple(out)

    def file_urls(self, rel_path: str, base_url: str | None = None) -> tuple[str, ...]:
        """某文件的候选 URL 列表（``base_url`` 优先，其次官方/镜像）。"""
        bases: list[str] = []
        if base_url:
            bases.append(base_url.rstrip("/"))
        bases.extend(self.hf_url_bases())
        quoted = urllib.parse.quote(rel_path)
        return tuple(f"{b}/{quoted}" for b in bases)

    def dir_name(self) -> str:
        """模型在 models/ 下的目录名（用仓库名更好读，没仓库名就用 name）。"""
        return self.hf_repo.split("/")[-1] if self.hf_repo else self.name


@dataclasses.dataclass(frozen=True)
class ModelCheck:
    """一次本地模型检查的结果。"""

    spec: ModelSpec
    directory: Path
    missing: tuple[str, ...] = ()
    bad_size: tuple[str, ...] = ()
    bad_hash: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not (self.missing or self.bad_size or self.bad_hash)

    @property
    def present(self) -> bool:
        return not self.missing

    def problem_lines(self) -> list[str]:
        lines: list[str] = []
        for f in self.missing:
            lines.append(f"缺失: {f}")
        for f in self.bad_size:
            lines.append(f"大小不符: {f}")
        for f in self.bad_hash:
            lines.append(f"sha256 不符: {f}")
        return lines


@dataclasses.dataclass(frozen=True)
class DownloadItem:
    """下载计划中的一项（一个文件）。"""

    spec: ModelSpec
    file: ModelFile
    dest: Path
    urls: tuple[str, ...]
    present: bool

    @property
    def size(self) -> int:
        return self.file.size


# --------------------------------------------------------------------------
# 路径解析
# --------------------------------------------------------------------------


def repo_root() -> Path:
    """仓库根目录（``bridge/asr/models.py`` 往上三级）。"""
    return Path(__file__).resolve().parents[2]


def default_models_dir() -> Path:
    return repo_root() / "models"


MODELS_DIR_ENV = "DSH_PHONE_MODELS_DIR"


def resolve_models_dir(explicit: str | os.PathLike[str] | None = None) -> Path:
    """解析模型根目录：显式参数 > 环境变量 > <repo>/models。

    返回值不需要已存在（下载脚本会创建）。不做 ``expanduser`` 之外的
    兼容处理；相对路径按当前工作目录解析（与其余 CLI 一致）。
    """
    if explicit:
        return Path(explicit).expanduser().resolve()
    env = os.environ.get(MODELS_DIR_ENV)
    if env:
        return Path(env).expanduser().resolve()
    return default_models_dir()


def model_dir(
    spec_or_name: ModelSpec | str,
    models_dir: str | os.PathLike[str] | None = None,
) -> Path:
    """模型根目录下该模型的子目录（不保证存在）。"""
    spec = get_spec(spec_or_name)
    return resolve_models_dir(models_dir) / spec.dir_name()


# --------------------------------------------------------------------------
# 清单
# --------------------------------------------------------------------------

_STREAMING_2023_02_20 = ModelSpec(
    name="streaming-zipformer-bilingual-zh-en",
    kind="streaming",
    description=(
        "sherpa-onnx 流式 zipformer 中英双语（16kHz，chunk≈320ms，CPU int8），"
        "用于 asr.partial 实时预览"
    ),
    hf_repo="csukuangfj/sherpa-onnx-streaming-zipformer-bilingual-zh-en-2023-02-20",
    license="Apache-2.0 (sherpa-onnx 模型页)",
    notes="int8 量化；encoder 约 173 MiB。",
    files=(
        ModelFile(
            "encoder-epoch-99-avg-1.int8.onnx",
            181_895_032,
            "8fa764187a261844f859d7143ebaa563af5d10adfece4c18a8f414c88cba2a9b",
        ),
        ModelFile(
            "decoder-epoch-99-avg-1.int8.onnx",
            13_091_040,
            "1a70c593d71e53f023f5f55b0b4cfff5055abb786ee3992e5f63dc2e273cc4fa",
        ),
        ModelFile(
            "joiner-epoch-99-avg-1.int8.onnx",
            3_228_404,
            "1ed689c5ed19dbaa725d9d191bb4822b5f4855a39e1ffd28cbc1f340d25b2ee0",
        ),
        ModelFile(
            "tokens.txt",
            56_317,
            "a8e0e4ec53810e433789b54a5c0134a7eaa2ffca595a6334d54c00da858841d3",
        ),
    ),
)

_SENSEVOICE_2024_07_17 = ModelSpec(
    name="sensevoice-small",
    kind="offline",
    description=(
        "SenseVoice-Small（offline，zh/en/ja/ko/yue，int8，内置 ITN），"
        "用于松手/静音后的 asr.final 整段复判"
    ),
    hf_repo="csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17",
    license="SenseVoice 模型许可（见模型页 README/LICENSE）",
    notes="int8 约 228 MiB；语言可 auto 或 zh/en/...，use_itn=True。",
    files=(
        ModelFile(
            "model.int8.onnx",
            239_233_841,
            "c71f0ce00bec95b07744e116345e33d8cbbe08cef896382cf907bf4b51a2cd51",
        ),
        ModelFile(
            "tokens.txt",
            315_894,
            "f449eb28dc567533d7fa59be34e2abca8784f771850c78a47fb731a31429a1dc",
        ),
    ),
)

MODELS: dict[str, ModelSpec] = {
    _STREAMING_2023_02_20.name: _STREAMING_2023_02_20,
    _SENSEVOICE_2024_07_17.name: _SENSEVOICE_2024_07_17,
}

# 易用别名（bridge 配置/CLI 里可以写短名）
MODEL_ALIASES: dict[str, str] = {
    "streaming": _STREAMING_2023_02_20.name,
    "streaming-zipformer": _STREAMING_2023_02_20.name,
    "partial": _STREAMING_2023_02_20.name,
    "zipformer": _STREAMING_2023_02_20.name,
    "sensevoice": _SENSEVOICE_2024_07_17.name,
    "sensevoice-small": _SENSEVOICE_2024_07_17.name,
    "final": _SENSEVOICE_2024_07_17.name,
    "offline": _SENSEVOICE_2024_07_17.name,
}

DEFAULT_STREAMING_MODEL = _STREAMING_2023_02_20.name
DEFAULT_FINAL_MODEL = _SENSEVOICE_2024_07_17.name
DEFAULT_MODELS = (DEFAULT_STREAMING_MODEL, DEFAULT_FINAL_MODEL)


def get_spec(name: str | ModelSpec) -> ModelSpec:
    """按名称/别名取清单条目；未知名称抛 :class:`UnknownModelError`。"""
    if isinstance(name, ModelSpec):
        return name
    key = (name or "").strip()
    canonical = MODEL_ALIASES.get(key, key)
    try:
        return MODELS[canonical]
    except KeyError:
        known = ", ".join(sorted(MODELS))
        raise UnknownModelError(
            f"未知 ASR 模型 {name!r}；可用模型: {known}（别名见 MODEL_ALIASES）"
        ) from None


# --------------------------------------------------------------------------
# 本地查找 / 校验
# --------------------------------------------------------------------------


def _sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            block = fh.read(chunk_size)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def missing_files(
    spec_or_name: ModelSpec | str,
    models_dir: str | os.PathLike[str] | None = None,
) -> list[str]:
    """返回缺失文件的相对路径列表（空列表 = 全部存在）。"""
    spec = get_spec(spec_or_name)
    root = model_dir(spec, models_dir)
    return [f.rel_path for f in spec.files if not (root / f.rel_path).is_file()]


def check_model(
    spec_or_name: ModelSpec | str,
    models_dir: str | os.PathLike[str] | None = None,
    *,
    verify_hash: bool = False,
) -> ModelCheck:
    """检查模型是否就绪。

    ``verify_hash=False``（默认）只做存在性 + 大小检查（启动路径快）；
    ``verify_hash=True`` 再算 sha256（下载后/诊断用）。
    """
    spec = get_spec(spec_or_name)
    root = model_dir(spec, models_dir)
    missing: list[str] = []
    bad_size: list[str] = []
    bad_hash: list[str] = []
    for f in spec.files:
        p = root / f.rel_path
        if not p.is_file():
            missing.append(f.rel_path)
            continue
        if f.size and p.stat().st_size != f.size:
            bad_size.append(f.rel_path)
            continue
        if verify_hash and f.sha256 and _sha256_file(p) != f.sha256:
            bad_hash.append(f.rel_path)
    return ModelCheck(
        spec=spec,
        directory=root,
        missing=tuple(missing),
        bad_size=tuple(bad_size),
        bad_hash=tuple(bad_hash),
    )


_check_to_error: dict[str, type[ModelError]] = {
    "missing": ModelNotFoundError,
    "bad_size": ModelIntegrityError,
    "bad_hash": ModelIntegrityError,
}


def require_model(
    spec_or_name: ModelSpec | str,
    models_dir: str | os.PathLike[str] | None = None,
    *,
    verify_hash: bool = False,
) -> Path:
    """确认模型就绪并返回模型目录；否则抛带可执行提示的 :class:`ModelError`。

    上层（sherpa 后端/``asr/__init__.py``）捕获后映射为
    ``error{code:2002,"retryable":true}``。
    """
    check = check_model(spec_or_name, models_dir, verify_hash=verify_hash)
    if check.ok:
        return check.directory
    problems = check.problem_lines()
    hint = (
        f"ASR 模型 {check.spec.name!r} 不可用（{'; '.join(problems)}）。"
        f"模型目录: {check.directory}。"
        f"请运行: python scripts/download_models.py --model {check.spec.name} "
        f"--models-dir \"{check.directory.parent}\""
    )
    if check.missing or check.bad_size:
        raise ModelNotFoundError(hint)
    raise ModelIntegrityError(hint)


def verify_model(
    spec_or_name: ModelSpec | str,
    models_dir: str | os.PathLike[str] | None = None,
) -> ModelCheck:
    """强制做完整 sha256 校验（下载完成/诊断页用）。"""
    return check_model(spec_or_name, models_dir, verify_hash=True)


# --------------------------------------------------------------------------
# 下载
# --------------------------------------------------------------------------


def download_plan(
    spec_or_name: ModelSpec | str,
    models_dir: str | os.PathLike[str] | None = None,
    *,
    base_url: str | None = None,
    force: bool = False,
) -> list[DownloadItem]:
    """生成下载计划：默认只包含缺失文件；``force=True`` 包含全部。"""
    spec = get_spec(spec_or_name)
    root = model_dir(spec, models_dir)
    plan: list[DownloadItem] = []
    for f in spec.files:
        dest = root / f.rel_path
        present = dest.is_file() and (not f.size or dest.stat().st_size == f.size)
        if present and not force:
            continue
        plan.append(
            DownloadItem(
                spec=spec,
                file=f,
                dest=dest,
                urls=spec.file_urls(f.rel_path, base_url),
                present=present,
            )
        )
    return plan


def _open_url(url: str, timeout: float, start: int = 0):
    """打开 URL 返回 (响应对象, 是否支持 Range)。``file://``/本地路径用于测试。"""
    parsed = urllib.parse.urlparse(url)
    headers = {"User-Agent": "dsh-phone-bridge/1.0 (+models)"}
    # Windows 盘符路径（如 "D:\models\x.onnx"）会被 urlparse 解析成 scheme="d"，
    # 所以「单字符 scheme」也要按本地文件处理。
    if parsed.scheme in ("", "file") or len(parsed.scheme) == 1:
        if parsed.scheme == "file":
            path = Path(urllib.request.url2pathname(parsed.path))
        else:
            path = Path(url)
        if start:
            raise DownloadError(f"本地文件不支持断点续传: {url}")
        return path.open("rb"), False, path.stat().st_size
    if start:
        headers["Range"] = f"bytes={start}-"
    req = urllib.request.Request(url, headers=headers)
    resp = urllib.request.urlopen(req, timeout=timeout)  # noqa: S310 - 模型 URL 白名单来自清单
    length = resp.headers.get("Content-Length")
    total = int(length) + start if length is not None else 0
    supports_range = resp.status == 206 or resp.headers.get("Accept-Ranges") == "bytes"
    return resp, supports_range, total


def download_file(
    urls: str | Sequence[str],
    dest: str | os.PathLike[str],
    *,
    size: int = 0,
    sha256: str = "",
    resume: bool = True,
    timeout: float = 60.0,
    retries: int = 2,
    chunk_size: int = 1024 * 1024,
    progress: Callable[[int, int], None] | None = None,
) -> Path:
    """下载单个文件到 ``dest``（先写 ``.part``，校验后原子改名）。

    参数
    ----
    urls
        候选 URL，按顺序尝试；支持 ``file://``（测试用）。
    size / sha256
        期望大小与摘要（来自清单）；``size=0``/``sha256=""`` 表示不校验。
    resume
        为真时利用 ``.part`` + HTTP Range 断点续传；服务器不支持 Range
        时自动从头下载。
    progress
        ``progress(downloaded_bytes, total_bytes)``；``total=0`` 表示未知。

    返回最终文件路径；失败抛 :class:`DownloadError` / :class:`ModelIntegrityError`。
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    url_list = [urls] if isinstance(urls, str) else list(urls)
    if not url_list:
        raise DownloadError(f"没有可用的下载地址: {dest}")

    if resume and part.is_file() and part.stat().st_size > 0 and size and part.stat().st_size >= size:
        # 之前已下完但没来得及改名/校验；直接进入校验流程
        pass
    elif not resume and part.exists():
        part.unlink()

    last_err: Exception | None = None
    for url in url_list:
        attempts = max(1, retries + 1)
        for attempt in range(attempts):
            start = part.stat().st_size if (resume and part.is_file()) else 0
            try:
                resp, supports_range, total = _open_url(url, timeout, start)
                if start and (not supports_range or resp.status != 206):
                    # 服务器忽略 Range：从头来
                    resp.close()
                    if isinstance(url, str) and urllib.parse.urlparse(url).scheme in ("", "file"):
                        start = 0
                    else:
                        part.unlink(missing_ok=True)
                        start = 0
                        continue
                if total == 0:
                    total = start + (size or 0)
                mode = "ab" if start else "wb"
                downloaded = start
                with part.open(mode) as out:
                    while True:
                        block = resp.read(chunk_size)
                        if not block:
                            break
                        out.write(block)
                        downloaded += len(block)
                        if progress:
                            progress(downloaded, total)
                resp.close()
                if size and part.stat().st_size != size:
                    raise DownloadError(
                        f"下载大小不符: {dest.name} 期望 {size} 实际 {part.stat().st_size} ({url})"
                    )
                if sha256:
                    actual = _sha256_file(part)
                    if actual != sha256:
                        part.unlink(missing_ok=True)
                        raise ModelIntegrityError(
                            f"sha256 校验失败: {dest.name} 期望 {sha256} 实际 {actual}"
                        )
                os.replace(part, dest)
                return dest
            except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, DownloadError, ModelIntegrityError) as exc:
                last_err = exc
                # 大小/哈希错误换下一个源前先删脏文件
                if isinstance(exc, (DownloadError, ModelIntegrityError)):
                    part.unlink(missing_ok=True)
                if attempt + 1 < attempts:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                break  # 换下一个 URL
        # 下一个候选 URL
    # 所有源都失败：哈希/大小错误更有诊断价值，直接透出对应异常
    if isinstance(last_err, ModelIntegrityError):
        raise last_err
    raise DownloadError(
        f"下载失败: {dest.name}（已尝试 {len(url_list)} 个源）: {last_err}"
    ) from last_err


ProgressFn = Callable[[str, int, int], None]


def download_model(
    spec_or_name: ModelSpec | str,
    models_dir: str | os.PathLike[str] | None = None,
    *,
    base_url: str | None = None,
    force: bool = False,
    resume: bool = True,
    timeout: float = 60.0,
    retries: int = 2,
    progress: ProgressFn | None = None,
) -> Path:
    """下载并校验一个模型的全部文件，返回模型目录。

    ``progress(name, downloaded, total)`` 按文件回调（下载中多次）。
    已存在且大小正确的文件默认跳过（``force=True`` 重下）。
    """
    spec = get_spec(spec_or_name)
    plan = download_plan(spec, models_dir, base_url=base_url, force=force)
    root = model_dir(spec, models_dir)
    root.mkdir(parents=True, exist_ok=True)
    if not plan:
        if progress:
            progress(spec.name, spec.total_size, spec.total_size)
        return root

    for item in plan:
        if progress:
            progress(f"{spec.name}/{item.file.rel_path}", 0, item.size)

        def _cb(downloaded: int, total: int, _item: DownloadItem = item) -> None:
            if progress:
                progress(f"{spec.name}/{_item.file.rel_path}", downloaded, total)

        download_file(
            item.urls,
            item.dest,
            size=item.file.size,
            sha256=item.file.sha256,
            resume=resume,
            timeout=timeout,
            retries=retries,
            progress=_cb,
        )

    # 全量 sha256 校验（下载完成后）
    check = verify_model(spec, models_dir)
    if not check.ok:
        raise ModelIntegrityError(
            f"模型 {spec.name} 下载后校验失败: {'; '.join(check.problem_lines())}"
        )
    return root


# --------------------------------------------------------------------------
# 展示辅助
# --------------------------------------------------------------------------


def format_bytes(num: int | float) -> str:
    """人类可读大小（1024 进制，1 位小数）。"""
    num = float(num)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if abs(num) < 1024.0 or unit == "GiB":
            return f"{num:.1f} {unit}" if unit != "B" else f"{int(num)} B"
        num /= 1024.0
    return f"{num:.1f} GiB"


def main(argv: Iterable[str] | None = None) -> int:  # pragma: no cover - 便捷入口
    """``python -m bridge.asr.models`` 打印模型状态摘要。"""
    args = list(argv if argv is not None else sys.argv[1:])
    models_dir = args[0] if args else None
    root = resolve_models_dir(models_dir)
    print(f"模型目录: {root}")
    for name, spec in MODELS.items():
        check = check_model(spec, root)
        state = "OK" if check.ok else "缺失/损坏: " + "; ".join(check.problem_lines())
        print(f"- {name} [{spec.kind}] {format_bytes(spec.total_size)}: {state}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
