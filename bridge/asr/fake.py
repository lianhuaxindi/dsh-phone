"""假 ASR：不依赖任何模型/网络，供 M1 与 CI/端到端测试使用。

行为：

- 按累计音频时长产出**渐进 partial**（前缀文本，文本变化才发）；
- ``stop()`` 时定稿：有音频 → 固定中文整句；无音频 → 空串
  （PROTOCOL §10.2 要求空串也要发 final）；
- ``abort()`` 后不再产生任何事件。

真正的 sherpa-onnx 实现在 ``bridge/asr/sherpa.py``（由 asr-pipeline 维护），
通过 ``bridge.asr.create_recognizer`` 工厂选择。
"""

from __future__ import annotations

from typing import ClassVar

from .base import AsrStream, StreamingASR

__all__ = ["DEFAULT_FINAL_TEXT", "FakeASR", "FakeStream"]

#: fake 识别的默认整句（测试与演示用，稳定可断言）。
DEFAULT_FINAL_TEXT = "帮我查一下今天的待办事项"

#: (音频时长下限 ms, 该阶段 partial 文本)；最后一段用整句。
_PARTIAL_STEPS: tuple[tuple[int, str], ...] = (
    (300, "帮"),
    (600, "帮我"),
    (900, "帮我查"),
    (1200, "帮我查一下"),
    (1600, "帮我查一下今天"),
    (2200, "帮我查一下今天的"),
)


class FakeStream(AsrStream):
    """按音频时长生成渐进结果的假流。"""

    def __init__(
        self,
        stream_id: str,
        *,
        sample_rate: int = 16000,
        lang: str = "zh",
        final_text: str = DEFAULT_FINAL_TEXT,
        **kwargs,
    ) -> None:
        super().__init__(stream_id, sample_rate=sample_rate, lang=lang, **kwargs)
        self.final_text = final_text
        self._last_partial = ""

    def _text_for_duration(self, duration_ms: int) -> str:
        for threshold, text in _PARTIAL_STEPS:
            if duration_ms < threshold:
                return text
        return self.final_text

    def accept_pcm(self, pcm: bytes, *, ts_ms: int = 0, aseq: int = 0) -> None:
        if self._aborted or self._stopped or self._finalized:
            return
        if not pcm:
            return
        self.samples_received += len(pcm) // 2
        text = self._text_for_duration(self.duration_ms)
        if text and text != self._last_partial:
            self._last_partial = text
            stability = min(0.95, self.duration_ms / 3000.0)
            self._emit_partial(text, aseq=aseq, stability=round(stability, 3))

    def stop(self, *, aseq: int = 0) -> None:
        if self._aborted or self._stopped or self._finalized:
            return
        self._stopped = True
        text = self.final_text if self.samples_received > 0 else ""
        self._emit_final(text, aseq=aseq)

    def abort(self) -> None:
        self._aborted = True
        self._stopped = True


class FakeASR(StreamingASR):
    """假识别器工厂。"""

    mode: ClassVar[str] = "fake"

    def __init__(self, *, final_text: str = DEFAULT_FINAL_TEXT) -> None:
        self.final_text = final_text
        self._streams: list[FakeStream] = []

    async def create_stream(
        self,
        stream_id: str,
        *,
        sample_rate: int = 16000,
        lang: str = "zh",
    ) -> AsrStream:
        stream = FakeStream(
            stream_id,
            sample_rate=sample_rate,
            lang=lang,
            final_text=self.final_text,
        )
        self._streams.append(stream)
        return stream

    async def aclose(self) -> None:
        for stream in self._streams:
            stream.abort()
        self._streams.clear()
