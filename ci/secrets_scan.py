#!/usr/bin/env python3
"""ci/secrets_scan.py —— 仓库敏感内容 / 违规产物扫描（纯标准库，无第三方依赖）。

用途：CI 的 secrets-scan job + 本地提交前自检。检查三类问题：

1. 禁止入库的文件：本机配置（local.properties 等）、keystore / 证书 / 私钥、
   APK / AAB 产物、模型权重、运行态数据、构建生成的 web assets；
2. 高置信密钥 / 凭据内容：完整私钥 / PEM 证书块（多行块，代码里逐行比较
   ``"-----BEGIN CERTIFICATE-----"`` 这类标记不算）、云厂商 token、OpenAI 风格 key；
3. 中置信敏感内容：64 位 hex token / 密钥、形如 token="..." 的写死凭据。

模式（互斥，默认 --working-tree）：
  --all                 扫描全部 git 跟踪文件（全仓体检）
  --range BASE..HEAD    扫描该提交区间内新增 / 修改文件的新增行（CI 用）
  --working-tree        扫描未提交改动（git diff HEAD 的新增行 + 未跟踪文件全部内容）

退出码：0 = 干净（可能含测试夹具告警）；1 = 发现必须阻塞的问题；2 = 用法 / git 错误。

设计约定：
  * --all 只扫 git 跟踪文件；被 .gitignore 忽略的文件本来就不会入库，无需扫描。
  * 二进制 / 超大文件跳过内容扫描，但文件名规则仍然生效。
  * 输出会遮蔽敏感片段，避免把密钥原样复制进 CI 日志。
  * 测试/演示夹具例外：tests/、test_*.py、*.test.mjs、mock.js、mocks/、fixtures/
    等路径下的高置信命中只告警不阻塞、中置信规则直接跳过（测试和 WebUI 的 mock
    数据里常见 64 hex 假 token，不应让 CI 变红）；文档（docs/、*.md）同理，
    因为协议文档会包含示例 token。真密钥请务必不要放这些位置。
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SELF_PATH = "ci/secrets_scan.py"

# ---------------------------------------------------------------------------
# 文件名 / 路径规则
# ---------------------------------------------------------------------------
BLOCKED_FILENAMES = {
    "local.properties": "Android SDK 本机路径配置，不应入库",
    "keystore.properties": "签名配置（含口令），不应入库",
    "google-services.json": "Firebase 配置，可能含凭据，不应入库",
    "id_rsa": "SSH 私钥，不应入库",
    "id_ed25519": "SSH 私钥，不应入库",
    "id_ecdsa": "SSH 私钥，不应入库",
    "id_dsa": "SSH 私钥，不应入库",
}

BLOCKED_SUFFIXES = {
    ".jks": "Android keystore，不应入库",
    ".keystore": "Android keystore，不应入库",
    ".p12": "PKCS#12 证书/私钥，不应入库",
    ".pfx": "PKCS#12 证书/私钥，不应入库",
    ".pem": "证书/私钥文件，不应入库",
    ".key": "私钥文件，不应入库",
    ".crt": "证书文件，不应入库",
    ".cer": "证书文件，不应入库",
    ".der": "证书文件，不应入库",
    ".apk": "构建产物，不应入库（CI 产物走 Actions Artifact / Releases）",
    ".aab": "构建产物，不应入库（CI 产物走 Actions Artifact / Releases）",
    ".onnx": "模型权重，不应入库",
    ".gguf": "模型权重，不应入库",
    ".safetensors": "模型权重，不应入库",
    ".pt": "模型权重，不应入库",
    ".pth": "模型权重，不应入库",
    ".sqlite3": "运行态数据库，不应入库",
}

# 生成物 / 运行态目录（path 前缀，使用 POSIX 分隔符）
BLOCKED_PREFIXES = (
    ("app/android/app/src/main/assets/web/", "lib→Android 的构建生成资源（scripts/sync_web.sh 产出），不应入库"),
    ("bridge/state/", "bridge 运行态数据，不应入库"),
    ("bridge/certs/", "运行时生成的证书，不应入库"),
)

MODELS_PREFIX = "models/"

# .env* 允许入库的模板名
ENV_TEMPLATE_NAMES = {".env.example", ".env.sample", ".env.template"}

# ---------------------------------------------------------------------------
# 内容规则
# ---------------------------------------------------------------------------
HIGH_PATTERNS = (
    # 私钥 / PEM 证书块按“完整多行块”检测（PEM_BLOCK_RE，扫全文/整段新增行），
    # 单行只出现 ``b"-----BEGIN CERTIFICATE-----"`` 的字符串比较代码不应误报。
    ("aws-access-key-id", re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AWS access key id"),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"), "GitHub token"),
    ("slack-token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"), "Slack token"),
    ("openai-style-key", re.compile(r"\bsk-[A-Za-z0-9]{32,}\b"), "OpenAI 风格 API key"),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"), "Google API key"),
)

MEDIUM_PATTERNS = (
    ("hex-token-64", re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{64}(?![0-9A-Fa-f])"), "64 位 hex token / 密钥"),
    (
        "secret-assignment",
        re.compile(
            r"""(?i)\b(?:device[_-]?token|pairing[_-]?(?:token|code)|access[_-]?token|"""
            r"""api[_-]?key|client[_-]?secret|password)\b\s*[:=]\s*['"][^'"\s]{16,}['"]"""
        ),
        "疑似写死的密钥 / 口令",
    ),
)

# 完整 PEM 块：BEGIN 行 + 至少一行 base64 + 匹配的 END 行（允许 CRLF）。
# 只匹配真正的证书/私钥块；代码中比较 ``"-----BEGIN CERTIFICATE-----"`` 标记不会命中。
PEM_BLOCK_RE = re.compile(
    r"-----BEGIN ([A-Z0-9 ]{3,})-----[ \t]*\r?\n"
    r"(?:[A-Za-z0-9+/=]{4,}[ \t]*\r?\n)+"
    r"-----END \1-----"
)

MAX_TEXT_BYTES = 2 * 1024 * 1024  # 超过 2MB 的文本文件跳过内容扫描


@dataclass
class Finding:
    kind: str          # "file" | "content"
    rule: str
    path: str
    line: int | None
    detail: str
    blocking: bool


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def _force_utf8_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError, OSError):
            pass


def die(message: str, code: int = 2) -> "NoReturn":
    print(f"[secrets-scan][ERROR] {message}")
    sys.exit(code)


def posix(path: str) -> str:
    return path.replace("\\", "/")


def is_doc_path(path: str) -> bool:
    return path.startswith("docs/") or path.endswith(".md")


def is_test_path(path: str) -> bool:
    if "/tests/" in path or path.startswith("tests/") or "/test/" in path:
        return True
    name = path.rsplit("/", 1)[-1]
    return bool(
        re.match(r"test_.*\.py$", name)
        or re.match(r".*_test\.py$", name)
        or re.match(r".*\.(?:test|spec)\.[cm]?[jt]s$", name)
    )


FIXTURE_DIR_RE = re.compile(r"(?:^|/)(?:mocks?|fixtures?|__fixtures__|testdata|test_data|sample_data)(?:/|$)")
FIXTURE_FILE_RE = re.compile(
    r"(?:^|/)(?:mock|mocks|fake|demo|sample)[^/]*\.(?:[cm]?[jt]s|json|py|html|css)$"
)


def is_fixture_path(path: str) -> bool:
    """WebUI mock / 示例数据等“故意假值”文件：内容规则降级为告警。"""
    return bool(FIXTURE_DIR_RE.search(path) or FIXTURE_FILE_RE.search(path))


def mask(secret: str) -> str:
    secret = secret.strip()
    if len(secret) <= 8:
        return "***"
    return f"{secret[:6]}…（{len(secret)} 字符，已遮蔽）"


DIGEST_HINTS = ("sha256", "sha-256", "sha1", "sha-1", "md5", "checksum", "digest", "hash")
BARE_HEX_LITERAL = re.compile(r"""^\s*['"][0-9A-Fa-f]{64}['"],?\s*$""")
DIGEST_MANIFEST_PATH = re.compile(
    r"(?:^|/)(?:models|model_manifest|model_list|checksums?)\.(?:py|json|toml|txt)$"
)


