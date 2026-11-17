"""二进制音频帧解析与构造（PROTOCOL §10.2）。

帧格式（小端）::

    偏移 0  1 字节  帧类型 0x01=PCM16LE（0x02=Opus 保留）
    偏移 1  2 字节  aseq（u16 LE，从 1 递增，65535 后回绕到 1）
    偏移 3  4 字节  tsMs（u32 LE，相对 audio.start 的采集时刻，毫秒）
    偏移 7  640 字节 PCM（16000 Hz / 16 bit / 单声道 / 20 ms）

V1 只允许 ``pcm16``；Opus 帧类型保留（V1.1）。
"""

from __future__ import annotations

from dataclasses import dataclass

#: 帧头长度（字节）。
HEADER_SIZE = 7
#: V1 唯一支持的帧类型：PCM16LE。
FRAME_TYPE_PCM16 = 0x01
#: V1.1 保留：Opus。
FRAME_TYPE_OPUS = 0x02
#: 单条二进制帧上限 8 KiB（PROTOCOL §1）。
MAX_AUDIO_FRAME_BYTES = 8 * 1024
#: 每帧固定 20 ms（PROTOCOL §13）。
FRAME_DURATION_MS = 20
#: 默认 PCM payload 长度：16000 Hz × 16 bit × 单声道 × 20 ms / 8 = 640 B。
DEFAULT_PAYLOAD_BYTES = 640
#: u16 序号回绕上限。
ASEQ_MAX = 65535


class AudioFrameError(ValueError):
    """二进制音频帧格式错误（调用方应丢弃该帧并计数，不关闭连接）。"""


@dataclass(frozen=True)
class AudioFrame:
    """解析后的音频帧。"""

    aseq: int
    ts_ms: int
    payload: bytes

    @property
    def frame_type(self) -> int:
        return FRAME_TYPE_PCM16

    def __len__(self) -> int:
        return HEADER_SIZE + len(self.payload)


def next_aseq(aseq: int) -> int:
    """音频帧序号推进：``65535 -> 1``（PROTOCOL §2/§10.2）。"""
    _check_aseq(aseq)
    return (aseq % ASEQ_MAX) + 1


def _check_aseq(aseq: int) -> None:
    if not isinstance(aseq, int) or isinstance(aseq, bool):
        raise AudioFrameError("aseq must be an integer")
    if not 1 <= aseq <= ASEQ_MAX:
        raise AudioFrameError(f"aseq out of range 1..{ASEQ_MAX}: {aseq}")


def payload_size_for(
    sample_rate: int,
    channels: int = 1,
    bit_depth: int = 16,
    duration_ms: int = FRAME_DURATION_MS,
) -> int:
    """按采样参数计算 20 ms 帧的 PCM 字节数；不能整除 20 ms 时抛错。

    16000 Hz / 16 bit / 单声道 / 20 ms → 640 B（PROTOCOL §10.2 注）。
    """
    if sample_rate <= 0 or channels <= 0 or bit_depth <= 0 or duration_ms <= 0:
        raise AudioFrameError("invalid audio parameters")
    if bit_depth % 8 != 0:
        raise AudioFrameError("bit depth must be a multiple of 8")
    numerator = sample_rate * channels * (bit_depth // 8) * duration_ms
    if numerator % 1000 != 0:
        raise AudioFrameError("frame duration does not map to whole bytes")
    size = numerator // 1000
    if not 1 <= size <= MAX_AUDIO_FRAME_BYTES:
        raise AudioFrameError(f"payload size out of range: {size}")
    return size


def build_audio_frame(aseq: int, ts_ms: int, payload: bytes) -> bytes:
    """构造一条二进制音频帧（客户端/测试用）。

    Raises:
        AudioFrameError: aseq 越界、tsMs 越界、payload 长度不是 640 B。
    """
    _check_aseq(aseq)
    if not isinstance(ts_ms, int) or isinstance(ts_ms, bool) or not 0 <= ts_ms <= 0xFFFFFFFF:
        raise AudioFrameError("tsMs must be an unsigned 32-bit integer")
    if len(payload) != DEFAULT_PAYLOAD_BYTES:
        raise AudioFrameError(
            f"PCM payload must be exactly {DEFAULT_PAYLOAD_BYTES} bytes, got {len(payload)}"
        )
    return (
        bytes([FRAME_TYPE_PCM16])
        + aseq.to_bytes(2, "little")
        + ts_ms.to_bytes(4, "little")
        + payload
    )


def parse_audio_frame(
    data: bytes,
    *,
    expected_payload: int = DEFAULT_PAYLOAD_BYTES,
    allow_reserved: bool = False,
) -> AudioFrame:
    """解析并校验一条二进制音频帧。

    Args:
        data: 完整帧字节（帧头 + PCM）。
        expected_payload: 期望的 PCM 字节数；默认 640（16k/16bit/mono/20ms）。
        allow_reserved: 为 ``True`` 时接受保留帧类型 0x02（解析为 payload 但不解码）；
            V1 服务端保持 ``False``。

    Raises:
        AudioFrameError: 长度不符、帧类型不支持、aseq 为 0、payload 长度不符、帧过大。
    """
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise AudioFrameError("frame must be bytes-like")
    data = bytes(data)
    if len(data) > MAX_AUDIO_FRAME_BYTES:
        raise AudioFrameError(f"frame too large: {len(data)} > {MAX_AUDIO_FRAME_BYTES}")
    if len(data) < HEADER_SIZE:
        raise AudioFrameError(f"frame too short: {len(data)} < {HEADER_SIZE}")
    frame_type = data[0]
    if frame_type != FRAME_TYPE_PCM16 and not (allow_reserved and frame_type == FRAME_TYPE_OPUS):
        raise AudioFrameError(f"unsupported frame type 0x{frame_type:02x}")
    aseq = int.from_bytes(data[1:3], "little")
    ts_ms = int.from_bytes(data[3:7], "little")
    _check_aseq(aseq)
    payload = data[HEADER_SIZE:]
    if len(payload) != expected_payload:
        raise AudioFrameError(
            f"payload length mismatch: got {len(payload)}, expected {expected_payload}"
        )
    return AudioFrame(aseq=aseq, ts_ms=ts_ms, payload=payload)


def pcm16_rms(payload: bytes) -> float:
    """计算 PCM16LE 单声道数据的 RMS（0..32767），用于服务端静音 VAD。

    纯标准库实现（``array``），不引入 numpy 依赖。
    """
    from array import array

    samples = array("h")
    samples.frombytes(payload[: len(payload) // 2 * 2])
    if not samples:
        return 0.0
    # 大小端：Windows/x86 与协议一致为 little-endian；非 little 平台做字节交换。
    import sys

    if sys.byteorder != "little":
        samples.byteswap()
    total = 0
    for value in samples:
        total += value * value
    return (total / len(samples)) ** 0.5


def is_silent(payload: bytes, threshold: float = 120.0) -> bool:
    """判断一帧 PCM 是否静音（默认 RMS 阈值 120，约 -48 dBFS）。"""
    return pcm16_rms(payload) < threshold
