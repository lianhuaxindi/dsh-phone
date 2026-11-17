#!/usr/bin/env python
"""M0 SDK spike: static/offline verification of the official Python SDK.

What this does
--------------
1. Bootstraps ``scripts/spike/.vendor`` onto ``sys.path`` (the wheel contents were
   extracted there because this sandbox cannot write into the conda env).
2. Imports ``deepseek_harness`` and prints the real exported surface, package
   versions and install locations.
3. Prints genuine signatures (``inspect.signature``) for the high-level
   ``DeepSeekHarness`` / ``Session`` and the low-level ``HarnessClient``.
4. Prints dataclass / pydantic field lists, including wire aliases.
5. Scans the installed sources for JSON-RPC method strings actually used.
6. Builds approval / question / notification request objects offline and dumps
   them (``model_dump(by_alias=True)``) so the wire shape is evidence, not guess.
7. Reports a capability matrix (cancel? list sessions? read history? ...).
8. Explains ``dsh`` CLI discovery: bundled runtime vs ``dsh_bin`` vs ``DSH_HOME``.
9. Optional health check: runs the bundled runtime exe with ``--version`` only.
   No model is ever called, no API key is needed, no tokens are spent.

Usage::

    python scripts/spike/spike_sdk.py [--no-probe] [--log <path>]

Exit code: 0 when every section ran (findings are data, not failures).
"""

from __future__ import annotations

import dataclasses
import importlib.metadata as md
import inspect
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

SPIKE_DIR = Path(__file__).resolve().parent
VENDOR_DIR = SPIKE_DIR / ".vendor"
RUN_RUNTIME_PROBE = "--no-probe" not in sys.argv

RESULT: dict[str, object] = {}


class _Tee:
    """Mirror stdout into a UTF-8 log file.

    Windows PowerShell 5.1 redirection writes UTF-16LE, which makes the log
    unreadable for tooling, and the console code page mangles non-ASCII paths.
    Writing the log from inside the process keeps one reproducible artefact.
    """

    def __init__(self, stream, path: Path) -> None:
        self._stream = stream
        self._fh = path.open("w", encoding="utf-8", newline="\n")

    def write(self, text: str) -> int:
        self._stream.write(text)
        self._fh.write(text)
        return len(text)

    def flush(self) -> None:
        self._stream.flush()
        self._fh.flush()

    def reconfigure(self, **_kwargs: object) -> None:  # bootstrap_vendor asks for UTF-8
        pass

    def close(self) -> None:
        self.flush()
        self._fh.close()


