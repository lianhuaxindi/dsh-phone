"""配对与认证：配对码 TTL/失败锁定、设备令牌存储、HMAC 校验、nonce 防重放。

安全模型（PROTOCOL §5–§6，D-008/D-016）：

- 配对码：6 位数字，TTL 120s；同一 deviceId / 来源失败 5 次锁定 5 分钟。
- 配对成功下发 ``deviceToken``（32 字节随机数，base64 传输）。
- 服务端持久化两份（D-016）：``sha256(token)``（设备索引/吊销校验）与
  AES-256-GCM 密文（密钥为每安装独立的 ``master.key``，权限 0600，因 §6.1
  校验 HMAC 必须能取回原始 token）。原始 token 严禁写入日志/诊断/导出。
- 认证：``HMAC-SHA256(key=deviceToken 原始字节, msg=canonical)``，其中
  ``canonical = "DSPHONE1\\n{deviceId}\\n{ts}\\n{nonce}"``。
- 时间偏差 ≤120s；nonce 按 deviceId 缓存 10 分钟防重放。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac as hmac_mod
import logging
import os
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .config import SecurityConfig
from .protocol.errors import CloseCode, ErrorCode, ProtocolError

logger = logging.getLogger("bridge.auth")

__all__ = [
    "Device",
    "PairCode",
    "PairResult",
    "AuthResult",
    "DeviceStore",
    "TokenCipher",
    "TokenCipherError",
    "load_or_create_master_key",
    "NonceCache",
    "PairingLock",
    "PairingFailed",
    "PairLocked",
    "AuthFailed",
    "AuthManager",
    "hmac_canonical",
    "compute_hmac_b64",
    "verify_hmac",
    "generate_pair_code",
]

#: 认证 canonical 前缀（PROTOCOL §6.1）。
AUTH_PREFIX = "DSPHONE1"
#: nonce 重放窗口（秒）。
NONCE_WINDOW_SEC = 600
#: deviceToken 字节数。
TOKEN_BYTES = 32
#: 每安装独立的 AES-256-GCM 主密钥文件名（D-016；位于 state 目录）。
MASTER_KEY_FILENAME = "master.key"
#: 主密钥长度。
MASTER_KEY_BYTES = 32
#: AES-GCM nonce 长度。
GCM_NONCE_BYTES = 12
#: 设备令牌密文 AAD（防止密文被跨用途复用）。
TOKEN_AAD = b"dsphone:device-token:v1"


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class AuthFailed(ProtocolError):
    """认证失败（1001，不可重试；客户端应重新配对）。"""

    def __init__(self, message: str, *, need_pair: bool = True) -> None:
        super().__init__(
            message,
            code=ErrorCode.AUTH_FAILED,
            retryable=False,
            close_code=CloseCode.AUTH_FAILED,
            need_pair=need_pair,
        )


class PairingFailed(ProtocolError):
    """配对码错误/过期（1002，可重试，带 attemptsLeft）。"""

    def __init__(
        self,
        message: str,
        *,
        attempts_left: int,
        retryable: bool = True,
        close_code: int | None = None,
    ) -> None:
        super().__init__(
            message,
            code=ErrorCode.PAIR_CODE,
            retryable=retryable,
            attempts_left=attempts_left,
            close_code=close_code,
        )


class PairLocked(PairingFailed):
    """失败次数超限被锁定（关闭码 4410）。"""

    def __init__(self, message: str = "配对失败次数过多，已锁定") -> None:
        super().__init__(
            message,
            attempts_left=0,
            retryable=False,
            close_code=int(CloseCode.PAIR_LOCKED),
        )


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Device:
    """已配对设备（``token_enc`` 为 AES-256-GCM 密文，D-016）。"""

    device_id: str
    token_hash: str
    token_enc: bytes
    device_name: str = ""
    created_at: int = 0
    last_seen_at: int | None = None
    revoked_at: int | None = None

    @property
    def revoked(self) -> bool:
        return self.revoked_at is not None


@dataclass(frozen=True)
class PairCode:
    """当前有效配对码。"""

    code: str
    issued_at: int
    expires_at: int

    def expired(self, now: int | None = None) -> bool:
        now = int(time.time()) if now is None else now
        return now >= self.expires_at

    def remaining_sec(self, now: int | None = None) -> int:
        now = int(time.time()) if now is None else now
        return max(0, self.expires_at - now)


@dataclass(frozen=True)
class PairResult:
    """配对成功结果。"""

    device: Device
    token_bytes: bytes
    token_b64: str
    server_id: str = ""


@dataclass(frozen=True)
class AuthResult:
    """认证成功结果。"""

    device_id: str
    device_name: str = ""
    device: Device | None = None


# ---------------------------------------------------------------------------
# 令牌与 HMAC
# ---------------------------------------------------------------------------


def generate_pair_code(rng: secrets.SystemRandom | None = None) -> str:
    """生成 6 位数字配对码（保留前导 0）。"""
    rng = rng or secrets.SystemRandom()
    return f"{rng.randrange(0, 1_000_000):06d}"


def token_hash(token: bytes) -> str:
    return hashlib.sha256(token).hexdigest()


class TokenCipherError(ValueError):
    """设备令牌密文损坏或主密钥不匹配。"""


def _chmod_600(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover - Windows 上 ACL 可能拒绝
        pass


def load_or_create_master_key(path: str | Path) -> bytes:
    """加载或创建 32B 主密钥（权限 0600，D-016）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        key = path.read_bytes()
        if len(key) != MASTER_KEY_BYTES:
            raise TokenCipherError(f"主密钥长度非法（{len(key)}B）: {path}")
        _chmod_600(path)
        return key
    key = secrets.token_bytes(MASTER_KEY_BYTES)
    # Windows 上 os.open 默认文本模式，会把 0x0a 翻译成 0x0d0a 污染密钥。
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(str(path), flags, 0o600)
    except FileExistsError:  # pragma: no cover - 并发首次启动
        return load_or_create_master_key(path)
    try:
        os.write(fd, key)
    finally:
        os.close(fd)
    _chmod_600(path)
    return key


