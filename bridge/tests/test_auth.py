"""认证与配对单元测试：HMAC、时间偏移、nonce 重放、锁定、撤销、TTL。"""

from __future__ import annotations

import hashlib
import secrets

import pytest

from bridge.auth import (
    AuthFailed,
    AuthManager,
    DeviceStore,
    PairLocked,
    PairingFailed,
    TokenCipher,
    TokenCipherError,
    b64d,
    b64e,
    load_or_create_master_key,
)
from bridge.config import SecurityConfig

DEVICE_ID = "dev-abc123"
VECTOR_TOKEN = bytes.fromhex(
    "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff"
)
VECTOR_TS = 1710000000
VECTOR_NONCE = "bm9uY2UxMjM0NTY3OA=="
VECTOR_HMAC_B64 = "gYfqlKpD9PLEml5FzwSMmtd93hlDbF/JrA7lGcDBSjo="


@pytest.fixture()
def store(workdir):
    store = DeviceStore(workdir / "devices.sqlite3")
    try:
        yield store
    finally:
        store.close()


@pytest.fixture()
def manager(store, workdir):
    security = SecurityConfig(
        pair_code_ttl_sec=120,
        max_pair_failures=5,
        lock_minutes=5,
        auth_skew_sec=120,
    )
    auth = AuthManager(security, store, server_id="srv-test000000")
    try:
        yield auth
    finally:
        auth.close()


class TestPairing:
    def test_pair_success_returns_32_byte_token(self, manager, store) -> None:
        code = manager.ensure_pair_code().code
        result = manager.pair(DEVICE_ID, code, device_name="phone")
        assert len(result.token_bytes) == 32
        assert b64d(result.token_b64) == result.token_bytes
        assert result.server_id == "srv-test000000"
        device = store.get(DEVICE_ID)
        assert device is not None
        assert device.token_hash == hashlib.sha256(result.token_bytes).hexdigest()
        # D-016：落库的是密文，不是明文 token；解密后可取回原始 token。
        assert device.token_enc != result.token_bytes
        assert store.decrypt_token(device) == result.token_bytes
        assert device.device_name == "phone"

    def test_pair_code_is_single_use(self, manager) -> None:
        code = manager.ensure_pair_code().code
        manager.pair(DEVICE_ID, code)
        with pytest.raises(PairingFailed) as excinfo:
            manager.pair(DEVICE_ID, code)
        assert excinfo.value.attempts_left == 4

    def test_wrong_code_attempts_left(self, manager) -> None:
        manager.ensure_pair_code()
        for expected in (4, 3, 2, 1):
            with pytest.raises(PairingFailed) as excinfo:
                manager.pair(DEVICE_ID, "000000" if expected != 1 else "999999")
            assert excinfo.value.attempts_left == expected

    def test_five_failures_lock_device(self, manager) -> None:
        manager.ensure_pair_code()
        for _ in range(4):
            with pytest.raises(PairingFailed):
                manager.pair(DEVICE_ID, "000000")
        with pytest.raises(PairLocked) as excinfo:
            manager.pair(DEVICE_ID, "000000")
        assert excinfo.value.attempts_left == 0
        assert excinfo.value.close_code == 4410
        # 锁定后即使验证码正确也不放行
        code = manager.ensure_pair_code().code
        with pytest.raises(PairLocked):
            manager.pair(DEVICE_ID, code)

    def test_pair_code_expires(self, store) -> None:
        clock = [1000.0]
        auth = AuthManager(
            SecurityConfig(pair_code_ttl_sec=120),
            store,
            clock=lambda: clock[0],
        )
        try:
            code = auth.ensure_pair_code().code
            assert auth.ensure_pair_code().code == code
            clock[0] += 121
            new_code = auth.refresh_pair_code().code
            assert new_code != code
            with pytest.raises(PairingFailed) as excinfo:
                auth.pair(DEVICE_ID, code)
            assert excinfo.value.attempts_left == 4
        finally:
            auth.close()


