"""协议错误码、WebSocket 关闭码与统一异常 ``ProtocolError``。

对应 ``docs/PROTOCOL.md`` §12。所有业务错误统一走本模块，禁止在其它模块里裸抛
``ValueError`` 来表示协议错误。
"""

from __future__ import annotations

from enum import IntEnum
from typing import Any


class ErrorCode(IntEnum):
    """PROTOCOL §12 通用错误码（``error.code`` / ``pair.error.code`` 等字段）。"""

    AUTH_FAILED = 1001
    PAIR_CODE = 1002
    PROTOCOL_VERSION = 1003
    AUDIO_BUSY = 2001
    ASR_UNAVAILABLE = 2002
    SESSION_NOT_FOUND = 3001
    APPROVAL_TIMEOUT = 3002
    MODEL_ERROR = 3003
    SERVER_BUSY = 4001
    INTERNAL_ERROR = 5001
    SDK_PROTOCOL_ERROR = 5002


class CloseCode(IntEnum):
    """PROTOCOL §12 WebSocket 关闭码。"""

    NORMAL = 1000
    INTERNAL_ERROR = 1011
    PROTOCOL = 4400
    AUTH_FAILED = 4401
    VERSION_MISMATCH = 4402
    HEARTBEAT_TIMEOUT = 4408
    PAIR_LOCKED = 4410


#: PROTOCOL §12「可重试」列：True 表示客户端可自动/手动重试。
RETRYABLE: dict[int, bool] = {
    ErrorCode.AUTH_FAILED: False,
    ErrorCode.PAIR_CODE: True,
    ErrorCode.PROTOCOL_VERSION: False,
    ErrorCode.AUDIO_BUSY: True,
    ErrorCode.ASR_UNAVAILABLE: True,
    ErrorCode.SESSION_NOT_FOUND: False,
    ErrorCode.APPROVAL_TIMEOUT: True,
    ErrorCode.MODEL_ERROR: False,
    ErrorCode.SERVER_BUSY: True,
    ErrorCode.INTERNAL_ERROR: True,
    ErrorCode.SDK_PROTOCOL_ERROR: False,
}

#: 某些错误码在「终止连接」语义下对应的关闭码（PROTOCOL §3/§5.1/§12）。
CLOSE_CODE_FOR_ERROR: dict[int, int] = {
    ErrorCode.AUTH_FAILED: CloseCode.AUTH_FAILED,
    ErrorCode.PROTOCOL_VERSION: CloseCode.VERSION_MISMATCH,
}


def is_retryable(code: int | None) -> bool:
    """返回错误码是否可重试（未知错误码按可重试处理，保守但不阻塞客户端）。"""
    if code is None:
        return False
    return RETRYABLE.get(int(code), True)


class ProtocolError(Exception):
    """协议层异常。

    Attributes:
        code: PROTOCOL §12 业务错误码；``None`` 表示只关闭连接、不回错误帧
            （例如文本帧不是合法 JSON）。
        message: 面向诊断的英文短消息（可安全展示给用户/日志）。
        retryable: 是否可重试；缺省按 ``code`` 查表。
        close_code: 非 ``None`` 时，发送完 ``error`` 帧后以该码关闭连接。
        reply_to: 错误帧回带的请求 ``seq``（未知则 ``None``）。
        attempts_left: 仅配对失败使用（``pair.error.attemptsLeft``）。
        detail: 错误帧 ``detail`` 字段（默认 ``None``，按协议显式发送 null）。
        need_pair: 仅认证失败使用（``auth.error.needPair``）。
    """

    def __init__(
        self,
        message: str,
        *,
        code: int | None = None,
        retryable: bool | None = None,
        close_code: int | None = None,
        reply_to: int | None = None,
        attempts_left: int | None = None,
        detail: Any = None,
        need_pair: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code: int | None = None if code is None else int(code)
        self.retryable = is_retryable(self.code) if retryable is None else retryable
        self.close_code = close_code
        self.reply_to = reply_to
        self.attempts_left = attempts_left
        self.detail = detail
        self.need_pair = need_pair

    def __str__(self) -> str:  # pragma: no cover - 调试辅助
        parts = [self.message]
        if self.code is not None:
            parts.append(f"code={int(self.code)}")
        if self.close_code is not None:
            parts.append(f"close={int(self.close_code)}")
        return " ".join(parts)


def protocol_error(message: str, *, reply_to: int | None = None) -> ProtocolError:
    """构造「非法 JSON / 缺信封字段」类协议错误（PROTOCOL §12 → 4400）。"""
    return ProtocolError(message, close_code=CloseCode.PROTOCOL, reply_to=reply_to)
