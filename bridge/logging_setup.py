"""日志：JSON 行文件 + 人读控制台，敏感字段脱敏。

约定（ARCHITECTURE §7）：

- 文件日志是 JSON Lines（每行一个 JSON 对象），便于事后分析。
- 控制台是人读格式，字段以 ``key=value`` 追加。
- **绝不记录**：deviceToken、HMAC、nonce、配对码、正文全文、音频字节。
"""

from __future__ import annotations

import json
import logging
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Mapping, TextIO

from .config import LogConfig

#: 日志中的敏感键（比较时忽略大小写与下划线）。
SENSITIVE_KEYS = {
    "token",
    "devicetoken",
    "device_token",
    "tokenhash",
    "token_hash",
    "tokenblob",
    "hmac",
    "nonce",
    "code",
    "paircode",
    "pair_code",
    "secret",
    "password",
    "authorization",
}

#: 这些键只记录长度/摘要，不记录原文。
TRUNCATED_KEYS = {"text", "args", "argspreview", "args_preview", "payload", "pcm"}
MAX_STRING_LEN = 120
MAX_TEXT_LOG_LEN = 64


def _is_sensitive(key: str) -> bool:
    return key.strip().lower().replace("-", "_") in SENSITIVE_KEYS


def redact_value(key: str, value: Any) -> Any:
    """按上下文脱敏单个值。"""
    if _is_sensitive(key):
        return "***"
    if isinstance(value, Mapping):
        return {k: redact_value(str(k), v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_value(key, item) for item in value]
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<{len(bytes(value))}B>"
    if isinstance(value, str):
        if key.strip().lower().replace("-", "_") in TRUNCATED_KEYS:
            if len(value) > MAX_TEXT_LOG_LEN:
                return value[:MAX_TEXT_LOG_LEN] + f"…(+{len(value) - MAX_TEXT_LOG_LEN})"
            return value
        if len(value) > MAX_STRING_LEN:
            return value[:MAX_STRING_LEN] + "…"
    return value


def redact_fields(fields: Mapping[str, Any]) -> dict[str, Any]:
    """递归脱敏一组字段。"""
    return {str(k): redact_value(str(k), v) for k, v in fields.items()}


_STANDARD_ATTRS = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message",
    "asctime",
    "event",
    "fields",
    "taskName",
}


def _record_fields(record: logging.LogRecord) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    raw = getattr(record, "fields", None)
    if isinstance(raw, Mapping):
        fields.update(raw)
    for key, value in record.__dict__.items():
        if key in _STANDARD_ATTRS or key.startswith("_"):
            continue
        fields.setdefault(key, value)
    return fields


class JsonLineFormatter(logging.Formatter):
    """每行一个 JSON 对象。"""

    def __init__(self, *, service: str = "bridge") -> None:
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime(
                "%Y-%m-%dT%H:%M:%S", time.localtime(record.created)
            )
            + f".{int(record.msecs):03d}",
            "level": record.levelname,
            "logger": record.name,
            "event": getattr(record, "event", None) or record.getMessage(),
            "service": self.service,
        }
        fields = redact_fields(_record_fields(record))
        if fields:
            payload["fields"] = fields
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


class HumanFormatter(logging.Formatter):
    """``HH:MM:SS LEVEL logger event key=value …``"""

    _RESET = "\x1b[0m"
    _COLORS = {
        "DEBUG": "\x1b[2m",
        "INFO": "\x1b[36m",
        "WARNING": "\x1b[33m",
        "ERROR": "\x1b[31m",
        "CRITICAL": "\x1b[35m",
    }

    def __init__(self, *, color: bool = False) -> None:
        super().__init__()
        self.color = color

    def format(self, record: logging.LogRecord) -> str:
        ts = time.strftime("%H:%M:%S", time.localtime(record.created))
        level = record.levelname
        if self.color:
            level = f"{self._COLORS.get(level, '')}{level}{self._RESET}"
        event = getattr(record, "event", None) or record.getMessage()
        parts = [f"{ts} {level:<8} {record.name:<18} {event}"]
        fields = redact_fields(_record_fields(record))
        if fields:
            rendered = " ".join(_format_kv(k, v) for k, v in fields.items())
            parts.append(rendered)
        line = "  ".join(parts)
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


def _format_kv(key: str, value: Any) -> str:
    if isinstance(value, str):
        if not value or any(ch.isspace() for ch in value) or "=" in value:
            return f'{key}="{value}"'
        return f"{key}={value}"
    if isinstance(value, bool):
        return f"{key}={'true' if value else 'false'}"
    if value is None:
        return f"{key}=null"
    if isinstance(value, float):
        return f"{key}={value:.3f}".rstrip("0").rstrip(".")
    if isinstance(value, (dict, list)):
        return f"{key}={json.dumps(value, ensure_ascii=False, separators=(',', ':'))}"
    return f"{key}={value}"


#: 进程级单例，避免重复添加 handler。
_configured = False


def setup_logging(
    config: LogConfig,
    *,
    base_dir: str | Path | None = None,
    console: bool = True,
    stream: TextIO | None = None,
    force: bool = False,
) -> logging.Logger:
    """初始化 ``bridge`` logger。

    文件日志写到 ``<base_dir>/<config.dir>/bridge.log``（``base_dir`` 默认
    当前工作目录）；目录不可写时降级为仅控制台并输出 WARNING。
    """
    global _configured
    logger = logging.getLogger("bridge")
    if _configured and not force:
        return logger
    if force:
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            try:
                handler.close()
            except Exception:  # pragma: no cover
                pass
    logger.setLevel(getattr(logging, config.level.upper(), logging.INFO))
    logger.propagate = False
    logger.handlers.clear()

    if console:
        stream = stream or sys.stderr
        is_tty = bool(getattr(stream, "isatty", lambda: False)())
        handler = logging.StreamHandler(stream)
        handler.setFormatter(HumanFormatter(color=is_tty))
        logger.addHandler(handler)

    log_dir = Path(config.dir)
    if not log_dir.is_absolute():
        log_dir = Path(base_dir or Path.cwd()) / log_dir
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_dir / "bridge.log",
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
    except OSError as exc:  # pragma: no cover - 权限问题
        logger.warning("log file disabled: %s", exc, extra={"event": "log.file_disabled"})
    else:
        file_handler.setFormatter(JsonLineFormatter())
        logger.addHandler(file_handler)

    _configured = True
    return logger


def get_logger(name: str = "bridge") -> logging.Logger:
    """取得 ``bridge`` 命名空间下的 logger。"""
    if name == "bridge" or name.startswith("bridge."):
        return logging.getLogger(name)
    return logging.getLogger(f"bridge.{name}")


def log_event(
    logger: logging.Logger,
    event: str,
    *,
    level: int = logging.INFO,
    **fields: Any,
) -> None:
    """结构化记录一条事件。"""
    logger.log(level, event, extra={"event": event, "fields": fields})


def reset_logging() -> None:
    """测试辅助：移除并关闭所有 handler。"""
    global _configured
    logger = logging.getLogger("bridge")
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:  # pragma: no cover
            pass
    _configured = False