class TestAuthentication:
    def _prepared(self, store) -> AuthManager:
        store.upsert(DEVICE_ID, VECTOR_TOKEN, device_name="phone")
        return AuthManager(
            SecurityConfig(auth_skew_sec=120),
            store,
            clock=lambda: float(VECTOR_TS),
        )

    def test_protocol_vector_authenticates(self, store) -> None:
        auth = self._prepared(store)
        try:
            result = auth.authenticate(
                DEVICE_ID, VECTOR_TS, VECTOR_NONCE, VECTOR_HMAC_B64
            )
            assert result.device_id == DEVICE_ID
            assert result.device is not None
            assert store.decrypt_token(result.device) == VECTOR_TOKEN
        finally:
            auth.close()

    def test_time_skew_rejected(self, store) -> None:
        auth = self._prepared(store)
        try:
            with pytest.raises(AuthFailed):
                auth.authenticate(
                    DEVICE_ID, VECTOR_TS - 121, VECTOR_NONCE, VECTOR_HMAC_B64
                )
        finally:
            auth.close()

    def test_nonce_replay_rejected(self, store) -> None:
        auth = self._prepared(store)
        try:
            auth.authenticate(DEVICE_ID, VECTOR_TS, VECTOR_NONCE, VECTOR_HMAC_B64)
            with pytest.raises(AuthFailed):
                auth.authenticate(
                    DEVICE_ID, VECTOR_TS, VECTOR_NONCE, VECTOR_HMAC_B64
                )
        finally:
            auth.close()

    def test_bad_hmac_rejected(self, store) -> None:
        auth = self._prepared(store)
        try:
            with pytest.raises(AuthFailed):
                auth.authenticate(
                    DEVICE_ID,
                    VECTOR_TS,
                    b64e(b"x" * 16),
                    VECTOR_HMAC_B64,
                )
        finally:
            auth.close()

    def test_unknown_device_rejected(self, store) -> None:
        auth = self._prepared(store)
        try:
            with pytest.raises(AuthFailed):
                auth.authenticate(
                    "dev-unknown", VECTOR_TS, VECTOR_NONCE, VECTOR_HMAC_B64
                )
        finally:
            auth.close()

    def test_revoked_device_rejected(self, store) -> None:
        auth = self._prepared(store)
        try:
            assert store.revoke(DEVICE_ID) is True
            with pytest.raises(AuthFailed):
                auth.authenticate(
                    DEVICE_ID, VECTOR_TS, VECTOR_NONCE, VECTOR_HMAC_B64
                )
        finally:
            auth.close()

    def test_device_name_flow(self, manager, store) -> None:
        code = manager.ensure_pair_code().code
        manager.pair(DEVICE_ID, code, device_name="Pixel")
        device = store.get(DEVICE_ID)
        assert device is not None and device.device_name == "Pixel"


class TestTokenStorage:
    """D-016：sha256 索引 + AES-256-GCM 密文，不得存明文 token。"""

    def test_master_key_created(self, manager, workdir) -> None:
        key_path = workdir / "master.key"
        assert key_path.exists()
        assert len(key_path.read_bytes()) == 32

    def test_plaintext_token_not_in_db_files(self, manager, workdir) -> None:
        code = manager.ensure_pair_code().code
        result = manager.pair(DEVICE_ID, code, device_name="phone")
        blobs = b"".join(
            path.read_bytes()
            for path in sorted(workdir.glob("devices.sqlite3*"))
            if path.is_file()
        )
        assert result.token_bytes not in blobs
        assert b64e(result.token_bytes).encode() not in blobs
        assert result.token_bytes.hex().encode() not in blobs

    def test_token_cipher_roundtrip_and_tamper(self) -> None:
        cipher = TokenCipher(secrets.token_bytes(32))
        token = secrets.token_bytes(32)
        blob = cipher.encrypt(token)
        assert blob != token
        assert len(blob) == 12 + len(token) + 16
        assert cipher.decrypt(blob) == token
        tampered = bytearray(blob)
        tampered[-1] ^= 0x01
        with pytest.raises(TokenCipherError):
            cipher.decrypt(bytes(tampered))

    def test_wrong_master_key_cannot_decrypt(self, manager, store) -> None:
        code = manager.ensure_pair_code().code
        manager.pair(DEVICE_ID, code)
        device = store.get(DEVICE_ID)
        assert device is not None
        with pytest.raises(TokenCipherError):
            TokenCipher(secrets.token_bytes(32)).decrypt(device.token_enc)

    def test_corrupted_stored_token_rejected(self, manager, store) -> None:
        code = manager.ensure_pair_code().code
        manager.pair(DEVICE_ID, code)
        with store._lock:
            store._conn.execute(
                "UPDATE devices SET token_enc = ? WHERE device_id = ?",
                (b"\x00" * 40, DEVICE_ID),
            )
            store._conn.commit()
        auth = AuthManager(
            SecurityConfig(auth_skew_sec=120),
            store,
            clock=lambda: float(VECTOR_TS),
        )
        with pytest.raises(AuthFailed):
            auth.authenticate(DEVICE_ID, VECTOR_TS, VECTOR_NONCE, VECTOR_HMAC_B64)

    def test_load_or_create_master_key_is_stable(self, workdir) -> None:
        path = workdir / "state" / "master.key"
        first = load_or_create_master_key(path)
        second = load_or_create_master_key(path)
        assert len(first) == 32
        assert first == second

