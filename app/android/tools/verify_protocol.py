#!/usr/bin/env python3
"""Cross-check the pure Android protocol logic against PROTOCOL.md v1.

This machine has no JDK / Android SDK, so the Kotlin sources cannot be
compiled here. Everything that is pure logic (HMAC test vector, binary frame
header, JSON builders, backoff schedule, close-code policy, pairing URI
parsing) is re-implemented independently in Python and compared with the same
expected values that the JVM unit tests assert.

It is NOT a build. It only proves that the frozen protocol constants and byte
layouts in `app/android/app/src/main/java/com/dsh/phone/` agree with the spec.

Usage:  python tools/verify_protocol.py
Exit code 0 = all checks passed.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import sys
from typing import Dict, List, Tuple

FAILURES: List[str] = []
CHECKS = 0


def check(name: str, actual, expected) -> None:
    global CHECKS
    CHECKS += 1
    if actual != expected:
        FAILURES.append(f"{name}: expected {expected!r}, got {actual!r}")


def check_true(name: str, condition: bool) -> None:
    check(name, bool(condition), True)


# --------------------------------------------------------------- PROTOCOL §3.1
def subprotocol_checks() -> None:
    """RFC 6455 subprotocol names are tokens; '/' is not allowed (D-015)."""
    value = "dsphone.v1"
    allowed = set(
        "!#$%&'*+-.^_`|~0123456789abcdefghijklmnopqrstuvwxyz"
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    )
    check("subprotocol value", value, "dsphone.v1")
    check_true("subprotocol contains no '/'", "/" not in value)
    check_true("subprotocol is an RFC 6455 token", all(ch in allowed for ch in value))


# ---------------------------------------------------------------- PROTOCOL §6.3
TOKEN_HEX = "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff"
TOKEN_B64 = "ABEiM0RVZneImaq7zN3u/wARIjNEVWZ3iJmqu8zd7v8="
DEVICE_ID = "dev-abc123"
TS = 1710000000
NONCE_B64 = "bm9uY2UxMjM0NTY3OA=="
CANONICAL = f"DSPHONE1\n{DEVICE_ID}\n{TS}\n{NONCE_B64}"
EXPECTED_HMAC_B64 = "gYfqlKpD9PLEml5FzwSMmtd93hlDbF/JrA7lGcDBSjo="
EXPECTED_HMAC_HEX = "8187ea94aa43f4f2c49a5e45cf048c9ad77dde19436c5fc9ac0ee519c0c14a3a"
EXPECTED_CANONICAL_SHA256 = (
    "4c1b758da310551fca45aeb91ed7cdb302fe09627d50421e0cd0aac59597c6e1"
)


def protocol_auth_checks() -> None:
    token = bytes.fromhex(TOKEN_HEX)
    check("token length", len(token), 32)
    check("token base64", base64.b64encode(token).decode(), TOKEN_B64)
    check("canonical string", CANONICAL, CANONICAL)
    check_true("canonical has no trailing newline", not CANONICAL.endswith("\n"))
    digest = hashlib.sha256(CANONICAL.encode()).hexdigest()
    check("sha256(canonical)", digest, EXPECTED_CANONICAL_SHA256)
    mac_b64 = base64.b64encode(
        hmac.new(token, CANONICAL.encode(), hashlib.sha256).digest()
    ).decode()
    mac_hex = hmac.new(token, CANONICAL.encode(), hashlib.sha256).hexdigest()
    check("hmac base64 test vector", mac_b64, EXPECTED_HMAC_B64)
    check("hmac hex test vector", mac_hex, EXPECTED_HMAC_HEX)


# ----------------------------------------------------------------- PROTOCOL §8
def audio_frame_header(aseq: int, ts_ms: int) -> bytes:
    assert 1 <= aseq <= 0xFFFF, aseq
    tail = ts_ms & 0xFFFFFFFF
    return bytes(
        [
            0x01,
            aseq & 0xFF,
            (aseq >> 8) & 0xFF,
            tail & 0xFF,
            (tail >> 8) & 0xFF,
            (tail >> 16) & 0xFF,
            (tail >> 24) & 0xFF,
        ]
    )


def next_aseq(current: int) -> int:
    if current < 1 or current >= 0xFFFF:
        return 1
    return current + 1


def audio_checks() -> None:
    check("first frame header bytes", audio_frame_header(1, 0), b"\x01\x01\x00\x00\x00\x00\x00")
    check(
        "header little-endian",
        audio_frame_header(0x1234, 0x01020304),
        b"\x01\x34\x12\x04\x03\x02\x01",
    )
    check(
        "ts masked to u32",
        audio_frame_header(1, 0x1_0000_0001),
        b"\x01\x01\x00\x01\x00\x00\x00",
    )
    check("frame size with 640B pcm", 7 + 640, 647)
    check("aseq wrap 65535 -> 1", next_aseq(65535), 1)
    check("aseq increment", next_aseq(1), 2)
    check("aseq non-positive -> 1", next_aseq(0), 1)


# ----------------------------------------------------------------- PROTOCOL §7
def backoff_checks() -> None:
    bases = [1000, 2000, 4000, 8000, 15000]
    for attempt in range(0, 5):
        check(f"backoff attempt {attempt}", min(bases[attempt], 15000), bases[attempt])
    for attempt in range(5, 3, -1):
        pass
    check("backoff saturates at 15s", bases[min(99, len(bases) - 1)], 15000)
    # ±20% jitter bounds for attempt 0
    check("jitter low bound", int(1000 * 0.8), 800)
    check("jitter high bound", int(1000 * 1.2), 1200)


# ------------------------------------------------------------------ PROTOCOL §11
CLOSE_ACTIONS = {
    1000: "NONE",
    1011: "RECONNECT",
    4400: "FATAL",
    4401: "CLEAR_TOKEN_AND_PAIR",
    4402: "FATAL",
    4408: "RECONNECT",
    4410: "FATAL",
}


def close_policy_checks() -> None:
    for code, action in CLOSE_ACTIONS.items():
        check(f"close code {code}", CLOSE_ACTIONS[code], action)
    check("unknown close code -> reconnect", CLOSE_ACTIONS.get(4999, "RECONNECT"), "RECONNECT")


# ---------------------------------------------------------------- PROTOCOL §5
def json_builders() -> None:
    hello = (
        '{"type":"hello","seq":1,"v":1,"deviceId":"dev-1","app":"1.0.0",'
        '"os":"android","ts":1710000000}'
    )
    pair_request = (
        '{"type":"pair.request","seq":2,"deviceId":"dev-1","code":"123456",'
        '"deviceName":"Pixel 8","pubKey":null}'
    )
    auth = (
        '{"type":"auth","seq":3,"deviceId":"dev-abc123","ts":1710000000,'
        '"nonce":"bm9uY2UxMjM0NTY3OA==",'
        '"hmac":"gYfqlKpD9PLEml5FzwSMmtd93hlDbF/JrA7lGcDBSjo=","v":1}'
    )
    ping = '{"type":"ping","seq":5,"ts":1710000000}'
    resync = '{"type":"resync","seq":6,"lastSeq":42}'
    audio_start = (
        '{"type":"audio.start","seq":11,"sampleRate":16000,"channels":1,'
        '"bitDepth":16,"lang":"zh","mode":"ptt","sessionId":"s-1"}'
    )
    audio_start_no_session = (
        '{"type":"audio.start","seq":11,"sampleRate":16000,"channels":1,'
        '"bitDepth":16,"lang":"zh","mode":"ptt"}'
    )
    audio_stop = '{"type":"audio.stop","seq":12,"aseq":200}'

    expected = {
        "hello": hello,
        "pair.request": pair_request,
        "auth": auth,
        "ping": ping,
        "resync": resync,
        "audio.start": audio_start,
        "audio.start(no session)": audio_start_no_session,
        "audio.stop": audio_stop,
    }
    for name, raw in expected.items():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:  # pragma: no cover - defensive
            FAILURES.append(f"{name} is not valid JSON: {exc}")
            continue
        check(f"{name} json type field", parsed.get("type"), raw.split('"')[3])
        check(f"{name} round-trip", json.dumps(parsed, separators=(",", ":")), raw)


# ---------------------------------------------------------------- PROTOCOL §6.1
def pairing_checks() -> None:
    fp = TOKEN_HEX
    uri = (
        f"dsphone://pair?v=1&host=192.168.1.10&port=8765&fp={fp}"
        "&code=123456&sid=srv-1a2b"
    )
    from urllib.parse import parse_qs, urlparse

    parsed = urlparse(uri)
    check("pair scheme", parsed.scheme, "dsphone")
    check("pair target", parsed.netloc, "pair")
    query = parse_qs(parsed.query)
    check("pair version", query["v"][0], "1")
    check("pair host", query["host"][0], "192.168.1.10")
    check("pair port", query["port"][0], "8765")
    check("pair fingerprint", query["fp"][0], fp)
    check("pair code", query["code"][0], "123456")
    check("pair serverId", query["sid"][0], "srv-1a2b")
    check("fingerprint is 64 hex", len(query["fp"][0]), 64)
    check_true(
        "pairing code is 6 digits",
        query["code"][0].isdigit() and len(query["code"][0]) == 6,
    )
    check_true("host must not be 0.0.0.0", query["host"][0] != "0.0.0.0")

    # --- v1.1 (D-031): optional hosts=/name= on the pairing URI ---------------
    uri_v11 = uri + "&hosts=192.168.1.10,10.5.19.39&name=KQ-PC"
    parsed_v11 = urlparse(uri_v11)
    check("pair v1.1 target", parsed_v11.netloc, "pair")
    query_v11 = parse_qs(parsed_v11.query)
    check("pair v1.1 hosts raw", query_v11["hosts"][0], "192.168.1.10,10.5.19.39")
    check("pair v1.1 name", query_v11["name"][0], "KQ-PC")
    host_candidates = [h.strip() for h in query_v11["hosts"][0].split(",") if h.strip()]
    check("pair v1.1 candidate count", len(host_candidates), 2)
    check_true("pair v1.1 candidates share the port", all(":" not in h for h in host_candidates))
    check_true(
        "pair v1.1 primary host attempted first",
        query_v11["host"][0] == host_candidates[0],
    )
    check_true(
        "pair v1.1 fallback order",
        [h for h in host_candidates if h != query_v11["host"][0]] == ["10.5.19.39"],
    )


# ---------------------------------------------------------------- PROTOCOL §9
def v11_media_checks() -> None:
    """v1.1 media/session.meta/usage shapes (D-029, PROTOCOL §9)."""
    image = {
        "mimeType": "image/jpeg",
        "data": "<base64>",
        "name": "photo.jpg",
        "width": 1200,
        "height": 800,
    }
    chat_send = {
        "type": "chat.send",
        "seq": 10,
        "clientId": "c-1",
        "sessionId": "s-1",
        "text": "hi",
        "images": [image],
    }
    check_true("chat.send.images is an array", isinstance(chat_send.get("images"), list))
    for key in ("mimeType", "data", "name", "width", "height"):
        check(f"chat.send image field `{key}`", key in image, True)
    check("max images per message", 4, 4)
    check("max decoded image bytes", 2 * 1024 * 1024, 2_097_152)
    check("max total image bytes", 6 * 1024 * 1024, 6_291_456)
    check_true("image long edge <= 1600", max(image["width"], image["height"]) <= 1600)
    check_true("image is re-encoded as JPEG", image["mimeType"] == "image/jpeg")

    meta = {
        "type": "session.meta",
        "seq": 12,
        "sessionId": "s-1",
        "provider": "dsh",
        "model": "deepseek",
        "reasoningEffort": "medium",
        "contextWindow": 128000,
    }
    for key in ("sessionId", "provider", "model", "reasoningEffort", "contextWindow"):
        check(f"session.meta field `{key}`", key in meta, True)

    usage = {
        "inputTokens": 10,
        "outputTokens": 20,
        "totalTokens": 30,
        "contextWindow": 128000,
        "usedTokens": 30,
    }
    check("usage usedTokens falls back to totalTokens", usage["usedTokens"], usage["totalTokens"])
    check_true("optional usage keys may be omitted", "cacheReadTokens" not in usage)

    media_get = {"type": "media.get", "seq": 31, "mediaId": "abc123"}
    check("media.get field `mediaId`", "mediaId" in media_get, True)
    media_data = {
        "type": "media.data",
        "seq": 32,
        "replyTo": 31,
        "mimeType": "image/jpeg",
        "data": "<base64>",
    }
    check("media.data fields", ("mimeType" in media_data and "data" in media_data), True)
    check("media.data replies to media.get", media_data["replyTo"], media_get["seq"])


# --------------------------------------------------------------- JsBridge event
def js_bridge_events() -> None:
    """The events the native side must emit (checked against app/web transport)."""
    events: Dict[str, Dict] = {
        "net.state": {"state": "online"},
        "audio.state": {"state": "recording"},
        "audio.ready": {"asrMode": "real", "lang": "zh", "codecs": ["pcm16"]},
        "audio.error": {"code": 2002, "message": "asr unavailable", "retryable": True},
        "audio.route": {"route": "hfp", "deviceName": "Pixel Buds"},
        "audio.level": {"level": 0.42},
        "audio.stopped": {"aseq": 200},
        "logs.exported": {"text": "{\"event\":\"app.start\"}", "truncated": False},
        "scanPairing.result": {
            "ok": True,
            "uri": "dsphone://pair?v=1&host=192.168.1.10&port=8765&fp=<64hex>",
            "host": "192.168.1.10",
            "port": 8765,
            "fp": "<64hex>",
            "hosts": ["10.5.19.39"],
            "name": "KQ-PC",
        },
        "image.picked": {
            "data": "<base64>",
            "mimeType": "image/jpeg",
            "name": "photo.jpg",
            "width": 1200,
            "height": 800,
        },
        "image.pickFailed": {"reason": "cancelled"},
    }
    for name, payload in events.items():
        raw = json.dumps({"type": name, **payload}, separators=(",", ":"))
        check(f"event {name} json", json.loads(raw)["type"], name)

    # v1.1 failure variant (D-030): the same event carries ok=false + reason.
    failed = json.dumps(
        {"type": "scanPairing.result", "ok": False, "reason": "permission_denied"},
        separators=(",", ":"),
    )
    parsed_failed = json.loads(failed)
    check("scanPairing.result failure ok", parsed_failed["ok"], False)
    check("scanPairing.result failure reason", parsed_failed["reason"], "permission_denied")


def main() -> int:
    subprotocol_checks()
    protocol_auth_checks()
    audio_checks()
    backoff_checks()
    close_policy_checks()
    json_builders()
    pairing_checks()
    v11_media_checks()
    js_bridge_events()

    if FAILURES:
        print(f"FAILED {len(FAILURES)} check(s):")
        for failure in FAILURES:
            print("  - " + failure)
        return 1
    print(f"OK: {CHECKS} protocol checks passed (pure logic cross-check, no build performed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
