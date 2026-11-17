"""DSH SDK 适配层：统一接口 + 假实现（M1）。

M1 阶段真实 SDK 尚未接线（``docs/spike/SDK-SPIKE.md`` 由 spike 产出后再接，
D-009），``[dsh].enabled=false`` 时使用 :class:`FakeDsh`。真实 SDK 的同步调用
必须放进 ``ThreadPoolExecutor(max_workers=2)``，严禁阻塞事件循环
（ARCHITECTURE §4）——该约束在接入真实 SDK 的适配器里实现。

接口要点：

- :class:`DshClient` 的生命周期：``await start()`` → 业务 → ``await aclose()``。
- ``start_turn()`` 返回 :class:`DshTurn`，异步迭代 :class:`DshEvent` 子类。
- 审批/提问事件由 turn 发出；上层把 ``approval.request``/``question.request``
  发给客户端后用 ``turn.respond_approval()`` / ``turn.respond_question()`` 回填。
  超时未回填时传 ``None``（视为 denied/expired）。
"""

from __future__ import annotations

import abc
import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, ClassVar, Union

from .config import DshConfig

logger = logging.getLogger("bridge.dsh")

__all__ = [
    "DshSession",
    "DshDelta",
    "DshTool",
    "DshDone",
    "DshError",
    "DshApproval",
    "DshQuestion",
    "DshEvent",
    "DshTurn",
    "DshClient",
    "FakeDsh",
    "FakeTurn",
    "create_dsh_client",
]


# ---------------------------------------------------------------------------
# 事件
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DshDelta:
    """助手文本增量（SDK 只有整段时上层只用一条）。"""

    text: str


@dataclass(frozen=True)
class DshTool:
    """工具调用状态。"""

    tool_call_id: str
    name: str
    state: str = "running"  # running | done | error
    title: str = ""
    args_preview: str = ""


@dataclass(frozen=True)
class DshDone:
    """turn 正常结束（或取消）。"""

    finish_reason: str = "completed"  # completed | cancelled


@dataclass(frozen=True)
class DshError:
    """turn 失败（映射为 turn.error）。"""

    message: str
    code: int = 3003


@dataclass(frozen=True)
class DshApproval:
    """审批请求（映射为 approval.request）。"""

    id: str
    tool: str
    reason: str = ""
    args_preview: str = ""
    deadline: int = 0
    default_outcome: str = "denied"


@dataclass(frozen=True)
class DshQuestion:
    """提问请求（映射为 question.request）。"""

    id: str
    questions: list[dict[str, Any]] = field(default_factory=list)
    deadline: int = 0


DshEvent = Union[DshDelta, DshTool, DshDone, DshError, DshApproval, DshQuestion]


# ---------------------------------------------------------------------------
# 接口
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DshSession:
    """SDK 侧会话。"""

    session_id: str
    title: str = ""


class DshTurn(abc.ABC):
    """一次对话 turn：异步迭代事件，可取消、可回填审批/提问。"""

    def __init__(self) -> None:
        self.cancelled = False
        self.finish_reason: str | None = None
        self.cancel_supported: bool = True

    @abc.abstractmethod
    def __aiter__(self) -> "DshTurn": ...

    @abc.abstractmethod
    async def __anext__(self) -> DshEvent: ...

    @abc.abstractmethod
    def cancel(self) -> None:
        """尽力取消；不支持时置 ``cancel_supported=False``（协议要求 3003）。"""

    @abc.abstractmethod
    def respond_approval(self, request_id: str, outcome: str | None) -> None:
        """回填审批结果（``None`` = 超时/放弃，按 denied）。"""

    @abc.abstractmethod
    def respond_question(self, request_id: str, answers: list[dict[str, Any]] | None) -> None:
        """回填提问结果（``None`` = 超时/取消）。"""