def is_digest_context(line: str) -> bool:
    """行内出现 sha256/checksum 等摘要语义，或整行只是一个 64 位 hex 字符串字面量。"""
    if any(hint in line.lower() for hint in DIGEST_HINTS):
        return True
    return bool(BARE_HEX_LITERAL.match(line))


DOC_HEX_RE = re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{64}(?![0-9A-Fa-f])")
KNOWN_DOC_HEX: set[str] = set()


def load_doc_hex_vectors(repo: Path) -> set[str]:
    """收集 docs/**/*.md 中出现的 64 位 hex 字符串（协议冻结测试向量），供内容规则豁免。

    PROTOCOL.md §6.3 等文档冻结了 deviceToken / hmac / sha256 测试向量；各端实现
    （如 app/android/tools/verify_protocol.py）会原样复制这些向量做跨端一致性校验，
    不应被 hex-token-64 规则误报。只豁免与文档中的值逐字节相同的 token。
    """
    tokens: set[str] = set()
    docs_dir = repo / "docs"
    if not docs_dir.is_dir():
        return tokens
    for path in docs_dir.rglob("*.md"):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        tokens.update(match.group(0).lower() for match in DOC_HEX_RE.finditer(text))
    return tokens


# ---------------------------------------------------------------------------
# 文件规则
# ---------------------------------------------------------------------------
def check_path(path: str) -> list[Finding]:
    """对单个仓库相对路径（POSIX 分隔符）应用文件名/路径规则。"""
    findings: list[Finding] = []
    name = path.rsplit("/", 1)[-1]
    lower_name = name.lower()

    if path == SELF_PATH:
        return findings  # 扫描器自身包含规则字面量，豁免

    if lower_name in BLOCKED_FILENAMES:
        findings.append(Finding("file", "forbidden-file", path, None, BLOCKED_FILENAMES[lower_name], True))

    suffix = Path(lower_name).suffix.lower()
    suffix_blocked = suffix in BLOCKED_SUFFIXES
    if suffix_blocked:
        findings.append(Finding("file", "forbidden-file", path, None, BLOCKED_SUFFIXES[suffix], True))

    if lower_name.startswith(".env") and lower_name not in ENV_TEMPLATE_NAMES:
        findings.append(
            Finding("file", "forbidden-file", path, None, ".env 环境文件（可能含真实密钥），只允许 .env.example 等模板", True)
        )

    for prefix, detail in BLOCKED_PREFIXES:
        if path.startswith(prefix):
            findings.append(Finding("file", "generated-artifact", path, None, detail, True))
            break

    if path.startswith(MODELS_PREFIX) and name != ".gitkeep" and not suffix_blocked:
        findings.append(
            Finding("file", "forbidden-file", path, None, "models/ 下不应提交权重文件（用 scripts/download_models.py 下载，见 PRD）", True)
        )

    return findings


