"""协议层单元测试：§6.3 测试向量、信封、消息编解码、错误码。"""

from __future__ import annotations

import hashlib
import json

import pytest

from bridge.auth import (
    compute_hmac,
    compute_hmac_b64,
    hmac_canonical,
    token_hash,
    b64d,
    b64e,
)
from bridge.certs import pair_uri
from bridge.protocol.audio import build_audio_frame, next_aseq
from bridge.protocol.envelope import (
    Envelope,
    IncomingSeqTracker,
    SeqCounter,
    parse_json_text,
    validate_envelope,
)
from bridge.protocol.errors import (
    CloseCode,
    ErrorCode,
    ProtocolError,
    is_retryable,
)
from bridge.protocol.messages import (
    AssistantDelta,
    ChatSend,
    Hello,
    MessageError,
    PairRequest,
    decode_message,
)

# PROTOCOL §6.3 测试向量
VECTOR_TOKEN_HEX = "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff"
VECTOR_DEVICE_ID = "dev-abc123"
VECTOR_TS = 1710000000
VECTOR_NONCE = "bm9uY2UxMjM0NTY3OA=="
VECTOR_CANONICAL = (
    "DSPHONE1\ndev-abc123\n1710000000\nbm9uY2UxMjM0NTY3OA=="
)
VECTOR_HMAC_B64 = "gYfqlKpD9PLEml5FzwSMmtd93hlDbF/JrA7lGcDBSjo="
VECTOR_HMAC_HEX = (
    "8187ea94aa43f4f2c49a5e45cf048c9ad77dde19436c5fc9ac0ee519c0c14a3a"
)
VECTOR_CANONICAL_SHA256 = (
    "4c1b758da310551fca45aeb91ed7cdb302fe09627d50421e0cd0aac59597c6e1"
)


class TestProtocolTestVector:
    """逐字节验证 PROTOCOL §6.3（冻结协议不得漂移）。"""

    def test_device_token_hex_and_b64(self) -> None:
        token = bytes.fromhex(VECTOR_TOKEN_HEX)
        assert len(token) == 32
        assert b64e(token) == "ABEiM0RVZneImaq7zN3u/wARIjNEVWZ3iJmqu8zd7v8="

    def test_canonical_string(self) -> None:
        assert hmac_canonical(VECTOR_DEVICE_ID, VECTOR_TS, VECTOR_NONCE).decode() == (
            VECTOR_CANONICAL
        )
        assert (
            hashlib.sha256(VECTOR_CANONICAL.encode("utf-8")).hexdigest()
            == VECTOR_CANONICAL_SHA256
        )

    def test_hmac_matches_vector(self) -> None:
        token = bytes.fromhex(VECTOR_TOKEN_HEX)
        digest = compute_hmac(token, VECTOR_DEVICE_ID, VECTOR_TS, VECTOR_NONCE)
        assert digest.hex() == VECTOR_HMAC_HEX
        assert compute_hmac_b64(
            token, VECTOR_DEVICE_ID, VECTOR_TS, VECTOR_NONCE
        ) == VECTOR_HMAC_B64
        assert b64d(VECTOR_HMAC_B64) == digest

    def test_token_hash_is_sha256_hex(self) -> None:
        token = bytes.fromhex(VECTOR_TOKEN_HEX)
        assert token_hash(token) == hashlib.sha256(token).hexdigest()


class TestEnvelope:
    def test_parse_valid(self) -> None:
        obj = parse_json_text('{"type": "ping", "seq": 3, "ts": 1}')
        env = validate_envelope(obj)
        assert env == Envelope(type="ping", seq=3, reply_to=None)

    def test_parse_invalid_json(self) -> None:
        with pytest.raises(ProtocolError) as excinfo:
            parse_json_text("{not json")
        assert excinfo.value.code == ErrorCode.INTERNAL_ERROR or (
            excinfo.value.close_code == CloseCode.PROTOCOL
        )

    def test_parse_rejects_non_object(self) -> None:
        with pytest.raises(ProtocolError):
            parse_json_text("[1, 2, 3]")

    def test_validate_rejects_missing_seq(self) -> None:
        with pytest.raises(ProtocolError):
            validate_envelope({"type": "ping"})

    def test_validate_rejects_zero_seq(self) -> None:
        with pytest.raises(ProtocolError):
            validate_envelope({"type": "ping", "seq": 0})

    def test_validate_rejects_bool_seq(self) -> None:
        with pytest.raises(ProtocolError):
            validate_envelope({"type": "ping", "seq": True})

    def test_seq_counter_starts_at_one(self) -> None:
        counter = SeqCounter()
        assert counter.next_seq == 1
        assert counter.next() == 1
        assert counter.next() == 2
        assert counter.next_seq == 3

    def test_incoming_seq_tracker(self) -> None:
        tracker = IncomingSeqTracker()
        assert tracker.observe(1) is True
        assert tracker.last_seq == 1
        assert tracker.observe(2) is True
        # 乱序/重复不抛异常，仅记录
        assert tracker.observe(2) is False
        assert tracker.violations >= 1


