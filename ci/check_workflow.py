#!/usr/bin/env python3
"""ci/check_workflow.py —— CI 配置与项目就绪度自检（本地用，依赖 pyyaml）。

检查内容：
  1. .github/workflows/ci.yml 能解析，四个 job（bridge-tests / web-tests /
     android-build / secrets-scan）及关键步骤齐全；
  2. ci/version.json 版本规则自洽（versionName 三段式、versionCode >= 公式值）；
  3. .gitignore 忽略构建生成的 assets/web 目标目录；
  4. 其他模块（bridge-core / web-ui / android-shell）的 CI 关键输入是否就绪；
     --strict 时缺失即失败，否则只报告。

退出码：0 = 配置 OK（非 strict 时允许模块未就绪）；1 = 配置错误或 strict 缺依赖。

用法：
  python ci/check_workflow.py            # 报告模式（CI 配置错误仍会失败）
  python ci/check_workflow.py --strict   # 同时要求其他模块输入已就绪
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

try:
    import yaml  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover - 本地环境提示
    print("[check-workflow][ERROR] 缺少 pyyaml，请先安装：python -m pip install pyyaml")
    sys.exit(2)

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_PATH = Path(".github/workflows/ci.yml")

EXPECTED_JOBS = ("bridge-tests", "web-tests", "android-build", "secrets-scan")

# 其他模块负责的 CI 输入；缺失时对应 job 会在 "Check ... inputs" 步骤报错
DEPENDENCIES = (
    ("bridge/requirements.txt", "bridge-core"),
    ("bridge/tests", "bridge-core"),
    ("app/web/index.html", "web-ui"),
    ("app/web/tests", "web-ui"),
    ("app/android/settings.gradle.kts", "android-shell（或 settings.gradle）"),
    ("app/android/app/build.gradle.kts", "android-shell（或 app/build.gradle）"),
    ("app/android/app/src/main/AndroidManifest.xml", "android-shell"),
    ("app/android/tools/verify_protocol.py", "android-shell（离线协议/向量自检）"),
    ("app/android/tools/check_contract.py", "android-shell（离线 Web/Native 契约自检）"),
    ("app/android/tools/check_kotlin_static.py", "android-shell（离线 Kotlin 静态自检）"),
    ("app/android/tools/check_gradle_config.py", "android-shell（离线 Gradle 配置自检）"),
)

FAILURES: list[str] = []
WARNINGS: list[str] = []


def ok(message: str) -> None:
    print(f"[check-workflow] [OK]   {message}")


def fail(message: str) -> None:
    FAILURES.append(message)
    print(f"[check-workflow] [FAIL] {message}")


def warn(message: str) -> None:
    WARNINGS.append(message)
    print(f"[check-workflow] [WARN] {message}")


def section(title: str) -> None:
    print(f"\n== {title} ==")


# ---------------------------------------------------------------------------
# YAML 辅助
# ---------------------------------------------------------------------------
def get_triggers(workflow: dict) -> object:
    """YAML 1.1 会把裸 key `on:` 解析成布尔 True，两种都要兼容。"""
    if "on" in workflow:
        return workflow["on"]
    if True in workflow:  # type: ignore[operator]
        return workflow[True]
    return None


def job_steps(job: dict) -> list[dict]:
    steps = job.get("steps")
    return steps if isinstance(steps, list) else []


def step_runs(job: dict) -> list[str]:
    texts = []
    for step in job_steps(job):
        run = step.get("run")
        if isinstance(run, str):
            texts.append(run)
    return texts


def step_uses(job: dict) -> list[str]:
    return [step["uses"] for step in job_steps(job) if isinstance(step.get("uses"), str)]


def job_run_text(job: dict) -> str:
    return "\n".join(step_runs(job))


def job_uses_text(job: dict) -> str:
    return "\n".join(step_uses(job))


def require_contains(label: str, haystack: str, needle: str) -> None:
    if needle in haystack:
        ok(f"{label}: 含 `{needle}`")
    else:
        fail(f"{label}: 缺少 `{needle}`")


def resolve_env_value(value: object, env_map: dict) -> str:
    """把 `${{ env.X }}` 解析成 workflow env 里的字面量，方便校验版本号。"""
    text = str(value)
    match = re.fullmatch(r"\$\{\{\s*env\.([A-Za-z_][A-Za-z0-9_]*)\s*\}\}", text)
    if match:
        return str(env_map.get(match.group(1), ""))
    return text


# ---------------------------------------------------------------------------
# 1. workflow 结构
# ---------------------------------------------------------------------------
def check_workflow(repo: Path) -> None:
    section("CI 配置自检（.github/workflows/ci.yml）")
    path = repo / WORKFLOW_PATH
    if not path.is_file():
        fail(f"缺少 workflow 文件: {WORKFLOW_PATH}")
        return

    try:
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        fail(f"YAML 解析失败: {exc}")
        return
    if not isinstance(workflow, dict):
        fail("workflow 顶层不是映射（YAML 结构错误）")
        return
    ok("YAML 解析成功")

    name = workflow.get("name")
    if isinstance(name, str) and name.strip():
        ok(f"workflow name: {name}")
    else:
        fail("缺少 workflow name")

    jobs = workflow.get("jobs")
    if not isinstance(jobs, dict):
        fail("缺少 jobs 映射")
        return
    jobs = {str(key): value for key, value in jobs.items()}
    env_map = workflow.get("env") if isinstance(workflow.get("env"), dict) else {}
    missing_jobs = [job for job in EXPECTED_JOBS if job not in jobs]
    if missing_jobs:
        fail(f"缺少 job: {', '.join(missing_jobs)}")
    else:
        ok(f"四个 job 齐全: {', '.join(EXPECTED_JOBS)}")

    triggers = get_triggers(workflow)
    trigger_names: list[str] = []
    if isinstance(triggers, str):
        trigger_names = [triggers]
    elif isinstance(triggers, dict):
        trigger_names = [str(key) for key in triggers]
    for required in ("push", "pull_request", "workflow_dispatch"):
        if required in trigger_names:
            ok(f"触发条件含 `{required}`")
        else:
            fail(f"触发条件缺少 `{required}`")

    # 并发取消旧的同类 run
    if "concurrency" in workflow:
        ok("配置了 concurrency（取消过期 run）")
    else:
        warn("未配置 concurrency（同分支重复触发会排队）")

    for job_name in EXPECTED_JOBS:
        job = jobs.get(job_name)
        if isinstance(job, dict) and job_name != "secrets-scan":
            check_job(job_name, job, env_map)

    if jobs.get("secrets-scan"):
        check_secrets_job(jobs["secrets-scan"])

    # CI 自身依赖的本地脚本
    for rel in ("ci/secrets_scan.py", "scripts/sync_web.sh", "scripts/sync_web.ps1"):
        if (repo / rel).is_file():
            ok(f"存在 {rel}")
        else:
            fail(f"缺少 {rel}")


def check_job(job_name: str, job: dict, env_map: dict) -> None:
    print(f"  -- job {job_name} --")
    runs_on = job.get("runs-on")
    if job_name == "bridge-tests":
        if isinstance(runs_on, str) and runs_on.startswith("${{"):
            ok("bridge-tests: runs-on 走 matrix")
        else:
            warn(f"bridge-tests: runs-on={runs_on!r}（预期 matrix 变量）")
        matrix = (job.get("strategy") or {}).get("matrix") or {}
        os_list = matrix.get("os") or []
        if "ubuntu-latest" in os_list and "windows-latest" in os_list:
            ok("bridge-tests: matrix 覆盖 ubuntu-latest + windows-latest")
        else:
            fail(f"bridge-tests: matrix.os 应含 ubuntu-latest 与 windows-latest，实际 {os_list!r}")
        uses = job_uses_text(job)
        runs = job_run_text(job)
        require_contains("bridge-tests", uses, "actions/setup-python@v5")
        python_version = ""
        for step in job_steps(job):
            with_ = step.get("with") or {}
            if "python-version" in with_:
                python_version = resolve_env_value(with_["python-version"], env_map)
        if python_version.startswith("3.11"):
            ok(f"bridge-tests: Python {python_version}")
        else:
            fail(f"bridge-tests: setup-python python-version 应为 3.11，实际 {python_version!r}")
        require_contains("bridge-tests", runs, "pytest bridge/tests")
        require_contains("bridge-tests", uses, "actions/upload-artifact@v4")

    elif job_name == "web-tests":
        if runs_on == "ubuntu-latest":
            ok("web-tests: runs-on ubuntu-latest")
        else:
            warn(f"web-tests: runs-on={runs_on!r}")
        uses = job_uses_text(job)
        runs = job_run_text(job)
        require_contains("web-tests", uses, "actions/setup-node@v4")
        node_version = ""
        for step in job_steps(job):
            with_ = step.get("with") or {}
            if "node-version" in with_:
                node_version = resolve_env_value(with_["node-version"], env_map)
        if node_version.startswith("20"):
            ok(f"web-tests: Node {node_version}")
        else:
            fail(f"web-tests: setup-node node-version 应为 20，实际 {node_version!r}")
        require_contains("web-tests", runs, "node --test app/web/tests")

    elif job_name == "android-build":
        if runs_on == "ubuntu-latest":
            ok("android-build: runs-on ubuntu-latest")
        else:
            warn(f"android-build: runs-on={runs_on!r}")
        uses = job_uses_text(job)
        runs = job_run_text(job)
        require_contains("android-build", uses, "actions/setup-java@v4")
        java_version = ""
        for step in job_steps(job):
            with_ = step.get("with") or {}
            if "java-version" in with_:
                java_version = str(with_["java-version"])
        if java_version.startswith("17"):
            ok(f"android-build: JDK {java_version}")
        else:
            fail(f"android-build: setup-java java-version 应为 17，实际 {java_version!r}")
        require_contains("android-build", uses, "gradle/actions/setup-gradle@v4")
        require_contains("android-build", runs, "scripts/sync_web.sh")
        require_contains("android-build", runs, "sdkmanager")
        require_contains("android-build", runs, "assembleDebug")
        require_contains("android-build", runs, "-Pdsphone.versionName")
        require_contains("android-build", runs, "-Pdsphone.versionCode")
        require_contains("android-build", uses, "actions/upload-artifact@v4")
        upload_names: list[str] = []
        for step in job_steps(job):
            if "actions/upload-artifact" in str(step.get("uses", "")):
                with_ = step.get("with") or {}
                upload_names.append(str(with_.get("name", "")))
        if any(name.startswith("app-debug") for name in upload_names):
            ok(f"android-build: 上传 app-debug-* artifact（{upload_names}）")
        else:
            fail(f"android-build: artifact 名应以 app-debug 开头，实际 {upload_names!r}")
        if "ci/version.json" in runs:
            ok("android-build: 版本从 ci/version.json 读取")
        else:
            fail("android-build: 未从 ci/version.json 读取版本")

        # android-shell 的 4 个纯 stdlib 离线自检必须在 assembleDebug 之前执行，
        # 任一脚本非 0 即让 job 失败（见 ci.yml 的 "Run Android offline self-checks"）。
        android_tools = (
            "verify_protocol.py",
            "check_contract.py",
            "check_kotlin_static.py",
            "check_gradle_config.py",
        )
        for tool in android_tools:
            require_contains("android-build", runs, f"app/android/tools/{tool}")
            if f"app/android/tools/{tool}" in runs:
                ok(f"android-build: 接入 app/android/tools/{tool}")
        static_pos = runs.find("app/android/tools/verify_protocol.py")
        last_static_pos = runs.find("app/android/tools/check_gradle_config.py")
        assemble_pos = runs.find("assembleDebug")
        test_pos = runs.find("testDebugUnitTest")
        if static_pos >= 0 and last_static_pos >= 0 and assemble_pos >= 0 and last_static_pos < assemble_pos:
            ok("android-build: 4 个离线静态自检在 assembleDebug 之前执行")
        else:
            fail("android-build: 离线静态自检必须全部位于 assembleDebug 之前")
        if test_pos >= 0 and assemble_pos >= 0 and test_pos > assemble_pos:
            ok("android-build: testDebugUnitTest 在 assembleDebug 之后执行")
        else:
            fail("android-build: 缺少 assembleDebug 之后的 ./gradlew testDebugUnitTest 步骤")


def check_secrets_job(job: dict) -> None:
    print("  -- job secrets-scan --")
    uses = job_uses_text(job)
    runs = job_run_text(job)
    require_contains("secrets-scan", uses, "actions/checkout@v4")
    checkout_depth = ""
    for step in job_steps(job):
        if "actions/checkout" in str(step.get("uses", "")):
            with_ = step.get("with") or {}
            checkout_depth = str(with_.get("fetch-depth", ""))
    if checkout_depth == "0":
        ok("secrets-scan: checkout fetch-depth=0（可解析 diff 基线）")
    else:
        fail(f"secrets-scan: checkout 需 fetch-depth: 0，实际 {checkout_depth!r}")
    require_contains("secrets-scan", runs, "ci/secrets_scan.py")
    if "--range" in runs and "--all" in runs:
        ok("secrets-scan: 同时支持 --range 与 --all 回退")
    else:
        warn("secrets-scan: 未同时处理 --range 与 --all 回退")


# ---------------------------------------------------------------------------
# 2. version.json
# ---------------------------------------------------------------------------
def check_version(repo: Path) -> None:
    section("ci/version.json 版本规则")
    path = repo / "ci/version.json"
    if not path.is_file():
        fail("缺少 ci/version.json")
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        fail(f"ci/version.json 解析失败: {exc}")
        return

    name = data.get("versionName")
    code = data.get("versionCode")
    if not isinstance(name, str) or not re.fullmatch(r"\d+\.\d+\.\d+", name):
        fail(f"versionName 应为三段式 MAJOR.MINOR.PATCH，实际 {name!r}")
        return
    if not isinstance(code, int) or isinstance(code, bool) or code < 1:
        fail(f"versionCode 应为 >=1 的整数，实际 {code!r}")
        return
    major, minor, patch = (int(part) for part in name.split("."))
    formula = major * 10000 + minor * 100 + patch
    if code >= formula:
        ok(f"versionName={name} versionCode={code}（公式值 {formula}，满足 >=）")
    else:
        fail(f"versionCode={code} 小于公式值 {formula}（请用 ci/bump_version.ps1 生成）")
    if data.get("protocolVersion") is not None:
        ok(f"protocolVersion={data['protocolVersion']}")
    else:
        warn("version.json 缺少 protocolVersion")
    for key in ("minSdk", "targetSdk"):
        value = data.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            ok(f"{key}={value}")
        else:
            warn(f"version.json 缺少整数 {key}")


# ---------------------------------------------------------------------------
# 3. .gitignore
# ---------------------------------------------------------------------------
def check_gitignore(repo: Path) -> None:
    section(".gitignore 构建产物忽略规则")
    path = repo / ".gitignore"
    if not path.is_file():
        fail("缺少 .gitignore")
        return
    lines = [line.strip() for line in path.read_text(encoding="utf-8", errors="replace").splitlines()]
    entries = {line.rstrip("/") for line in lines if line and not line.startswith("#")}
    required = {
        "app/android/app/src/main/assets/web": "sync_web 目标（构建生成资源）",
        "app/android/.gradle": "Gradle 本地缓存",
    }
    for entry, why in required.items():
        if entry in entries or any(item.endswith(entry) for item in entries):
            ok(f"忽略 {entry}（{why}）")
        else:
            fail(f".gitignore 缺少 {entry}（{why}）")
    optional = {
        "app/android/gradle/wrapper/gradle-wrapper.jar": "CI 自动生成，不提交",
        "bridge/state": "bridge 运行态数据",
        "models": "模型权重（models/* + !models/.gitkeep）",
    }
    for entry, why in optional.items():
        normalized = entry.rstrip("/")
        matched = any(
            item.rstrip("/") == normalized or item.startswith(normalized + "/")
            for item in entries
        )
        if matched:
            ok(f"忽略 {entry}（{why}）")
        else:
            warn(f".gitignore 未显式忽略 {entry}（{why}）")


# ---------------------------------------------------------------------------
# 4. 其他模块就绪度
# ---------------------------------------------------------------------------
def check_dependencies(repo: Path, strict: bool) -> None:
    section("其他模块 CI 输入就绪度")
    missing: list[tuple[str, str]] = []
    for rel, owner in DEPENDENCIES:
        path = repo / rel
        if path.exists():
            ok(f"{rel}（{owner}）")
        else:
            alternatives = []
            if rel.endswith(".kts"):
                alternatives.append(rel[:-4] + ".gradle")
            found_alt = next((alt for alt in alternatives if (repo / alt).exists()), None)
            if found_alt:
                ok(f"{rel} 缺失但存在 {found_alt}（{owner}）")
            else:
                missing.append((rel, owner))
                print(f"[check-workflow] [MISS] {rel}（{owner} 负责）未就绪")
    if missing:
        if strict:
            fail(f"{len(missing)} 个模块输入未就绪（--strict）：" + "、".join(rel for rel, _ in missing))
            print("[check-workflow] 这些文件缺失会让对应 CI job 在输入检查步骤失败；补齐后再跑即可。")
        else:
            warn(f"{len(missing)} 个模块输入尚未就绪（非 strict 模式只报告）：" + "、".join(rel for rel, _ in missing))
    else:
        ok("所有模块 CI 输入已就绪")


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError, OSError):
            pass

    parser = argparse.ArgumentParser(description="CI 配置与模块就绪度自检（依赖 pyyaml）。")
    parser.add_argument("--repo", default=str(REPO_ROOT), help="仓库路径（默认脚本所在仓库根）")
    parser.add_argument("--strict", action="store_true", help="模块输入未就绪也视为失败")
    args = parser.parse_args(argv)

    repo = Path(args.repo).resolve()
    if not repo.is_dir():
        print(f"[check-workflow][ERROR] 仓库路径不存在: {repo}")
        return 2

    print(f"[check-workflow] 仓库: {repo}  strict={args.strict}")
    check_workflow(repo)
    check_version(repo)
    check_gitignore(repo)
    check_dependencies(repo, args.strict)

    print("\n== 汇总 ==")
    if FAILURES:
        print(f"[check-workflow][FAIL] {len(FAILURES)} 项失败：")
        for item in FAILURES:
            print(f"  - {item}")
        return 1
    if WARNINGS:
        print(f"[check-workflow][OK] 配置检查通过（{len(WARNINGS)} 项告警，见上）。")
    else:
        print("[check-workflow][OK] 全部检查通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