def install_tee(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:
        pass
    sys.stdout = _Tee(sys.stdout, path)  # type: ignore[assignment]


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def hr(title: str) -> None:
    print()
    print("=" * 78)
    print(f"== {title}")
    print("=" * 78)


def kv(key: str, value: object) -> None:
    text = str(value)
    if len(text) > 300:
        text = text[:297] + "..."
    print(f"  {key:<34} {text}")


def section(name: str, payload: object) -> None:
    RESULT[name] = payload


def bootstrap_vendor() -> bool:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    if VENDOR_DIR.is_dir():
        sys.path.insert(0, str(VENDOR_DIR))
        return True
    print(f"[!] vendor dir missing: {VENDOR_DIR}")
    print("    run: python scripts/spike/_bootstrap_wheel.py  (then _vendor_install.py)")
    return False


def dist_info(name: str) -> dict[str, object] | None:
    try:
        dist = md.distribution(name)
    except md.PackageNotFoundError:
        return None
    meta = dist.metadata
    return {
        "name": meta["Name"],
        "version": meta["Version"],
        "requires_python": meta["Requires-Python"],
        "requires_dist": list(meta.get_all("Requires-Dist") or []),
        "summary": meta["Summary"],
        "location": str(dist.locate_file("")),
    }


def dataclass_fields(cls: type) -> list[dict[str, object]]:
    out = []
    for f in dataclasses.fields(cls):
        default = "<required>"
        if f.default is not dataclasses.MISSING:
            default = repr(f.default)
        elif f.default_factory is not dataclasses.MISSING:  # type: ignore[misc]
            default = f"<factory {getattr(f.default_factory, '__name__', f.default_factory)}>"
        out.append({"name": f.name, "type": str(f.type), "default": default})
    return out


def model_fields(cls: type) -> list[dict[str, object]]:
    out = []
    for name, field in cls.model_fields.items():  # type: ignore[attr-defined]
        out.append({
            "name": name,
            "alias": field.alias,
            "annotation": str(field.annotation),
            "required": field.is_required(),
        })
    return out


def signatures(target: object, names: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for name in names:
        attr = getattr(target, name, None)
        if attr is None:
            out[name] = "<MISSING>"
            continue
        try:
            out[name] = f"{name}{inspect.signature(attr)}"
        except (TypeError, ValueError) as exc:  # builtins / properties
            kind = type(attr).__name__
            out[name] = f"{name}<{kind}; no signature: {exc}>"
    return out


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main() -> int:
    hr("0. environment")
    kv("python", sys.version.split()[0])
    kv("executable", sys.executable)
    kv("platform", platform.platform())
    kv("cwd", os.getcwd())
    kv("vendor dir", VENDOR_DIR if VENDOR_DIR.is_dir() else "MISSING")
    if not bootstrap_vendor():
        return 2
    section("environment", {
        "python": sys.version.split()[0],
        "executable": sys.executable,
        "platform": platform.platform(),
        "vendor_dir": str(VENDOR_DIR),
    })

    hr("1. package metadata / versions")
    sdk_dist = dist_info("deepseek-harness-sdk")
    rt_dist = dist_info("deepseek-harness-runtime-bin")
    for label, dist in (("deepseek-harness-sdk", sdk_dist), ("deepseek-harness-runtime-bin", rt_dist)):
        if dist is None:
            kv(label, "NOT INSTALLED")
            continue
        kv(f"{label} version", dist["version"])
        kv(f"{label} location", dist["location"])
        kv(f"{label} requires", ", ".join(dist["requires_dist"]) or "(none)")
    try:
        import pydantic

        kv("pydantic (runtime)", md.version("pydantic") if hasattr(pydantic, "__version__") else pydantic.VERSION)
    except Exception as exc:  # pragma: no cover - defensive
        kv("pydantic", f"<error: {exc}>")
    section("metadata", {"sdk": sdk_dist, "runtime_bin": rt_dist})

    hr("2. import + exported surface")
    try:
        import deepseek_harness as sdk
        from deepseek_harness import api, client, errors, models  # noqa: F401
    except Exception as exc:
        kv("import", f"FAILED: {type(exc).__name__}: {exc}")
        return 2
    kv("module file", sdk.__file__)
    kv("__all__ count", len(sdk.__all__))
    missing = [n for n in sdk.__all__ if not hasattr(sdk, n)]
    kv("__all__ all resolvable", "yes" if not missing else f"NO: {missing}")
    print("  __all__:", ", ".join(sdk.__all__))
    print("  package dir contents:", ", ".join(sorted(p.name for p in Path(sdk.__file__).parent.glob("*.py"))))
    section("exports", {"__all__": list(sdk.__all__), "unresolvable": missing,
                        "modules": sorted(p.name for p in Path(sdk.__file__).parent.glob("*.py"))})

    hr("3. high-level API signatures (DeepSeekHarness / Session)")
    high = signatures(sdk.DeepSeekHarness, [
        "__init__", "start", "close", "__enter__", "__exit__",
        "start_session", "resume_session", "run", "client",
    ])
    for name, sig in high.items():
        print(f"  {sig}")
    sess = signatures(sdk.Session, [
        "__init__", "run", "get_working_directory", "set_working_directory",
    ])
    for name, sig in sess.items():
        print(f"  {sig}")
    section("high_level", {"DeepSeekHarness": high, "Session": sess})

    hr("4. low-level API signatures (HarnessClient)")
    low = signatures(sdk.HarnessClient, [
        "__init__", "start", "close", "initialize", "request", "notify",
        "session_prompt", "resume_session", "_query", "next_request", "respond",
        "respond_error", "next_notification", "subscribe_notifications",
        "subscribe_session_notifications", "get_working_directory", "set_working_directory",
    ])
    for name, sig in low.items():
        print(f"  {sig}")
    sub = signatures(client.NotificationSubscription, ["next", "drain", "close"])
    for name, sig in sub.items():
        print(f"  {sig}")
    kv("UserQuestionHandler type", client.UserQuestionHandler)
    section("low_level", {"HarnessClient": low, "NotificationSubscription": sub})

    hr("5. dataclass configs / wire objects")
    for cls in (sdk.DeepSeekHarnessConfig, sdk.HarnessConfig, sdk.RunResult, sdk.Notification, sdk.IncomingRequest):
        print(f"  -- {cls.__module__}.{cls.__name__}")
        fields = dataclass_fields(cls)
        for f in fields:
            print(f"     {f['name']:<26} {f['type']:<44} default={f['default']}")
        section(f"dataclass:{cls.__name__}", fields)

    hr("6. pydantic models (approval / question / protocol)")
    pyd_models = [
        sdk.ApprovalRequestParams, sdk.ApprovalRequestResult,
        sdk.UserQuestionRequest, sdk.UserQuestionAnswerItem, sdk.UserQuestionAnswer,
        sdk.InitializeResponse, sdk.ServerInfo,
        models.CommandExecution, models.CommandSuccessResult, models.CommandErrorResult,
    ]
    for cls in pyd_models:
        print(f"  -- {cls.__module__}.{cls.__name__}  (strict={cls.model_config.get('strict')})")
        fields = model_fields(cls)
        for f in fields:
            req = "required" if f["required"] else "optional"
            alias = f" alias={f['alias']}" if f["alias"] != f["name"] else ""
            print(f"     {f['name']:<22} {f['annotation']:<46} {req}{alias}")
        section(f"model:{cls.__name__}", fields)

    hr("7. JSON-RPC methods found in installed sources")
    pattern = re.compile(r'"((?:session|approval|user-question|subagent|shutdown|initialize)[A-Za-z0-9./_-]*)"')
    found: dict[str, set[str]] = {}
    literal_names: set[str] = set()
    for module in (api, client):
        src = inspect.getsource(module)
        for match in pattern.finditer(src):
            found.setdefault(inspect.getsourcefile(module) or module.__name__, set()).add(match.group(1))
    methods: set[str] = set()
    flags: set[str] = set()
    for path, names in found.items():
        print(f"  {path}")
        for name in sorted(names):
            # A JSON-RPC method either carries a namespace separator or is one of the
            # two bare names (initialize/shutdown); other literals are wire field names.
            if "/" in name or "." in name or name in {"initialize", "shutdown"}:
                methods.add(name)
                print(f"     method  {name}")
            else:
                flags.add(name)
                print(f"     field   {name}")
    section("rpc_methods", sorted(methods))
    section("wire_literals", sorted(flags))

    hr("8. offline object construction (wire shape evidence)")
    approval_params = sdk.ApprovalRequestParams(
        sessionId="session-demo", toolName="shell", callId="call-1", reason="write outside workspace",
    )
    print("  ApprovalRequestParams ->", json.dumps(approval_params.model_dump(by_alias=True), ensure_ascii=False))
    for outcome in ("allowed-once", "rejected", "cancelled"):
        value = sdk.ApprovalRequestResult(outcome=outcome)  # type: ignore[arg-type]
        print("  ApprovalRequestResult ->", json.dumps(value.model_dump(by_alias=True), ensure_ascii=False))
    question = sdk.UserQuestionRequest(
        requestId="q-1", sessionId="session-demo",
        questions=[{"id": "scope", "question": "要改哪些文件？", "options": [{"label": "仅 docs"}, {"label": "全部"}]}],
    )
    print("  UserQuestionRequest ->", json.dumps(question.model_dump(by_alias=True), ensure_ascii=False))
    answer = sdk.UserQuestionAnswer(answers=[
        sdk.UserQuestionAnswerItem(id="scope", selected=["仅 docs"]),
        sdk.UserQuestionAnswerItem(id="note", selected=[], custom="先给计划"),
    ])
    print("  UserQuestionAnswer  ->", json.dumps(answer.model_dump(by_alias=True, exclude_none=True), ensure_ascii=False))
    print("  cancelled form      ->", json.dumps({"cancelled": True}, ensure_ascii=False))
    notification = sdk.Notification(
        method="session.event",
        payload={"sessionId": "session-demo",
                 "event": {"type": "assistant/message", "seq": 7, "time": 1763000000000,
                           "data": {"message": {"content": [{"type": "text", "text": "hi"}]}}}},
    )
    print("  Notification        ->", json.dumps({"method": notification.method, "payload": notification.payload},
                                                  ensure_ascii=False))
    incoming = sdk.IncomingRequest(id=3, method="approval/request", payload=approval_params.model_dump(by_alias=True))
    print("  IncomingRequest     ->", json.dumps({"id": incoming.id, "method": incoming.method,
                                                  "payload": incoming.payload}, ensure_ascii=False))
    run_result = sdk.RunResult(session_id="session-demo", final_response="hi",
                               finish_reason="completed", events=[], notifications=[notification])
    print("  RunResult           ->", json.dumps({"session_id": run_result.session_id,
                                                  "final_response": run_result.final_response,
                                                  "finish_reason": run_result.finish_reason,
                                                  "events": 0,
                                                  "notifications": len(run_result.notifications)},
                                                 ensure_ascii=False))
    section("objects", {
        "approval_request_params": approval_params.model_dump(by_alias=True),
        "approval_result_enum": ["allowed-once", "rejected", "cancelled"],
        "user_question_request_keys": sorted(question.model_dump(by_alias=True)),
        "user_question_answer": answer.model_dump(by_alias=True, exclude_none=True),
        "incoming_request_keys": ["id", "method", "payload"],
        "run_result_keys": ["session_id", "final_response", "finish_reason", "events", "notifications"],
    })

    hr("9. capability matrix (what the SDK actually exposes)")
    surfaces = {
        "DeepSeekHarness": sdk.DeepSeekHarness,
        "Session": sdk.Session,
        "HarnessClient": sdk.HarnessClient,
    }
    probes = {
        "cancel": ("cancel", "abort", "interrupt", "stop"),
        "list_sessions": ("list_sessions", "sessions", "history_list"),
        "read_history": ("history", "get_events", "messages", "transcript"),
        "model_selection": ("set_model", "select_model"),
        "token_stream": ("delta", "on_delta", "assistant_delta"),
        "session_wait": ("wait", "session_wait"),
    }
    matrix: dict[str, dict[str, str]] = {}
    for surface_name, surface in surfaces.items():
        row: dict[str, str] = {}
        for capability, candidates in probes.items():
            hit = next((name for name in candidates if hasattr(surface, name)), None)
            row[capability] = f"present: {hit}" if hit else "MISSING"
        matrix[surface_name] = row
        print(f"  -- {surface_name}")
        for capability, verdict in row.items():
            print(f"     {capability:<16} {verdict}")
    # ``session/query`` is private but is the only high-level prompt entry.
    matrix["HarnessClient._query"] = {
        "_query": "present: " + inspect.signature(sdk.HarnessClient._query).__str__()
    }
    print(f"  -- HarnessClient._query  {inspect.signature(sdk.HarnessClient._query)}")
    section("capability_matrix", matrix)
    kv("start_session exists", hasattr(sdk.DeepSeekHarness, "start_session"))
    kv("resume_session exists", hasattr(sdk.DeepSeekHarness, "resume_session"))
    kv("stop/cancel on Session", any(hasattr(sdk.Session, n) for n in ("cancel", "abort", "interrupt", "stop")))
    kv("session/wait exposed", "session/wait" in methods)

    hr("10. dsh CLI discovery logic")
    from deepseek_harness_runtime import bundled_package_dir, bundled_runtime_path, resolve_bundled_launch_args

    which_dsh = shutil.which("dsh")
    kv("shutil.which('dsh')", which_dsh or "NOT FOUND on PATH")
    launch_args = None
    try:
        launch_args = resolve_bundled_launch_args()
        kv("resolve_bundled_launch_args()", launch_args)
        exe = Path(launch_args[0])
        kv("bundled exe exists", exe.is_file())
        kv("bundled exe size (bytes)", exe.stat().st_size if exe.is_file() else "n/a")
    except Exception as exc:
        kv("resolve_bundled_launch_args()", f"FAILED: {type(exc).__name__}: {exc}")
    kv("bundled_package_dir()", bundled_package_dir() if launch_args else "n/a")
    kv("DSH_HOME (env)", os.environ.get("DSH_HOME") or "<unset>")
    kv("DSH_RUNTIME_MODE (env)", os.environ.get("DSH_RUNTIME_MODE") or "<unset>")

    # Prove the explicit-HOME requirement by removing DSH_HOME from a copy.
    env_without_home = {k: v for k, v in os.environ.items() if k != "DSH_HOME"}
    bare = sdk.HarnessClient(sdk.HarnessConfig())  # no subprocess yet
    argv_without_home_error: str | None = None
    try:
        bare._default_launch_args(dict(env_without_home))
        kv("launch args w/o DSH_HOME", "NO ERROR (unexpected)")
    except Exception as exc:
        argv_without_home_error = f"{type(exc).__name__}: {exc}"
        kv("launch args w/o DSH_HOME", argv_without_home_error)

    spike_home = str(Path.cwd() / ".spike-dsh-home")
    env_with_home = dict(env_without_home)
    env_with_home["DSH_HOME"] = spike_home
    argv_with_home: list[str] | None = None
    try:
        argv_with_home = list(bare._default_launch_args(dict(env_with_home)))
        kv("launch args with DSH_HOME", argv_with_home)
    except Exception as exc:
        kv("launch args with DSH_HOME", f"FAILED: {type(exc).__name__}: {exc}")

    # The desktop app ships its own dsh launcher; show the dsh_bin override path.
    external_dsh = Path(r"D:\others\DSH\resources\runtime\cli\bin\dsh.cmd")
    kv("external dsh.cmd exists", external_dsh.is_file())
    custom = sdk.HarnessClient(sdk.HarnessConfig(dsh_bin=str(external_dsh), dsh_home=spike_home))
    kv("dsh_bin override argv", custom._default_launch_args(dict(env_with_home)))
    section("cli_discovery", {
        "which_dsh": which_dsh,
        "external_dsh_cmd": str(external_dsh),
        "external_dsh_exists": external_dsh.is_file(),
        "bundled_launch_args": list(launch_args) if launch_args else None,
        "dsh_home_required": argv_without_home_error is not None,
        "dsh_home_required_message": argv_without_home_error,
        "argv_with_dsh_home": argv_with_home,
    })

    hr("11. runtime health check (no model call)")
    probe: dict[str, object] = {}
    if not RUN_RUNTIME_PROBE:
        kv("runtime --version", "skipped (--no-probe)")
    elif launch_args:
        try:
            done = subprocess.run([launch_args[0], "--version"], capture_output=True,
                                  text=True, encoding="utf-8", errors="replace", timeout=120)
            probe = {
                "returncode": done.returncode,
                "stdout": done.stdout.strip()[:200],
                "stderr": done.stderr.strip()[:200],
            }
            kv("runtime --version rc", done.returncode)
            kv("runtime --version out", done.stdout.strip() or "(empty)")
            if done.stderr.strip():
                kv("runtime --version err", done.stderr.strip())
        except Exception as exc:
            probe = {"error": f"{type(exc).__name__}: {exc}"}
            kv("runtime --version", f"FAILED: {probe['error']}")
    section("runtime_probe", probe)

    hr("12. structured summary")
    summary = {
        "sdk_version": (sdk_dist or {}).get("version"),
        "runtime_bin_version": (rt_dist or {}).get("version"),
        "vendor_dir": str(VENDOR_DIR),
        "rpc_methods": sorted(methods),
        "capabilities": matrix,
        "dsh_cli": RESULT.get("cli_discovery"),
        "runtime_probe": probe,
        "sections": sorted(RESULT),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    section("summary", summary)
    print()
    print(f"[OK] spike completed: {len(RESULT)} sections, exit 0")
    return 0


if __name__ == "__main__":
    # usage: python scripts/spike/spike_sdk.py [--no-probe] [--log <path>]
    if "--log" in sys.argv:
        index = sys.argv.index("--log")
        log_path = Path(sys.argv[index + 1]) if index + 1 < len(sys.argv) else SPIKE_DIR / "spike_sdk.log"
        del sys.argv[index:index + 2]
        install_tee(log_path)
    try:
        code = main()
    finally:
        if isinstance(sys.stdout, _Tee):
            sys.stdout.close()
            sys.stdout = sys.stdout._stream  # type: ignore[attr-defined]
    sys.exit(code)
