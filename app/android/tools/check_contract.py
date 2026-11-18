#!/usr/bin/env python3
"""Offline cross-check of the JsBridge contract (Web <-> Android).

This is a *static* check: the machine has no JDK / Android SDK, so an APK can
only be built by CI.  The scripts under `app/android/tools/` reduce the risk of
a red CI run by pinning down contracts that are otherwise only exercised on a
real device.

What is compared
----------------
1. `app/web/js/transport.js`  (NativeTransport)
     - ops:      `this.post('<op>', ...)`
     - events:   `case '<event>':` inside `handleEvent()`
2. `app/android/**` (Kotlin sources)
     - `@JavascriptInterface` method names (the native side of
       `window.DshNative.post`); for this project there is exactly one generic
       `post(message: String)` dispatcher, so the ops are the string branches of
       its `when (op)` dispatch.
     - bridge event names: every `.put("type", "<event>")` in the main source
       set, excluding `protocol/Protocol.kt` (those are wire frames sent to the
       server, not WebView events).
3. `docs/ARCHITECTURE.md` §3.2 op table (ops + documented return events) and
   the "Native → Web" event list.

Failure / warning policy
------------------------
FAIL (exit 1)
  - web posts an op that native does not implement
  - docs §3.2 op table lists an op that native does not implement
  - docs §3.2 op table lists an op that web never posts
  - web handles an event that native never emits (except documented legacy
    aliases in LEGACY_WEB_EVENTS, kept for mock/older-bridge compatibility)
  - the WebView bridge surface is missing (`@JavascriptInterface post`,
    `evaluateJavascript(window.DshBridge.onEvent(...))`, `DshNative` binding)

WARN (exit stays 0, printed for humans)
  - native implements ops web does not use (allowed: future / diagnostic ops)
  - native ops missing from docs table
  - docs table documents return events that native never emits (docs drift:
    needs the docs owner, not this repo's Kotlin code)
  - native emits events web does not handle (allowed: web may ignore unknown
    event types per PROTOCOL §14)

Exit code: 0 = no FAIL, 1 = drift detected.  This is NOT a build.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
WEB_TRANSPORT = REPO_ROOT / "app" / "web" / "js" / "transport.js"
ANDROID_JAVA = REPO_ROOT / "app" / "android" / "app" / "src" / "main" / "java"
BRIDGE_KT = ANDROID_JAVA / "com" / "dsh" / "phone" / "bridge" / "DshBridge.kt"
MAIN_ACTIVITY_KT = ANDROID_JAVA / "com" / "dsh" / "phone" / "MainActivity.kt"
ARCH_DOC = REPO_ROOT / "docs" / "ARCHITECTURE.md"

# Event types web handles although native never emits them: legacy names kept
# for mock parity / older bridge builds. Not a contract violation.
LEGACY_WEB_EVENTS = {"scan.result"}

# Wire protocol frame types are not bridge events; Protocol.kt is excluded.
WIRE_PROTOCOL_FILE = "Protocol.kt"

FAILS: list[str] = []
WARNS: list[str] = []


def fail(message: str) -> None:
    FAILS.append(message)


def warn(message: str) -> None:
    WARNS.append(message)


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        fail(f"cannot read {rel(path)}: {exc}")
        return ""


def rel(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT)).replace("\\", "/")
    except ValueError:
        return str(path)


def line_of(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def balanced_body(text: str, open_index: int) -> str:
    """Return the substring from `{` at open_index to its matching brace.

    Handles JS comments and strings well enough for the small amounts of code
    parsed here (no regex literals are expected inside the inspected bodies).
    """
    if open_index < 0 or open_index >= len(text) or text[open_index] != "{":
        raise ValueError("balanced_body: not at an opening brace")
    depth = 0
    i = open_index
    while i < len(text):
        ch = text[i]
        if ch in "'\"":
            quote = ch
            i += 1
            while i < len(text):
                if text[i] == "\\":
                    i += 2
                    continue
                if text[i] == quote:
                    i += 1
                    break
                i += 1
            continue
        if ch == "`":
            i += 1
            while i < len(text):
                if text[i] == "\\":
                    i += 2
                    continue
                if text[i] == "`":
                    i += 1
                    break
                i += 1
            continue
        if ch == "/" and i + 1 < len(text) and text[i + 1] == "/":
            nl = text.find("\n", i)
            i = len(text) if nl < 0 else nl
            continue
        if ch == "/" and i + 1 < len(text) and text[i + 1] == "*":
            end = text.find("*/", i + 2)
            i = len(text) if end < 0 else end + 2
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[open_index : i + 1]
        i += 1
    raise ValueError("balanced_body: unbalanced braces")


def js_method_body(text: str, name: str) -> str:
    """Body of a JS method *definition* `name(args) { ... }` (not a call)."""
    match = re.search(rf"\b{re.escape(name)}\s*\([^)]*\)\s*\{{", text)
    if match is None:
        raise ValueError(f"definition not found: {name}")
    return balanced_body(text, match.end() - 1)


def parse_web_ops(text: str) -> dict[str, int]:
    ops: dict[str, int] = {}
    for match in re.finditer(r"\.post\(\s*['\"]([A-Za-z0-9_]+)['\"]", text):
        ops.setdefault(match.group(1), line_of(text, match.start()))
    return ops


def parse_web_events(text: str) -> dict[str, int]:
    body = js_method_body(text, "handleEvent")
    body_start = text.find(body)
    events: dict[str, int] = {}
    for match in re.finditer(r"case\s+['\"]([A-Za-z0-9_.]*)['\"]\s*:", body):
        name = match.group(1)
        if not name:
            continue
        events.setdefault(name, line_of(text, body_start + match.start()))
    return events


def parse_native_ops(text: str) -> tuple[dict[str, int], dict[str, int]]:
    """Return (js_interface_methods, ops)."""
    methods: dict[str, int] = {}
    for match in re.finditer(r"@JavascriptInterface\s+(?:public\s+)?(?:override\s+)?fun\s+(\w+)", text):
        methods.setdefault(match.group(1), line_of(text, match.start()))

    ops: dict[str, int] = {}
    marker = text.find("when (val op")
    if marker < 0:
        marker = text.find("when(op")
    if marker >= 0:
        brace = text.find("{", marker)
        body = balanced_body(text, brace)
        base = text.find(body)
        for match in re.finditer(r"\"([A-Za-z0-9_]+)\"\s*->", body):
            ops.setdefault(match.group(1), line_of(text, base + match.start()))
    return methods, ops


def parse_native_events() -> dict[str, tuple[str, int]]:
    """event name -> (file, line), excluding outgoing wire protocol frames."""
    events: dict[str, tuple[str, int]] = {}
    for path in sorted(ANDROID_JAVA.rglob("*.kt")):
        if path.name == WIRE_PROTOCOL_FILE:
            continue
        text = read_text(path)
        for match in re.finditer(r"put\(\s*\"type\"\s*,\s*\"([A-Za-z0-9_.]+)\"", text):
            events.setdefault(match.group(1), (rel(path), line_of(text, match.start())))
    return events


def parse_doc_contract(text: str) -> tuple[dict[str, int], dict[str, int], dict[str, int]]:
    """Parse ARCHITECTURE §3.2: (op -> line, return event -> line, native event list)."""
    start = text.find("### 3.2 JsBridge")
    if start < 0:
        fail("docs/ARCHITECTURE.md: §3.2 heading not found")
        return {}, {}, {}
    end = text.find("### 3.2.1", start)
    section = text[start : end if end >= 0 else len(text)]
    base_line = text.count("\n", 0, start) + 1

    ops: dict[str, int] = {}
    return_events: dict[str, int] = {}
    for offset, raw_line in enumerate(section.splitlines()):
        line_no = base_line + offset
        stripped = raw_line.strip()
        if not stripped.startswith("|"):
            continue
        # NOTE: some cells contain a literal `|` (e.g. `phone|hfp|le`), so a
        # naive split gives more than 3 cells; merge the tail back into cell #3.
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if len(cells) > 3:
            cells = cells[:2] + ["|".join(cells[2:])]
        if not cells:
            continue
        op_cell = cells[0].strip("` ")
        if not op_cell or op_cell.lower() == "op" or set(op_cell) <= {"-", ":"}:
            continue
        for op in re.split(r"\s*/\s*", op_cell):
            op = op.strip("` ")
            if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", op):
                ops.setdefault(op, line_no)
        if len(cells) >= 3:
            for token in re.findall(r"`([^`]+)`", cells[2]):
                token = token.strip()
                if re.fullmatch(r"[a-z][a-z0-9_.]*", token):
                    return_events.setdefault(token, line_no)

    native_events: dict[str, int] = {}
    for offset, raw_line in enumerate(section.splitlines()):
        if "Native → Web" not in raw_line:
            continue
        line_no = base_line + offset
        for token in re.findall(r"`([^`]+)`", raw_line):
            token = token.strip()
            if token == "json":
                continue
            if re.fullmatch(r"[a-z][a-z0-9_.]*", token):
                native_events.setdefault(token, line_no)
        break
    return ops, return_events, native_events


def main() -> int:
    web_text = read_text(WEB_TRANSPORT)
    bridge_text = read_text(BRIDGE_KT)
    docs_text = read_text(ARCH_DOC)

    if not web_text or not bridge_text or not docs_text:
        for item in FAILS:
            print(f"FAIL: {item}")
        return 1

    web_ops = parse_web_ops(web_text)
    web_events = parse_web_events(web_text)
    js_methods, native_ops = parse_native_ops(bridge_text)
    native_events = parse_native_events()
    doc_ops, doc_return_events, doc_native_events = parse_doc_contract(docs_text)

    # --- WebView bridge surface -------------------------------------------
    if "post" not in js_methods:
        fail(f"{rel(BRIDGE_KT)}: `@JavascriptInterface fun post(String)` not found")
    if len(js_methods) > 1:
        warn(f"{rel(BRIDGE_KT)}: multiple @JavascriptInterface methods {sorted(js_methods)} (expected one generic `post`)")
    if "evaluateJavascript" not in bridge_text or "window.DshBridge.onEvent" not in bridge_text:
        fail(f"{rel(BRIDGE_KT)}: event marshalling `evaluateJavascript(window.DshBridge.onEvent(...))` not found")
    activity_text = read_text(MAIN_ACTIVITY_KT)
    if "addJavascriptInterface" not in activity_text or "DshNative" not in activity_text:
        fail(f"{rel(MAIN_ACTIVITY_KT)}: `window.DshNative` binding not found")
    if "DshBridge.onBack" not in activity_text:
        warn(f"{rel(MAIN_ACTIVITY_KT)}: `window.DshBridge.onBack` probe not found (back key contract §3.2.1)")

    # --- ops ----------------------------------------------------------------
    web_only_ops = set(web_ops) - set(native_ops)
    native_only_ops = set(native_ops) - set(web_ops)
    doc_missing_native = set(doc_ops) - set(native_ops)
    doc_missing_web = set(doc_ops) - set(web_ops)
    native_missing_doc = set(native_ops) - set(doc_ops)

    for op in sorted(web_only_ops):
        fail(f"web posts op `{op}` ({rel(WEB_TRANSPORT)}:{web_ops[op]}) but native has no branch")
    for op in sorted(native_only_ops):
        warn(f"native implements op `{op}` not used by web (allowed, future/diagnostic op)")
    for op in sorted(doc_missing_native):
        fail(f"docs §3.2 lists op `{op}` ({rel(ARCH_DOC)}:{doc_ops[op]}) but native does not implement it")
    for op in sorted(doc_missing_web):
        # v1.1 (D-029/D-030): the Android shell implements `pickImage` first; the Web
        # UI is a parallel work stream and may not post the op yet. Not a hard drift
        # until the web v1.1 change lands, so it stays a warning.
        warn(f"docs §3.2 lists op `{op}` but web (app/web) never posts it yet (web v1.1 pending)")
    for op in sorted(native_missing_doc):
        warn(f"native op `{op}` missing from docs §3.2 table (docs drift)")

    # --- events --------------------------------------------------------------
    web_only_events = set(web_events) - set(native_events)
    for event in sorted(web_only_events - LEGACY_WEB_EVENTS):
        fail(f"web handles event `{event}` ({rel(WEB_TRANSPORT)}:{web_events[event]}) but native never emits it")
    for event in sorted(web_only_events & LEGACY_WEB_EVENTS):
        warn(f"web handles legacy event `{event}` native no longer emits (allowed alias)")

    for event in sorted(set(doc_return_events) - set(native_events)):
        warn(
            f"docs drift: §3.2 row documents return event `{event}` ({rel(ARCH_DOC)}:{doc_return_events[event]}) "
            f"but native never emits it; needs docs owner"
        )
    for event in sorted(set(doc_native_events) - set(native_events)):
        warn(f"docs drift: 'Native → Web' list has `{event}` ({rel(ARCH_DOC)}:{doc_native_events[event]}) but native never emits it")
    for event in sorted(set(native_events) - set(web_events)):
        warn(f"native emits event `{event}` web does not handle (allowed: §14 unknown events are ignored)")

    # --- report --------------------------------------------------------------
    for message in FAILS:
        print(f"FAIL: {message}")
    for message in WARNS:
        print(f"WARN: {message}")

    if FAILS:
        print(f"\nFAIL: {len(FAILS)} contract drift(s), {len(WARNS)} warning(s) (static check only, no build)")
        return 1

    print(
        "OK: {w} web ops / {n} native ops (docs {d}), {we} web events / {ne} native events, "
        "{e} documented return events ({c} warning(s)) — static check only, no build performed".format(
            w=len(web_ops),
            n=len(native_ops),
            d=len(doc_ops),
            we=len(web_events),
            ne=len(native_events),
            e=len(doc_return_events),
            c=len(WARNS),
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