class TokenCipher:
    """AES-256-GCM 设备令牌加解密（D-016）。

    密文格式：``nonce(12B) || ciphertext+tag``。主密钥不落库、不写日志。
    """

    def __init__(self, key: bytes) -> None:
        if len(key) != MASTER_KEY_BYTES:
            raise TokenCipherError("AES-256-GCM 主密钥必须为 32 字节")
        self._aead = AESGCM(bytes(key))

    def encrypt(self, token: bytes) -> bytes:
        nonce = secrets.token_bytes(GCM_NONCE_BYTES)
        return nonce + self._aead.encrypt(nonce, bytes(token), TOKEN_AAD)

    def decrypt(self, blob: bytes) -> bytes:
        if not blob or len(blob) <= GCM_NONCE_BYTES:
            raise TokenCipherError("令牌密文为空或过短")
        nonce, ciphertext = blob[:GCM_NONCE_BYTES], blob[GCM_NONCE_BYTES:]
        try:
            return self._aead.decrypt(nonce, ciphertext, TOKEN_AAD)
        except InvalidTag as exc:
            raise TokenCipherError("令牌密文校验失败") from exc


def b64e(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def b64d(data: str) -> bytes:
    """严格 base64 解码（非 str/非法编码抛 ValueError）。"""
    if not isinstance(data, str) or not data:
        raise ValueError("base64 值不能为空")
    try:
        return base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"非法 base64: {exc}") from exc


def hmac_canonical(device_id: str, ts: int, nonce: str) -> bytes:
    """认证 canonical 串（UTF-8，``\\n`` 分隔，无末尾换行）。"""
    return f"{AUTH_PREFIX}\n{device_id}\n{int(ts)}\n{nonce}".encode("utf-8")


def compute_hmac(token: bytes, device_id: str, ts: int, nonce: str) -> bytes:
    """计算 HMAC-SHA256（key=原始 deviceToken）。"""
    return hmac_mod.new(token, hmac_canonical(device_id, ts, nonce), hashlib.sha256).digest()


def compute_hmac_b64(token: bytes, device_id: str, ts: int, nonce: str) -> str:
    return b64e(compute_hmac(token, device_id, ts, nonce))