# ---------------------------------------------------------------------------
# 内容规则
# ---------------------------------------------------------------------------
def scan_line(path: str, lineno: int, line: str) -> list[Finding]:
    if path == SELF_PATH:
        return []
    doc = is_doc_path(path)
    test = is_test_path(path)
    fixture = is_fixture_path(path)
    nonprod = doc or test or fixture
    findings: list[Finding] = []

    for rule, regex, desc in HIGH_PATTERNS:
        match = regex.search(line)
        if not match:
            continue
        findings.append(
            Finding(
                "content",
                rule,
                path,
                lineno,
                f"{desc}：命中 {mask(match.group(0))}",
                blocking=not nonprod,
            )
        )

    if nonprod:
        return findings  # 文档/测试/mock 夹具不检查中置信规则

    for rule, regex, desc in MEDIUM_PATTERNS:
        match = regex.search(line)
        if not match:
            continue
        if rule == "hex-token-64":
            if is_digest_context(line):
                continue  # 模型清单等处 sha256 摘要属于正常内容
            if match.group(0).lower() in KNOWN_DOC_HEX:
                continue  # 与 docs/ 冻结的协议测试向量逐字节相同（如 PROTOCOL.md §6.3）
            if DIGEST_MANIFEST_PATH.search(path):
                findings.append(
                    Finding(
                        "content",
                        rule,
                        path,
                        lineno,
                        f"{desc}（清单文件上下文，疑似 sha256 校验和，人工确认后可忽略）：命中 {mask(match.group(0))}",
                        False,
                    )
                )
                continue
        findings.append(
            Finding("content", rule, path, lineno, f"{desc}：命中 {mask(match.group(0))}", True)
        )

    return findings


