"""单个 WebSocket 连接的协议状态机（PROTOCOL §3–§11）。

连接生命周期：``connect → hello → hello.ok → (pair.request → pair.ok)?
→ auth → auth.ok → resync → resync.ok → 业务``。

认证前只接受 ``hello``/``pair.request``/``auth``/``ping``，其它消息回
``error{1001}`` 并以 4401 关闭（PROTOCOL §3）。所有 ``S→C`` 消息的 ``seq``
由 :class:`SeqCounter` 按连接独立分配，从 1 开始。

可重放的「事件」类消息（无 ``replyTo``）写入 :class:`DeviceSession` 的重放
缓冲，供客户端重连后 ``resync`` 补拉；带 ``replyTo`` 的响应只发给当前连接。
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from websockets.asyncio.server import ServerConnection
from websockets.exceptions import ConnectionClosed

from . import __version__
from .dsh import (
    DshApproval,
    DshDelta,
    DshDone,
    DshError,
    DshMeta,
    DshQuestion,
    DshTool,
    DshTurn,
)
from .media import (
    ImagePayload,
    MediaError,
    decode_attachments,
    load_image,
    save_images,
)
from .protocol.audio import AudioFrameError, is_silent, parse_audio_frame
from .protocol.envelope import (
    IncomingSeqTracker,
    SeqCounter,
    parse_json_text,
    validate_envelope,
)
from .protocol.errors import (
    CloseCode,
    ErrorCode,
    ProtocolError,
    is_retryable,
    protocol_error,
)
from .protocol.messages import (
    ApprovalExpired,
    ApprovalRequest,
    ApprovalResponse,
    AsrFinal,
    AsrPartial,
    AssistantDelta,
    AssistantDone,
    AudioReady,
    AudioStart,
    AudioStop,
    Auth,
    AuthError,
    AuthOK,
    C2S_TYPES,
    ChatAck,
    ChatCancel,
    ChatSend,
    Hello,
    HelloOK,
    ErrorMessage,
    MediaData,
    MediaGet,
    PairError,
    PairOK,
    PairRequest,
    Ping,
    Pong,
    QuestionExpired,
    QuestionRequest,
    QuestionResponse,
    Resync,
    ResyncOK,
    S2C_TYPES,
    SessionCreate,
    SessionCreateOK,
    SessionHistory,
    SessionHistoryOK,
    SessionList,
    SessionListOK,
    SessionMeta,
    SessionResume,
    SessionResumeOK,
    ToolStatus,
    TurnError,
    decode_message,
)

if TYPE_CHECKING:  # pragma: no cover - 仅类型提示
    from .server import BridgeServer

logger = logging.getLogger("bridge.connection")

#: 文本消息字符上限（PROTOCOL §13）。
MAX_TEXT_CHARS = 32_000
#: S→C 重放缓冲：最近 512 条或 10 分钟（PROTOCOL §2）。
REPLAY_MAX_ENTRIES = 512
REPLAY_WINDOW_SEC = 600.0
#: 未认证连接存活上限（PROTOCOL §3）。
UNAUTH_DEADLINE_SEC = 15.0
#: 心跳：服务端 60s 未收到客户端任何消息则关闭 4408（PROTOCOL §7）。
HEARTBEAT_TIMEOUT_SEC = 60.0
#: 审批/提问 deadline 默认 10 分钟（PROTOCOL §8.6）。
DEFAULT_INTERACTION_DEADLINE_SEC = 600


# ---------------------------------------------------------------------------
# 事件重放
# ---------------------------------------------------------------------------


@dataclass
class _ReplayEntry:
    seq: int
    ts: float
    payload: dict[str, Any]


class DeviceSession:
    """按 ``deviceId`` 持久（跨连接）的会话状态：事件重放缓冲 + 进行中的 turn。"""

    def __init__(self, device_id: str, server: "BridgeServer") -> None:
        self.device_id = device_id
        self.server = server
        self.connection: "Connection | None" = None
        self._replay: deque[_ReplayEntry] = deque(maxlen=REPLAY_MAX_ENTRIES)
        #: S→C 方向 seq 按设备（跨连接）连续计数（PROTOCOL §2/§7：只有客户端重连后重置）。
        self.seq_counter = SeqCounter()
        self.turns: dict[str, "_TurnHandle"] = {}
        self.pending: dict[str, "_PendingInteraction"] = {}
        self.device_name = ""
        #: 最近一次已发送的 session.meta 键（设备级去重，跨 turn 生效；D-032）。
        self.last_session_meta: tuple[Any, ...] | None = None

    # -- 连接 -------------------------------------------------------------

    def attach(self, connection: "Connection") -> None:
        self.connection = connection

    def detach(self, connection: "Connection") -> None:
        if self.connection is connection:
            self.connection = None

    @property
    def online(self) -> bool:
        return self.connection is not None and not self.connection.closed

    # -- 重放 -------------------------------------------------------------

    def _prune(self, now: float) -> None:
        while self._replay and now - self._replay[0].ts > REPLAY_WINDOW_SEC:
            self._replay.popleft()

    def _record(self, seq: int, payload: dict[str, Any]) -> None:
        self._replay.append(_ReplayEntry(seq=seq, ts=asyncio.get_running_loop().time(), payload=payload))

    async def send_event(self, message: Any) -> None:
        """发送一条可重放事件（无 ``replyTo``），并写入重放缓冲。

        S→C seq 在设备维度连续（跨连接不重置），因此离线期间也立即占号，
        保证 ``resync`` 的 ``lastSeq`` 比较有效。
        """
        loop = asyncio.get_running_loop()
        self._prune(loop.time())
        message.seq = self.seq_counter.next()
        payload = message.to_dict()
        self._record(message.seq, dict(payload))
        conn = self.connection
        if conn is not None and not conn.closed:
            await conn._send_raw(message.to_json())

    async def replay_from(self, last_seq: int, connection: "Connection", reply_to: int) -> None:
        """重连后补拉：先发 ``resync.ok``，再按原顺序重放 ``seq > last_seq`` 的事件。"""
        loop = asyncio.get_running_loop()
        self._prune(loop.time())
        entries = [e for e in self._replay if e.seq > last_seq]
        ack_seq = connection._seq.next()
        from_seq = ack_seq + 1
        upto_seq = from_seq + len(entries) - 1 if entries else ack_seq
        truncated = bool(self._replay) and bool(entries) and self._replay[0].seq > last_seq + 1
        ack = ResyncOK(from_seq=from_seq, upto_seq=upto_seq, truncated=truncated)
        ack.seq = ack_seq
        ack.reply_to = reply_to
        await connection._send_raw(ack.to_json())
        for entry in entries:
            new_seq = connection._seq.next()
            payload = dict(entry.payload)
            payload["seq"] = new_seq
            await connection._send_raw(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            )
            entry.seq = new_seq
        logger.debug(
            "resync.replayed",
            extra={"event": "resync.replayed", "fields": {"count": len(entries), "truncated": truncated}},
        )

    # -- turn -------------------------------------------------------------

    def start_turn(
        self,
        *,
        session_id: str,
        dsh_session_id: str,
        text: str,
        assistant_message_id: str,
        images: list[dict[str, Any]] | None = None,
    ) -> None:
        handle = _TurnHandle(
            device=self,
            session_id=session_id,
            dsh_session_id=dsh_session_id,
            text=text,
            assistant_message_id=assistant_message_id,
            images=images,
        )
        self.turns[session_id] = handle
        handle.task = asyncio.create_task(
            self._run_turn(handle), name=f"turn-{session_id}"
        )

    def cancel_turn(self, session_id: str) -> bool:
        handle = self.turns.get(session_id)
        if handle is None:
            return False
        handle.cancel_requested = True
        if not handle.turn.cancel_supported:
            return False
        handle.turn.cancel()
        return True

    async def _run_turn(self, handle: "_TurnHandle") -> None:
        """消费 DSH turn 事件并映射为协议 S→C 事件。"""
        history = self.server.history
        finish_reason = "completed"
        accumulated: list[str] = []
        try:
            init = self.server.dsh.start_turn(
                handle.dsh_session_id, handle.text, images=handle.images
            )
            handle.turn = init
            if handle.cancel_requested:
                init.cancel()
            async for event in init:
                if isinstance(event, DshDelta):
                    accumulated.append(event.text)
                    await self.send_event(
                        AssistantDelta(
                            session_id=handle.session_id,
                            message_id=handle.assistant_message_id,
                            text=event.text,
                            index=handle.delta_index,
                        )
                    )
                    handle.delta_index += 1
                elif isinstance(event, DshMeta):
                    await self._emit_session_meta(handle, event)
                elif isinstance(event, DshTool):
                    await self.send_event(
                        ToolStatus(
                            session_id=handle.session_id,
                            tool_call_id=event.tool_call_id,
                            name=event.name,
                            state=event.state,
                            title=event.title,
                        )
                    )
                elif isinstance(event, DshApproval):
                    await self._emit_approval(handle, event)
                elif isinstance(event, DshQuestion):
                    await self._emit_question(handle, event)
                elif isinstance(event, DshError):
                    finish_reason = "error"
                    await self.send_event(
                        TurnError(
                            session_id=handle.session_id,
                            code=event.code,
                            message=event.message,
                        )
                    )
                elif isinstance(event, DshDone):
                    finish_reason = event.finish_reason
                    handle.usage = event.usage
        except asyncio.CancelledError:
            finish_reason = "cancelled"
            raise
        except Exception as exc:  # pragma: no cover - 防御
            logger.exception("turn.failed", extra={"event": "turn.failed"})
            finish_reason = "error"
            await self.send_event(
                TurnError(
                    session_id=handle.session_id,
                    code=ErrorCode.SDK_PROTOCOL_ERROR,
                    message=f"DSH 执行失败: {exc}",
                )
            )
        finally:
            status = "error" if finish_reason == "error" else "done"
            try:
                history.update_message(
                    handle.assistant_message_id,
                    text="".join(accumulated),
                    status=status,
                )
                history.touch_session(
                    handle.session_id, preview="".join(accumulated)
                )
            except Exception:  # pragma: no cover - 防御
                logger.exception("turn.persist_failed", extra={"event": "turn.persist_failed"})
            await self.send_event(
                AssistantDone(
                    session_id=handle.session_id,
                    message_id=handle.assistant_message_id,
                    finish_reason=finish_reason,
                    usage=handle.usage,
                )
            )
            self.turns.pop(handle.session_id, None)

    async def _emit_session_meta(self, handle: "_TurnHandle", event: DshMeta) -> None:
        """把运行时事件里的 model/context 变化转成 ``session.meta``（D-032）。"""
        meta = SessionMeta(
            session_id=handle.session_id,
            provider=event.provider,
            model=event.model,
            reasoning_effort=event.reasoning_effort,
            context_window=event.context_window,
        )
        key = (
            meta.provider,
            meta.model,
            meta.reasoning_effort,
            meta.context_window,
        )
        if self.last_session_meta == key:
            return
        self.last_session_meta = key
        await self.send_event(meta)

    async def _emit_approval(self, handle: "_TurnHandle", event: DshApproval) -> None:
        deadline = int(event.deadline or (time.time() + DEFAULT_INTERACTION_DEADLINE_SEC))
        pending = _PendingInteraction(
            id=event.id,
            kind="approval",
            session_id=handle.session_id,
            turn=handle.turn,
            deadline=deadline,
            default_outcome=event.default_outcome or "denied",
        )
        self.pending[event.id] = pending
        await self.send_event(
            ApprovalRequest(
                id=event.id,
                session_id=handle.session_id,
                tool=event.tool,
                reason=event.reason,
                args_preview=event.args_preview,
                deadline=deadline,
                default_outcome=event.default_outcome or "denied",
            )
        )
        pending.timer = asyncio.create_task(self._interaction_timer(event.id))

    async def _emit_question(self, handle: "_TurnHandle", event: DshQuestion) -> None:
        deadline = int(event.deadline or (time.time() + DEFAULT_INTERACTION_DEADLINE_SEC))
        pending = _PendingInteraction(
            id=event.id,
            kind="question",
            session_id=handle.session_id,
            turn=handle.turn,
            deadline=deadline,
        )
        self.pending[event.id] = pending
        await self.send_event(
            QuestionRequest(
                id=event.id,
                session_id=handle.session_id,
                questions=event.questions,
                deadline=deadline,
            )
        )
        pending.timer = asyncio.create_task(self._interaction_timer(event.id))

    async def _interaction_timer(self, request_id: str) -> None:
        pending = self.pending.get(request_id)
        if pending is None:
            return
        delay = max(0.0, pending.deadline - time.time())
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        pending = self.pending.pop(request_id, None)
        if pending is None or pending.resolved:
            return
        pending.resolved = True
        if pending.kind == "approval":
            await self.send_event(
                ApprovalExpired(id=pending.id, outcome=pending.default_outcome)
            )
            pending.turn.respond_approval(pending.id, None)
        else:
            await self.send_event(QuestionExpired(id=pending.id, cancelled=True))
            pending.turn.respond_question(pending.id, None)

    # -- 交互响应 ---------------------------------------------------------

    def resolve_approval(self, request_id: str, outcome: str) -> bool:
        pending = self.pending.get(request_id)
        if pending is None or pending.kind != "approval" or pending.resolved:
            return False
        self.pending.pop(request_id, None)
        pending.resolved = True
        if pending.timer is not None:
            pending.timer.cancel()
        pending.turn.respond_approval(request_id, outcome)
        return True

    def resolve_question(
        self, request_id: str, answers: list[dict[str, Any]] | None
    ) -> bool:
        pending = self.pending.get(request_id)
        if pending is None or pending.kind != "question" or pending.resolved:
            return False
        self.pending.pop(request_id, None)
        pending.resolved = True
        if pending.timer is not None:
            pending.timer.cancel()
        pending.turn.respond_question(request_id, answers)
        return True

    # -- 关闭 -------------------------------------------------------------

    async def aclose(self) -> None:
        for pending in list(self.pending.values()):
            pending.resolved = True
            if pending.timer is not None:
                pending.timer.cancel()
        self.pending.clear()
        tasks: list[asyncio.Task[None]] = []
        for handle in list(self.turns.values()):
            if handle.turn is not None:
                try:
                    handle.turn.cancel()
                except Exception:  # pragma: no cover - 防御
                    pass
            if handle.task is not None:
                handle.task.cancel()
                tasks.append(handle.task)
        self.turns.clear()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.connection = None
        self._replay.clear()


@dataclass
class _TurnHandle:
    device: DeviceSession
    session_id: str
    dsh_session_id: str
    text: str
    assistant_message_id: str
    turn: Any = None
    task: asyncio.Task[None] | None = None
    cancelled: bool = False
    cancel_requested: bool = False
    delta_index: int = 0
    #: v1.1：SDK 图片内容块（已剥 base64 前由 media 层校验/落盘）。
    images: list[dict[str, Any]] | None = None
    #: v1.1：assistant.done.usage（来自运行时）。
    usage: dict[str, Any] | None = None
    #: 最近一次 session.meta 的字段元组（避免重复发送相同 meta）。
    last_meta: tuple[Any, ...] | None = None


@dataclass
class _PendingInteraction:
    id: str
    kind: str  # approval | question
    session_id: str
    turn: DshTurn
    deadline: int
    default_outcome: str = "denied"
    resolved: bool = False
    timer: asyncio.Task[Any] | None = None


# ---------------------------------------------------------------------------
# 音频流状态
# ---------------------------------------------------------------------------


@dataclass
class _AudioState:
    stream: Any
    stream_id: str
    lang: str
    ready: bool = False
    started_at: float = field(default_factory=time.monotonic)
    last_aseq: int = 0
    frames: int = 0
    silent_ms: int = 0
    voice_frames: int = 0
    finalizing: bool = False
    final_sent: bool = False
    final_event: asyncio.Event = field(default_factory=asyncio.Event)
    reader_task: asyncio.Task[None] | None = None
    timer_task: asyncio.Task[None] | None = None
    last_partial_text: str = ""
    last_partial_at: float = 0.0
    dropped_frames: int = 0

    @property
    def duration_ms(self) -> int:
        return self.frames * 20


# ---------------------------------------------------------------------------
# 连接
# ---------------------------------------------------------------------------


class Connection:
    """一条 WebSocket 连接的协议状态机。"""

    def __init__(self, ws: ServerConnection, server: "BridgeServer") -> None:
        self.ws = ws
        self.server = server
        self.config = server.config
        self.device_id = ""
        self.device: DeviceSession | None = None
        self.state = "new"  # new → hello → authed
        self._seq = SeqCounter()
        self.incoming = IncomingSeqTracker()
        self.connected_at = asyncio.get_running_loop().time()
        self.last_rx = self.connected_at
        self._closed = False
        self._tasks: set[asyncio.Task[Any]] = set()
        self._audio: _AudioState | None = None
        self._unauth_task: asyncio.Task[Any] | None = None
        self._heartbeat_task: asyncio.Task[Any] | None = None

    # -- 属性 -------------------------------------------------------------

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def remote(self) -> str:
        try:
            return str(self.ws.remote_address)
        except Exception:  # pragma: no cover - 防御
            return "unknown"

    # -- 发送 -------------------------------------------------------------

    async def _send_raw(self, payload: str) -> None:
        if self._closed:
            return
        try:
            await self.ws.send(payload)
        except ConnectionClosed:
            self._closed = True

    async def _send(self, message: Any) -> None:
        message.seq = self._seq.next()
        await self._send_raw(message.to_json())

    async def _send_error(self, error: ProtocolError, *, reply_to: int | None) -> None:
        if error.code is None:
            return
        frame = ErrorMessage(
            code=error.code,
            message=error.message,
            retryable=is_retryable(error.code) if error.retryable is None else error.retryable,
            detail=error.detail,
        )
        frame.reply_to = reply_to if reply_to is not None else error.reply_to
        await self._send(frame)

    async def _send_protocol_error(self, error: ProtocolError, *, reply_to: int | None = None) -> None:
        """发送错误帧（若定义了 code）并按要求关闭连接。"""
        await self._send_error(error, reply_to=reply_to)
        if error.close_code is not None:
            await self._close(int(error.close_code), error.message)

    async def _close(self, code: int, reason: str = "") -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self.ws.close(int(code), reason)
        except ConnectionClosed:
            pass

    # -- 主循环 -----------------------------------------------------------

    async def run(self) -> None:
        logger.info(
            "connection.open",
            extra={"event": "connection.open", "fields": {"remote": self.remote}},
        )
        self._unauth_task = asyncio.create_task(self._unauth_timer())
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        try:
            async for raw in self.ws:
                self.last_rx = asyncio.get_running_loop().time()
                if isinstance(raw, (bytes, bytearray)):
                    await self._handle_binary(bytes(raw))
                else:
                    await self._handle_text(raw)
        except ConnectionClosed:
            pass
        except ProtocolError as error:  # pragma: no cover - 防御
            await self._send_protocol_error(error)
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - 防御
            logger.exception("connection.crashed", extra={"event": "connection.crashed"})
            await self._close(int(CloseCode.INTERNAL_ERROR), "internal error")
        finally:
            await self._cleanup()

    async def _cleanup(self) -> None:
        for task in (self._unauth_task, self._heartbeat_task):
            _cancel(task)
        for task in list(self._tasks):
            _cancel(task)
        self._tasks.clear()
        if self._audio is not None:
            await self._abort_audio(reason="connection closed")
        if self.device is not None:
            self.device.detach(self)
        await self._close(int(CloseCode.NORMAL), "bye")
        logger.info(
            "connection.closed",
            extra={
                "event": "connection.closed",
                "fields": {"remote": self.remote, "deviceId": self.device_id or None},
            },
        )

    # -- 文本消息 ---------------------------------------------------------

    async def _handle_text(self, raw: str | bytes) -> None:
        try:
            obj = parse_json_text(raw)
            envelope = validate_envelope(obj)
        except ProtocolError as error:
            await self._send_protocol_error(error)
            return
        self.incoming.observe(envelope.seq)

        if envelope.type in S2C_TYPES:
            await self._send_protocol_error(
                protocol_error(
                    f"unexpected server-to-client type {envelope.type!r}",
                    reply_to=envelope.seq,
                )
            )
            return
        if envelope.type not in C2S_TYPES:
            if self.state != "authed":
                await self._send_protocol_error(
                    ProtocolError(
                        f"unknown type {envelope.type!r} before auth",
                        code=ErrorCode.AUTH_FAILED,
                        close_code=CloseCode.AUTH_FAILED,
                        reply_to=envelope.seq,
                    )
                )
            else:
                logger.warning(
                    "message.unknown_type_ignored",
                    extra={"event": "message.unknown_type_ignored", "fields": {"type": envelope.type}},
                )
            return
        try:
            message = decode_message(obj)
        except ProtocolError as error:
            await self._send_protocol_error(error, reply_to=envelope.seq)
            return

        handler = _HANDLERS.get(envelope.type)
        if handler is None:  # pragma: no cover - C2S_TYPES 全覆盖
            return
        try:
            await handler(self, message, envelope)
        except ProtocolError as error:
            if error.reply_to is None:
                error.reply_to = envelope.seq
            await self._send_protocol_error(error, reply_to=envelope.seq)
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - 防御
            logger.exception(
                "message.handler_failed",
                extra={"event": "message.handler_failed", "fields": {"type": envelope.type}},
            )
            await self._send_error(
                ProtocolError(
                    "internal server error",
                    code=ErrorCode.INTERNAL_ERROR,
                    retryable=True,
                ),
                reply_to=envelope.seq,
            )

    def _require_authed(self, envelope: Any) -> None:
        if self.state != "authed" or self.device is None:
            raise ProtocolError(
                "authentication required",
                code=ErrorCode.AUTH_FAILED,
                retryable=False,
                close_code=CloseCode.AUTH_FAILED,
                reply_to=envelope.seq,
            )

    # -- hello / pair / auth ---------------------------------------------

    async def _on_hello(self, message: Hello, envelope: Any) -> None:
        if self.state == "authed":
            raise protocol_error("hello after auth", reply_to=envelope.seq)
        if not isinstance(message.v, int) or message.v != 1:
            raise ProtocolError(
                f"unsupported protocol version {message.v!r}",
                code=ErrorCode.PROTOCOL_VERSION,
                close_code=CloseCode.VERSION_MISMATCH,
                reply_to=envelope.seq,
            )
        device_id = (message.device_id or "").strip()
        if not device_id:
            raise ProtocolError(
                "missing deviceId",
                code=ErrorCode.AUTH_FAILED,
                close_code=CloseCode.AUTH_FAILED,
                reply_to=envelope.seq,
            )
        self.device_id = device_id
        self.state = "hello"
        device = self.server.get_device_session(device_id)
        self.device = device
        # S→C seq 按设备跨连接连续（PROTOCOL §2/§7）
        self._seq = device.seq_counter
        need_pair = not self.server.auth.is_device_paired(device_id)
        reply = HelloOK(
            server_id=self.server.server_id,
            bridge=__version__,
            need_pair=need_pair,
            auth_required=True,
            ts=int(time.time()),
        )
        reply.reply_to = envelope.seq
        await self._send(reply)
        logger.info(
            "hello.ok",
            extra={
                "event": "hello.ok",
                "fields": {"deviceId": device_id, "needPair": need_pair, "v": message.v},
            },
        )

    async def _on_pair_request(self, message: PairRequest, envelope: Any) -> None:
        if self.state != "hello":
            raise protocol_error("hello required before pair.request", reply_to=envelope.seq)
        try:
            result = self.server.auth.pair(
                message.device_id,
                message.code,
                device_name=message.device_name or "",
                source=self.remote,
            )
        except ProtocolError as error:
            reply = PairError(
                code=int(error.code or ErrorCode.PAIR_CODE),
                message=error.message,
                attempts_left=int(error.attempts_left or 0),
                retryable=bool(error.retryable),
            )
            reply.reply_to = envelope.seq
            await self._send(reply)
            if error.close_code is not None:
                await self._close(int(error.close_code), error.message)
            return
        reply = PairOK(
            device_token=result.token_b64,
            server_fingerprint=self.server.fingerprint,
            server_id=self.server.server_id,
            expires_at=None,
        )
        reply.reply_to = envelope.seq
        await self._send(reply)
        logger.info(
            "pair.ok",
            extra={"event": "pair.ok", "fields": {"deviceId": result.device.device_id}},
        )

    async def _on_auth(self, message: Auth, envelope: Any) -> None:
        if self.state != "hello":
            raise protocol_error("hello required before auth", reply_to=envelope.seq)
        try:
            result = self.server.auth.authenticate(
                message.device_id,
                message.ts,
                message.nonce,
                message.hmac,
                source=self.remote,
            )
        except ProtocolError as error:
            reply = AuthError(
                code=int(error.code or ErrorCode.AUTH_FAILED),
                message=error.message,
                need_pair=True,
            )
            reply.reply_to = envelope.seq
            await self._send(reply)
            if error.close_code is not None:
                await self._close(int(error.close_code), error.message)
            return
        self.device_id = result.device_id
        self.device = self.server.get_device_session(result.device_id)
        self.device.attach(self)
        self.state = "authed"
        _cancel(self._unauth_task)
        sessions = [session.to_dict() for session in self.server.history.list_sessions()]
        reply = AuthOK(server_id=self.server.server_id, sessions=sessions)
        reply.reply_to = envelope.seq
        await self._send(reply)
        logger.info(
            "auth.ok",
            extra={"event": "auth.ok", "fields": {"deviceId": result.device_id}},
        )

    # -- 心跳 -------------------------------------------------------------

    async def _on_ping(self, message: Ping, envelope: Any) -> None:
        reply = Pong()
        reply.reply_to = envelope.seq
        await self._send(reply)

    async def _unauth_timer(self) -> None:
        try:
            await asyncio.sleep(UNAUTH_DEADLINE_SEC)
        except asyncio.CancelledError:
            return
        if self.state != "authed" and not self._closed:
            logger.info(
                "connection.unauth_timeout",
                extra={"event": "connection.unauth_timeout", "fields": {"remote": self.remote}},
            )
            await self._close(int(CloseCode.AUTH_FAILED), "authentication timeout")

    async def _heartbeat_loop(self) -> None:
        while not self._closed:
            try:
                await asyncio.sleep(5.0)
            except asyncio.CancelledError:
                return
            if self._closed:
                return
            idle = asyncio.get_running_loop().time() - self.last_rx
            if idle > HEARTBEAT_TIMEOUT_SEC:
                logger.info(
                    "heartbeat.timeout",
                    extra={"event": "heartbeat.timeout", "fields": {"idle": round(idle, 1)}},
                )
                await self._close(int(CloseCode.HEARTBEAT_TIMEOUT), "heartbeat timeout")
                return

    # -- resync -----------------------------------------------------------

    async def _on_resync(self, message: Resync, envelope: Any) -> None:
        self._require_authed(envelope)
        last_seq = message.last_seq
        if isinstance(last_seq, bool) or not isinstance(last_seq, int) or last_seq < 0:
            raise protocol_error("invalid lastSeq", reply_to=envelope.seq)
        assert self.device is not None
        await self.device.replay_from(last_seq, self, envelope.seq)

    # -- 会话 -------------------------------------------------------------

    async def _on_session_list(self, message: SessionList, envelope: Any) -> None:
        self._require_authed(envelope)
        reply = SessionListOK(
            sessions=[session.to_dict() for session in self.server.history.list_sessions()]
        )
        reply.reply_to = envelope.seq
        await self._send(reply)

    async def _on_session_create(self, message: SessionCreate, envelope: Any) -> None:
        self._require_authed(envelope)
        title = (message.title or "").strip() or None
        session = self.server.history.create_session(title)
        try:
            dsh_session = await self.server.dsh.create_session(session.title)
            self.server.history.set_dsh_session_id(session.session_id, dsh_session.session_id)
        except Exception as exc:  # pragma: no cover - SDK 异常不阻断
            logger.warning(
                "session.dsh_create_failed",
                extra={"event": "session.dsh_create_failed", "fields": {"error": str(exc)}},
            )
        reply = SessionCreateOK(session=session.to_dict())
        reply.reply_to = envelope.seq
        await self._send(reply)
        # PROTOCOL §9：session.create.ok 之后立即发一次 session.meta
        await self._send_session_meta(session.session_id)

    async def _send_session_meta(self, session_id: str) -> None:
        """发送 ``session.meta``（v1.1，D-032）：模型/推理强度/上下文窗口。"""
        if self.device is None:
            return
        meta = self._session_meta(session_id)
        # 记录已发送键，后续 turn 内相同 DshMeta 不再重复发（D-032）。
        self.device.last_session_meta = (
            meta.provider,
            meta.model,
            meta.reasoning_effort,
            meta.context_window,
        )
        await self.device.send_event(meta)

    def _session_meta(self, session_id: str) -> SessionMeta:
        config = self.config.dsh
        dsh = self.server.dsh
        base = getattr(dsh, "config", None) or config
        provider = (
            getattr(dsh, "provider", None)
            or getattr(base, "provider", "")
            or config.provider
        )
        model = (
            getattr(dsh, "model", None)
            or getattr(base, "model", "")
            or config.model
        )
        effort = (
            getattr(dsh, "reasoning_effort", None)
            or getattr(base, "reasoning_effort", "")
            or config.reasoning_effort
        )
        window = (
            getattr(dsh, "context_window_tokens", None)
            or getattr(base, "context_window_tokens", None)
            or config.context_window_tokens
        )
        return SessionMeta(
            session_id=session_id,
            provider=provider or None,
            model=model or None,
            reasoning_effort=effort or None,
            context_window=int(window) if window else None,
        )

    async def _on_session_resume(self, message: SessionResume, envelope: Any) -> None:
        self._require_authed(envelope)
        session, messages = self.server.history.resume(message.session_id)
        if session is None:
            raise ProtocolError(
                "session not found",
                code=ErrorCode.SESSION_NOT_FOUND,
                retryable=False,
                reply_to=envelope.seq,
            )
        reply = SessionResumeOK(
            session=session.to_dict(),
            messages=[item.to_dict() for item in messages],
        )
        reply.reply_to = envelope.seq
        await self._send(reply)
        # PROTOCOL §9：session.resume.ok 之后立即发一次 session.meta
        await self._send_session_meta(session.session_id)

    async def _on_session_history(self, message: SessionHistory, envelope: Any) -> None:
        self._require_authed(envelope)
        if not self.server.history.session_exists(message.session_id):
            raise ProtocolError(
                "session not found",
                code=ErrorCode.SESSION_NOT_FOUND,
                retryable=False,
                reply_to=envelope.seq,
            )
        limit = message.limit if isinstance(message.limit, int) else 50
        limit = max(1, min(limit, 200))
        messages, has_more = self.server.history.history_page(
            message.session_id, limit=limit, before=message.before
        )
        reply = SessionHistoryOK(
            messages=[item.to_dict() for item in messages], has_more=has_more
        )
        reply.reply_to = envelope.seq
        await self._send(reply)

    async def _on_media_get(self, message: MediaGet, envelope: Any) -> None:
        """v1.1：按 mediaId 取回原图（仅已认证；未知 id → error 3001）。"""
        self._require_authed(envelope)
        media_id = (message.media_id or "").strip()
        media = load_image(self.config.state_dir / "media", media_id)
        if media is None:
            raise ProtocolError(
                "media not found",
                code=ErrorCode.SESSION_NOT_FOUND,
                retryable=False,
                reply_to=envelope.seq,
            )
        mime_type, data = media
        reply = MediaData(
            media_id=media_id,
            mime_type=mime_type,
            data=base64.b64encode(data).decode("ascii"),
        )
        reply.reply_to = envelope.seq
        await self._send(reply)

    # -- 对话 -------------------------------------------------------------

    async def _on_chat_send(self, message: ChatSend, envelope: Any) -> None:
        self._require_authed(envelope)
        assert self.device is not None
        session_id = message.session_id or ""
        text = message.text or ""
        if not self.server.history.session_exists(session_id):
            raise ProtocolError(
                "session not found",
                code=ErrorCode.SESSION_NOT_FOUND,
                retryable=False,
                reply_to=envelope.seq,
            )
        if len(text) > MAX_TEXT_CHARS:
            raise ProtocolError(
                f"text exceeds {MAX_TEXT_CHARS} characters",
                code=ErrorCode.MODEL_ERROR,
                retryable=False,
                reply_to=envelope.seq,
            )
        client_id = message.client_id or ""
        if client_id:
            existing = self.server.history.find_by_client_id(session_id, client_id)
            if existing is not None:
                reply = ChatAck(client_id=client_id, message_id=existing.message_id)
                reply.reply_to = envelope.seq
                await self._send(reply)
                logger.info(
                    "chat.duplicate",
                    extra={"event": "chat.duplicate", "fields": {"clientId": client_id}},
                )
                return
        if session_id in self.device.turns:
            raise ProtocolError(
                "a turn is already running in this session",
                code=ErrorCode.SERVER_BUSY,
                retryable=True,
                reply_to=envelope.seq,
            )
        # v1.1 图片：magic bytes 校验 + 尺寸上限 → error 4002，且不启动 turn
        payloads: list[ImagePayload] = []
        if message.images:
            try:
                payloads = decode_attachments(message.images)
            except MediaError as exc:
                raise ProtocolError(
                    f"invalid images: {exc}",
                    code=ErrorCode.IMAGE_INVALID,
                    retryable=False,
                    reply_to=envelope.seq,
                ) from exc
        if not text.strip() and not payloads:
            raise ProtocolError(
                "empty text",
                code=ErrorCode.MODEL_ERROR,
                retryable=False,
                reply_to=envelope.seq,
            )
        image_refs: list[dict[str, Any]] | None = None
        image_blocks: list[dict[str, Any]] | None = None
        if payloads:
            try:
                save_images(self.config.state_dir / "media", payloads)
            except OSError as exc:
                raise ProtocolError(
                    f"failed to persist images: {exc}",
                    code=ErrorCode.INTERNAL_ERROR,
                    retryable=True,
                    reply_to=envelope.seq,
                ) from exc
            image_refs = [payload.ref() for payload in payloads]
            image_blocks = [payload.content_block() for payload in payloads]
        user_message = self.server.history.save_message(
            session_id,
            "user",
            text,
            status="done",
            client_id=client_id or None,
            images=image_refs,
        )
        reply = ChatAck(client_id=client_id, message_id=user_message.message_id)
        reply.reply_to = envelope.seq
        await self._send(reply)
        assistant_message = self.server.history.save_message(
            session_id,
            "assistant",
            "",
            status="streaming",
            update_preview=False,
        )
        dsh_session_id = self.server.history.get_dsh_session_id(session_id)
        if not dsh_session_id:
            try:
                dsh_session = await self.server.dsh.create_session()
                dsh_session_id = dsh_session.session_id
                self.server.history.set_dsh_session_id(session_id, dsh_session_id)
            except Exception as exc:  # pragma: no cover - SDK 异常不阻断
                logger.warning(
                    "chat.dsh_session_failed",
                    extra={"event": "chat.dsh_session_failed", "fields": {"error": str(exc)}},
                )
        self.device.start_turn(
            session_id=session_id,
            dsh_session_id=dsh_session_id or session_id,
            text=text,
            assistant_message_id=assistant_message.message_id,
            images=image_blocks,
        )
        logger.info(
            "chat.send",
            extra={
                "event": "chat.send",
                "fields": {
                    "sessionId": session_id,
                    "clientId": client_id,
                    "images": len(image_refs or []),
                },
            },
        )

    async def _on_chat_cancel(self, message: ChatCancel, envelope: Any) -> None:
        self._require_authed(envelope)
        assert self.device is not None
        session_id = message.session_id or ""
        handle = self.device.turns.get(session_id)
        if handle is None:
            logger.info(
                "chat.cancel_noop",
                extra={"event": "chat.cancel_noop", "fields": {"sessionId": session_id}},
            )
            return
        if handle.turn is None:
            # turn 任务尚未开始，标记后由 _run_turn 在启动时立即取消。
            handle.cancel_requested = True
            return
        if not handle.turn.cancel_supported:
            await self._send(
                _with_reply(
                    TurnError(
                        session_id=session_id,
                        code=ErrorCode.MODEL_ERROR,
                        message="cancel unsupported",
                    ),
                    envelope.seq,
                )
            )
            return
        handle.cancel_requested = True
        handle.turn.cancel()
        logger.info(
            "chat.cancel",
            extra={"event": "chat.cancel", "fields": {"sessionId": session_id}},
        )

    # -- 音频 -------------------------------------------------------------

    async def _on_audio_start(self, message: AudioStart, envelope: Any) -> None:
        self._require_authed(envelope)
        if self._audio is not None:
            raise ProtocolError(
                "audio stream already active",
                code=ErrorCode.AUDIO_BUSY,
                retryable=True,
                reply_to=envelope.seq,
            )
        recognizer = self.server.asr
        if not getattr(recognizer, "available", True):
            raise ProtocolError(
                getattr(recognizer, "unavailable_reason", "") or "ASR unavailable",
                code=ErrorCode.ASR_UNAVAILABLE,
                retryable=True,
                reply_to=envelope.seq,
            )
        if int(message.sample_rate) != int(self.config.audio.sample_rate):
            raise ProtocolError(
                f"unsupported sampleRate {message.sample_rate}",
                code=ErrorCode.ASR_UNAVAILABLE,
                retryable=False,
                reply_to=envelope.seq,
            )
        if int(message.channels) != 1 or int(message.bit_depth) != 16:
            raise protocol_error(
                "only mono/16-bit PCM is supported", reply_to=envelope.seq
            )
        if message.mode not in ("ptt", "continuous"):
            raise protocol_error(f"unsupported audio mode {message.mode!r}", reply_to=envelope.seq)
        if message.session_id and not self.server.history.session_exists(message.session_id):
            raise ProtocolError(
                "session not found",
                code=ErrorCode.SESSION_NOT_FOUND,
                retryable=False,
                reply_to=envelope.seq,
            )
        stream_id = f"a-{self.device_id}-{int(time.time() * 1000)}"
        try:
            stream = await recognizer.create_stream(
                stream_id,
                sample_rate=int(message.sample_rate),
                lang=message.lang or self.config.asr.lang,
            )
        except ProtocolError:
            raise
        except Exception as exc:
            raise ProtocolError(
                f"ASR unavailable: {exc}",
                code=ErrorCode.ASR_UNAVAILABLE,
                retryable=True,
                reply_to=envelope.seq,
            ) from exc
        audio = _AudioState(stream=stream, stream_id=stream_id, lang=message.lang or self.config.asr.lang)
        self._audio = audio
        audio.reader_task = asyncio.create_task(
            self._audio_reader(audio), name="asr-reader"
        )
        audio.timer_task = asyncio.create_task(
            self._audio_max_duration(audio), name="asr-max-duration"
        )
        reply = AudioReady(
            asr_mode=getattr(recognizer, "mode", "fake"),
            lang=audio.lang,
            codecs=["pcm16"],
        )
        reply.reply_to = envelope.seq
        await self._send(reply)
        audio.ready = True
        logger.info(
            "audio.ready",
            extra={
                "event": "audio.ready",
                "fields": {"asrMode": reply.asr_mode, "sampleRate": message.sample_rate},
            },
        )

    async def _on_audio_stop(self, message: AudioStop, envelope: Any) -> None:
        self._require_authed(envelope)
        audio = self._audio
        if audio is None:
            logger.info("audio.stop.noop", extra={"event": "audio.stop.noop"})
            return
        aseq = message.aseq if isinstance(message.aseq, int) else 0
        await self._finalize_audio(aseq=aseq or audio.last_aseq, reason="client stop")

    async def _audio_reader(self, audio: _AudioState) -> None:
        try:
            while True:
                event = await audio.stream.events.get()
                if getattr(event, "type", "") == "final":
                    await self._send_asr_final(audio, event)
                    return
                await self._send_asr_partial(audio, event)
        except asyncio.CancelledError:
            return
        except Exception:  # pragma: no cover - 防御
            logger.exception("asr.reader_failed", extra={"event": "asr.reader_failed"})

    async def _audio_max_duration(self, audio: _AudioState) -> None:
        try:
            await asyncio.sleep(float(self.config.audio.max_stream_seconds))
        except asyncio.CancelledError:
            return
        if self._audio is audio and not audio.finalizing:
            logger.warning("audio.max_duration", extra={"event": "audio.max_duration"})
            await self._finalize_audio(aseq=audio.last_aseq, reason="max duration")

    async def _send_asr_partial(self, audio: _AudioState, event: Any) -> None:
        text = (getattr(event, "text", "") or "").strip()
        if not text:
            return
        if text == audio.last_partial_text:
            return
        rate = max(1, int(self.config.asr.partial_rate_per_sec))
        now = asyncio.get_running_loop().time()
        if now - audio.last_partial_at < 1.0 / rate:
            return
        audio.last_partial_at = now
        audio.last_partial_text = text
        assert self.device is not None
        await self.device.send_event(
            AsrPartial(
                text=text,
                stability=float(getattr(event, "stability", 0.0) or 0.0),
                aseq=int(getattr(event, "aseq", 0) or audio.last_aseq),
            )
        )

    async def _send_asr_final(self, audio: _AudioState, event: Any, *, text: str | None = None) -> None:
        if audio.final_sent:
            return
        audio.final_sent = True
        final_text = text if text is not None else (getattr(event, "text", "") or "")
        if audio.voice_frames == 0:
            # VAD 语义：整条流没有任何非静音帧时定稿空串（ASR 后端可能返回默认句）。
            final_text = ""
        duration = int(getattr(event, "duration_ms", 0) or 0) or audio.duration_ms
        if self.device is not None:
            await self.device.send_event(
                AsrFinal(
                    text=final_text,
                    aseq=int(getattr(event, "aseq", 0) or audio.last_aseq),
                    duration_ms=duration,
                    lang=audio.lang,
                )
            )
        audio.final_event.set()

    async def _finalize_audio(self, *, aseq: int, reason: str = "") -> None:
        audio = self._audio
        if audio is None or audio.finalizing:
            return
        audio.finalizing = True
        _cancel(audio.timer_task)
        try:
            audio.stream.stop(aseq=int(aseq))
        except Exception as exc:  # pragma: no cover - 防御
            logger.warning(
                "asr.stop_failed",
                extra={"event": "asr.stop_failed", "fields": {"error": str(exc)}},
            )
        try:
            await asyncio.wait_for(audio.final_event.wait(), timeout=1.5)
        except asyncio.TimeoutError:
            if not audio.final_sent:
                await self._send_asr_final(audio, None, text="")
        finally:
            _cancel(audio.reader_task)
            if self._audio is audio:
                self._audio = None
            logger.info(
                "audio.stopped",
                extra={
                    "event": "audio.stopped",
                    "fields": {
                        "reason": reason,
                        "frames": audio.frames,
                        "durationMs": audio.duration_ms,
                    },
                },
            )

    async def _abort_audio(self, *, reason: str = "") -> None:
        audio = self._audio
        if audio is None:
            return
        self._audio = None
        _cancel(audio.reader_task)
        _cancel(audio.timer_task)
        try:
            audio.stream.abort()
        except Exception:  # pragma: no cover - 防御
            pass
        logger.info(
            "audio.aborted",
            extra={"event": "audio.aborted", "fields": {"reason": reason}},
        )

    async def _handle_binary(self, data: bytes) -> None:
        audio = self._audio
        if audio is None or not audio.ready or audio.finalizing:
            if audio is not None:
                audio.dropped_frames += 1
            return
        try:
            frame = parse_audio_frame(data)
        except AudioFrameError as exc:
            raise protocol_error(f"invalid audio frame: {exc}") from exc
        audio.last_aseq = frame.aseq
        audio.frames += 1
        if is_silent(frame.payload):
            audio.silent_ms += 20
        else:
            audio.silent_ms = 0
            audio.voice_frames += 1
        try:
            audio.stream.accept_pcm(frame.payload, ts_ms=frame.ts_ms, aseq=frame.aseq)
        except Exception as exc:  # pragma: no cover - 防御
            logger.warning(
                "asr.accept_failed",
                extra={"event": "asr.accept_failed", "fields": {"error": str(exc)}},
            )
        if audio.silent_ms >= int(self.config.asr.silence_ms) and not audio.finalizing:
            logger.info("audio.vad_final", extra={"event": "audio.vad_final"})
            asyncio.create_task(self._finalize_audio(aseq=audio.last_aseq, reason="vad silence"))

    # -- 审批 / 提问 ------------------------------------------------------

    async def _on_approval_response(self, message: ApprovalResponse, envelope: Any) -> None:
        self._require_authed(envelope)
        assert self.device is not None
        outcome = message.outcome if message.outcome in ("approved", "denied") else "denied"
        if not self.device.resolve_approval(message.id, outcome):
            logger.info(
                "approval.response_ignored",
                extra={"event": "approval.response_ignored", "fields": {"id": message.id}},
            )

    async def _on_question_response(self, message: QuestionResponse, envelope: Any) -> None:
        self._require_authed(envelope)
        assert self.device is not None
        answers = None if message.cancelled else message.answers
        if not self.device.resolve_question(message.id, answers):
            logger.info(
                "question.response_ignored",
                extra={"event": "question.response_ignored", "fields": {"id": message.id}},
            )


def _cancel(task: asyncio.Task[Any] | None) -> None:
    if task is not None and not task.done():
        task.cancel()


def _with_reply(message: Any, reply_to: int) -> Any:
    message.reply_to = reply_to
    return message


_HANDLERS = {
    "hello": Connection._on_hello,
    "pair.request": Connection._on_pair_request,
    "auth": Connection._on_auth,
    "ping": Connection._on_ping,
    "resync": Connection._on_resync,
    "session.list": Connection._on_session_list,
    "session.create": Connection._on_session_create,
    "session.resume": Connection._on_session_resume,
    "session.history": Connection._on_session_history,
    "media.get": Connection._on_media_get,
    "chat.send": Connection._on_chat_send,
    "chat.cancel": Connection._on_chat_cancel,
    "audio.start": Connection._on_audio_start,
    "audio.stop": Connection._on_audio_stop,
    "approval.response": Connection._on_approval_response,
    "question.response": Connection._on_question_response,
}