class DshClient(abc.ABC):
    """DSH 客户端接口。"""

    kind: ClassVar[str] = "fake"

    @abc.abstractmethod
    async def start(self) -> None: ...

    @abc.abstractmethod
    async def aclose(self) -> None: ...

    @abc.abstractmethod
    async def create_session(self, title: str | None = None) -> DshSession: ...

    @abc.abstractmethod
    async def list_sessions(self) -> list[DshSession]: ...

    @abc.abstractmethod
    def start_turn(self, session_id: str, text: str) -> DshTurn: ...


# ---------------------------------------------------------------------------
# Fake 实现
# ---------------------------------------------------------------------------

_END = object()

#: fake 模式下触发审批/提问/错误的文本标记
APPROVAL_MARKERS = ("审批", "approval")
QUESTION_MARKERS = ("提问", "question")
SLOW_MARKERS = ("慢", "slow")
ERROR_MARKERS = ("错误", "error")


class FakeTurn(DshTurn):
    """确定性的假 turn：分片 delta + tool.status + done。"""

    def __init__(
        self,
        client: "FakeDsh",
        session_id: str,
        text: str,
        *,
        chunk_delay: float = 0.01,
    ) -> None:
        super().__init__()
        self.client = client
        self.session_id = session_id
        self.text = text
        self.chunk_delay = chunk_delay
        self._queue: asyncio.Queue[Any] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None
        self._cancel_event = asyncio.Event()
        self._approval_futures: dict[str, asyncio.Future[str | None]] = {}
        self._question_futures: dict[str, asyncio.Future[list[dict[str, Any]] | None]] = {}

    # -- 生命周期 ---------------------------------------------------------

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="fake-dsh-turn")

    def _emit(self, event: DshEvent) -> None:
        self._queue.put_nowait(event)

    def cancel(self) -> None:
        self.cancelled = True
        self._cancel_event.set()
        for fut in (*self._approval_futures.values(), *self._question_futures.values()):
            if not fut.done():
                fut.set_result(None)

    def respond_approval(self, request_id: str, outcome: str | None) -> None:
        fut = self._approval_futures.get(request_id)
        if fut is not None and not fut.done():
            fut.set_result(outcome)

    def respond_question(self, request_id: str, answers: list[dict[str, Any]] | None) -> None:
        fut = self._question_futures.get(request_id)
        if fut is not None and not fut.done():
            fut.set_result(answers)

    async def __anext__(self) -> DshEvent:
        event = await self._queue.get()
        if event is _END:
            raise StopAsyncIteration
        if isinstance(event, DshDone):
            self.finish_reason = event.finish_reason
        return event

    def __aiter__(self) -> "FakeTurn":
        return self

    # -- 内部 -------------------------------------------------------------

    async def _pause(self, seconds: float) -> bool:
        """等待一小段；返回 False 表示已被取消。"""
        try:
            await asyncio.wait_for(self._cancel_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            return not self.cancelled
        return False

    async def _stream_text(self, text: str, chunk_size: int = 4) -> bool:
        """按块输出文本；返回 False 表示被取消。"""
        for i in range(0, len(text), chunk_size):
            if self.cancelled:
                return False
            self._emit(DshDelta(text=text[i : i + chunk_size]))
            if not await self._pause(self.chunk_delay):
                return False
        return True

    async def _run(self) -> None:
        try:
            await self._run_inner()
        except asyncio.CancelledError:  # pragma: no cover - 关闭路径
            raise
        except Exception as exc:  # pragma: no cover - 防御
            logger.exception("fake turn crashed: %s", exc)
            self._emit(DshError(message=f"内部错误: {exc}"))
            self._emit(DshDone(finish_reason="error"))
        finally:
            self._emit(_END) if False else self._queue.put_nowait(_END)

    async def _run_inner(self) -> None:
        text = self.text
        lower = text.lower()
        if any(mark in lower for mark in ERROR_MARKERS):
            await self._pause(self.chunk_delay)
            self._emit(DshError(message="模拟模型错误（fake）"))
            return
        slow = any(mark in lower for mark in SLOW_MARKERS)
        if any(mark in lower for mark in APPROVAL_MARKERS):
            await self._approval_flow()
            return
        if any(mark in lower for mark in QUESTION_MARKERS):
            await self._question_flow()
            return
        tool_id = f"tool-{uuid.uuid4().hex[:8]}"
        self._emit(
            DshTool(tool_call_id=tool_id, name="search", state="running", title="搜索资料")
        )
        if not await self._pause(self.chunk_delay):
            self._emit(DshDone(finish_reason="cancelled"))
            return
        reply = f"收到：{text}"
        if slow:
            reply = reply + "。" + "这是一段用于测试取消的较长回复。" * 5
            chunk_delay, old_delay = 0.05, self.chunk_delay
            self.chunk_delay = chunk_delay
        ok = await self._stream_text(reply)
        self.chunk_delay = old_delay if slow else self.chunk_delay  # type: ignore[has-type]
        self._emit(DshTool(tool_call_id=tool_id, name="search", state="done", title="搜索资料"))
        if not ok:
            self._emit(DshDone(finish_reason="cancelled"))
        else:
            self._emit(DshDone(finish_reason="completed"))

    async def _approval_flow(self) -> None:
        request_id = f"appr-{uuid.uuid4().hex[:8]}"
        now = int(self.client.now())
        deadline = now + int(self.client.approval_deadline_sec)
        self._emit(
            DshTool(tool_call_id=request_id, name="delete_file", state="running",
                    title="删除临时文件")
        )
        self._emit(
            DshApproval(
                id=request_id,
                tool="delete_file",
                reason="即将删除 /tmp/dsh-cache（fake 演示）",
                args_preview='{"path": "/tmp/dsh-cache"}',
                deadline=deadline,
            )
        )
        outcome = await self._wait_approval(request_id, deadline)
        if self.cancelled:
            self._emit(DshDone(finish_reason="cancelled"))
            return
        if outcome == "approved":
            await self._stream_text("已批准，操作完成。")
        else:
            await self._stream_text("操作未获批准，已跳过。")
        self._emit(DshTool(tool_call_id=request_id, name="delete_file", state="done",
                            title="删除临时文件"))
        self._emit(DshDone(finish_reason="completed"))

    async def _wait_approval(self, request_id: str, deadline: int) -> str | None:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[str | None] = loop.create_future()
        self._approval_futures[request_id] = fut
        try:
            remaining = max(0.0, deadline - self.client.now())
            return await asyncio.wait_for(fut, timeout=max(0.05, remaining) + 0.5)
        except asyncio.TimeoutError:
            return None
        finally:
            self._approval_futures.pop(request_id, None)

    async def _question_flow(self) -> None:
        request_id = f"q-{uuid.uuid4().hex[:8]}"
        now = int(self.client.now())
        deadline = now + int(self.client.question_deadline_sec)
        self._emit(
            DshTool(tool_call_id=request_id, name="ask_user", state="running",
                    title="等待用户选择")
        )
        self._emit(
            DshQuestion(
                id=request_id,
                questions=[
                    {
                        "id": "q1",
                        "question": "要使用哪个颜色主题？",
                        "multiSelect": False,
                        "required": True,
                        "allowCustom": True,
                        "options": [
                            {"id": "light", "label": "浅色"},
                            {"id": "dark", "label": "深色"},
                        ],
                    }
                ],
                deadline=deadline,
            )
        )
        answers = await self._wait_question(request_id, deadline)
        if self.cancelled:
            self._emit(DshDone(finish_reason="cancelled"))
            return
        if answers:
            await self._stream_text("已收到你的选择。")
        else:
            await self._stream_text("未收到选择，已使用默认值。")
        self._emit(DshTool(tool_call_id=request_id, name="ask_user", state="done",
                            title="等待用户选择"))
        self._emit(DshDone(finish_reason="completed"))

    async def _wait_question(
        self, request_id: str, deadline: int
    ) -> list[dict[str, Any]] | None:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[list[dict[str, Any]] | None] = loop.create_future()
        self._question_futures[request_id] = fut
        try:
            remaining = max(0.0, deadline - self.client.now())
            return await asyncio.wait_for(fut, timeout=max(0.05, remaining) + 0.5)
        except asyncio.TimeoutError:
            return None
        finally:
            self._question_futures.pop(request_id, None)


class FakeDsh(DshClient):
    """假 DSH 客户端：确定性、无网络、无模型依赖。

    触发标记（用于 M1 演练审批/提问/取消/错误）：

    - 文本含 ``审批``/``approval`` → approval.request 流程；
    - 文本含 ``提问``/``question`` → question.request 流程；
    - 文本含 ``慢``/``slow`` → 慢速流，便于测试 chat.cancel；
    - 文本含 ``错误``/``error`` → turn.error。
    """

    kind: ClassVar[str] = "fake"

    def __init__(
        self,
        *,
        approval_deadline_sec: int = 600,
        question_deadline_sec: int = 600,
        chunk_delay: float = 0.01,
        clock: Any = None,
    ) -> None:
        self.approval_deadline_sec = approval_deadline_sec
        self.question_deadline_sec = question_deadline_sec
        self.chunk_delay = chunk_delay
        import time as _time

        self._clock = clock or _time.time
        self._sessions: dict[str, DshSession] = {}
        self._turns: set[FakeTurn] = set()
        self._closed = False

    def now(self) -> float:
        return float(self._clock())

    async def start(self) -> None:
        self._closed = False

    async def aclose(self) -> None:
        self._closed = True
        for turn in list(self._turns):
            turn.cancel()
        self._turns.clear()
        self._sessions.clear()

    async def create_session(self, title: str | None = None) -> DshSession:
        session = DshSession(
            session_id=f"dsh-{uuid.uuid4().hex[:12]}",
            title=(title or "").strip(),
        )
        self._sessions[session.session_id] = session
        return session

    async def list_sessions(self) -> list[DshSession]:
        return list(self._sessions.values())

    async def get_session(self, session_id: str) -> DshSession | None:
        return self._sessions.get(session_id)

    def start_turn(self, session_id: str, text: str) -> FakeTurn:
        turn = FakeTurn(self, session_id, text, chunk_delay=self.chunk_delay)
        self._turns.add(turn)
        turn.start()
        return turn


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------


def create_dsh_client(
    config: DshConfig | None = None,
    *,
    home: Any = None,
) -> DshClient:
    """按配置创建 DSH 客户端。

    M1：``[dsh].enabled=true`` 但真实 SDK 适配尚未接线时，记录 WARNING 并回退
    ``FakeDsh``，保证 Bridge 仍可启动（验收可跑）。
    """
    config = config or DshConfig()
    if not config.enabled:
        logger.info("dsh.fake_mode", extra={"event": "dsh.fake_mode"})
        return FakeDsh()

    # 真实 SDK 适配器（spike 结论落地后新增 bridge/dsh_sdk.py；当前不存在）
    try:
        from . import dsh_sdk  # type: ignore[attr-defined]
    except Exception as exc:
        logger.warning(
            "dsh.sdk_unavailable: %s；M1 回退 fake（待 docs/spike/SDK-SPIKE.md 接线）",
            exc,
            extra={"event": "dsh.sdk_unavailable"},
        )
        return FakeDsh()
    try:
        factory = getattr(dsh_sdk, "create_client", None)
        if callable(factory):
            try:
                return factory(config, home=home)
            except TypeError:
                return factory(config)
        client_cls = getattr(dsh_sdk, "SdkDshClient", None)
        if client_cls is not None:
            return client_cls(config, home=home)
    except Exception as exc:  # pragma: no cover - 适配器实现细节
        logger.warning("dsh.sdk_init_failed: %s；回退 fake", exc)
        return FakeDsh()
    logger.warning("dsh.sdk_contract_missing: 回退 fake")
    return FakeDsh()