def scan_pem_blocks(path: str, numbered_lines: list[tuple[int, str]]) -> list[Finding]:
    """在整段文本（或某文件全部新增行）中找完整 PEM 私钥 / 证书块。"""
    if path == SELF_PATH or not numbered_lines:
        return []
    nonprod = is_doc_path(path) or is_test_path(path) or is_fixture_path(path)
    text = "\n".join(line for _, line in numbered_lines)
    findings: list[Finding] = []
    for match in PEM_BLOCK_RE.finditer(text):
        label = match.group(1)
        lineno = numbered_lines[0][0] + text[: match.start()].count("\n")
        is_key = "PRIVATE KEY" in label
        findings.append(
            Finding(
                "content",
                "private-key-block" if is_key else "pem-certificate-block",
                path,
                lineno,
                f"{'私钥块' if is_key else 'PEM 证书块'}（BEGIN {label}）",
                blocking=not nonprod,
            )
        )
    return findings


# ---------------------------------------------------------------------------
# git 访问
# ---------------------------------------------------------------------------
def run_git(repo: Path, args: list[str]) -> subprocess.CompletedProcess:
    cmd = ["git", "-C", str(repo), "-c", "core.quotepath=false", "-c", "color.ui=false", *args]
    env = dict(os.environ)
    env.update({"GIT_PAGER": "cat", "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C.UTF-8", "LANG": "C.UTF-8"})
    try:
        return subprocess.run(cmd, capture_output=True, text=False, env=env, timeout=180)
    except FileNotFoundError:
        die("找不到 git 可执行文件，请先安装 git")
    except subprocess.TimeoutExpired:
        die(f"git {' '.join(args)} 执行超时")


def git_text(repo: Path, args: list[str]) -> str:
    proc = run_git(repo, args)
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", "replace").strip()
        die(f"git {' '.join(args)} 失败（exit {proc.returncode}）：{stderr}")
    return proc.stdout.decode("utf-8", "replace")


def git_ok(repo: Path, args: list[str]) -> bool:
    return run_git(repo, args).returncode == 0


def commit_exists(repo: Path, rev: str) -> bool:
    return git_ok(repo, ["rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}"])


def ls_files(repo: Path, extra_args: list[str] | None = None) -> list[str]:
    args = ["ls-files", "-z", *(extra_args or [])]
    proc = run_git(repo, args)
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", "replace").strip()
        die(f"git ls-files 失败：{stderr}")
    paths = []
    for chunk in proc.stdout.split(b"\x00"):
        if not chunk:
            continue
        paths.append(posix(chunk.decode("utf-8", "replace")))
    return sorted(set(paths))


def diff_name_status(repo: Path, diff_args: list[str]) -> list[tuple[str, str]]:
    out = git_text(repo, ["diff", "--name-status", "--no-renames", "--diff-filter=ACMRT", *diff_args])
    entries: list[tuple[str, str]] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        status = parts[0].strip()[:1]
        entries.append((status, posix(parts[-1])))
    return entries


def diff_added_lines(repo: Path, diff_args: list[str]) -> list[tuple[str, int, str]]:
    """返回 diff 中新增行的 (path, new_lineno, text)，不含 +++ 头和删除行。"""
    patch = git_text(repo, ["diff", "-U0", "--no-renames", "--diff-filter=ACMRT", *diff_args])
    entries: list[tuple[str, int, str]] = []
    current: str | None = None
    lineno = 0
    for line in patch.splitlines():
        if line.startswith("+++ "):
            target = line[4:].strip()
            if target == "/dev/null":
                current = None
            else:
                current = posix(target[2:] if target.startswith("b/") else target)
            continue
        if line.startswith("@@ "):
            match = re.search(r"\+(\d+)(?:,\d+)?", line)
            lineno = int(match.group(1)) if match else 0
            continue
        if current is None:
            continue
        if line.startswith("+"):
            entries.append((current, lineno, line[1:]))
            lineno += 1
        elif line.startswith(" "):
            lineno += 1
    return entries


