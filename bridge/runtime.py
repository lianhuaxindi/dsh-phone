"""DSH SDK runtime 解析链（D-021 / D-022）。

按以下顺序解析 SDK 运行时，**每一步都写日志**（选中路径 + 原因）：

1. ``DSH_PHONE_RUNTIME_EXE`` 环境变量（CI / 冒烟测试用，覆盖配置）
2. ``[dsh].runtimeExe`` 配置显式路径
3. 本机已安装的 DSH（``%PATH%`` 中的 ``dsh.cmd``/``dsh.exe`` + 候选列表，
   可用 ``DSH_PHONE_RUNTIME_CANDIDATES`` 环境变量追加）
4. SDK wheel 内置 runtime exe

显式来源（1/2）指向不存在的文件时**不静默回退**，而是直接报错——避免配置写错
却偷偷用了别的 runtime。全部失败时抛出 :class:`RuntimeUnavailable`，错误文案带
每个候选的探测结果与下一步操作建议。
"""

from __future__ import annotations

import importlib.util
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from .config import DshConfig

logger = logging.getLogger("bridge.dsh")

#: 显式指定 runtime 的环境变量（优先级最高，便于 CI）。
ENV_RUNTIME_EXE = "DSH_PHONE_RUNTIME_EXE"
#: 追加「本机已安装 DSH」候选路径的环境变量（``os.pathsep`` 分隔）。
ENV_RUNTIME_CANDIDATES = "DSH_PHONE_RUNTIME_CANDIDATES"

#: 本机已安装 DSH 的候选 runtime 路径。
#: D-022：模块逻辑只按「候选列表」探测，不依赖硬编码盘符分支；本机默认安装位置
#: 只是列表里的一项，可用环境变量追加更多位置。
_INSTALLED_RUNTIME_CANDIDATES: tuple[str, ...] = (
    r"D:\others\DSH\resources\runtime\cli\bin\dsh.cmd",
    r"D:\others\DSH\resources\runtime\cli\bin\dsh.exe",
    r"C:\Program Files\DSH\resources\runtime\cli\bin\dsh.cmd",
)

#: 错误提示里的下一步操作（D-022 要求的固定文案）。
NEXT_STEP_HINT = (
    "请安装 DSH 桌面版，或在 bridge/config.toml 的 [dsh].runtimeExe 指向已安装的 "
    "dsh.cmd；详见 docs/DECISIONS.md D-022"
)


class RuntimeUnavailable(RuntimeError):
    """无法解析出可用的 DSH runtime。"""

    def __init__(self, message: str, resolution: "RuntimeResolution") -> None:
        super().__init__(message)
        self.resolution = resolution


@dataclass(frozen=True)
class CandidateProbe:
    """单个 runtime 候选的探测结果。"""

    source: str
    path: str
    exists: bool
    detail: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "source": self.source,
            "path": self.path,
            "exists": self.exists,
            "detail": self.detail,
        }

    def describe(self) -> str:
        status = "存在" if self.exists else "不存在"
        extra = f"；{self.detail}" if self.detail else ""
        return f"[{self.source}] {self.path or '(未提供路径)'} -> {status}{extra}"


@dataclass(frozen=True)
class RuntimeResolution:
    """runtime 解析结果。"""

    selected: Path | None
    source: str  # env | config | installed | bundled | none
    candidates: tuple[CandidateProbe, ...]
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.selected is not None

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "selected": str(self.selected) if self.selected else None,
            "source": self.source,
            "error": self.error,
            "candidates": [c.as_dict() for c in self.candidates],
        }


def _probe_file(source: str, path: str, detail: str = "") -> CandidateProbe:
    raw = (path or "").strip()
    if not raw:
        return CandidateProbe(source=source, path="", exists=False, detail=detail or "路径为空")
    expanded = Path(os.path.expandvars(os.path.expanduser(raw)))
    try:
        exists = expanded.is_file()
    except OSError as exc:  # pragma: no cover - 防御
        return CandidateProbe(source=source, path=str(expanded), exists=False, detail=f"探测失败: {exc}")
    return CandidateProbe(source=source, path=str(expanded), exists=exists, detail=detail)


def _installed_candidates(env: Mapping[str, str]) -> list[CandidateProbe]:
    probes: list[CandidateProbe] = []
    seen: set[str] = set()
    for name in ("dsh.cmd", "dsh.exe"):
        found = shutil.which(name)
        if found and found not in seen:
            seen.add(found)
            probes.append(_probe_file("installed", found, f"%PATH% 中的 {name}"))
    extra: list[str] = list(_INSTALLED_RUNTIME_CANDIDATES)
    override = (env.get(ENV_RUNTIME_CANDIDATES) or "").strip()
    if override:
        extra.extend(p for p in override.split(os.pathsep) if p.strip())
    for raw in extra:
        expanded = str(Path(os.path.expandvars(os.path.expanduser(raw))))
        if expanded in seen:
            continue
        seen.add(expanded)
        probes.append(_probe_file("installed", raw, "已安装 DSH 的候选位置"))
    return probes


