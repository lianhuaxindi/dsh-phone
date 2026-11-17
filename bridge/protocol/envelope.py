"""JSON 信封校验与序号计数器（PROTOCOL §2）。

信封字段：``type``（必填 string）、``seq``（必填 int，该方向单调递增，首条为 1）、
``replyTo``（响应类必填，回带请求的 ``seq``）。

本模块只负责**信封**层的校验；``replyTo`` 与请求的对应关系由调用方（服务端分发 /
客户端请求表）核对。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .errors import ProtocolError, protocol_error

#: 文本帧上限 64 KiB（PROTOCOL §1/§13），按 UTF-8 编码后字节数计。
MAX_TEXT_FRAME_BYTES = 64 * 1024


@dataclass(frozen=True)
class Envelope:
    """已校验的信封字段。"""

    type: str
    seq: int
    reply_to: int | None


def _is_int(value: Any) -> bool:
    # bool 是 int 的子类，协议里的 seq 不接受 True/False。
    return isinstance(value, int) and not isinstance(value, bool)


def parse_json_text(raw: str | bytes) -> dict[str, Any]:
    """解析文本帧为 JSON 对象。

    Raises:
        ProtocolError: 非法 JSON、非 UTF-8、或顶层不是对象（→ 关闭 4400）。
    """
    if isinstance(raw, (bytes, bytearray)):
        if len(raw) > MAX_TEXT_FRAME_BYTES:
            raise protocol_error("text frame exceeds 64 KiB")
        try:
            text = bytes(raw).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise protocol_error("text frame is not valid UTF-8") from exc
    else:
        text = raw
        if len(text.encode("utf-8")) > MAX_TEXT_FRAME_BYTES:
            raise protocol_error("text frame exceeds 64 KiB")
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise protocol_error(f"invalid JSON: {exc.msg}") from exc
    if not isinstance(obj, dict):
        raise protocol_error("JSON message must be an object")
    return obj


def validate_envelope(obj: dict[str, Any]) -> Envelope:
    """校验信封必填字段并返回 ``Envelope``。

    Raises:
        ProtocolError: 缺 ``type``/``seq``、类型错误、``replyTo`` 非 int|null（→ 4400）。
    """
    msg_type = obj.get("type")
    if not isinstance(msg_type, str) or not msg_type:
        raise protocol_error("missing or invalid 'type'")
    if "seq" not in obj:
        raise protocol_error("missing 'seq'")
    seq = obj["seq"]
    if not _is_int(seq) or seq < 1:
        raise protocol_error("'seq' must be an integer >= 1")
    reply_to = obj.get("replyTo")
    if reply_to is not None:
        if not _is_int(reply_to) or reply_to < 1:
            raise protocol_error("'replyTo' must be an integer >= 1 or null")
    return Envelope(type=msg_type, seq=seq, reply_to=reply_to)


def require_reply(envelope: Envelope, request_seq: int) -> None:
    """断言响应信封的 ``replyTo`` 等于请求的 ``seq``（客户端/测试辅助）。"""
    if envelope.reply_to is None:
        raise ProtocolError(
            f"response '{envelope.type}' missing replyTo",
            close_code=4400,
            reply_to=request_seq,
        )
    if envelope.reply_to != request_seq:
        raise ProtocolError(
            f"response '{envelope.type}' replyTo={envelope.reply_to} != request seq={request_seq}",
            close_code=4400,
            reply_to=request_seq,
        )


class SeqCounter:
    """单方向出站消息序号计数器，首个 ``next()`` 返回 1（PROTOCOL §2）。"""

    __slots__ = ("_next",)

    def __init__(self, start: int = 1) -> None:
        self._next = start

    @property
    def next_seq(self) -> int:
        """下一次 ``next()`` 将返回的值（用于预告 ``fromSeq`` 等）。"""
        return self._next

    def next(self) -> int:
        """返回当前序号并自增。"""
        seq = self._next
        self._next += 1
        return seq


class IncomingSeqTracker:
    """入站 ``seq`` 观测器。

    协议要求同方向 ``seq`` 单调递增，但**不因违例断开**（PROTOCOL §12 的 4400 只覆盖
    非法 JSON / 未知 type / 缺字段）。这里只记录违例次数供诊断日志使用。
    """

    __slots__ = ("last_seq", "accepted", "violations")

    def __init__(self) -> None:
        self.last_seq = 0
        self.accepted = 0
        self.violations = 0

    def observe(self, seq: int) -> bool:
        """记录一个入站 ``seq``；返回是否满足严格单调递增。"""
        self.accepted += 1
        if seq <= self.last_seq:
            self.violations += 1
            return False
        self.last_seq = seq
        return True