def verify_hmac(token: bytes, device_id: str, ts: int, nonce: str, hmac_b64: str) -> bool:
    """校验客户端 HMAC（常量时间比较）。"""
    try:
        provided = b64d(hmac_b64)
    except ValueError:
        return False
    expected = compute_hmac(token, device_id, ts, nonce)
    return hmac_mod.compare_digest(provided, expected)


# ---------------------------------------------------------------------------
# 设备存储（sqlite）
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    device_id     TEXT PRIMARY KEY,
    token_hash    TEXT NOT NULL,
    token_enc     BLOB NOT NULL,
    device_name   TEXT NOT NULL DEFAULT '',
    created_at    INTEGER NOT NULL,
    last_seen_at  INTEGER,
    revoked_at    INTEGER
);
"""


class DeviceStore:
    """已配对设备表（sqlite，线程安全）。

    token 落库为 ``sha256(token)`` + AES-256-GCM 密文（D-016）；主密钥默认是
    db 同目录的 ``master.key``（0600，首次自动生成）。
    """

    def __init__(
        self,
        path: str | Path,
        *,
        master_key: bytes | None = None,
        master_key_path: str | Path | None = None,
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.master_key_path = (
            Path(master_key_path)
            if master_key_path is not None
            else self.path.parent / MASTER_KEY_FILENAME
        )
        key = master_key if master_key is not None else load_or_create_master_key(
            self.master_key_path
        )
        self.cipher = TokenCipher(key)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(_SCHEMA)
            columns = {
                str(row["name"])
                for row in self._conn.execute("PRAGMA table_info(devices)").fetchall()
            }
            if "token_enc" not in columns:
                # M1 早期 schema（token_blob 明文）无法解密：重置设备表，需重新配对。
                logger.warning(
                    "device_store.migrated",
                    extra={
                        "event": "device_store.migrated",
                        "fields": {"reason": "D-016 token 加密迁移"},
                    },
                )
                self._conn.execute("DROP TABLE devices")
                self._conn.executescript(_SCHEMA)
            self._conn.commit()

    # -- 内部 -------------------------------------------------------------

    def _row_to_device(self, row: sqlite3.Row) -> Device:
        blob = row["token_enc"]
        return Device(
            device_id=row["device_id"],
            token_hash=row["token_hash"],
            token_enc=bytes(blob) if blob is not None else b"",
            device_name=row["device_name"] or "",
            created_at=int(row["created_at"] or 0),
            last_seen_at=(int(row["last_seen_at"]) if row["last_seen_at"] is not None else None),
            revoked_at=(int(row["revoked_at"]) if row["revoked_at"] is not None else None),
        )

    def decrypt_token(self, device: Device) -> bytes:
        """解密设备令牌并校验 hash（失败抛 :class:`TokenCipherError`）。"""
        token = self.cipher.decrypt(device.token_enc)
        if token_hash(token) != device.token_hash:
            raise TokenCipherError("令牌哈希与密文不匹配")
        return token

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:  # pragma: no cover
                pass

    # -- 写入 -------------------------------------------------------------

    def upsert(
        self,
        device_id: str,
        token: bytes,
        *,
        device_name: str = "",
        created_at: int | None = None,
    ) -> Device:
        """新增或覆盖设备令牌（重复配对时刷新）。"""
        now = int(time.time()) if created_at is None else int(created_at)
        token = bytes(token)
        encrypted = self.cipher.encrypt(token)
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO devices
                    (device_id, token_hash, token_enc, device_name, created_at,
                     last_seen_at, revoked_at)
                VALUES (?, ?, ?, ?, ?, NULL, NULL)
                ON CONFLICT(device_id) DO UPDATE SET
                    token_hash = excluded.token_hash,
                    token_enc = excluded.token_enc,
                    device_name = excluded.device_name,
                    created_at = excluded.created_at,
                    last_seen_at = NULL,
                    revoked_at = NULL
                """,
                (device_id, token_hash(token), encrypted, device_name or "", now),
            )
            self._conn.commit()
        return self.get(device_id)  # type: ignore[return-value]

    def touch_last_seen(self, device_id: str, ts: int | None = None) -> None:
        now = int(time.time()) if ts is None else int(ts)
        with self._lock:
            self._conn.execute(
                "UPDATE devices SET last_seen_at = ? WHERE device_id = ?",
                (now, device_id),
            )
            self._conn.commit()

    def revoke(self, device_id: str, ts: int | None = None) -> bool:
        """撤销设备；返回是否命中了有效设备。"""
        now = int(time.time()) if ts is None else int(ts)
        with self._lock:
            cur = self._conn.execute(
                "UPDATE devices SET revoked_at = ? "
                "WHERE device_id = ? AND revoked_at IS NULL",
                (now, device_id),
            )
            self._conn.commit()
            return cur.rowcount > 0

    # -- 读取 -------------------------------------------------------------

    def get(self, device_id: str) -> Device | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM devices WHERE device_id = ?", (device_id,)
            ).fetchone()
        return self._row_to_device(row) if row else None

    def is_paired(self, device_id: str) -> bool:
        """是否已配对且未撤销。"""
        device = self.get(device_id)
        return bool(device and not device.revoked)

    def list(self) -> list[Device]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM devices ORDER BY created_at DESC, device_id"
            ).fetchall()
        return [self._row_to_device(row) for row in rows]

    def delete(self, device_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM devices WHERE device_id = ?", (device_id,))
            self._conn.commit()
            return cur.rowcount > 0


# ---------------------------------------------------------------------------
# nonce 缓存
# ---------------------------------------------------------------------------


class NonceCache:
    """按 deviceId 缓存最近 nonce，防重放（窗口 10 分钟）。"""

    def __init__(self, window_sec: float = NONCE_WINDOW_SEC, max_entries: int = 1024) -> None:
        self.window_sec = float(window_sec)
        self.max_entries = int(max_entries)
        self._seen: dict[str, dict[str, float]] = {}

    def observe(self, device_id: str, nonce: str, now: float | None = None) -> bool:
        """登记 nonce；首次出现返回 True，窗口内重复返回 False。"""
        now = time.time() if now is None else float(now)
        bucket = self._seen.setdefault(device_id, {})
        for key in [k for k, ts in bucket.items() if now - ts > self.window_sec]:
            bucket.pop(key, None)
        if nonce in bucket:
            return False
        bucket[nonce] = now
        self._prune(now)
        return True

    def _prune(self, now: float) -> None:
        total = sum(len(b) for b in self._seen.values())
        if total <= self.max_entries:
            return
        # 丢弃最旧的整个 bucket 中的过期项；若仍超限则清空最旧 bucket
        for device_id in sorted(self._seen, key=lambda d: min(self._seen[d].values() or [0])):
            bucket = self._seen[device_id]
            for key in [k for k, ts in bucket.items() if now - ts > self.window_sec]:
                bucket.pop(key, None)
            if sum(len(b) for b in self._seen.values()) <= self.max_entries:
                break
        if sum(len(b) for b in self._seen.values()) > self.max_entries:
            oldest = min(self._seen, key=lambda d: min(self._seen[d].values() or [0]))
            self._seen.pop(oldest, None)

    def clear(self) -> None:
        self._seen.clear()


# ---------------------------------------------------------------------------
# 配对失败锁定
# ---------------------------------------------------------------------------


class PairingLock:
    """按 key（deviceId / 来源 IP）统计失败次数并锁定。"""

    def __init__(self, max_failures: int = 5, lock_seconds: float = 300.0) -> None:
        self.max_failures = int(max_failures)
        self.lock_seconds = float(lock_seconds)
        self._failures: dict[str, int] = {}
        self._locked_until: dict[str, float] = {}

    def locked_until(self, key: str) -> float:
        return self._locked_until.get(key, 0.0)

    def is_locked(self, key: str, now: float | None = None) -> bool:
        now = time.time() if now is None else float(now)
        until = self._locked_until.get(key)
        if until is None:
            return False
        if now >= until:
            self._locked_until.pop(key, None)
            self._failures.pop(key, None)
            return False
        return True

    def check(self, *keys: str, now: float | None = None) -> None:
        """任一 key 被锁定则抛 :class:`PairLocked`。"""
        for key in keys:
            if key and self.is_locked(key, now):
                raise PairLocked()

    def register_failure(self, *keys: str, now: float | None = None) -> int:
        """记一次失败；返回剩余尝试次数（触发锁定时为 0）。"""
        now = time.time() if now is None else float(now)
        remaining = self.max_failures
        for key in keys:
            if not key:
                continue
            count = self._failures.get(key, 0) + 1
            self._failures[key] = count
            if count >= self.max_failures:
                self._locked_until[key] = now + self.lock_seconds
            remaining = min(remaining, max(0, self.max_failures - count))
        self._prune(now)
        return remaining

    def reset(self, *keys: str) -> None:
        for key in keys:
            self._failures.pop(key, None)
            self._locked_until.pop(key, None)

    def attempts_left(self, *keys: str) -> int:
        worst = 0
        for key in keys:
            worst = max(worst, self._failures.get(key, 0))
        return max(0, self.max_failures - worst)

    def _prune(self, now: float) -> None:
        if len(self._failures) + len(self._locked_until) <= 512:
            return
        for key in [k for k, until in self._locked_until.items() if now >= until]:
            self._locked_until.pop(key, None)
            self._failures.pop(key, None)

    def clear(self) -> None:
        self._failures.clear()
        self._locked_until.clear()


# ---------------------------------------------------------------------------
# AuthManager
# ---------------------------------------------------------------------------


class AuthManager:
    """配对码 + 设备令牌 + 认证校验的门面。

    ``clock`` 可注入（测试用），返回 Unix 秒（可为 float）。
    """

    def __init__(
        self,
        config: SecurityConfig,
        store: DeviceStore,
        *,
        clock: Callable[[], float] = time.time,
        server_id: str = "",
        nonce_cache: NonceCache | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.server_id = server_id
        self._clock = clock
        self.nonce_cache = nonce_cache or NonceCache()
        self.pairing_lock = PairingLock(
            max_failures=config.max_pair_failures,
            lock_seconds=config.lock_minutes * 60,
        )
        self._pair_code: PairCode | None = None

    # -- 时间 -------------------------------------------------------------

    def now(self) -> int:
        return int(self._clock())

    def now_float(self) -> float:
        return float(self._clock())

    # -- 配对码 -----------------------------------------------------------

    def current_pair_code(self) -> PairCode | None:
        """当前配对码（可能已过期，过期返回 None）。"""
        code = self._pair_code
        if code is None:
            return None
        if code.expired(self.now()):
            return None
        return code

    def ensure_pair_code(self) -> PairCode:
        """返回有效配对码；不存在/过期则生成新的。"""
        code = self.current_pair_code()
        if code is not None:
            return code
        return self.refresh_pair_code()

    def refresh_pair_code(self) -> PairCode:
        """强制生成新的配对码并开始新的 TTL 计时。"""
        now = self.now()
        code = generate_pair_code()
        pair_code = PairCode(
            code=code,
            issued_at=now,
            expires_at=now + int(self.config.pair_code_ttl_sec),
        )
        self._pair_code = pair_code
        logger.info(
            "pair.code_refreshed",
            extra={"event": "pair.code_refreshed", "fields": {"ttl": self.config.pair_code_ttl_sec}},
        )
        return pair_code

    def pair_code_expires_at(self) -> int:
        return self._pair_code.expires_at if self._pair_code else self.now()

    # -- 配对 -------------------------------------------------------------

    def is_device_paired(self, device_id: str) -> bool:
        return self.store.is_paired(device_id)

    def pair(
        self,
        device_id: str,
        code: str,
        *,
        device_name: str = "",
        source: str = "",
        now: float | None = None,
    ) -> PairResult:
        """校验配对码并登记设备，返回原始 token。

        Raises:
            PairLocked: 已因失败超限被锁定（close 4410）。
            PairingFailed: 配对码错误/过期（1002，带 attemptsLeft）。
        """
        now_f = self.now_float() if now is None else float(now)
        now_i = int(now_f)
        lock_keys = [_lock_key_device(device_id), _lock_key_source(source)]
        self.pairing_lock.check(*lock_keys, now=now_f)

        current = self.current_pair_code()
        valid = current is not None and not current.expired(now_i) and self._codes_equal(
            code, current.code
        )
        if not valid:
            left = self.pairing_lock.register_failure(*lock_keys, now=now_f)
            if left <= 0 or any(self.pairing_lock.is_locked(k, now_f) for k in lock_keys):
                raise PairLocked()
            raise PairingFailed("配对码错误或已过期", attempts_left=left)

        self.pairing_lock.reset(*lock_keys)
        token = secrets.token_bytes(TOKEN_BYTES)
        device = self.store.upsert(
            device_id, token, device_name=device_name or "", created_at=now_i
        )
        self.store.touch_last_seen(device_id, now_i)
        # 配对成功后旧配对码作废（一次性使用）
        self._pair_code = None
        logger.info(
            "pair.ok",
            extra={
                "event": "pair.ok",
                "fields": {"deviceId": device_id, "deviceName": device.device_name},
            },
        )
        return PairResult(
            device=device,
            token_bytes=token,
            token_b64=b64e(token),
            server_id=self.server_id,
        )

    @staticmethod
    def _codes_equal(provided: Any, expected: str) -> bool:
        if not isinstance(provided, str):
            return False
        provided = provided.strip()
        if len(provided) != len(expected):
            return False
        return hmac_mod.compare_digest(provided.encode(), expected.encode())

    # -- 认证 -------------------------------------------------------------

    def authenticate(
        self,
        device_id: str,
        ts: Any,
        nonce: Any,
        hmac_b64: Any,
        *,
        source: str = "",
    ) -> AuthResult:
        """校验 auth 消息；失败抛 :class:`AuthFailed`（1001）。

        校验顺序：设备存在/未撤销 → 时间偏差 → nonce 格式 → HMAC → nonce 防重放。
        """
        if not isinstance(device_id, str) or not device_id:
            raise AuthFailed("缺少 deviceId")
        device = self.store.get(device_id)
        if device is None:
            raise AuthFailed("设备未配对，请先配对")
        if device.revoked:
            raise AuthFailed("设备已被撤销，请重新配对")
        if isinstance(ts, bool) or not isinstance(ts, int):
            raise AuthFailed("非法 ts")
        now = self.now()
        if abs(now - int(ts)) > int(self.config.auth_skew_sec):
            raise AuthFailed("时间戳超出允许偏差，请校准设备时间")
        if not isinstance(nonce, str) or not nonce:
            raise AuthFailed("缺少 nonce")
        try:
            raw_nonce = b64d(nonce)
        except ValueError:
            raise AuthFailed("非法 nonce") from None
        if not raw_nonce:
            raise AuthFailed("非法 nonce")
        if not isinstance(hmac_b64, str):
            raise AuthFailed("HMAC 校验失败")
        try:
            token = self.store.decrypt_token(device)
        except TokenCipherError:
            raise AuthFailed("设备令牌不可用，请重新配对") from None
        if not verify_hmac(token, device_id, int(ts), nonce, hmac_b64):
            raise AuthFailed("HMAC 校验失败")
        if not self.nonce_cache.observe(device_id, nonce, self.now_float()):
            raise AuthFailed("nonce 重放")
        self.store.touch_last_seen(device_id, now)
        logger.info(
            "auth.ok",
            extra={"event": "auth.ok", "fields": {"deviceId": device_id}},
        )
        return AuthResult(
            device_id=device_id, device_name=device.device_name, device=device
        )

    # -- 设备管理 ---------------------------------------------------------

    def revoke(self, device_id: str) -> bool:
        ok = self.store.revoke(device_id, self.now())
        if ok:
            logger.info(
                "device.revoked",
                extra={"event": "device.revoked", "fields": {"deviceId": device_id}},
            )
        return ok

    def list_devices(self) -> list[Device]:
        return self.store.list()

    def close(self) -> None:
        self.store.close()


def _lock_key_device(device_id: str) -> str:
    return f"dev:{device_id}"


def _lock_key_source(source: str) -> str:
    return f"src:{source}" if source else ""
