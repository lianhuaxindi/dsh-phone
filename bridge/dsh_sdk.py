"""DeepSeek Harness Python SDK（``0.2.2rc2``）适配器。

依据 ``docs/spike/SDK-SPIKE.md`` 与 ``docs/ARCHITECTURE.md`` §2.2.1 实现：

- SDK 是**同步阻塞** API（``DeepSeekHarness.run``），所有调用都跑在专用
  :class:`~concurrent.futures.ThreadPoolExecutor` 里，绝不在事件循环线程直接调用。
- 通知回调（``on_notification``）运行在 SDK 线程：只做
  ``loop.call_soon_threadsafe(...)``，不触碰 asyncio 状态。
- 文本只在 ``assistant/message`` 事件整段到达（每 step 一条），本适配器把整个 turn
  的文本拼成一段，**只发一条** ``DshDelta``（D-018），随后 ``DshDone``。
- 审批需要并发 drain ``next_request()``（否则 turn 挂死）：专用 daemon 线程。
- 提问来自 ``on_user_question`` handler（daemon 线程）：等待用户答案或超时取消。
- SDK 无 cancel：``cancel_supported=False``，上层按 D-019 回 ``turn.error(3003)``。
- 运行时发现：``[dsh].runtimeExe`` 为空时用 wheel 内置 exe；``[dsh].home`` 必填
  （D-021，SDK 拒绝隐式 ``~/.dsh``）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from .config import DshConfig
from .dsh import (
    DshApproval,
    DshClient,
    DshDelta,
    DshDone,
    DshError,
    DshEvent,
    DshQuestion,
    DshSession,
    DshTool,
    DshTurn,
)

logger = logging.getLogger("bridge.dsh_sdk")

#: 审批/提问等待上限（PROTOCOL §11：默认 10 分钟）。
INTERACTION_TIMEOUT_SEC = 600.0
#: 每次默认加载 SDK 的路径（开发期：spike 用 --target 解压在这里）。
_VENDOR_HINT = Path(__file__).resolve().parents[1] / "scripts" / "spike" / ".vendor"

_END = object()


def _import_sdk() -> Any:
    """导入 ``deepseek_harness`` SDK。

    正式部署用 ``bridge/requirements-sdk.txt`` 安装；本机因沙箱限制 SDK 装在
    ``scripts/spike/.vendor``，开发期通过 ``DSH_PHONE_SDK_PATH`` 或 vendor 目录回退导入。
    """
    try:
        import deepseek_harness as sdk  # noqa: F401
        return sdk
    except ImportError:
        pass
    candidates: list[Path] = []
    env_path = os.environ.get("DSH_PHONE_SDK_PATH", "").strip()
    if env_path:
        candidates.append(Path(env_path))
    candidates.append(_VENDOR_HINT)
    for candidate in candidates:
        if candidate.is_dir() and str(candidate) not in sys.path:
            sys.path.insert(0, str(candidate))
            try:
                import deepseek_harness as sdk  # noqa: F811
                return sdk
            except ImportError:
                continue
    raise RuntimeError(
        "未安装 deepseek-harness-sdk；请执行 "
        "`python -m pip install -r bridge/requirements-sdk.txt --pre`，"
        "或设置 DSH_PHONE_SDK_PATH 指向已解压的 SDK 目录"
    )


class _ApprovalSlot:
    """一个审批请求的线程间等待槽。"""

    __slots__ = ("id", "event", "outcome")

    def __init__(self, request_id: str) -> None:
        self.id = request_id
        self.event = threading.Event()
        self.outcome: str | None = None

    def wait(self, timeout: float) -> str | None:
        self.event.wait(timeout)
        return self.outcome


class _QuestionSlot:
    """一个提问请求的线程间等待槽。"""

    __slots__ = ("id", "event", "answers")

    def __init__(self, request_id: str) -> None:
        self.id = request_id
        self.event = threading.Event()
        self.answers: list[dict[str, Any]] | None = None

    def wait(self, timeout: float, cancelled: threading.Event | None = None) -> list[dict[str, Any]] | None:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            if self.event.wait(min(0.1, remaining)):
                return self.answers
            if cancelled is not None and cancelled.is_set():
                return None


class SdkTurn(DshTurn):
    """一次真实 SDK turn：消费通知 → 映射为 Bridge 事件。"""

    def __init__(self, client: "SdkDshClient", session_id: str, text: str) -> None:
        super().__init__()
        self.client = client
        self.session_id = session_id
        self.text = text
        self.cancel_supported = False  # D-019：SDK 无 cancel
        self.finish_reason = "completed"
        self._loop = asyncio.get_running_loop()
        self._queue: asyncio.Queue[Any] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None
        self._text_parts: list[str] = []
        self._finish_kind: str | None = None
        self._approvals: dict[str, _ApprovalSlot] = {}
        self._questions: dict[str, _QuestionSlot] = {}
        self._lock = threading.Lock()

    # -- 生命周期 ---------------------------------------------------------

    def start(self) -> None:
        self.client.register_turn(self)
        self._task = asyncio.create_task(self._run(), name=f"sdk-turn-{self.session_id}")

    def cancel(self) -> None:
        # SDK 无取消能力：仅唤醒等待中的审批/提问，避免线程永久阻塞
        self.cancelled = True
        with self._lock:
            for slot in self._approvals.values():
                slot.outcome = "rejected"
                slot.event.set()
            for slot in self._questions.values():
                slot.answers = None
                slot.event.set()

    # -- 事件队列 ---------------------------------------------------------

    def _emit(self, event: Any) -> None:
        self._loop.call_soon_threadsafe(self._queue.put_nowait, event)

    def _inject(self, event: DshEvent) -> None:
        self._emit(event)

    def __aiter__(self) -> "SdkTurn":
        return self

    async def __anext__(self) -> DshEvent:
        item = await self._queue.get()
        if item is _END:
            raise StopAsyncIteration
        return item

    # -- 通知处理（SDK 线程） ---------------------------------------------

    def on_notification(self, notification: Any) -> None:
        try:
            self._handle_notification(notification)
        except Exception:  # pragma: no cover - SDK 线程防御
            logger.exception("sdk.notification_failed", extra={"event": "sdk.notification_failed"})

    def _handle_notification(self, notification: Any) -> None:
        method = getattr(notification, "method", "")
        payload = getattr(notification, "payload", None) or {}
        if method != "session.event":
            return
        event = payload.get("event") if isinstance(payload, dict) else None
        if not isinstance(event, dict):
            return
        etype = event.get("type")
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        if etype == "tool/call":
            call_id = str(data.get("callId") or data.get("id") or "")
            name = str(data.get("name") or "tool")
            self._inject(
                DshTool(
                    tool_call_id=call_id,
                    name=name,
                    state="running",
                    title=f"调用 {name}",
                    args_preview=_json_short(data.get("arguments")),
                )
            )
        elif etype == "tool/result":
            call_id = str(data.get("callId") or "")
            name = str(data.get("name") or "tool")
            error = data.get("error")
            self._inject(
                DshTool(
                    tool_call_id=call_id,
                    name=name,
                    state="error" if error else "done",
                    title=f"{name} 完成" if not error else f"{name} 失败",
                    args_preview=_json_short(error),
                )
            )
        elif etype == "assistant/message":
            text = _assistant_text(data)
            if text:
                self._text_parts.append(text)
        elif etype == "turn/end":
            reason = data.get("reason") if isinstance(data.get("reason"), dict) else {}
            kind = reason.get("kind")
            self._finish_kind = kind if isinstance(kind, str) else None

    def _sync_run(self, harness: Any) -> Any:
        return harness.run(
            self.text,
            session_id=self.session_id,
            on_notification=self.on_notification,
        )

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        result: Any = None
        try:
            result = await loop.run_in_executor(
                self.client.executor, self._sync_run, self.client.harness
            )
        except asyncio.CancelledError:
            self._emit(_END)
            raise
        except Exception as exc:  # pragma: no cover - 运行时错误
            logger.exception("sdk.turn_failed", extra={"event": "sdk.turn_failed"})
            self.finish_reason = "error"
            self._emit(DshError(message=f"DSH 执行失败: {exc}", code=3003))
            self._emit(_END)
            return
        finally:
            self.client.unregister_turn(self)

        text = "".join(self._text_parts)
        if not text:
            text = str(getattr(result, "final_response", "") or "")
        kind = self._finish_kind or str(getattr(result, "finish_reason", "") or "")
        if kind == "error":
            self.finish_reason = "error"
            if text:
                self._emit(DshDelta(text))
            self._emit(DshError(message="DSH turn 以错误结束", code=3003))
        else:
            self.finish_reason = "cancelled" if kind == "cancelled" else "completed"
            if text:
                self._emit(DshDelta(text))
            self._emit(DshDone(finish_reason=self.finish_reason))
        self._emit(_END)

    # -- 审批 / 提问回填 ---------------------------------------------------

    def _new_approval(self, request_id: str) -> _ApprovalSlot:
        slot = _ApprovalSlot(request_id)
        with self._lock:
            self._approvals[request_id] = slot
        return slot

    def _new_question(self, request_id: str) -> _QuestionSlot:
        slot = _QuestionSlot(request_id)
        with self._lock:
            self._questions[request_id] = slot
        return slot

    def respond_approval(self, request_id: str, outcome: str | None) -> None:
        with self._lock:
            slot = self._approvals.get(str(request_id))
        if slot is None:
            return
        mapping = {
            "approved": "allowed-once",
            "allowed-once": "allowed-once",
            "denied": "rejected",
            "rejected": "rejected",
            "cancelled": "cancelled",
        }
        slot.outcome = mapping.get(outcome or "", "rejected")
        slot.event.set()

    def respond_question(self, request_id: str, answers: list[dict[str, Any]] | None) -> None:
        with self._lock:
            slot = self._questions.get(str(request_id))
        if slot is None:
            return
        slot.answers = answers
        slot.event.set()


class _InteractionRouter:
    """审批请求 drain 线程（SDK 要求与阻塞的 run 并发）。"""

    def __init__(self, client: "SdkDshClient") -> None:
        self.client = client
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._drain, name="dsh-approval-drain", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _drain(self) -> None:
        sdk_client = self.client.sdk_client
        while not self._stop.is_set():
            try:
                request = sdk_client.next_request()
            except Exception:
                return
            try:
                self._handle(request)
            except Exception:  # pragma: no cover - 防御
                logger.exception("sdk.approval_failed", extra={"event": "sdk.approval_failed"})
                try:
                    sdk_client.respond(request.id, {"outcome": "rejected"})
                except Exception:
                    pass

    def _handle(self, request: Any) -> None:
        method = getattr(request, "method", "")
        payload = getattr(request, "payload", None) or {}
        sdk_client = self.client.sdk_client
        if method != "approval/request":
            # 未知请求（SDK 只声明 approval/request 会进 next_request），保守拒绝
            sdk_client.respond(request.id, {"outcome": "rejected"})
            return
        session_id = str(payload.get("sessionId") or "")
        turn = self.client.turn_for_session(session_id)
        if turn is None:
            logger.warning(
                "sdk.approval_without_turn",
                extra={"event": "sdk.approval_without_turn", "fields": {"sessionId": session_id}},
            )
            sdk_client.respond(request.id, {"outcome": "rejected"})
            return
        request_id = str(request.id)
        slot = turn._new_approval(request_id)
        known = {"sessionId", "toolName", "callId", "reason"}
        extra = {k: v for k, v in payload.items() if k not in known}
        approval = DshApproval(
            id=request_id,
            tool=str(payload.get("toolName") or "tool"),
            reason=str(payload.get("reason") or ""),
            args_preview=_json_short(extra) if extra else "",
            deadline=int(time.time() + INTERACTION_TIMEOUT_SEC),
            default_outcome="denied",
        )
        turn._inject(approval)
        outcome = slot.wait(INTERACTION_TIMEOUT_SEC)
        with turn._lock:
            turn._approvals.pop(request_id, None)
        sdk_client.respond(request.id, {"outcome": outcome or "rejected"})


def _json_short(value: Any, limit: int = 200) -> str:
    if value is None:
        return ""
    try:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        text = str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _assistant_text(data: dict[str, Any]) -> str:
    """从 ``assistant/message`` 事件 data 中提取文本块。"""
    message = data.get("message")
    owner = message if isinstance(message, dict) else data
    content = owner.get("content")
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
    return "".join(parts)


class SdkDshClient(DshClient):
    """真实 DSH SDK 客户端（``[dsh].enabled=true``）。"""

    kind = "real"

    def __init__(self, config: DshConfig | None = None, *, home: Any = None) -> None:
        self.config = config or DshConfig()
        self.home = Path(home or self.config.home).expanduser().resolve()
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="dsh-sdk")
        self._sdk: Any = None
        self._harness: Any = None
        self._sdk_client: Any = None
        self._router: _InteractionRouter | None = None
        self._turns: dict[str, SdkTurn] = {}
        self._turns_lock = threading.Lock()
        self._closed = False

    # -- 属性 -------------------------------------------------------------

    @property
    def harness(self) -> Any:
        return self._harness

    @property
    def sdk_client(self) -> Any:
        return self._sdk_client

    def turn_for_session(self, session_id: str) -> SdkTurn | None:
        with self._turns_lock:
            return self._turns.get(session_id)

    def register_turn(self, turn: SdkTurn) -> None:
        with self._turns_lock:
            self._turns[turn.session_id] = turn

    def unregister_turn(self, turn: SdkTurn) -> None:
        with self._turns_lock:
            if self._turns.get(turn.session_id) is turn:
                self._turns.pop(turn.session_id, None)

    # -- 生命周期 ---------------------------------------------------------

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self.home.mkdir(parents=True, exist_ok=True)
        await loop.run_in_executor(self.executor, self._sync_start)
        self._router = _InteractionRouter(self)
        self._router.start()
        logger.info(
            "dsh.sdk_started",
            extra={
                "event": "dsh.sdk_started",
                "fields": {
                    "home": str(self.home),
                    "provider": self.config.provider or "(default)",
                    "model": self.config.model or "(default)",
                },
            },
        )

    def _sync_start(self) -> None:
        sdk = _import_sdk()
        self._sdk = sdk
        kwargs: dict[str, Any] = {
            "dsh_home": str(self.home),
            "approval_requests": True,
            "on_user_question": self._on_user_question,
        }
        if self.config.runtime_exe.strip():
            kwargs["dsh_bin"] = str(Path(self.config.runtime_exe).expanduser().resolve())
        if self.config.provider.strip():
            kwargs["provider"] = self.config.provider
        if self.config.model.strip():
            kwargs["model"] = self.config.model
        if self.config.base_url.strip():
            kwargs["base_url"] = self.config.base_url
        if self.config.api_key.strip():
            kwargs["api_key"] = self.config.api_key
        harness_config = sdk.DeepSeekHarnessConfig(**kwargs)
        harness = sdk.DeepSeekHarness(harness_config)
        harness.start()
        self._harness = harness
        self._sdk_client = harness.client

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._router is not None:
            self._router.stop()
            self._router = None
        with self._turns_lock:
            turns = list(self._turns.values())
        for turn in turns:
            turn.cancel()
        harness = self._harness
        if harness is not None:
            try:
                await asyncio.get_running_loop().run_in_executor(self.executor, harness.close)
            except Exception:  # pragma: no cover - 关闭失败不阻塞退出
                logger.exception("dsh.sdk_close_failed", extra={"event": "dsh.sdk_close_failed"})
        self._harness = None
        self._sdk_client = None
        self.executor.shutdown(wait=False, cancel_futures=True)

    # -- 会话 -------------------------------------------------------------

    async def create_session(self, title: str | None = None) -> DshSession:
        # SDK start_session 不发 RPC，只生成 id；Bridge 自己维护会话索引
        session = await asyncio.get_running_loop().run_in_executor(
            self.executor, lambda: self._require_harness().start_session()
        )
        return DshSession(session_id=str(session.id), title=title or "")

    async def list_sessions(self) -> list[DshSession]:
        # SDK 无 list_sessions：会话列表以 Bridge 的 history.sqlite3 为准
        return []

    def start_turn(self, session_id: str, text: str) -> SdkTurn:
        turn = SdkTurn(self, session_id, text)
        turn.start()
        return turn

    # -- 提问 handler（SDK daemon 线程） ----------------------------------

    def _on_user_question(self, request: Any, cancelled: threading.Event) -> Any:
        try:
            return self._handle_user_question(request, cancelled)
        except Exception:  # pragma: no cover - 防御
            logger.exception("sdk.question_failed", extra={"event": "sdk.question_failed"})
            return {"cancelled": True}

    def _handle_user_question(self, request: Any, cancelled: threading.Event) -> Any:
        session_id = str(getattr(request, "session_id", "") or "")
        turn = self.turn_for_session(session_id)
        if turn is None:
            return {"cancelled": True}
        request_id = str(getattr(request, "request_id", "") or uuid.uuid4().hex)
        questions = _protocol_questions(getattr(request, "questions", None) or [])
        slot = turn._new_question(request_id)
        deadline = int(time.time() + INTERACTION_TIMEOUT_SEC)
        turn._inject(DshQuestion(id=request_id, questions=questions, deadline=deadline))
        answers = slot.wait(INTERACTION_TIMEOUT_SEC, cancelled)
        with turn._lock:
            turn._questions.pop(request_id, None)
        if answers is None:
            return {"cancelled": True}
        return {"answers": _sdk_answers(answers)}

    def _require_harness(self) -> Any:
        if self._harness is None:
            raise RuntimeError("DSH SDK 尚未启动")
        return self._harness


def _protocol_questions(questions: Any) -> list[dict[str, Any]]:
    """SDK ``UserQuestionRequest.questions`` → PROTOCOL ``question.request.questions``。"""
    result: list[dict[str, Any]] = []
    for index, raw in enumerate(list(questions)):
        if isinstance(raw, dict):
            qid = raw.get("id") or f"q{index + 1}"
            text = raw.get("question") or ""
            options = raw.get("options") or []
        else:
            qid = getattr(raw, "id", None) or f"q{index + 1}"
            text = getattr(raw, "question", "") or ""
            options = getattr(raw, "options", None) or []
        option_list: list[dict[str, str]] = []
        for opt_index, option in enumerate(list(options)):
            if isinstance(option, dict):
                label = str(option.get("label") or option.get("id") or "")
                oid = str(option.get("id") or label or f"o{opt_index + 1}")
            else:
                label = str(getattr(option, "label", "") or "")
                oid = str(getattr(option, "id", "") or label or f"o{opt_index + 1}")
            option_list.append({"id": oid, "label": label})
        result.append(
            {
                "id": str(qid),
                "question": str(text),
                "multiSelect": False,
                "required": True,
                "allowCustom": True,
                "options": option_list,
            }
        )
    return result


def _sdk_answers(answers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """PROTOCOL ``question.response.answers`` → SDK ``UserQuestionAnswer`` 的 answers。"""
    result: list[dict[str, Any]] = []
    for item in answers:
        if not isinstance(item, dict):
            continue
        qid = item.get("questionId") or item.get("id")
        if qid is None:
            continue
        entry: dict[str, Any] = {
            "id": str(qid),
            "selected": [str(x) for x in (item.get("selected") or [])],
        }
        custom = item.get("custom")
        if custom:
            entry["custom"] = str(custom)
        result.append(entry)
    return result


def create_client(config: DshConfig | None = None, *, home: Any = None) -> SdkDshClient:
    """工厂：供 :func:`bridge.dsh.create_dsh_client` 调用。"""
    return SdkDshClient(config, home=home)
