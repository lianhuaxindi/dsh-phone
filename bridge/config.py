"""Bridge 配置：TOML 加载、校验、路径解析。

配置文件名与键名都遵循 ``docs/ARCHITECTURE.md`` 的 ``config.example.toml``
（键为 camelCase）。**所有相对路径都相对配置文件所在目录解析**，因此
``bridge/config.example.toml`` 里的 ``state/certs/bridge.crt`` 落在
``bridge/state/certs/bridge.crt``（已在 .gitignore 中忽略）。
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping


class ConfigError(ValueError):
    """配置文件缺失、语法错误或取值非法。"""


# ---------------------------------------------------------------------------
# 各配置段
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ServerConfig:
    """[server] WebSocket 监听参数。"""

    host: str = "127.0.0.1"
    port: int = 8765
    max_connections: int = 4
    bind_all: bool = False

    @property
    def address(self) -> str:
        """``host:port`` 展示用。"""
        if ":" in self.host:  # IPv6
            return f"[{self.host}]:{self.port}"
        return f"{self.host}:{self.port}"


@dataclass(frozen=True)
class TlsConfig:
    """[tls] 自签证书位置（相对配置目录）。"""

    cert_file: str = "state/certs/bridge.crt"
    key_file: str = "state/certs/bridge.key"


@dataclass(frozen=True)
class DshConfig:
    """[dsh] 真实 DSH SDK 接入参数（D-028）。"""

    enabled: bool = True
    home: str = "state/dsh_home"
    provider: str = "deepseek-official"
    model: str = "deepseek-flash"
    reasoning_effort: str = "high"
    max_tokens: int = 384000
    context_window_tokens: int = 1000000
    runtime_exe: str = ""
    api_key: str = ""
    base_url: str = "https://api.deepseek.com"


@dataclass(frozen=True)
class AsrConfig:
    """[asr] 语音识别后端与行为参数。"""

    mode: str = "fake"
    lang: str = "zh"
    partial_rate_per_sec: int = 5
    silence_ms: int = 500
    models_dir: str = ""


@dataclass(frozen=True)
class AudioConfig:
    """[audio] 音频流参数。"""

    sample_rate: int = 16000
    max_stream_seconds: int = 120


@dataclass(frozen=True)
class SecurityConfig:
    """[security] 配对码、认证与锁定策略。"""

    pair_code_ttl_sec: int = 120
    max_pair_failures: int = 5
    lock_minutes: int = 5
    auth_skew_sec: int = 120


@dataclass(frozen=True)
class LogConfig:
    """[log] 日志级别与目录。"""

    level: str = "INFO"
    dir: str = "state/logs"


# ---------------------------------------------------------------------------
# 汇总配置
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BridgeConfig:
    """Bridge 全量配置。

    ``base_dir`` 是相对路径的解析基准（默认取配置文件父目录）。
    """

    server: ServerConfig = field(default_factory=ServerConfig)
    tls: TlsConfig = field(default_factory=TlsConfig)
    dsh: DshConfig = field(default_factory=DshConfig)
    asr: AsrConfig = field(default_factory=AsrConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)
    log: LogConfig = field(default_factory=LogConfig)
    base_dir: Path = field(default_factory=Path.cwd)
    source_path: Path | None = None

    # -- 路径工具 ---------------------------------------------------------

    def path(self, value: str | Path) -> Path:
        """把配置里的路径解析为绝对路径（相对 ``base_dir``）。"""
        p = Path(value).expanduser()
        if not p.is_absolute():
            p = Path(self.base_dir) / p
        return p

    @property
    def cert_file(self) -> Path:
        return self.path(self.tls.cert_file)

    @property
    def key_file(self) -> Path:
        return self.path(self.tls.key_file)

    @property
    def dsh_home(self) -> Path:
        return self.path(self.dsh.home)

    @property
    def log_dir(self) -> Path:
        return self.path(self.log.dir)

    @property
    def state_dir(self) -> Path:
        """``base_dir/state``（证书、日志、数据库的公共父目录）。"""
        return Path(self.base_dir) / "state"

    @property
    def history_db(self) -> Path:
        return self.state_dir / "history.sqlite3"

    def as_dict(self) -> dict[str, Any]:
        """脱敏前的配置快照（诊断日志用；不含证书私钥内容）。"""
        return {
            "server": {"host": self.server.host, "port": self.server.port,
                       "maxConnections": self.server.max_connections,
                       "bindAll": self.server.bind_all},
            "tls": {"certFile": self.tls.cert_file, "keyFile": self.tls.key_file},
            "dsh": {"enabled": self.dsh.enabled, "home": self.dsh.home,
                    "provider": self.dsh.provider, "model": self.dsh.model,
                    "reasoningEffort": self.dsh.reasoning_effort,
                    "maxTokens": self.dsh.max_tokens,
                    "contextWindowTokens": self.dsh.context_window_tokens,
                    "runtimeExe": self.dsh.runtime_exe,
                    "apiKey": "***" if self.dsh.api_key else "",
                    "baseUrl": self.dsh.base_url},
            "asr": {"mode": self.asr.mode, "lang": self.asr.lang,
                    "partialRatePerSec": self.asr.partial_rate_per_sec,
                    "silenceMs": self.asr.silence_ms, "modelsDir": self.asr.models_dir},
            "audio": {"sampleRate": self.audio.sample_rate,
                      "maxStreamSeconds": self.audio.max_stream_seconds},
            "security": {"pairCodeTtlSec": self.security.pair_code_ttl_sec,
                         "maxPairFailures": self.security.max_pair_failures,
                         "lockMinutes": self.security.lock_minutes,
                         "authSkewSec": self.security.auth_skew_sec},
            "log": {"level": self.log.level, "dir": self.log.dir},
        }


# ---------------------------------------------------------------------------
# 加载与校验
# ---------------------------------------------------------------------------

_TOP_LEVEL_SECTIONS = {
    "server",
    "tls",
    "dsh",
    "asr",
    "audio",
    "security",
    "log",
}

_ALLOWED_KEYS: dict[str, set[str]] = {
    "server": {"host", "port", "maxConnections", "bindAll"},
    "tls": {"certFile", "keyFile"},
    "dsh": {
        "enabled",
        "home",
        "provider",
        "model",
        "reasoningEffort",
        "maxTokens",
        "contextWindowTokens",
        "runtimeExe",
        "apiKey",
        "baseUrl",
    },
    "asr": {"mode", "lang", "partialRatePerSec", "silenceMs", "modelsDir"},
    "audio": {"sampleRate", "maxStreamSeconds"},
    "security": {
        "pairCodeTtlSec",
        "maxPairFailures",
        "lockMinutes",
        "authSkewSec",
    },
    "log": {"level", "dir"},
}

_ASR_MODES = {"fake", "sherpa"}
_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
_REASONING_EFFORTS = {"low", "high", "max"}
_ALLOWED_SAMPLE_RATES = {16000}  # V1 协议固定 16 kHz（640B/20ms/单声道/16bit）
_API_KEY_ENV_NAMES = ("DEEPSEEK_API_KEY", "DSH_PHONE_API_KEY")


def _section(data: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = data.get(name)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"[{name}] 必须是表（table），收到 {type(value).__name__}")
    unknown = set(value) - _ALLOWED_KEYS[name]
    if unknown:
        raise ConfigError(
            f"[{name}] 含未知键: {', '.join(sorted(unknown))}；"
            f"允许的键: {', '.join(sorted(_ALLOWED_KEYS[name]))}"
        )
    return value


def _require_type(section: str, key: str, value: Any, typ: type, default: Any) -> Any:
    if value is None:
        return default
    if typ is bool:
        if not isinstance(value, bool):
            raise ConfigError(f"[{section}].{key} 必须是布尔值，收到 {value!r}")
        return value
    if typ is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"[{section}].{key} 必须是整数，收到 {value!r}")
        return value
    if typ is str:
        if not isinstance(value, str):
            raise ConfigError(f"[{section}].{key} 必须是字符串，收到 {value!r}")
        return value
    raise AssertionError(typ)  # pragma: no cover


def _validate_host(host: str) -> str:
    host = host.strip()
    if not host:
        raise ConfigError("[server].host 不能为空")
    bare = host.strip("[]")
    if bare in {"0.0.0.0", "::", "*"}:
        raise ConfigError(
            "[server].host 不能是通配地址（0.0.0.0/::）；"
            "默认用 127.0.0.1，供局域网访问时请显式填写本机 IP"
        )
    return host


def _validate_port(port: int) -> int:
    if not 1 <= port <= 65535:
        raise ConfigError(f"[server].port 必须在 1..65535，收到 {port}")
    return port


def _validate_interface(section: str, key: str, value: str) -> str:
    value = value.strip()
    if not value:
        raise ConfigError(f"[{section}].{key} 不能为空")
    return value


def _resolve_api_key(value: str) -> str:
    """D-028：apiKey 留空时回退环境变量（DEEPSEEK_API_KEY → DSH_PHONE_API_KEY）。"""
    value = value.strip()
    if value:
        return value
    for env_name in _API_KEY_ENV_NAMES:
        env_value = os.environ.get(env_name, "").strip()
        if env_value:
            return env_value
    return ""


def load_config(path: str | Path) -> BridgeConfig:
    """加载并校验 TOML 配置文件。

    Raises:
        ConfigError: 文件不存在、TOML 语法错误、未知键或取值非法。
    """
    config_path = Path(path).expanduser()
    try:
        config_path = config_path.resolve()
    except OSError:  # pragma: no cover - 极端路径
        pass
    if not config_path.is_file():
        raise ConfigError(f"配置文件不存在: {config_path}")
    try:
        with config_path.open("rb") as fh:
            data = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"TOML 语法错误（{config_path}）: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"无法读取配置文件 {config_path}: {exc}") from exc

    unknown_sections = set(data) - _TOP_LEVEL_SECTIONS
    if unknown_sections:
        raise ConfigError(
            f"未知配置段: {', '.join(sorted(unknown_sections))}；"
            f"允许的段: {', '.join(sorted(_TOP_LEVEL_SECTIONS))}"
        )

    # [server]
    s = _section(data, "server")
    server = ServerConfig(
        host=_validate_host(
            _require_type("server", "host", s.get("host"), str, "127.0.0.1")
        ),
        port=_validate_port(
            _require_type("server", "port", s.get("port"), int, 8765)
        ),
        max_connections=_require_type(
            "server", "maxConnections", s.get("maxConnections"), int, 4
        ),
        bind_all=_require_type("server", "bindAll", s.get("bindAll"), bool, False),
    )
    if not 1 <= server.max_connections <= 64:
        raise ConfigError(
            f"[server].maxConnections 必须在 1..64，收到 {server.max_connections}"
        )

    # [tls]
    t = _section(data, "tls")
    tls = TlsConfig(
        cert_file=_validate_interface(
            "tls", "certFile", _require_type("tls", "certFile", t.get("certFile"), str,
                                             "state/certs/bridge.crt")
        ),
        key_file=_validate_interface(
            "tls", "keyFile", _require_type("tls", "keyFile", t.get("keyFile"), str,
                                            "state/certs/bridge.key")
        ),
    )

    # [dsh]
    d = _section(data, "dsh")
    dsh = DshConfig(
        enabled=_require_type("dsh", "enabled", d.get("enabled"), bool, True),
        home=_validate_interface(
            "dsh", "home", _require_type("dsh", "home", d.get("home"), str, "state/dsh_home")
        ),
        provider=_require_type(
            "dsh", "provider", d.get("provider"), str, "deepseek-official"
        ),
        model=_require_type("dsh", "model", d.get("model"), str, "deepseek-flash"),
        reasoning_effort=_require_type(
            "dsh", "reasoningEffort", d.get("reasoningEffort"), str, "high"
        ).strip().lower(),
        max_tokens=_require_type("dsh", "maxTokens", d.get("maxTokens"), int, 384000),
        context_window_tokens=_require_type(
            "dsh", "contextWindowTokens", d.get("contextWindowTokens"), int, 1000000
        ),
        runtime_exe=_require_type("dsh", "runtimeExe", d.get("runtimeExe"), str, ""),
        api_key=_resolve_api_key(
            _require_type("dsh", "apiKey", d.get("apiKey"), str, "")
        ),
        base_url=_require_type(
            "dsh", "baseUrl", d.get("baseUrl"), str, "https://api.deepseek.com"
        ),
    )
    if dsh.reasoning_effort not in _REASONING_EFFORTS:
        raise ConfigError(
            f"[dsh].reasoningEffort 必须是 {sorted(_REASONING_EFFORTS)} 之一，"
            f"收到 {dsh.reasoning_effort!r}"
        )
    if not 1 <= dsh.max_tokens <= 384000:
        raise ConfigError(f"[dsh].maxTokens 必须在 1..384000，收到 {dsh.max_tokens}")
    if not 1000 <= dsh.context_window_tokens <= 2000000:
        raise ConfigError(
            f"[dsh].contextWindowTokens 必须在 1000..2000000，"
            f"收到 {dsh.context_window_tokens}"
        )

    # [asr]
    a = _section(data, "asr")
    asr = AsrConfig(
        mode=_require_type("asr", "mode", a.get("mode"), str, "fake").strip().lower(),
        lang=_require_type("asr", "lang", a.get("lang"), str, "zh"),
        partial_rate_per_sec=_require_type(
            "asr", "partialRatePerSec", a.get("partialRatePerSec"), int, 5
        ),
        silence_ms=_require_type("asr", "silenceMs", a.get("silenceMs"), int, 500),
        models_dir=_require_type("asr", "modelsDir", a.get("modelsDir"), str, ""),
    )
    if asr.mode not in _ASR_MODES:
        raise ConfigError(f"[asr].mode 必须是 {sorted(_ASR_MODES)} 之一，收到 {asr.mode!r}")
    if not 1 <= asr.partial_rate_per_sec <= 20:
        raise ConfigError(
            f"[asr].partialRatePerSec 必须在 1..20，收到 {asr.partial_rate_per_sec}"
        )
    if not 100 <= asr.silence_ms <= 5000:
        raise ConfigError(f"[asr].silenceMs 必须在 100..5000，收到 {asr.silence_ms}")

    # [audio]
    au = _section(data, "audio")
    audio = AudioConfig(
        sample_rate=_require_type("audio", "sampleRate", au.get("sampleRate"), int, 16000),
        max_stream_seconds=_require_type(
            "audio", "maxStreamSeconds", au.get("maxStreamSeconds"), int, 120
        ),
    )
    if audio.sample_rate not in _ALLOWED_SAMPLE_RATES:
        raise ConfigError(
            f"[audio].sampleRate 在 V1 仅支持 16000，收到 {audio.sample_rate}"
        )
    if not 10 <= audio.max_stream_seconds <= 600:
        raise ConfigError(
            f"[audio].maxStreamSeconds 必须在 10..600，收到 {audio.max_stream_seconds}"
        )

    # [security]
    sec = _section(data, "security")
    security = SecurityConfig(
        pair_code_ttl_sec=_require_type(
            "security", "pairCodeTtlSec", sec.get("pairCodeTtlSec"), int, 120
        ),
        max_pair_failures=_require_type(
            "security", "maxPairFailures", sec.get("maxPairFailures"), int, 5
        ),
        lock_minutes=_require_type(
            "security", "lockMinutes", sec.get("lockMinutes"), int, 5
        ),
        auth_skew_sec=_require_type(
            "security", "authSkewSec", sec.get("authSkewSec"), int, 120
        ),
    )
    if not 30 <= security.pair_code_ttl_sec <= 3600:
        raise ConfigError(
            f"[security].pairCodeTtlSec 必须在 30..3600，收到 {security.pair_code_ttl_sec}"
        )
    if not 1 <= security.max_pair_failures <= 20:
        raise ConfigError(
            f"[security].maxPairFailures 必须在 1..20，收到 {security.max_pair_failures}"
        )
    if not 1 <= security.lock_minutes <= 1440:
        raise ConfigError(
            f"[security].lockMinutes 必须在 1..1440，收到 {security.lock_minutes}"
        )
    if not 10 <= security.auth_skew_sec <= 900:
        raise ConfigError(
            f"[security].authSkewSec 必须在 10..900，收到 {security.auth_skew_sec}"
        )

    # [log]
    lg = _section(data, "log")
    log = LogConfig(
        level=_require_type("log", "level", lg.get("level"), str, "INFO").strip().upper(),
        dir=_validate_interface(
            "log", "dir", _require_type("log", "dir", lg.get("dir"), str, "state/logs")
        ),
    )
    if log.level not in _LOG_LEVELS:
        raise ConfigError(f"[log].level 必须是 {sorted(_LOG_LEVELS)} 之一，收到 {log.level!r}")

    return BridgeConfig(
        server=server,
        tls=tls,
        dsh=dsh,
        asr=asr,
        audio=audio,
        security=security,
        log=log,
        base_dir=config_path.parent,
        source_path=config_path,
    )
