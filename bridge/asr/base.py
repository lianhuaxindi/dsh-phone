"""ASR 抽象接口（partial 流式识别 + final 定稿）。

实现约定：

- ``StreamingASR.create_stream()`` 创建一个流的识别会话。
- 识别器可以在任意线程（例如 sherpa 推理线程）里调用
  :meth:`AsrStream._emit`，事件通过 ``loop.call_soon_threadsafe`` 投回
  事件循环的 ``stream.events`` 队列。
- 上层（bridge.connection）负责节流（≤5 次/秒）、VAD、超时与
  ``asr.partial``/``asr.final`` 的发送；本层只负责产出事件。
- 错误统一用 :class:`AsrError` 子类，带 ``code`` / ``retryable`` 属性，
  便于 Bridge 直接映射为 ``error{code:2002,"retryable":true}``。
"""

from __future__ import annotations

import abc
import asyncio
import time
import weakref
from dataclasses import dataclass
from typing import Any, ClassVar, Literal

__all__ = [
    "AsrEvent",
    "AsrError",
    "AsrUnavailable",
    "AsrBusy",
    "AsrStream",
    "StreamingASR",
    "UnavailableASR",
]


@dataclass(frozen=True)
class AsrEvent:
    """识别事件：``partial``（过程中）或 ``final``（定稿）。"""

    type: Literal["partial", "final"]
    text: str
    stability: float = 0.0
    aseq: int = 0
    duration_ms: int = 0

    def __post_init__(self) -> None:
        if self.type not in ("partial", "final"):
            raise ValueError(f"未知 ASR 事件类型: {self.type!r}")

    @classmethod
    def partial(cls, text: str, *, aseq: int = 0, stability: float = 0.0) -> "AsrEvent":
        return cls(type="partial", text=text, stability=stability, aseq=aseq)

    @classmethod
    def final(cls, text: str, *, aseq: int = 0, duration_ms: int = 0) -> "AsrEvent":
        return cls(type="final", text=text, aseq=aseq, duration_ms=duration_ms)


class AsrError(RuntimeError):
    """ASR 层错误基类。"""

    code: int = 2002
    retryable: bool = True


class AsrUnavailable(AsrError):
    """识别器不可用（模型缺失、依赖未安装等）。映射为 ``error{code:2002}``。"""


class AsrBusy(AsrError):
    """已有正在进行的音频流（协议层通常用 2001 表示）。"""

    code = 2001


class AsrStream(abc.ABC):
    """一条音频流的识别会话。

    生命周期：``accept_pcm()*`` → ``stop()`` → 消费 ``events`` 直到
    ``final``；或 ``abort()`` 丢弃（不再产生 final）。
    """

    def __init__(
        self,
        stream_id: str,
        *,
        sample_rate: int = 16000,
        lang: str = "zh",
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        self.stream_id = stream_id
        self.sample_rate = int(sample_rate)
        self.lang = lang
        self.loop = loop or asyncio.get_running_loop()
        self.events: asyncio.Queue[AsrEvent] = asyncio.Queue()
        self.started_at = time.monotonic()
        self.samples_received = 0
        self._stopped = False
        self._aborted = False
        self._finalized = False
        self._loop_ref = weakref.ref(self.loop)

    # -- 状态 -------------------------------------------------------------

    @property
    def closed(self) -> bool:
        return self._aborted or self._finalized

    @property
    def finalized(self) -> bool:
        return self._finalized

    @property
    def stopped(self) -> bool:
        return self._stopped

    @property
    def duration_ms(self) -> int:
        """按已接收 PCM 字节数估算的音频时长。"""
        return int(self.samples_received * 1000 / self.sample_rate) if self.sample_rate else 0

    # -- 事件投递（线程安全） --------------------------------------------

    def _emit(self, event: AsrEvent) -> None:
        """把事件投回事件循环；流已关闭时丢弃。"""
        if self._aborted:
            return
        if event.type == "final":
            self._finalized = True
        loop = self._loop_ref()
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(self.events.put_nowait, event)
        except RuntimeError:  # pragma: no cover - 事件循环正在关闭
            pass

    def _emit_partial(self, text: str, *, aseq: int = 0, stability: float = 0.0) -> None:
        if self._stopped or self._aborted or self._finalized:
            return
        self._emit(AsrEvent.partial(text, aseq=aseq, stability=stability))

    def _emit_final(self, text: str, *, aseq: int = 0, duration_ms: int | None = None) -> None:
        if self._aborted or self._finalized:
            return
        self._emit(
            AsrEvent.final(
                text,
                aseq=aseq,
                duration_ms=self.duration_ms if duration_ms is None else duration_ms,
            )
        )

    # -- 子类实现 ---------------------------------------------------------

    @abc.abstractmethod
    def accept_pcm(self, pcm: bytes, *, ts_ms: int = 0, aseq: int = 0) -> None:
        """接收一帧 PCM16LE 数据（16 kHz/单声道/16 bit）。"""

    @abc.abstractmethod
    def stop(self, *, aseq: int = 0) -> None:
        """结束输入并产出 final（可能异步，由消费端等待）。"""

    @abc.abstractmethod
    def abort(self) -> None:
        """立即丢弃本流（不产出 final）。"""

    async def aclose(self) -> None:
        """释放资源；可重复调用。"""
        self.abort()


class StreamingASR(abc.ABC):
    """识别器工厂/生命周期接口。"""

    #: 协议 ``audio.ready.asrMode`` 取值：``fake`` 或 ``real``。
    mode: ClassVar[str] = "fake"

    @property
    def available(self) -> bool:
        """识别器当前是否可用。"""
        return True

    @property
    def unavailable_reason(self) -> str:
        """不可用原因（可用时为空串）。"""
        return ""

    @abc.abstractmethod
    async def create_stream(
        self,
        stream_id: str,
        *,
        sample_rate: int = 16000,
        lang: str = "zh",
    ) -> AsrStream:
        """创建一条识别流（同一时刻只允许一条）。"""

    async def aclose(self) -> None:
        """关闭识别器、释放模型等资源。"""


class UnavailableASR(StreamingASR):
    """占位识别器：构造成功但任何流请求都失败（ASR 不可用 → 2002）。"""

    def __init__(self, reason: str, *, mode: str = "real") -> None:
        self.reason = reason or "ASR 不可用"
        self._mode = mode

    @property
    def mode(self) -> str:  # type: ignore[override]
        return self._mode

    @property
    def available(self) -> bool:  # type: ignore[override]
        return False

    @property
    def unavailable_reason(self) -> str:  # type: ignore[override]
        return self.reason

    async def create_stream(
        self,
        stream_id: str,
        *,
        sample_rate: int = 16000,
        lang: str = "zh",
    ) -> AsrStream:  # pragma: no cover - 由 connection 捕获
        raise AsrUnavailable(self.reason)

    async def aclose(self) -> None:
        return None