def _bundled_candidate() -> CandidateProbe:
    module_name = "deepseek_harness_runtime"
    try:
        spec = importlib.util.find_spec(module_name)
    except (ImportError, ValueError) as exc:  # pragma: no cover - 防御
        spec = None
        detail = f"查找 {module_name} 失败: {exc}"
    else:
        detail = ""
    if spec is None:
        return CandidateProbe(
            source="bundled",
            path="",
            exists=False,
            detail="SDK 未安装（wheel 内置 runtime 不可探测）：先安装 bridge/requirements-sdk.txt",
        )
    try:
        from deepseek_harness_runtime import resolve_bundled_launch_args

        launch_args = resolve_bundled_launch_args()
    except Exception as exc:  # pragma: no cover - 依赖 SDK 版本
        return CandidateProbe(
            source="bundled", path="", exists=False, detail=f"解析 wheel 内置 runtime 失败: {exc}"
        )
    if not launch_args:
        return CandidateProbe(source="bundled", path="", exists=False, detail="SDK 未返回内置 runtime 参数")
    exe = Path(str(launch_args[0]))
    return _probe_file("bundled", str(exe), "SDK wheel 内置 runtime")


def resolve_runtime(
    config: DshConfig | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> RuntimeResolution:
    """按 D-022 顺序解析 runtime，并记录每个候选的探测日志。"""
    environ: Mapping[str, str] = os.environ if env is None else env
    probes: list[CandidateProbe] = []

    explicit = (environ.get(ENV_RUNTIME_EXE) or "").strip()
    if explicit:
        probes.append(_probe_file("env", explicit, f"环境变量 {ENV_RUNTIME_EXE}（覆盖配置）"))

    configured = (config.runtime_exe if config is not None else "").strip()
    if configured:
        probes.append(_probe_file("config", configured, "[dsh].runtimeExe"))

    explicit_probes = list(probes)
    if not explicit_probes:
        probes.extend(_installed_candidates(environ))
        probes.append(_bundled_candidate())

    for probe in probes:
        logger.info(
            "dsh.runtime.candidate",
            extra={
                "event": "dsh.runtime.candidate",
                "fields": {
                    "source": probe.source,
                    "path": probe.path,
                    "exists": probe.exists,
                    "detail": probe.detail,
                },
            },
        )

    selected_probe = next((p for p in probes if p.exists), None)
    if selected_probe is not None:
        resolution = RuntimeResolution(
            selected=Path(selected_probe.path),
            source=selected_probe.source,
            candidates=tuple(probes),
        )
        logger.info(
            "dsh.runtime.selected",
            extra={
                "event": "dsh.runtime.selected",
                "fields": {
                    "source": resolution.source,
                    "path": str(resolution.selected),
                },
            },
        )
        return resolution

    lines = [probe.describe() for probe in probes] or ["(没有可探测的 runtime 候选)"]
    if explicit_probes:
        reason = "显式配置的 runtime 路径不存在，已停止自动回退（避免误用其它运行时）"
    else:
        reason = "所有 runtime 候选都不可用"
    message = (
        f"无法找到可用的 DSH SDK runtime：{reason}。\n"
        + "\n".join(f"  - {line}" for line in lines)
        + f"\n下一步：{NEXT_STEP_HINT}"
    )
    logger.error(
        "dsh.runtime.unavailable",
        extra={
            "event": "dsh.runtime.unavailable",
            "fields": {"candidates": lines, "hint": NEXT_STEP_HINT},
        },
    )
    return RuntimeResolution(
        selected=None, source="none", candidates=tuple(probes), error=message
    )


def require_runtime(
    config: DshConfig | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> Path:
    """解析 runtime；失败时抛 :class:`RuntimeUnavailable`（可读错误）。"""
    resolution = resolve_runtime(config, env=env)
    if resolution.selected is None:
        raise RuntimeUnavailable(resolution.error or "无法找到 DSH runtime", resolution)
    return resolution.selected


@dataclass(frozen=True)
class VersionProbe:
    """``--version`` 轻量探测结果。"""

    ok: bool
    output: str = ""
    error: str = ""

    def as_dict(self) -> dict[str, object]:
        return {"ok": self.ok, "output": self.output, "error": self.error}


def probe_version(path: str | Path, *, timeout: float = 20.0) -> VersionProbe:
    """对 runtime 做 ``--version`` 轻量探测（不启动 session、不发 prompt）。"""
    target = Path(path)
    suffix = target.suffix.lower()
    if suffix in {".cmd", ".bat"}:
        comspec = os.environ.get("COMSPEC") or "cmd.exe"
        command: Sequence[str] = (comspec, "/c", str(target), "--version")
    else:
        command = (str(target), "--version")
    try:
        proc = subprocess.run(
            list(command),
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(target.parent),
        )
    except FileNotFoundError as exc:
        return VersionProbe(ok=False, error=f"无法执行: {exc}")
    except subprocess.TimeoutExpired:
        return VersionProbe(ok=False, error=f"--version 超时（>{timeout:.0f}s）")
    except OSError as exc:  # pragma: no cover - 防御
        return VersionProbe(ok=False, error=f"执行失败: {exc}")
    output = (proc.stdout or "").strip()
    if proc.returncode != 0:
        error = (proc.stderr or proc.stdout or "").strip()
        return VersionProbe(ok=False, output=output, error=f"退出码 {proc.returncode}: {error[:300]}")
    return VersionProbe(ok=True, output=output)


__all__ = [
    "ENV_RUNTIME_EXE",
    "ENV_RUNTIME_CANDIDATES",
    "NEXT_STEP_HINT",
    "RuntimeUnavailable",
    "CandidateProbe",
    "RuntimeResolution",
    "VersionProbe",
    "resolve_runtime",
    "require_runtime",
    "probe_version",
]
