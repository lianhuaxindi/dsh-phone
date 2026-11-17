"""ASR 后端工厂。

根据 ``[asr].mode`` 选择识别器实现：

- ``fake``：内置假识别器，无模型依赖，M1/CI/端到端测试默认；
- ``sherpa``：sherpa-onnx 真识别器（``bridge/asr/sherpa.py``，模型缺失或依赖
  不可用时**不阻塞 Bridge 启动**，返回 :class:`UnavailableASR`，音频请求时报
  ``error{code:2002,"retryable":true}``）。
"""

from __future__ import annotations

import logging
from typing import Any

from ..config import AsrConfig
from .base import (
    AsrBusy,
    AsrError,
    AsrEvent,
    AsrStream,
    AsrUnavailable,
    StreamingASR,
    UnavailableASR,
)
from .fake import DEFAULT_FINAL_TEXT, FakeASR, FakeStream

__all__ = [
    "AsrEvent",
    "AsrError",
    "AsrUnavailable",
    "AsrBusy",
    "AsrStream",
    "StreamingASR",
    "UnavailableASR",
    "FakeASR",
    "FakeStream",
    "DEFAULT_FINAL_TEXT",
    "create_recognizer",
]

logger = logging.getLogger("bridge.asr")


def _create_sherpa(config: AsrConfig) -> StreamingASR:
    """构造 sherpa 识别器；任何失败都降级为 UnavailableASR（Bridge 仍可启动）。"""
    try:
        from . import sherpa  # type: ignore[attr-defined]
    except Exception as exc:  # ImportError / 模型/依赖初始化错误
        logger.warning(
            "asr.sherpa_unavailable: %s", exc, extra={"event": "asr.sherpa_unavailable"}
        )
        return UnavailableASR(f"无法加载 sherpa 后端: {exc}")

    factory: Any = getattr(sherpa, "create_recognizer", None)
    if callable(factory):
        try:
            recognizer = factory(config)
        except TypeError:
            try:
                recognizer = factory()
            except Exception as exc:  # pragma: no cover - 后端实现细节
                logger.warning("asr.sherpa_init_failed: %s", exc)
                return UnavailableASR(f"sherpa 初始化失败: {exc}")
        except Exception as exc:
            logger.warning(
                "asr.sherpa_init_failed: %s", exc,
                extra={"event": "asr.sherpa_init_failed"},
            )
            return UnavailableASR(f"sherpa 初始化失败: {exc}")
        if isinstance(recognizer, StreamingASR):
            return recognizer
        return UnavailableASR(f"sherpa 工厂返回了非法对象: {type(recognizer).__name__}")

    cls = getattr(sherpa, "SherpaASR", None) or getattr(sherpa, "SherpaStreamingASR", None)
    if cls is None:
        return UnavailableASR("sherpa 后端缺少 create_recognizer() / SherpaASR 入口")
    try:
        recognizer = cls(config)
    except TypeError:
        try:
            recognizer = cls()
        except Exception as exc:  # pragma: no cover
            return UnavailableASR(f"sherpa 初始化失败: {exc}")
    except Exception as exc:  # pragma: no cover
        return UnavailableASR(f"sherpa 初始化失败: {exc}")
    if isinstance(recognizer, StreamingASR):
        return recognizer
    return UnavailableASR(f"sherpa 类不是 StreamingASR 子类: {type(recognizer).__name__}")


def create_recognizer(config: AsrConfig | None = None) -> StreamingASR:
    """按配置创建识别器（永不因 ASR 不可用而抛异常）。"""
    config = config or AsrConfig()
    mode = (config.mode or "fake").strip().lower()
    if mode == "fake":
        return FakeASR()
    if mode == "sherpa":
        return _create_sherpa(config)
    logger.warning("asr.unknown_mode: %r", mode, extra={"event": "asr.unknown_mode"})
    return UnavailableASR(f"未知 ASR 模式: {mode!r}")
