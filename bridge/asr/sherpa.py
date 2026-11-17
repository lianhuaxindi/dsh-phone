"""sherpa-onnx 真识别后端：partial 流式 + final 整段复判。

设计（docs/ARCHITECTURE.md §2.1–2.2、docs/PRD.md §9.3）：

- **partial**：``sherpa_onnx.OnlineRecognizer``（流式 zipformer transducer，
  中英双语 int8 模型），按 320 ms chunk 解码，文本变化才产出
  :class:`AsrEvent.partial`；静音/超时定稿由上层 connection 负责，本层不做 VAD。
- **final**：``sherpa_onnx.OfflineRecognizer.from_sense_voice``（SenseVoice
  int8，``use_itn=True``）对整段音频复判，失败/为空时回退到流式结果。
- **线程模型**：``__init__`` 只做轻量赋值、立即返回（后台线程负责 import
  ``sherpa_onnx``、校验模型、构造 recognizer 并 warmup）；每条流一个 worker
  线程，事件循环侧 ``accept_pcm`` 只把 PCM 放进线程内队列，worker 解码后通过
  ``AsrStream._emit``（``call_soon_threadsafe``）投回事件循环。同一时刻只允许
  一条流（``AsrBusy`` → 协议 2001）。
- **失败语义**：依赖缺失、模型缺失/损坏 **不在构造期抛异常**（Bridge 必须能启动）；
  后台加载把异常记录在 ``unavailable_reason``，``create_stream()`` 抛
  :class:`AsrUnavailable`（``code=2002, retryable=True``）；``create_recognizer``
  仅兜底构造期的意外异常并降级为 :class:`~bridge.asr.base.UnavailableASR`。

模型不在仓库内（``models/`` 已 gitignore），下载见 ``scripts/download_models.py``。
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import queue
import threading
import time
from typing import Any, ClassVar

import numpy as np

from .base import AsrBusy, AsrEvent, AsrStream, AsrUnavailable, StreamingASR
from .models import (
    DEFAULT_FINAL_MODEL,
    DEFAULT_STREAMING_MODEL,
    ModelError,
    require_model,
    resolve_models_dir,
)

__all__ = [
    "SherpaASR",
    "SherpaStream",
    "SherpaRuntime",
    "OnnxSherpaRuntime",
    "create_recognizer",
    "probe",
]

logger = logging.getLogger("bridge.asr.sherpa")

#: V1 只支持 16 kHz / 单声道 / PCM16。
SAMPLE_RATE = 16000
#: 流式解码 chunk：320 ms（PRD §9.3）。
CHUNK_MS = 320
CHUNK_SAMPLES = SAMPLE_RATE * CHUNK_MS // 1000  # 5120
#: 输入帧：20 ms / 640 B（PROTOCOL §10.2）。
FRAME_MS = 20
FRAME_BYTES = 640
#: 单帧队列上限：120 s 音频 = 6000 帧；超过则丢弃并记录（防内存膨胀）。
MAX_QUEUED_FRAMES = 6000
#: 后台加载/首次就绪等待上限（秒）。
LOAD_TIMEOUT = 60.0
#: 前一条流收尾等待上限（秒），超过视为仍在处理 → AsrBusy。
STREAM_SETTLE_TIMEOUT = 5.0


def _int16_to_float32(pcm: bytes) -> np.ndarray:
    """PCM16LE bytes → float32 [-1, 1]（sherpa-onnx 输入格式）。"""
    if not pcm:
        return np.zeros(0, dtype=np.float32)
    if len(pcm) % 2:
        pcm = pcm[:-1]
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
    return samples / 32768.0


class SherpaRuntime:
    """sherpa-onnx 运行时抽象（便于单元测试注入假实现，不依赖真模型）。"""

    #: 是否有 SenseVoice 复判能力。
    has_offline: bool = False

    def online_new_stream(self) -> Any:  # pragma: no cover - 接口
        raise NotImplementedError

    def online_accept(self, stream: Any, samples: np.ndarray) -> None:  # pragma: no cover
        raise NotImplementedError

    def online_decode(self, stream: Any) -> None:  # pragma: no cover
        raise NotImplementedError

    def online_result(self, stream: Any) -> str:  # pragma: no cover
        raise NotImplementedError

    def online_input_finished(self, stream: Any) -> None:  # pragma: no cover
        raise NotImplementedError

    def offline_decode(self, samples: np.ndarray) -> str:  # pragma: no cover
        raise NotImplementedError

    def close(self) -> None:  # pragma: no cover - 接口
        return None


def _pick(files: dict[str, str], *needles: str) -> str:
    """按关键词从 ``{相对路径: 绝对路径}`` 里挑一个文件（用于模型文件定位）。"""
    for rel, path in files.items():
        lower = rel.lower()
        if all(n in lower for n in needles):
            return path
    raise ModelError(f"模型文件集合中找不到包含 {needles} 的文件: {sorted(files)}")


class OnnxSherpaRuntime(SherpaRuntime):
    """基于 ``sherpa_onnx`` Python 包的真实运行时。"""

    def __init__(
        self,
        *,
        models_dir: str,
        streaming_model: str = DEFAULT_STREAMING_MODEL,
        final_model: str = DEFAULT_FINAL_MODEL,
        sample_rate: int = SAMPLE_RATE,
        num_threads: int = 2,
        final_enabled: bool = True,
        use_itn: bool = True,
        debug: bool = False,
    ) -> None:
        self.sample_rate = int(sample_rate)
        self.num_threads = max(1, int(num_threads))
        self.final_enabled = bool(final_enabled)
        self._closed = False

        try:
            sherpa_onnx = importlib.import_module("sherpa_onnx")
        except Exception as exc:  # ImportError / OSError（DLL 缺失）
            raise AsrUnavailable(
                f"sherpa-onnx 未安装或不可用（{type(exc).__name__}: {exc}）；"
                "请先安装: python -m pip install sherpa-onnx"
            ) from exc
        self._sherpa = sherpa_onnx
        self.version = str(getattr(sherpa_onnx, "__version__", "") or "")

        # 1) 流式 zipformer（partial）
        stream_dir = require_model(streaming_model, models_dir)
        self.streaming_model = streaming_model
        self.streaming_dir = stream_dir
        stream_files = {f.rel_path: str(stream_dir / f.rel_path) for f in _spec_files(streaming_model)}
        self._online = sherpa_onnx.OnlineRecognizer.from_transducer(
            tokens=_pick(stream_files, "tokens"),
            encoder=_pick(stream_files, "encoder"),
            decoder=_pick(stream_files, "decoder"),
            joiner=_pick(stream_files, "joiner"),
            num_threads=self.num_threads,
            sample_rate=self.sample_rate,
            feature_dim=80,
            decoding_method="greedy_search",
            max_active_paths=4,
            enable_endpoint_detection=False,
            provider="cpu",
            debug=bool(debug),
        )

        # 2) SenseVoice（final 整段复判 + ITN），可选
        self._offline = None
        self.has_offline = False
        if self.final_enabled:
            final_dir = require_model(final_model, models_dir)
            self.final_model = final_model
            self.final_dir = final_dir
            final_files = {f.rel_path: str(final_dir / f.rel_path) for f in _spec_files(final_model)}
            self._offline = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                model=_pick(final_files, ".onnx"),
                tokens=_pick(final_files, "tokens"),
                num_threads=self.num_threads,
                sample_rate=self.sample_rate,
                decoding_method="greedy_search",
                use_itn=bool(use_itn),
                language="",
                provider="cpu",
                debug=bool(debug),
            )
            self.has_offline = True

    # -- 流式 -------------------------------------------------------------

    def online_new_stream(self) -> Any:
        return self._online.create_stream()

    def online_accept(self, stream: Any, samples: np.ndarray) -> None:
        stream.accept_waveform(self.sample_rate, np.ascontiguousarray(samples, dtype=np.float32))

    def online_decode(self, stream: Any) -> None:
        while self._online.is_ready(stream):
            self._online.decode_stream(stream)

    def online_result(self, stream: Any) -> str:
        result = self._online.get_result(stream)
        if isinstance(result, str):
            return result
        return str(getattr(result, "text", "") or "")

    def online_input_finished(self, stream: Any) -> None:
        stream.input_finished()

    # -- 整段（final） ----------------------------------------------------

    def offline_decode(self, samples: np.ndarray) -> str:
        if self._offline is None:
            return ""
        stream = self._offline.create_stream()
        stream.accept_waveform(self.sample_rate, np.ascontiguousarray(samples, dtype=np.float32))
        self._offline.decode_stream(stream)
        result = stream.result
        if isinstance(result, str):
            return result
        return str(getattr(result, "text", "") or "")

    # -- 预热/关闭 --------------------------------------------------------

    def warmup(self) -> None:
        """用 0.5 s 静音跑一遍流式与离线，把权重/内存页预热（首字延迟）。"""
        silence = np.zeros(int(self.sample_rate * 0.5), dtype=np.float32)
        stream = self.online_new_stream()
        self.online_accept(stream, silence)
        self.online_decode(stream)
        self.online_input_finished(stream)
        self.online_decode(stream)
        self.online_result(stream)
        if self._offline is not None:
            self.offline_decode(silence)

    def close(self) -> None:
        self._closed = True
        self._online = None  # type: ignore[assignment]
        self._offline = None


def _spec_files(name: str) -> Any:
    """取模型清单里的文件列表（延迟 import，避免循环依赖）。"""
    from .models import MODELS, get_spec

    spec = get_spec(name)
    if spec.name not in MODELS:  # pragma: no cover - 清单自洽性
        raise ModelError(f"未知模型 {name!r}")
    return spec.files


class SherpaStream(AsrStream):
    """一条流的识别会话：event loop 侧只入队，worker 线程解码并回投事件。"""

    def __init__(
        self,
        asr: "SherpaASR",
        stream_id: str,
        *,
        sample_rate: int = SAMPLE_RATE,
        lang: str = "zh",
        loop: asyncio.AbstractEventLoop | None = None,
        final_enabled: bool = True,
    ) -> None:
        super().__init__(stream_id, sample_rate=sample_rate, lang=lang, loop=loop)
        self.final_enabled = final_enabled
        self.stats: dict[str, Any] = {
            "chunks": 0,
            "decode_ms": 0.0,
            "first_partial_ms": None,
            "final_latency_ms": None,
            "offline_ms": None,
            "dropped_frames": 0,
        }
        self._asr = asr
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=MAX_QUEUED_FRAMES)
        self._worker: threading.Thread | None = None
        self._done = threading.Event()
        self._abort_flag = False
        self._stop_aseq = 0
        self._last_aseq = 0
        self._all_samples: list[np.ndarray] = []
        self._samples_total = 0
        self._decode_started_at = time.perf_counter()
        self._stop_wall = 0.0

    # -- 公共状态 ---------------------------------------------------------

    @property
    def done(self) -> bool:
        return self._done.is_set()

    def wait_done(self, timeout: float | None = None) -> bool:
        return self._done.wait(timeout)

    # -- AsrStream 接口 ---------------------------------------------------

    def start(self) -> None:
        """启动 worker 线程（由 ``SherpaASR.create_stream`` 调用一次）。"""
        if self._worker is not None:
            return
        self._decode_started_at = time.perf_counter()
        self._worker = threading.Thread(
            target=self._worker_main, name=f"asr-stream-{self.stream_id}", daemon=True
        )
        self._worker.start()

    def accept_pcm(self, pcm: bytes, *, ts_ms: int = 0, aseq: int = 0) -> None:
        if self._aborted or self._stopped or self._finalized or self._abort_flag:
            return
        if not pcm:
            return
        samples = _int16_to_float32(pcm)
        if samples.size == 0:
            return
        self._last_aseq = aseq
        self.samples_received += samples.size
        self._samples_total += samples.size
        # 保留整段音频供 final 阶段 SenseVoice 复判（120s 上限 ≈ 7.7MB，可接受）
        self._all_samples.append(samples)
        try:
            self._queue.put_nowait(samples)
        except queue.Full:  # pragma: no cover - 极端慢机器
            self.stats["dropped_frames"] += 1
            if self.stats["dropped_frames"] in (1, 100, 1000):
                logger.warning(
                    "asr.queue_full: stream=%s dropped=%d", self.stream_id, self.stats["dropped_frames"]
                )

    def stop(self, *, aseq: int = 0) -> None:
        if self._aborted or self._stopped or self._finalized:
            return
        self._stopped = True
        self._stop_aseq = aseq
        self._queue.put_nowait(None)

    def abort(self) -> None:
        if self._aborted:
            return
        self._aborted = True
        self._abort_flag = True
        self._stopped = True
        self._drain_queue()
        try:
            self._queue.put_nowait(None)
        except queue.Full:  # pragma: no cover
            pass

    async def aclose(self) -> None:
        self.abort()
        await asyncio.to_thread(self._done.wait, STREAM_SETTLE_TIMEOUT)

    # -- worker -----------------------------------------------------------

    def _drain_queue(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                return

    def _worker_main(self) -> None:
        try:
            self._decode_loop()
        except Exception:  # pragma: no cover - 真模型异常路径
            logger.exception("asr.stream_failed: stream=%s", self.stream_id)
            if not self._abort_flag:
                try:
                    self._emit_final("", aseq=self._stop_aseq)
                except Exception:
                    logger.exception("asr.final_emit_failed: stream=%s", self.stream_id)
        finally:
            self._all_samples.clear()
            self._done.set()
            self._asr._on_stream_closed(self)

    def _decode_loop(self) -> None:
        runtime = self._asr._runtime
        if runtime is None:  # pragma: no cover - create_stream 已校验
            self._emit_final("", aseq=self._stop_aseq)
            return
        online = runtime.online_new_stream()
        pending: list[np.ndarray] = []
        pending_samples = 0
        last_partial = ""
        while True:
            item = self._queue.get()
            if self._abort_flag:
                return
            if item is None:  # stop() 哨兵
                break
            pending.append(item)
            pending_samples += item.size
            while pending_samples >= CHUNK_SAMPLES:
                chunk, pending, pending_samples = self._take_chunk(pending, pending_samples)
                started = time.perf_counter()
                runtime.online_accept(online, chunk)
                runtime.online_decode(online)
                decode_ms = (time.perf_counter() - started) * 1000.0
                self.stats["chunks"] += 1
                self.stats["decode_ms"] += decode_ms
                text = runtime.online_result(online).strip()
                if text and text != last_partial:
                    last_partial = text
                    if self.stats["first_partial_ms"] is None:
                        self.stats["first_partial_ms"] = round(
                            (time.perf_counter() - self._decode_started_at) * 1000.0, 2
                        )
                    self._emit_partial(text, aseq=self._last_aseq, stability=self._stability())
                if self._abort_flag:
                    return

        # 收尾：把剩余样本喂完（不足一个 chunk 也要 decode）
        self._stop_wall = time.perf_counter()
        if pending:
            chunk = np.concatenate(pending) if len(pending) > 1 else pending[0]
            pending.clear()
            pending_samples = 0
            started = time.perf_counter()
            runtime.online_accept(online, chunk)
            runtime.online_decode(online)
            self.stats["decode_ms"] += (time.perf_counter() - started) * 1000.0
        runtime.online_input_finished(online)
        runtime.online_decode(online)
        stream_text = runtime.online_result(online).strip()

        if self._abort_flag:
            return

        # 无音频 / 静音流：仍必须发 final（PROTOCOL §10.2，空串也发）
        if self._samples_total <= 0:
            self._mark_final("")
            return

        final_text = stream_text
        if self.final_enabled and runtime.has_offline:
            offline_started = time.perf_counter()
            try:
                audio = (
                    np.concatenate(self._all_samples)
                    if self._all_samples
                    else np.zeros(0, dtype=np.float32)
                )
                offline_text = runtime.offline_decode(audio).strip()
                self.stats["offline_ms"] = round((time.perf_counter() - offline_started) * 1000.0, 2)
                if offline_text:
                    final_text = offline_text
            except Exception:  # pragma: no cover - 真模型异常路径
                logger.exception("asr.offline_final_failed: stream=%s", self.stream_id)

        self._mark_final(final_text)

    def _take_chunk(
        self, pending: list[np.ndarray], pending_samples: int
    ) -> tuple[np.ndarray, list[np.ndarray], int]:
        audio = pending[0] if len(pending) == 1 else np.concatenate(pending)
        chunk = audio[:CHUNK_SAMPLES].copy()
        rest = audio[CHUNK_SAMPLES:]
        if rest.size:
            return chunk, [rest], rest.size
        return chunk, [], 0

    def _stability(self) -> float:
        # 与 fake 后端保持同类语义（0..0.95）：话说得越久越稳定。
        return round(min(0.95, 0.3 + 0.65 * min(1.0, self.duration_ms / 3000.0)), 3)

    def _mark_final(self, text: str) -> None:
        if self._stop_wall:
            self.stats["final_latency_ms"] = round((time.perf_counter() - self._stop_wall) * 1000.0, 2)
        self._emit_final(text, aseq=self._stop_aseq)
        logger.info(
            "asr.final: stream=%s text_len=%d audio_ms=%d final_ms=%s offline_ms=%s chunks=%d",
            self.stream_id,
            len(text),
            self.duration_ms,
            self.stats["final_latency_ms"],
            self.stats["offline_ms"],
            self.stats["chunks"],
        )


class SherpaASR(StreamingASR):
    """sherpa-onnx 识别器工厂/生命周期（同一时刻一条流）。"""

    mode: ClassVar[str] = "real"

    def __init__(
        self,
        config: Any | None = None,
        *,
        runtime: SherpaRuntime | None = None,
        models_dir: Any | None = None,
        streaming_model: str | None = None,
        final_model: str | None = None,
        sample_rate: int | None = None,
        num_threads: int = 2,
        final_enabled: bool = True,
        use_itn: bool = True,
        debug: bool = False,
        warmup: bool = True,
        load_in_background: bool = True,
    ) -> None:
        asr_config = config
        self.lang = str(getattr(asr_config, "lang", "zh") or "zh")
        self.partial_rate_per_sec = int(getattr(asr_config, "partial_rate_per_sec", 5) or 5)
        cfg_models_dir = getattr(asr_config, "models_dir", "") or ""
        self.models_dir = str(resolve_models_dir(models_dir or cfg_models_dir or None))
        self.streaming_model = streaming_model or DEFAULT_STREAMING_MODEL
        self.final_model = final_model or DEFAULT_FINAL_MODEL
        self.sample_rate = int(sample_rate or getattr(asr_config, "sample_rate", SAMPLE_RATE) or SAMPLE_RATE)
        if self.sample_rate != SAMPLE_RATE:
            raise AsrUnavailable(f"sherpa 后端 V1 仅支持 {SAMPLE_RATE} Hz，收到 {self.sample_rate}")
        self.num_threads = max(1, int(num_threads))
        self.final_enabled = bool(final_enabled)
        self.use_itn = bool(use_itn)
        self.debug = bool(debug)
        self.warmup_enabled = bool(warmup)

        self._runtime: SherpaRuntime | None = None
        self._ready = threading.Event()
        self._load_error: BaseException | None = None
        self._loader: threading.Thread | None = None
        self._closed = False
        self._active: SherpaStream | None = None
        self._lock = threading.Lock()

        if runtime is not None:
            # 单元测试/bench 注入路径：不加载模型、不校验依赖。
            self._runtime = runtime
            self._ready.set()
            return

        # 构造期不抛异常（Bridge 必须能启动）：依赖/模型问题在后台加载线程里
        # 记为 _load_error，``create_stream()`` 抛 AsrUnavailable（2002）。
        if load_in_background:
            self._loader = threading.Thread(target=self._load, name="asr-loader", daemon=True)
            self._loader.start()
        else:  # pragma: no cover - 测试/诊断用同步加载
            self._load()

    # -- 初始化 -----------------------------------------------------------

    def _preflight(self) -> None:
        """快速校验（不算 sha256）：依赖可导入 + 模型文件齐全。"""
        try:
            importlib.import_module("sherpa_onnx")
        except Exception as exc:
            raise AsrUnavailable(
                f"sherpa-onnx 未安装或不可用（{type(exc).__name__}: {exc}）；"
                "请先安装: python -m pip install sherpa-onnx"
            ) from exc
        try:
            require_model(self.streaming_model, self.models_dir)
            if self.final_enabled:
                require_model(self.final_model, self.models_dir)
        except ModelError as exc:
            raise AsrUnavailable(str(exc)) from exc

    def _load(self) -> None:
        started = time.perf_counter()
        try:
            self._preflight()
            runtime = OnnxSherpaRuntime(
                models_dir=self.models_dir,
                streaming_model=self.streaming_model,
                final_model=self.final_model,
                sample_rate=self.sample_rate,
                num_threads=self.num_threads,
                final_enabled=self.final_enabled,
                use_itn=self.use_itn,
                debug=self.debug,
            )
            if self.warmup_enabled:
                runtime.warmup()
            self._runtime = runtime
            logger.info(
                "asr.sherpa_ready: streaming=%s final=%s load_ms=%.1f",
                self.streaming_model,
                self.final_model if runtime.has_offline else "(disabled)",
                (time.perf_counter() - started) * 1000.0,
            )
        except BaseException as exc:  # noqa: BLE001 - 后台线程要把错误留给 create_stream
            self._load_error = exc
            logger.warning(
                "asr.sherpa_load_failed: %s: %s", type(exc).__name__, exc,
                extra={"event": "asr.sherpa_load_failed"},
            )
        finally:
            self._ready.set()

    async def ensure_ready(self, timeout: float = LOAD_TIMEOUT) -> None:
        """等待后台加载完成；失败/超时抛 :class:`AsrUnavailable`（2002）。"""
        if self._runtime is not None:
            return
        if not self._ready.is_set():
            await asyncio.to_thread(self._ready.wait, timeout)
        if self._load_error is not None:
            raise AsrUnavailable(self.unavailable_reason)
        if self._runtime is None:
            raise AsrUnavailable(f"ASR 模型加载超时（>{timeout:.0f}s）")

    # -- StreamingASR 接口 ------------------------------------------------

    @property
    def available(self) -> bool:
        return self._runtime is not None and not self._closed

    @property
    def unavailable_reason(self) -> str:
        if self._closed:
            return "识别器已关闭"
        if self._load_error is not None:
            return f"sherpa 后端不可用: {self._load_error}"
        if self._runtime is None:
            return "sherpa 模型仍在加载"
        return ""

    async def create_stream(
        self,
        stream_id: str,
        *,
        sample_rate: int = SAMPLE_RATE,
        lang: str = "zh",
    ) -> AsrStream:
        if self._closed:
            raise AsrUnavailable("识别器已关闭")
        if int(sample_rate) != self.sample_rate:
            raise AsrUnavailable(f"sherpa 后端仅支持 {self.sample_rate} Hz，收到 {sample_rate}")
        await self.ensure_ready()

        with self._lock:
            previous = self._active
        if previous is not None and not previous.closed:
            raise AsrBusy(f"已有进行中的音频流: {previous.stream_id}")
        if previous is not None:
            # 上一条流已定稿但 worker 可能还在收尾（离线复判）→ 等它结束，
            # 避免两条流同时使用同一组 sherpa recognizer。
            if not await asyncio.to_thread(previous.wait_done, STREAM_SETTLE_TIMEOUT):
                raise AsrBusy(f"上一条音频流仍在收尾: {previous.stream_id}")

        stream = SherpaStream(
            self,
            stream_id,
            sample_rate=self.sample_rate,
            lang=lang,
            loop=asyncio.get_running_loop(),
            final_enabled=self.final_enabled,
        )
        with self._lock:
            self._active = stream
        stream.start()
        logger.debug("asr.stream_open: stream=%s", stream_id)
        return stream

    def _on_stream_closed(self, stream: SherpaStream) -> None:
        with self._lock:
            if self._active is stream:
                self._active = None

    async def aclose(self) -> None:
        self._closed = True
        with self._lock:
            active = self._active
        if active is not None:
            active.abort()
            await asyncio.to_thread(active.wait_done, STREAM_SETTLE_TIMEOUT)
        runtime = self._runtime
        self._runtime = None
        if runtime is not None:
            try:
                runtime.close()
            except Exception:  # pragma: no cover
                logger.exception("asr.runtime_close_failed")

    # -- 诊断 -------------------------------------------------------------

    def describe(self) -> dict[str, Any]:
        runtime = self._runtime
        return {
            "mode": self.mode,
            "available": self.available,
            "reason": self.unavailable_reason,
            "models_dir": self.models_dir,
            "streaming_model": self.streaming_model,
            "final_model": self.final_model,
            "final_enabled": self.final_enabled,
            "sample_rate": self.sample_rate,
            "num_threads": self.num_threads,
            "sherpa_version": getattr(runtime, "version", None) if runtime else None,
        }


def create_recognizer(config: Any | None = None) -> StreamingASR:
    """``asr/__init__.py`` 工厂入口：按 ``[asr]`` 配置构造 sherpa 识别器。

    构造失败（依赖/模型缺失）时返回 :class:`UnavailableASR`，由上层把
    ``audio.start`` 映射为 ``error{code:2002,"retryable":true}``。
    """
    from .base import UnavailableASR

    try:
        return SherpaASR(config)
    except AsrUnavailable as exc:
        logger.warning(
            "asr.sherpa_unavailable: %s", exc, extra={"event": "asr.sherpa_unavailable"}
        )
        return UnavailableASR(str(exc), mode="real")
    except Exception as exc:  # pragma: no cover - 防御性
        logger.warning("asr.sherpa_init_failed: %s", exc)
        return UnavailableASR(f"sherpa 初始化失败: {exc}", mode="real")


def probe(config: Any | None = None) -> dict[str, Any]:
    """诊断用：返回依赖版本 + 模型就绪情况（不加载模型权重）。"""
    from .models import check_model

    models_dir = str(
        resolve_models_dir(getattr(config, "models_dir", "") or None)
        if config is not None
        else resolve_models_dir(None)
    )
    info: dict[str, Any] = {"models_dir": models_dir}
    try:
        sherpa_onnx = importlib.import_module("sherpa_onnx")
        info["sherpa_onnx_version"] = str(getattr(sherpa_onnx, "__version__", "") or "")
        info["installed"] = True
    except Exception as exc:
        info["installed"] = False
        info["import_error"] = f"{type(exc).__name__}: {exc}"
    for name in (DEFAULT_STREAMING_MODEL, DEFAULT_FINAL_MODEL):
        try:
            check = check_model(name, models_dir)
            info[name] = {
                "ok": check.ok,
                "directory": str(check.directory),
                "problems": list(check.problem_lines()),
            }
        except ModelError as exc:
            info[name] = {"ok": False, "problems": [str(exc)]}
    info["ready"] = bool(info.get("installed")) and all(
        info.get(name, {}).get("ok", False) for name in (DEFAULT_STREAMING_MODEL, DEFAULT_FINAL_MODEL)
    )
    return info
