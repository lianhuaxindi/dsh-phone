"""协议消息 dataclass（与 ``docs/PROTOCOL.md`` §4–§11 一一对应）。

约定：

- 字段用 snake_case 声明，序列化自动转 camelCase（协议字段一律 camelCase）。
- ``type`` 是类变量；每类对应协议中的一个 ``type`` 值。
- 所有消息都带 ``seq``（入站由信封校验，出站由 ``SeqCounter`` 分配）。
- 响应类（``reply_required = True``）序列化时必须带 ``replyTo``。
- ``from_dict`` 校验必填字段；未知字段忽略（PROTOCOL §14 向前兼容）。
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import Any, ClassVar, TypeVar

from .errors import ProtocolError


def to_camel(name: str) -> str:
    """``sample_rate`` → ``sampleRate``；无下划线的名字原样返回（如 ``aseq``）。"""
    head, *rest = name.split("_")
    return head + "".join(part.capitalize() for part in rest)


def to_snake(name: str) -> str:  # pragma: no cover - 仅诊断/测试可能用到
    """``sampleRate`` → ``sample_rate``。"""
    out: list[str] = []
    for ch in name:
        if ch.isupper():
            out.append("_")
            out.append(ch.lower())
        else:
            out.append(ch)
    return "".join(out)


class MessageError(ProtocolError):
    """消息体不合法（→ PROTOCOL §12 关闭码 4400）。"""

    def __init__(self, message: str) -> None:
        super().__init__(message, close_code=4400)


class UnknownMessageType(ProtocolError):
    """未知 ``type``。

    协议 §12 把「未知 type」列为 4400，§14 又要求接收方忽略未知 ``type``。
    服务端策略由 ``connection.py`` 决定：认证前按 §3 拒绝（4401），认证后按 §14
    记录诊断并忽略。本异常不携带关闭码，供调用方决定。
    """

    def __init__(self, msg_type: Any, *, known: bool = False) -> None:
        super().__init__(f"unexpected message type: {msg_type!r}", close_code=None)
        self.msg_type = msg_type
        self.known = known


_FIELD_META = {"omit_none": True}


@dataclass
class Message:
    """所有协议消息的基类。"""

    seq: int = 0
    reply_to: int | None = None

    #: 协议 ``type`` 值。
    type: ClassVar[str] = ""
    #: 响应类消息必须回带 ``replyTo``。
    reply_required: ClassVar[bool] = False

    def to_dict(self) -> dict[str, Any]:
        """序列化为协议的 JSON 对象（含 ``type``/``seq``/``replyTo``）。"""
        out: dict[str, Any] = {"type": self.type, "seq": self.seq}
        if self.reply_to is not None:
            out["replyTo"] = self.reply_to
        for f in fields(self):
            name = f.name
            value = getattr(self, name)
            key = to_camel(name)
            if value is None and f.metadata.get("omit_none", True):
                continue
            out[key] = value
        return out

    def to_json(self) -> str:
        import json

        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_dict(cls, obj: dict[str, Any]) -> "Message":
        """从 JSON 对象构造；校验 ``type`` 与必填字段。"""
        msg_type = obj.get("type", cls.type)
        if msg_type != cls.type:
            raise MessageError(f"expected type {cls.type!r}, got {msg_type!r}")
        if cls.reply_required and obj.get("replyTo") is None:
            raise MessageError(f"response {cls.type!r} requires 'replyTo'")
        values: dict[str, Any] = {}
        for f in fields(cls):
            key = to_camel(f.name)
            if key in obj:
                values[f.name] = obj[key]
            elif f.default is not _MISSING and f.default_factory is not _MISSING_DEFAULT:
                continue
            elif f.default is not _MISSING or f.default_factory is not _MISSING_DEFAULT:
                continue
            else:
                raise MessageError(f"missing field {key!r} for message {cls.type!r}")
        try:
            return cls(**values)  # type: ignore[arg-type]
        except TypeError as exc:  # pragma: no cover - 防御
            raise MessageError(f"cannot build {cls.type!r}: {exc}") from exc

    @classmethod
    def from_json(cls, raw: str | bytes) -> "Message":  # pragma: no cover - 便捷
        import json

        return cls.from_dict(json.loads(raw))


_MISSING = object()
_MISSING_DEFAULT = object()


# ---------------------------------------------------------------------------
# 会话/消息数据结构（PROTOCOL §8.1）
# ---------------------------------------------------------------------------


@dataclass
class Session:
    """会话摘要（``Session``）。"""

    session_id: str = ""
    title: str = ""
    updated_at: int = 0
    last_preview: str = ""

    def __post_init__(self) -> None:
        # 兼容 wire 字段缺失时的默认值
        self.title = self.title or ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "sessionId": self.session_id,
            "title": self.title,
            "updatedAt": int(self.updated_at),
            "lastPreview": self.last_preview,
        }

    @classmethod
    def from_dict(cls, obj: dict[str, Any]) -> "Session":
        return cls(
            session_id=str(obj.get("sessionId", "")),
            title=str(obj.get("title", "")),
            updated_at=int(obj.get("updatedAt", 0)),
            last_preview=str(obj.get("lastPreview", "")),
        )


@dataclass
class ChatMessage:
    """单条消息（``Message``，注意与协议消息基类区分）。"""

    message_id: str = ""
    session_id: str = ""
    role: str = "assistant"
    text: str = ""
    status: str = "done"
    created_at: int = 0
    #: v1.1：仅 user 消息可能带图片元数据（不含 base64）。
    images: list[dict[str, Any]] | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "messageId": self.message_id,
            "sessionId": self.session_id,
            "role": self.role,
            "text": self.text,
            "status": self.status,
            "createdAt": int(self.created_at),
        }
        if self.images is not None:
            out["images"] = [dict(item) for item in self.images]
        return out

    @classmethod
    def from_dict(cls, obj: dict[str, Any]) -> "ChatMessage":
        images = obj.get("images")
        return cls(
            message_id=str(obj.get("messageId", "")),
            session_id=str(obj.get("sessionId", "")),
            role=str(obj.get("role", "assistant")),
            text=str(obj.get("text", "")),
            status=str(obj.get("status", "done")),
            created_at=int(obj.get("createdAt", 0)),
            images=[dict(item) for item in images] if isinstance(images, list) else None,
        )


# ---------------------------------------------------------------------------
# C -> S 消息
# ---------------------------------------------------------------------------


@dataclass
class Hello(Message):
    type: ClassVar[str] = "hello"
    v: int = 1
    device_id: str = ""
    app: str = ""
    os: str = ""
    ts: int = 0


@dataclass
class PairRequest(Message):
    type: ClassVar[str] = "pair.request"
    device_id: str = ""
    code: str = ""
    device_name: str = ""
    pub_key: str | None = field(default=None, metadata={"omit_none": False})


@dataclass
class Auth(Message):
    type: ClassVar[str] = "auth"
    device_id: str = ""
    ts: int = 0
    nonce: str = ""
    hmac: str = ""
    v: int = 1


@dataclass
class Ping(Message):
    type: ClassVar[str] = "ping"
    ts: int = 0


@dataclass
class Resync(Message):
    type: ClassVar[str] = "resync"
    last_seq: int = 0


@dataclass
class SessionList(Message):
    type: ClassVar[str] = "session.list"


@dataclass
class SessionCreate(Message):
    type: ClassVar[str] = "session.create"
    title: str | None = None


@dataclass
class SessionResume(Message):
    type: ClassVar[str] = "session.resume"
    session_id: str = ""


@dataclass
class SessionHistory(Message):
    type: ClassVar[str] = "session.history"
    session_id: str = ""
    limit: int = 50
    before: str | None = field(default=None, metadata={"omit_none": False})


@dataclass
class ChatSend(Message):
    type: ClassVar[str] = "chat.send"
    client_id: str = ""
    session_id: str = ""
    text: str = ""
    #: v1.1：可选图片附件（base64 data URI 内容块，见 PROTOCOL §9）。
    images: list[dict[str, Any]] | None = None


@dataclass
class ChatCancel(Message):
    type: ClassVar[str] = "chat.cancel"
    session_id: str = ""


@dataclass
class AudioStart(Message):
    type: ClassVar[str] = "audio.start"
    sample_rate: int = 16000
    channels: int = 1
    bit_depth: int = 16
    lang: str = "zh"
    mode: str = "ptt"
    session_id: str | None = None


@dataclass
class AudioStop(Message):
    type: ClassVar[str] = "audio.stop"
    aseq: int = 0


@dataclass
class ApprovalResponse(Message):
    type: ClassVar[str] = "approval.response"
    id: str = ""
    outcome: str = "denied"


@dataclass
class QuestionResponse(Message):
    type: ClassVar[str] = "question.response"
    id: str = ""
    cancelled: bool = False
    answers: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class MediaGet(Message):
    """v1.1：按 ``mediaId`` 拉取原图（已认证连接可用）。"""

    type: ClassVar[str] = "media.get"
    media_id: str = ""


# ---------------------------------------------------------------------------
# S -> C 消息
# ---------------------------------------------------------------------------


@dataclass
class HelloOK(Message):
    type: ClassVar[str] = "hello.ok"
    reply_required: ClassVar[bool] = True
    server_id: str = ""
    bridge: str = ""
    need_pair: bool = True
    auth_required: bool = True
    ts: int = 0


@dataclass
class PairOK(Message):
    type: ClassVar[str] = "pair.ok"
    reply_required: ClassVar[bool] = True
    device_token: str = ""
    server_fingerprint: str = ""
    server_id: str = ""
    expires_at: int | None = field(default=None, metadata={"omit_none": False})


@dataclass
class PairError(Message):
    type: ClassVar[str] = "pair.error"
    reply_required: ClassVar[bool] = True
    code: int = 0
    message: str = ""
    attempts_left: int = 0
    retryable: bool = True


@dataclass
class AuthOK(Message):
    type: ClassVar[str] = "auth.ok"
    reply_required: ClassVar[bool] = True
    server_id: str = ""
    sessions: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class AuthError(Message):
    type: ClassVar[str] = "auth.error"
    reply_required: ClassVar[bool] = True
    code: int = 1001
    message: str = ""
    need_pair: bool = True


@dataclass
class Pong(Message):
    type: ClassVar[str] = "pong"
    reply_required: ClassVar[bool] = True


@dataclass
class ResyncOK(Message):
    type: ClassVar[str] = "resync.ok"
    reply_required: ClassVar[bool] = True
    from_seq: int = 0
    upto_seq: int = 0
    truncated: bool = False


@dataclass
class SessionListOK(Message):
    type: ClassVar[str] = "session.list.ok"
    reply_required: ClassVar[bool] = True
    sessions: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class SessionCreateOK(Message):
    type: ClassVar[str] = "session.create.ok"
    reply_required: ClassVar[bool] = True
    session: dict[str, Any] = field(default_factory=dict)


@dataclass
class SessionResumeOK(Message):
    type: ClassVar[str] = "session.resume.ok"
    reply_required: ClassVar[bool] = True
    session: dict[str, Any] = field(default_factory=dict)
    messages: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class SessionHistoryOK(Message):
    type: ClassVar[str] = "session.history.ok"
    reply_required: ClassVar[bool] = True
    messages: list[dict[str, Any]] = field(default_factory=list)
    has_more: bool = False


@dataclass
class SessionMeta(Message):
    """v1.1：会话运行时元信息（无 replyTo，可被 resync 重放）。"""

    type: ClassVar[str] = "session.meta"
    session_id: str = ""
    provider: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    context_window: int | None = None


@dataclass
class MediaData(Message):
    """v1.1：原图响应（base64；单次 ≤2MiB）。"""

    type: ClassVar[str] = "media.data"
    reply_required: ClassVar[bool] = True
    media_id: str = ""
    mime_type: str = ""
    data: str = ""


@dataclass
class ChatAck(Message):
    type: ClassVar[str] = "chat.ack"
    reply_required: ClassVar[bool] = True
    client_id: str = ""
    message_id: str = ""


@dataclass
class AssistantDelta(Message):
    type: ClassVar[str] = "assistant.delta"
    session_id: str = ""
    message_id: str = ""
    text: str = ""
    index: int = 0


@dataclass
class ToolStatus(Message):
    type: ClassVar[str] = "tool.status"
    session_id: str = ""
    tool_call_id: str = ""
    name: str = ""
    state: str = "running"
    title: str = ""


@dataclass
class AssistantDone(Message):
    type: ClassVar[str] = "assistant.done"
    session_id: str = ""
    message_id: str = ""
    finish_reason: str = "completed"
    #: v1.1：token 用量（缺失省略，见 PROTOCOL §9）。
    usage: dict[str, Any] | None = None


@dataclass
class TurnError(Message):
    type: ClassVar[str] = "turn.error"
    session_id: str = ""
    code: int = 3003
    message: str = ""


@dataclass
class AudioReady(Message):
    type: ClassVar[str] = "audio.ready"
    reply_required: ClassVar[bool] = True
    asr_mode: str = "fake"
    lang: str = "zh"
    codecs: list[str] = field(default_factory=lambda: ["pcm16"])


@dataclass
class AsrPartial(Message):
    type: ClassVar[str] = "asr.partial"
    text: str = ""
    stability: float = 0.0
    aseq: int = 0


@dataclass
class AsrFinal(Message):
    type: ClassVar[str] = "asr.final"
    text: str = ""
    aseq: int = 0
    duration_ms: int = 0
    lang: str = "zh"


@dataclass
class ApprovalRequest(Message):
    type: ClassVar[str] = "approval.request"
    id: str = ""
    session_id: str = ""
    tool: str = ""
    reason: str = ""
    args_preview: str = ""
    deadline: int = 0
    default_outcome: str = "denied"


@dataclass
class ApprovalExpired(Message):
    type: ClassVar[str] = "approval.expired"
    id: str = ""
    outcome: str = "denied"


@dataclass
class QuestionRequest(Message):
    type: ClassVar[str] = "question.request"
    id: str = ""
    session_id: str = ""
    questions: list[dict[str, Any]] = field(default_factory=list)
    deadline: int = 0


@dataclass
class QuestionExpired(Message):
    type: ClassVar[str] = "question.expired"
    id: str = ""
    cancelled: bool = True


@dataclass
class ErrorMessage(Message):
    type: ClassVar[str] = "error"
    code: int = 0
    message: str = ""
    retryable: bool = True
    detail: Any = field(default=None, metadata={"omit_none": False})


# ---------------------------------------------------------------------------
# 注册表与解码
# ---------------------------------------------------------------------------


C2S_MESSAGES: tuple[type[Message], ...] = (
    Hello,
    PairRequest,
    Auth,
    Ping,
    Resync,
    SessionList,
    SessionCreate,
    SessionResume,
    SessionHistory,
    ChatSend,
    ChatCancel,
    AudioStart,
    AudioStop,
    ApprovalResponse,
    QuestionResponse,
    MediaGet,
)

S2C_MESSAGES: tuple[type[Message], ...] = (
    HelloOK,
    PairOK,
    PairError,
    AuthOK,
    AuthError,
    Pong,
    ResyncOK,
    SessionListOK,
    SessionCreateOK,
    SessionResumeOK,
    SessionHistoryOK,
    SessionMeta,
    MediaData,
    ChatAck,
    AssistantDelta,
    ToolStatus,
    AssistantDone,
    TurnError,
    AudioReady,
    AsrPartial,
    AsrFinal,
    ApprovalRequest,
    ApprovalExpired,
    QuestionRequest,
    QuestionExpired,
    ErrorMessage,
)

MESSAGE_TYPES: dict[str, type[Message]] = {
    cls.type: cls for cls in (*C2S_MESSAGES, *S2C_MESSAGES)
}

C2S_TYPES: frozenset[str] = frozenset(cls.type for cls in C2S_MESSAGES)
S2C_TYPES: frozenset[str] = frozenset(cls.type for cls in S2C_MESSAGES)


def decode_message(obj: dict[str, Any]) -> Message:
    """按 ``obj['type']`` 找到类并构造。

    Raises:
        UnknownMessageType: 未知 ``type``。
        MessageError: 已知 ``type`` 但字段非法（→ 4400）。
    """
    msg_type = obj.get("type")
    cls = MESSAGE_TYPES.get(msg_type) if isinstance(msg_type, str) else None
    if cls is None:
        raise UnknownMessageType(msg_type, known=False)
    return cls.from_dict(obj)