class TestMessages:
    def test_hello_roundtrip_camel_case(self) -> None:
        hello = Hello(seq=1, v=1, device_id="dev-1", app="app", os="os", ts=123)
        data = hello.to_dict()
        assert data["type"] == "hello"
        assert data["seq"] == 1
        assert data["deviceId"] == "dev-1"
        assert data["ts"] == 123
        assert "device_id" not in data

    def test_chat_send_roundtrip(self) -> None:
        msg = ChatSend(seq=9, client_id="c-1", session_id="s-1", text="你好")
        data = json.loads(msg.to_json())
        decoded = decode_message(data)
        assert isinstance(decoded, ChatSend)
        assert decoded.client_id == "c-1"
        assert decoded.session_id == "s-1"
        assert decoded.text == "你好"
        assert decoded.seq == 9

    def test_pair_request_pubkey_explicit_null(self) -> None:
        msg = PairRequest(
            seq=2, device_id="dev-1", code="123456", device_name="phone"
        )
        data = msg.to_dict()
        assert "pubKey" in data and data["pubKey"] is None

    def test_unknown_fields_ignored(self) -> None:
        data = {
            "type": "chat.send",
            "seq": 3,
            "clientId": "c",
            "sessionId": "s",
            "text": "hi",
            "futureField": {"x": 1},
        }
        msg = decode_message(data)
        assert isinstance(msg, ChatSend)
        assert msg.text == "hi"

    def test_missing_reply_to_raises(self) -> None:
        # reply_required 的 S→C 响应必须带 replyTo
        with pytest.raises(MessageError):
            decode_message({"type": "hello.ok", "seq": 1})

    def test_unknown_type_raises(self) -> None:
        from bridge.protocol.messages import UnknownMessageType

        with pytest.raises(UnknownMessageType) as excinfo:
            decode_message({"type": "no.such.type", "seq": 1})
        assert excinfo.value.msg_type == "no.such.type"

    def test_assistant_delta_camel_case(self) -> None:
        msg = AssistantDelta(
            seq=5,
            session_id="s-1",
            message_id="m-1",
            text="增量",
            index=2,
        )
        data = json.loads(msg.to_json())
        assert data["sessionId"] == "s-1"
        assert data["messageId"] == "m-1"
        assert data["text"] == "增量"
        assert data["index"] == 2


class TestErrors:
    def test_retryable_table(self) -> None:
        assert is_retryable(ErrorCode.AUTH_FAILED) is False
        assert is_retryable(ErrorCode.PAIR_CODE) is True
        assert is_retryable(ErrorCode.SERVER_BUSY) is True
        # 3003 视情况可重试；服务端缺省按不可重试处理（由 SDK 错误类别决定）
        assert is_retryable(ErrorCode.MODEL_ERROR) is False
        assert is_retryable(ErrorCode.INTERNAL_ERROR) is True
        assert is_retryable(ErrorCode.SDK_PROTOCOL_ERROR) is False

    def test_close_codes(self) -> None:
        assert int(CloseCode.PROTOCOL) == 4400
        assert int(CloseCode.AUTH_FAILED) == 4401
        assert int(CloseCode.VERSION_MISMATCH) == 4402
        assert int(CloseCode.HEARTBEAT_TIMEOUT) == 4408
        assert int(CloseCode.PAIR_LOCKED) == 4410


class TestCerts:
    def test_pair_uri(self) -> None:
        fp = "a" * 64
        uri = pair_uri(
            host="192.168.1.10",
            port=8765,
            fingerprint=fp,
            code="123456",
            server_id="srv-1a2b3c4d5e6f",
        )
        assert uri.startswith("dsphone://pair?v=1&")
        assert "host=192.168.1.10" in uri
        assert "port=8765" in uri
        assert f"fp={fp}" in uri
        assert "code=123456" in uri
        assert "sid=srv-1a2b3c4d5e6f" in uri

    def test_pair_uri_rejects_wildcard_host(self) -> None:
        with pytest.raises(ValueError):
            pair_uri(
                host="0.0.0.0",
                port=8765,
                fingerprint="a" * 64,
                code="123456",
                server_id="srv-x",
            )


class TestAudioFrameHeader:
    def test_first_frame_header_exact_bytes(self) -> None:
        frame = build_audio_frame(1, 0, b"\x00" * 640)
        assert len(frame) == 647
        assert frame[:7] == bytes([0x01, 0x01, 0x00, 0x00, 0x00, 0x00, 0x00])

    def test_aseq_wraparound(self) -> None:
        assert next_aseq(65535) == 1
        assert next_aseq(1) == 2
