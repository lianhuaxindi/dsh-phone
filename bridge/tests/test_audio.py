"""音频帧编解码单元测试（PROTOCOL §7）。"""

from __future__ import annotations

import struct

import pytest

from bridge.protocol.audio import (
    ASEQ_MAX,
    DEFAULT_PAYLOAD_BYTES,
    FRAME_TYPE_OPUS,
    FRAME_TYPE_PCM16,
    HEADER_SIZE,
    MAX_AUDIO_FRAME_BYTES,
    AudioFrameError,
    build_audio_frame,
    is_silent,
    next_aseq,
    parse_audio_frame,
    payload_size_for,
    pcm16_rms,
)


def _pcm(amplitude: int, samples: int = 320) -> bytes:
    return struct.pack("<" + "h" * samples, *([amplitude] * samples))


class TestPayloadSize:
    def test_default_20ms_16k_mono_16bit(self) -> None:
        assert payload_size_for(16000) == 640 == DEFAULT_PAYLOAD_BYTES
        assert payload_size_for(16000, 1, 16, 20) == 640

    def test_other_sample_rate(self) -> None:
        assert payload_size_for(8000) == 320

    def test_invalid_rate_raises(self) -> None:
        # 12345 Hz × 2B × 20ms 不能整除为整数字节
        with pytest.raises(AudioFrameError):
            payload_size_for(12345, duration_ms=20)
        with pytest.raises(AudioFrameError):
            payload_size_for(0)


class TestBuildParse:
    def test_roundtrip(self) -> None:
        payload = _pcm(1000)
        raw = build_audio_frame(7, 140, payload)
        frame = parse_audio_frame(raw)
        assert frame.aseq == 7
        assert frame.ts_ms == 140
        assert frame.payload == payload
        assert frame.frame_type == FRAME_TYPE_PCM16
        assert len(raw) == HEADER_SIZE + 640

    def test_header_layout(self) -> None:
        raw = build_audio_frame(1, 0, b"\x00" * 640)
        frame_type, aseq, ts_ms = struct.unpack("<BHI", raw[:7])
        assert frame_type == FRAME_TYPE_PCM16
        assert aseq == 1
        assert ts_ms == 0

    def test_too_short(self) -> None:
        with pytest.raises(AudioFrameError):
            parse_audio_frame(b"\x01\x01\x00")

    def test_bad_payload_size(self) -> None:
        raw = build_audio_frame(1, 0, b"\x00" * 640)
        with pytest.raises(AudioFrameError):
            parse_audio_frame(raw[:-1])

    def test_wrong_frame_type(self) -> None:
        raw = bytes([FRAME_TYPE_OPUS]) + b"\x01\x00" + b"\x00\x00\x00\x00" + b"\x00" * 640
        with pytest.raises(AudioFrameError):
            parse_audio_frame(raw)
        # 保留类型默认拒绝，显式 allow_reserved 可解析（V1 不使用 Opus）
        frame = parse_audio_frame(raw, allow_reserved=True)
        assert frame.aseq == 1
        assert len(frame.payload) == 640

    def test_aseq_zero_rejected(self) -> None:
        raw = bytes([FRAME_TYPE_PCM16]) + b"\x00\x00" + b"\x00\x00\x00\x00" + b"\x00" * 640
        with pytest.raises(AudioFrameError):
            parse_audio_frame(raw)

    def test_build_rejects_bad_aseq(self) -> None:
        with pytest.raises(AudioFrameError):
            build_audio_frame(0, 0, b"\x00" * 640)
        with pytest.raises(AudioFrameError):
            build_audio_frame(65536, 0, b"\x00" * 640)

    def test_build_rejects_oversized_payload(self) -> None:
        with pytest.raises(AudioFrameError):
            build_audio_frame(1, 0, b"\x00" * (MAX_AUDIO_FRAME_BYTES + 1))

    def test_next_aseq_wrap(self) -> None:
        assert next_aseq(ASEQ_MAX) == 1
        assert next_aseq(5) == 6


class TestSilence:
    def test_zeros_are_silent(self) -> None:
        payload = b"\x00" * 640
        assert is_silent(payload) is True
        assert pcm16_rms(payload) == 0.0

    def test_loud_pcm_not_silent(self) -> None:
        payload = _pcm(1000)
        assert is_silent(payload) is False
        assert pcm16_rms(payload) > 120.0

    def test_threshold_boundary(self) -> None:
        assert is_silent(_pcm(100), threshold=120.0) is True
        assert is_silent(_pcm(1000), threshold=120.0) is False
