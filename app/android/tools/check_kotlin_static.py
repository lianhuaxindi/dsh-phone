#!/usr/bin/env python3
"""Static risk scan for the Android sources (no JDK / Android SDK available).

The dev machine cannot compile Kotlin or run an APK; CI does that.  This
script moves the cheap, deterministic part of a compile/`lint`/device smoke
test locally: it scans the Kotlin sources and a few resources/manifest facts
for the failure modes that a real Android build or a first real-device launch
would surface, and fails loudly when a known-bad pattern is found.

Checks (FAIL = exit code 1)
---------------------------
A  Forbidden / unavailable APIs on Android: java.net.http, AndroidX imports
   (project policy: zero androidx dependencies), java.net.http usage.
B  Java static constants of framework superclasses used without qualification
   (Kotlin does NOT inherit Java static members: `START_NOT_STICKY` is a
   compile error, `Service.START_NOT_STICKY` is correct).
C  JavaScript bridge surface: `@JavascriptInterface` on the bridge dispatcher,
   `addJavascriptInterface(..., "DshNative")`, `evaluateJavascript(...,
   window.DshBridge.onEvent(...))`, JSON-quoting (Json.quote) for the payload,
   and the `window.DshBridge.onBack` probe used by the back key.
D  Foreground service: `startForeground(...)` must pass
   FOREGROUND_SERVICE_TYPE_MICROPHONE (3-arg form on API 29+).
E  AudioRecord: the recording pipeline must verify STATE_INITIALIZED before
   reading.
F  API level guards: symbols that only exist on newer platforms must be
   preceded by a `Build.VERSION.SDK_INT` comparison of a sufficient level.
G  Cleartext network URLs: no `http://` / `ws://` literals (TLS-only policy).
H  Log redaction: sensitive field names must never be logged, the Redactor
   must cover token/deviceToken/hmac/nonce, and LogStore must call Redactor.
I  Token storage: SharedPreferences writes only in `storage/`, the token is
   encrypted via AndroidKeyStore AES/GCM (never written as plaintext), and no
   log call interpolates the token value.
J  Pairing: `0.0.0.0` is rejected as a host and appears nowhere else.
K  Audio permission: RECORD_AUDIO is checked before starting the recorder.
L  Kotlin syntax sanity: brackets balanced per file after stripping strings and
   comments (catches gross copy/paste damage that would redden CI immediately).
M  v1.1 scan pairing (D-030): CAMERA permission + uses-feature required=false,
   ScanActivity registered exported=false, Camera2 + ZXing QR decode path,
   runtime CAMERA request and typed cancel/permission reasons.
N  v1.1 image pipeline (D-029): custom ContentProvider authority registered with
   grantUriPermissions, cache-only files, BitmapFactory + JPEG q≈0.8 re-encode,
   long edge cap 1600 and Base64.NO_WRAP; pickImage/scanPairing bridge branches
   plus onShowFileChooser and camera/gallery intents in MainActivity.
O  v1.1 native event names: scanPairing.result / image.picked / image.pickFailed
   are emitted (no stale `supported:false, reason:"m3"` stub).
P  v1.1 multi-address (D-031): Pairing hosts/name parsing, AddressCandidates
   3 s attempt timeout, WssClient fallback + all_addresses_failed diagnostics,
   SettingsStore remembers last/known hosts.

This is a static analysis only — it does NOT build the APK.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SRC_ROOT = REPO_ROOT / "app" / "android" / "app" / "src" / "main"
JAVA_ROOT = SRC_ROOT / "java"
RES_ROOT = SRC_ROOT / "res"

FAILS: list[str] = []
WARNS: list[str] = []


def fail(message: str) -> None:
    FAILS.append(message)


def warn(message: str) -> None:
    WARNS.append(message)


def rel(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT)).replace("\\", "/")
    except ValueError:
        return str(path)


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        fail(f"cannot read {rel(path)}: {exc}")
        return ""


def _blank(text: str) -> str:
    """Replace every char with a space, keeping newlines (offset-preserving)."""
    return "".join("\n" if c == "\n" else " " for c in text)


def strip_strings_and_comments(source: str) -> str:
    """Blank out strings/comments, preserving offsets and newlines.

    Offsets are preserved so `line_of` on the stripped text matches the real
    file; strings are replaced by spaces (their content must not take part in
    identifier/constant scans).
    """
    out: list[str] = []
    i = 0
    n = len(source)
    while i < n:
        ch = source[i]
        if source.startswith("//", i):
            j = source.find("\n", i)
            j = n if j < 0 else j
            out.append(_blank(source[i:j]))
            i = j
            continue
        if source.startswith("/*", i):
            j = source.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append(_blank(source[i:j]))
            i = j
            continue
        if source.startswith('"""', i):
            j = source.find('"""', i + 3)
            j = n if j < 0 else j + 3
            out.append(_blank(source[i:j]))
            i = j
            continue
        if ch in ('"', "'"):
            quote = ch
            start = i
            i += 1
            while i < n:
                if source[i] == "\\":
                    i += 2
                    continue
                if source[i] == quote:
                    i += 1
                    break
                i += 1
            out.append(_blank(source[start:i]))
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def line_of(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def kotlin_files() -> list[Path]:
    return sorted(JAVA_ROOT.rglob("*.kt"))


def stripped_sources() -> dict[Path, tuple[str, str]]:
    result: dict[Path, tuple[str, str]] = {}
    for path in kotlin_files():
        raw = read_text(path)
        if not raw:
            continue
        result[path] = (raw, strip_strings_and_comments(raw))
    return result


# --------------------------------------------------------------------------- A
def check_forbidden_apis(sources: dict[Path, tuple[str, str]]) -> None:
    for path, (raw, _code) in sources.items():
        name = rel(path)
        if "java.net.http" in raw:
            fail(f"{name}: uses java.net.http (not available on Android; use OkHttp)")
        for match in re.finditer(r"^\s*import\s+androidx\.", raw, re.MULTILINE):
            fail(f"{name}:{line_of(raw, match.start())}: AndroidX import (project policy: zero androidx)")
        if re.search(r"\btext\s*=\s*\"http://|\"ws://", raw):
            fail(f"{name}: cleartext ws:// URL literal (TLS-only policy)")


# --------------------------------------------------------------------------- B
# Java statics of framework superclasses: Kotlin requires qualification.
FRAMEWORK_STATICS = [
    "STOP_FOREGROUND_REMOVE",
    "STOP_FOREGROUND_DETACH",
    "START_NOT_STICKY",
    "START_STICKY",
    "START_REDELIVER_INTENT",
    "MODE_IN_COMMUNICATION",
    "MODE_IN_CALL",
    "MODE_NORMAL",
    "STATE_INITIALIZED",
]


def check_unqualified_framework_constants(sources: dict[Path, tuple[str, str]]) -> None:
    for path, (raw, code) in sources.items():
        for constant in FRAMEWORK_STATICS:
            for match in re.finditer(rf"(?<![.\w]){constant}\b", code):
                fail(
                    f"{rel(path)}:{line_of(raw, match.start())}: `{constant}` used without qualification "
                    f"(Kotlin does not inherit Java statics; qualify it with the declaring framework class)"
                )


# --------------------------------------------------------------------------- C
def check_js_bridge_surface(sources: dict[Path, tuple[str, str]]) -> None:
    bridge = SRC_ROOT / "java" / "com" / "dsh" / "phone" / "bridge" / "DshBridge.kt"
    activity = SRC_ROOT / "java" / "com" / "dsh" / "phone" / "MainActivity.kt"
    bridge_text = read_text(bridge)
    activity_text = read_text(activity)

    if "@JavascriptInterface" not in bridge_text:
        fail(f"{rel(bridge)}: missing @JavascriptInterface on the WebView dispatcher")
    if not re.search(r"@JavascriptInterface\s+(?:public\s+)?(?:override\s+)?fun\s+post\s*\(", bridge_text):
        fail(f"{rel(bridge)}: `@JavascriptInterface fun post(...)` not found")
    if "Json.quote" not in bridge_text:
        fail(f"{rel(bridge)}: JSON event payload is not quoted via Json.quote before evaluateJavascript")
    if "window.DshBridge.onEvent" not in bridge_text:
        fail(f"{rel(bridge)}: evaluateJavascript does not call window.DshBridge.onEvent")
    if "addJavascriptInterface" not in activity_text or '"DshNative"' not in activity_text:
        fail(f"{rel(activity)}: WebView does not bind `DshNative` (addJavascriptInterface(..., \"DshNative\"))")
    if "DshBridge.onBack" not in activity_text:
        fail(f"{rel(activity)}: back key does not probe window.DshBridge.onBack()")


# --------------------------------------------------------------------------- D
def check_foreground_service(sources: dict[Path, tuple[str, str]]) -> None:
    found = False
    for path, (raw, code) in sources.items():
        for match in re.finditer(r"\bstartForeground\s*\(", code):
            found = True
            tail = raw[match.end() : match.end() + 400]
            if "FOREGROUND_SERVICE_TYPE_MICROPHONE" not in tail:
                fail(
                    f"{rel(path)}:{line_of(raw, match.start())}: startForeground without "
                    f"FOREGROUND_SERVICE_TYPE_MICROPHONE (API 29+ needs the 3-arg form)"
                )
    if not found:
        warn("no startForeground() call found (foreground service may be unused?)")


# --------------------------------------------------------------------------- E
def check_audio_record(sources: dict[Path, tuple[str, str]]) -> None:
    for path, (raw, code) in sources.items():
        if "AudioRecord" not in code or "AudioRecord.Builder" not in code:
            continue
        if "STATE_INITIALIZED" not in raw:
            fail(f"{rel(path)}: AudioRecord is created but recorder.state is never checked against STATE_INITIALIZED")


# --------------------------------------------------------------------------- F
API_GUARD_PATTERNS: list[tuple[str, int, str]] = [
    ("TYPE_BLE_HEADSET", 31, "AudioDeviceInfo.TYPE_BLE_HEADSET (API 31)"),
    ("setCommunicationDevice", 31, "AudioManager.setCommunicationDevice (API 31)"),
    ("clearCommunicationDevice", 31, "AudioManager.clearCommunicationDevice (API 31)"),
    ("availableCommunicationDevices", 31, "AudioManager.availableCommunicationDevices (API 31)"),
    ("setForegroundServiceBehavior", 31, "Notification.Builder.setForegroundServiceBehavior (API 31)"),
    ("FOREGROUND_SERVICE_IMMEDIATE", 31, "Notification.FOREGROUND_SERVICE_IMMEDIATE (API 31)"),
    ("POST_NOTIFICATIONS", 33, "Manifest.permission.POST_NOTIFICATIONS (API 33)"),
]
VERSION_NAMES = {
    "TIRAMISU": 33,
    "S_V2": 32,
    "S": 31,
    "R": 30,
    "Q": 29,
    "P": 28,
    "O_MR1": 27,
    "O": 26,
    "N_MR1": 25,
    "N": 24,
    "M": 23,
}


def _guard_satisfies(raw: str, start: int, required: int, window: int = 40) -> bool:
    """True if a sufficient SDK_INT guard precedes `start` within `window` lines.

    Two idioms are accepted:
      * `if (Build.VERSION.SDK_INT >= X) { ... }`  -> level X (X >= required)
      * `if (Build.VERSION.SDK_INT < X) return ...` -> code below only runs on
        X+ devices, so the guard level is X (X >= required).
    """
    lines = raw.splitlines()
    start_line = line_of(raw, start) - 1
    begin = max(0, start_line - window)
    snippet = "\n".join(lines[begin : start_line + 1])
    for match in re.finditer(
        r"SDK_INT\s*(>=|>|<|<=|==)\s*(?:Build\.VERSION_CODES\.([A-Z_0-9]+)|(\d{2}))",
        snippet,
    ):
        level = int(match.group(3)) if match.group(3) else VERSION_NAMES.get(match.group(2), -1)
        operator = match.group(1)
        if operator in (">=", ">", "==") and level >= required:
            return True
        if operator in ("<", "<=") and level >= required:
            return True
    return False


def check_api_guards(sources: dict[Path, tuple[str, str]]) -> None:
    for path, (raw, code) in sources.items():
        for symbol, required, description in API_GUARD_PATTERNS:
            for match in re.finditer(rf"\b{re.escape(symbol)}\b", code):
                if not _guard_satisfies(raw, match.start(), required):
                    fail(
                        f"{rel(path)}:{line_of(raw, match.start())}: {description} used without an "
                        f"API {required}+ guard (Build.VERSION.SDK_INT) in the preceding block"
                    )


# --------------------------------------------------------------------------- G
def check_cleartext_urls(sources: dict[Path, tuple[str, str]]) -> None:
    for path, (raw, _code) in sources.items():
        for match in re.finditer(r"\"(http|ws)://[^\"]*\"", raw):
            fail(f"{rel(path)}:{line_of(raw, match.start())}: cleartext URL literal {match.group(0)} (TLS-only policy)")


# --------------------------------------------------------------------------- H
SENSITIVE_LOG_FIELDS = ["token", "deviceToken", "hmac", "nonce", "body", "text", "pcm", "payload", "authorization"]


def check_log_redaction(sources: dict[Path, tuple[str, str]]) -> None:
    redactor = SRC_ROOT / "java" / "com" / "dsh" / "phone" / "log" / "Redactor.kt"
    log_store = SRC_ROOT / "java" / "com" / "dsh" / "phone" / "log" / "LogStore.kt"
    redactor_text = read_text(redactor)
    log_store_text = read_text(log_store)
    if "Redactor.redact" not in log_store_text and "Redactor.redactJson" not in log_store_text:
        fail(f"{rel(log_store)}: log lines are not passed through Redactor")
    for key in ("token", "deviceToken", "hmac", "nonce"):
        if f'"{key}"' not in redactor_text:
            fail(f"{rel(redactor)}: sensitive key `{key}` missing from the redaction key set")

    sensitive = "|".join(SENSITIVE_LOG_FIELDS)
    for path, (raw, _code) in sources.items():
        for match in re.finditer(rf"\b(?:LogStore|Log)\.\w+\([^)]*\"({sensitive})\"\s*(?:to|\+|=)", raw):
            fail(f"{rel(path)}:{line_of(raw, match.start())}: log call uses sensitive field `{match.group(1)}` (redaction must stay centralized)")
        for match in re.finditer(r"\bLogStore\.\w+\([^)]*\$\{?\s*(?:token|deviceToken|hmac|nonce)\b", raw):
            fail(f"{rel(path)}:{line_of(raw, match.start())}: log call interpolates a token value")


# --------------------------------------------------------------------------- I
def check_token_storage(sources: dict[Path, tuple[str, str]]) -> None:
    allowed_pref_writers = {"TokenStore.kt", "SettingsStore.kt"}
    token_store = SRC_ROOT / "java" / "com" / "dsh" / "phone" / "storage" / "TokenStore.kt"
    token_text = read_text(token_store)
    for path, (raw, _code) in sources.items():
        if path.name in allowed_pref_writers:
            continue
        for match in re.finditer(r"\bputString\s*\(", raw):
            fail(f"{rel(path)}:{line_of(raw, match.start())}: SharedPreferences putString outside storage/ (tokens must stay in TokenStore)")
    for required in ("AndroidKeyStore", "AES/GCM/NoPadding", "KeyGenParameterSpec", "GCMParameterSpec", "setRandomizedEncryptionRequired(true)"):
        if required not in token_text:
            fail(f"{rel(token_store)}: Keystore AES/GCM token encryption incomplete: missing `{required}`")
    if re.search(r"putString\(\s*KEY_TOKEN\s*,\s*tokenBase64", token_text):
        fail(f"{rel(token_store)}: device token written to SharedPreferences without encryption")


# --------------------------------------------------------------------------- J
def check_pairing_host_policy(sources: dict[Path, tuple[str, str]]) -> None:
    pairing = SRC_ROOT / "java" / "com" / "dsh" / "phone" / "protocol" / "Pairing.kt"
    pairing_text = read_text(pairing)
    if '"0.0.0.0"' not in pairing_text:
        fail(f"{rel(pairing)}: pairing URI parsing does not reject host 0.0.0.0")
    for path, (raw, _code) in sources.items():
        if path.name == "Pairing.kt":
            continue
        for match in re.finditer(r'"0\.0\.0\.0"', raw):
            fail(f"{rel(path)}:{line_of(raw, match.start())}: hard-coded 0.0.0.0 outside pairing validation")


# --------------------------------------------------------------------------- K
def check_audio_permission(sources: dict[Path, tuple[str, str]]) -> None:
    audio_session = SRC_ROOT / "java" / "com" / "dsh" / "phone" / "audio" / "AudioSession.kt"
    text = read_text(audio_session)
    if "checkSelfPermission" not in text or "RECORD_AUDIO" not in text:
        fail(f"{rel(audio_session)}: AudioSession does not check RECORD_AUDIO before recording")
    if "requestPermission" in text:
        warn(f"{rel(audio_session)}: runtime permission request inside audio session (should be owned by MainActivity)")


# --------------------------------------------------------------------------- L
def check_bracket_balance(sources: dict[Path, tuple[str, str]]) -> None:
    pairs = {"{": "}", "(": ")", "[": "]"}
    for path, (raw, code) in sources.items():
        stack: list[tuple[str, int]] = []
        for index, ch in enumerate(code):
            if ch in pairs:
                stack.append((ch, index))
            elif ch in pairs.values():
                if not stack:
                    fail(f"{rel(path)}:{line_of(raw, index)}: unbalanced closing `{ch}`")
                    break
                opener, offset = stack.pop()
                if pairs[opener] != ch:
                    fail(f"{rel(path)}:{line_of(raw, index)}: mismatched `{opener}` at line {line_of(raw, offset)} vs `{ch}`")
                    break
        else:
            if stack:
                opener, offset = stack[0]
                fail(f"{rel(path)}:{line_of(raw, offset)}: unclosed `{opener}` ({len(stack)} open bracket(s) left)")
        if "<<" in code and ">>" not in code:
            fail(f"{rel(path)}: nested generics `<<` without `>>` (Kotlin needs spaces)")


# --------------------------------------------------------------------------- M
def check_scan_pairing(sources: dict[Path, tuple[str, str]]) -> None:
    manifest = read_text(SRC_ROOT / "AndroidManifest.xml")
    required_manifest = (
        ("android.permission.CAMERA", "CAMERA permission"),
        ('android.hardware.camera', "camera uses-feature"),
        ('required="false"', "camera uses-feature required=false"),
        (".ScanActivity", "ScanActivity registration"),
    )
    for token, what in required_manifest:
        if token not in manifest:
            fail(f"AndroidManifest.xml: missing {what} (`{token}`)")
    scan = read_text(SRC_ROOT / "java" / "com" / "dsh" / "phone" / "ScanActivity.kt")
    if not scan:
        fail("ScanActivity.kt: v1.1 QR scanner missing")
        return
    for token in (
        "CameraManager",
        "ImageReader",
        "MultiFormatReader",
        "BarcodeFormat.QR_CODE",
        "checkSelfPermission(Manifest.permission.CAMERA)",
        "requestPermissions(",
        "RESULT_OK",
        "RESULT_CANCELED",
        "REASON_PERMISSION_DENIED",
        "REASON_NO_CAMERA",
    ):
        if token not in scan:
            fail(f"ScanActivity.kt: QR scanner incomplete: missing `{token}`")


# --------------------------------------------------------------------------- N
def check_media_pipeline(sources: dict[Path, tuple[str, str]]) -> None:
    java_root = SRC_ROOT / "java" / "com" / "dsh" / "phone"
    manifest = read_text(SRC_ROOT / "AndroidManifest.xml")
    for token in (
        'android:name=".storage.ImageFileProvider"',
        'android:authorities="com.dsh.phone.fileprovider"',
        'android:grantUriPermissions="true"',
    ):
        if token not in manifest:
            fail(f"AndroidManifest.xml: image FileProvider incomplete: missing `{token}`")

    provider = read_text(java_root / "storage" / "ImageFileProvider.kt")
    if not provider:
        fail("storage/ImageFileProvider.kt: missing custom ContentProvider for camera output")
    else:
        for token in ("ContentProvider", "com.dsh.phone.fileprovider", "openFile", "cacheDir"):
            if token not in provider:
                fail(f"ImageFileProvider.kt: missing `{token}`")

    picker = read_text(java_root / "media" / "ImagePicker.kt")
    if not picker:
        fail("media/ImagePicker.kt: missing native image downscale pipeline")
    else:
        for token in ("BitmapFactory", "CompressFormat.JPEG", "Base64.NO_WRAP", "MAX_IMAGE_BYTES", "ImageScaler"):
            if token not in picker:
                fail(f"ImagePicker.kt: missing `{token}`")

    scaler = read_text(java_root / "media" / "ImageScaler.kt")
    if "MAX_LONG_EDGE = 1600" not in scaler:
        fail("ImageScaler.kt: long edge cap is not 1600 px (D-029)")
    if "JPEG_QUALITY = 80" not in scaler:
        fail("ImageScaler.kt: JPEG quality is not 80 (D-029)")

    bridge = read_text(
        SRC_ROOT / "java" / "com" / "dsh" / "phone" / "bridge" / "DshBridge.kt",
    )
    for token in ('"pickImage"', '"scanPairing"', "AppCore.requestScanPairing", "AppCore.requestPickImage"):
        if token not in bridge:
            fail(f"bridge/DshBridge.kt: missing v1.1 op wiring `{token}`")

    activity = read_text(SRC_ROOT / "java" / "com" / "dsh" / "phone" / "MainActivity.kt")
    for token in (
        "onShowFileChooser",
        "ACTION_IMAGE_CAPTURE",
        "ACTION_GET_CONTENT",
        "FileChooserParams.parseResult",
    ):
        if token not in activity:
            fail(f"MainActivity.kt: missing image picker wiring `{token}`")


# --------------------------------------------------------------------------- O
def check_scan_media_events(sources: dict[Path, tuple[str, str]]) -> None:
    bridge = read_text(
        SRC_ROOT / "java" / "com" / "dsh" / "phone" / "bridge" / "DshBridge.kt",
    )
    if '"m3"' in bridge and "supported" in bridge:
        fail("bridge/DshBridge.kt: stale scanPairing stub (`supported:false, reason:\"m3\"`) still present")
    combined = "\n".join(raw for _path, (raw, _code) in sources.items())
    for event in ("scanPairing.result", "image.picked", "image.pickFailed"):
        if f'"{event}"' not in combined:
            fail(f"native event `{event}` is never emitted (v1.1 contract)")


# --------------------------------------------------------------------------- P
def check_multi_address(sources: dict[Path, tuple[str, str]]) -> None:
    java_root = SRC_ROOT / "java" / "com" / "dsh" / "phone"
    pairing = read_text(java_root / "protocol" / "Pairing.kt")
    for token in ("parseHosts", "isCandidateHost", "val hosts", "val name"):
        if token not in pairing:
            fail(f"protocol/Pairing.kt: missing v1.1 `{token}` (D-031)")
    candidates = read_text(java_root / "net" / "AddressCandidates.kt")
    if "ATTEMPT_TIMEOUT_MS = 3_000L" not in candidates:
        fail("net/AddressCandidates.kt: per-address timeout is not 3000 ms (D-031)")
    wss = read_text(java_root / "net" / "WssClient.kt")
    for token in ("AddressCandidates.order", "all_addresses_failed", "candidateHosts"):
        if token not in wss:
            fail(f"net/WssClient.kt: multi-address fallback incomplete: missing `{token}`")
    settings = read_text(java_root / "storage" / "SettingsStore.kt")
    for token in ("last_host", "known_hosts"):
        if token not in settings:
            fail(f"storage/SettingsStore.kt: missing remembered address key `{token}`")



def main() -> int:
    sources = stripped_sources()
    if not sources:
        print("FAIL: no Kotlin sources found under app/android/app/src/main/java")
        return 1

    check_forbidden_apis(sources)
    check_unqualified_framework_constants(sources)
    check_js_bridge_surface(sources)
    check_foreground_service(sources)
    check_audio_record(sources)
    check_api_guards(sources)
    check_cleartext_urls(sources)
    check_log_redaction(sources)
    check_token_storage(sources)
    check_pairing_host_policy(sources)
    check_audio_permission(sources)
    check_scan_pairing(sources)
    check_media_pipeline(sources)
    check_scan_media_events(sources)
    check_multi_address(sources)
    check_bracket_balance(sources)

    for message in FAILS:
        print(f"FAIL: {message}")
    for message in WARNS:
        print(f"WARN: {message}")

    if FAILS:
        print(f"\nFAIL: {len(FAILS)} static risk(s), {len(WARNS)} warning(s), {len(sources)} Kotlin files scanned")
        return 1

    print(
        f"OK: {len(sources)} Kotlin files scanned, {len(FAILS)} failures, {len(WARNS)} warnings — "
        f"static analysis only, no build performed"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