# ---------------------------------------------------------------------------
# 内容读取
# ---------------------------------------------------------------------------
def read_text(repo: Path, path: str) -> tuple[str | None, bool]:
    """返回 (text, skipped)。二进制 / 超大 / 不存在时 text=None。"""
    full = repo / path
    try:
        size = full.stat().st_size
    except OSError:
        return None, True
    if size > MAX_TEXT_BYTES:
        return None, True
    try:
        data = full.read_bytes()
    except OSError:
        return None, True
    if b"\x00" in data[:8192]:
        return None, True
    return data.decode("utf-8", "replace"), False


# ---------------------------------------------------------------------------
# 三种扫描模式
# ---------------------------------------------------------------------------
class Stats:
    def __init__(self) -> None:
        self.paths_checked = 0
        self.lines_scanned = 0
        self.skipped = 0

    def merge(self, other: "Stats") -> None:
        self.paths_checked += other.paths_checked
        self.lines_scanned += other.lines_scanned
        self.skipped += other.skipped


def scan_full_files(repo: Path, paths: list[str], stats: Stats) -> list[Finding]:
    findings: list[Finding] = []
    for path in paths:
        if path == SELF_PATH:
            continue
        text, skipped = read_text(repo, path)
        if text is None:
            stats.skipped += 1
            continue
        stats.paths_checked += 1
        for lineno, line in enumerate(text.splitlines(), start=1):
            stats.lines_scanned += 1
            findings.extend(scan_line(path, lineno, line))
        findings.extend(scan_pem_blocks(path, list(enumerate(text.splitlines(), start=1))))
    return findings


def scan_added_lines(
    repo: Path, diff_args: list[str], stats: Stats
) -> tuple[list[Finding], int]:
    findings: list[Finding] = []
    added = diff_added_lines(repo, diff_args)
    for path, lineno, line in added:
        stats.lines_scanned += 1
        findings.extend(scan_line(path, lineno, line))
    by_path: dict[str, list[tuple[int, str]]] = {}
    for path, lineno, line in added:
        by_path.setdefault(path, []).append((lineno, line))
    for path, numbered in by_path.items():
        findings.extend(scan_pem_blocks(path, numbered))
    return findings, len(added)


def scan_all(repo: Path, stats: Stats) -> list[Finding]:
    paths = ls_files(repo)
    findings: list[Finding] = []
    for path in paths:
        findings.extend(check_path(path))
    print(f"[secrets-scan] 跟踪文件: {len(paths)} 个，开始内容扫描……")
    findings.extend(scan_full_files(repo, paths, stats))
    return findings


def scan_range(repo: Path, range_spec: str, stats: Stats) -> list[Finding]:
    if "..." in range_spec:
        base, head = range_spec.split("...", 1)
        git_range = f"{base}...{head}"
    elif ".." in range_spec:
        base, head = range_spec.split("..", 1)
        git_range = f"{base}..{head}"
    else:
        die(f"--range 格式应为 BASE..HEAD（收到 {range_spec!r}）")

    base, head = base.strip(), head.strip()
    if not base or not head:
        die(f"--range 格式应为 BASE..HEAD（收到 {range_spec!r}）")
    if not commit_exists(repo, base):
        die(f"diff 基线 commit 不存在: {base}")
    if head.upper() != "HEAD" and not commit_exists(repo, head):
        die(f"diff 目标 commit 不存在: {head}")

    findings: list[Finding] = []
    changed = diff_name_status(repo, [git_range])
    added_files = [path for status, path in changed if status == "A"]
    for path in added_files:
        findings.extend(check_path(path))
    print(f"[secrets-scan] diff 区间 {git_range}：变更文件 {len(changed)} 个（新增 {len(added_files)}），扫描新增行……")

    content_findings, added_count = scan_added_lines(repo, [git_range], stats)
    findings.extend(content_findings)
    print(f"[secrets-scan] 新增行: {added_count} 行")
    return findings


def scan_working_tree(repo: Path, stats: Stats) -> list[Finding]:
    findings: list[Finding] = []
    has_head = git_ok(repo, ["rev-parse", "--verify", "--quiet", "HEAD"])
    untracked = ls_files(repo, ["--others", "--exclude-standard"])

    if not has_head:
        print("[secrets-scan] 仓库尚无提交（unborn HEAD），按全部工作区文件扫描。")
        tracked = ls_files(repo)
        candidates = sorted(set(tracked) | set(untracked))
        for path in candidates:
            findings.extend(check_path(path))
        findings.extend(scan_full_files(repo, candidates, stats))
        return findings

    changed = diff_name_status(repo, ["HEAD"])
    for status, path in changed:
        if status == "A":
            findings.extend(check_path(path))
    for path in untracked:
        findings.extend(check_path(path))

    content_findings, added_count = scan_added_lines(repo, ["HEAD"], stats)
    findings.extend(content_findings)
    print(
        f"[secrets-scan] 未提交改动：跟踪文件变更 {len(changed)} 个、新增行 {added_count} 行、"
        f"未跟踪文件 {len(untracked)} 个（未跟踪文件按全文扫描）"
    )
    findings.extend(scan_full_files(repo, untracked, stats))
    return findings


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------
def report(mode_desc: str, findings: list[Finding], stats: Stats, max_findings: int) -> int:
    blocking = [f for f in findings if f.blocking]
    warnings = [f for f in findings if not f.blocking]

    print(
        f"[secrets-scan] 内容检查: {stats.paths_checked} 个文件 / {stats.lines_scanned} 行"
        f"（跳过二进制或超大文件 {stats.skipped} 个）"
    )

    ordered = blocking + warnings
    for finding in ordered[:max_findings]:
        label = "BLOCK" if finding.blocking else "WARN "
        location = finding.path if finding.line is None else f"{finding.path}:{finding.line}"
        print(f"  - [{label}] {finding.rule}  {location}  {finding.detail}")
    if len(ordered) > max_findings:
        print(f"  …… 其余 {len(ordered) - max_findings} 项已省略（--max-findings 可调）")

    if warnings and not blocking:
        print(f"[secrets-scan] 通过，另有 {len(warnings)} 项测试夹具/文档告警（不阻塞）。")
    if blocking:
        print(f"[secrets-scan][FAIL] 发现 {len(blocking)} 项必须阻塞的问题（模式: {mode_desc}）。")
        print(
            "[secrets-scan] 处理建议：从 git 索引移除该文件并加入 .gitignore；"
            "若已提交/推送，请立即轮换对应凭据；测试夹具请放到 tests/ 路径或改用明显的假值。"
        )
        return 1
    print(f"[secrets-scan] 干净：未发现问题（模式: {mode_desc}）。")
    return 0


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    _force_utf8_console()
    parser = argparse.ArgumentParser(
        description="扫描仓库中的密钥/证书/模型/构建产物（纯标准库实现）。",
        epilog="退出码：0=干净 1=有阻塞问题 2=用法/git 错误",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--all", action="store_true", help="扫描全部 git 跟踪文件（全仓体检）")
    group.add_argument("--range", metavar="BASE..HEAD", help="扫描提交区间内新增/修改文件的新增行（CI）")
    group.add_argument("--working-tree", action="store_true", help="扫描未提交改动（默认）")
    parser.add_argument("--repo", default=str(REPO_ROOT), help="仓库路径（默认脚本所在仓库根）")
    parser.add_argument("--max-findings", type=int, default=200, help="最多打印多少条明细（默认 200）")
    args = parser.parse_args(argv)

    repo = Path(args.repo).resolve()
    if not repo.is_dir():
        die(f"仓库路径不存在: {repo}")
    if not git_ok(repo, ["rev-parse", "--git-dir"]):
        die(f"不是 git 仓库: {repo}")

    global KNOWN_DOC_HEX
    KNOWN_DOC_HEX = load_doc_hex_vectors(repo)

    stats = Stats()
    if args.all:
        mode_desc = "--all 全仓库"
        print(f"[secrets-scan] 模式: {mode_desc}  仓库: {repo}")
        findings = scan_all(repo, stats)
    elif args.range:
        mode_desc = f"--range {args.range}"
        print(f"[secrets-scan] 模式: {mode_desc}  仓库: {repo}")
        findings = scan_range(repo, args.range, stats)
    else:
        mode_desc = "--working-tree 未提交改动"
        print(f"[secrets-scan] 模式: {mode_desc}  仓库: {repo}")
        findings = scan_working_tree(repo, stats)

    return report(mode_desc, findings, stats, args.max_findings)


if __name__ == "__main__":
    sys.exit(main())
