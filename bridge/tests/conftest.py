"""pytest 共享夹具与测试客户端工具。"""

from __future__ import annotations

import asyncio
import contextlib
import json
import secrets
import shutil
import ssl
import time
import uuid
from pathlib import Path
from typing import Any, AsyncIterator, Iterator

import pytest
import pytest_asyncio
from websockets.asyncio.client import connect

from bridge.auth import b64d, b64e, compute_hmac_b64
from bridge.config import (
    AsrConfig,
    AudioConfig,
    BridgeConfig,
    DshConfig,
    LogConfig,
    SecurityConfig,
    ServerConfig,
    TlsConfig,
)
from bridge.server import BridgeServer

DEVICE_ID = "dev-abc123"
DEVICE_NAME = "pytest phone"

# 沙箱下系统临时目录不可写，测试临时目录放在 workspace 内（bridge/state/ 已被
# .gitignore 忽略，且 rmtree 会尽量清理）。
_REPO_ROOT = Path(__file__).resolve().parents[2]
_TMP_ROOT = _REPO_ROOT / "bridge" / "state" / "pytest"


@pytest.fixture()
def workdir() -> Iterator[Path]:
    _TMP_ROOT.mkdir(parents=True, exist_ok=True)
    path = _TMP_ROOT / uuid.uuid4().hex
    path.mkdir()
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def make_config(base_dir: Path) -> BridgeConfig:
    """构造测试配置：port=0（由系统分配），所有状态文件落在 base_dir 下。"""
    return BridgeConfig(
        server=ServerConfig(host="127.0.0.1", port=0, max_connections=8),
        tls=TlsConfig(
            cert_file="state/certs/bridge.crt",
            key_file="state/certs/bridge.key",
        ),
        dsh=DshConfig(enabled=False, home="state/dsh_home"),
        asr=AsrConfig(
            mode="fake",
            lang="zh",
            partial_rate_per_sec=20,
            silence_ms=500,
        ),
        audio=AudioConfig(sample_rate=16000, max_stream_seconds=120),
        security=SecurityConfig(
            pair_code_ttl_sec=120,
            max_pair_failures=5,
            lock_minutes=5,
            auth_skew_sec=120,
        ),
        log=LogConfig(level="INFO", dir="state/logs"),
        base_dir=base_dir,
    )


@pytest_asyncio.fixture
async def server(workdir: Path) -> AsyncIterator[BridgeServer]:
    srv = BridgeServer(make_config(workdir), announce=False)
    await srv.start()
    try:
        yield srv
    finally:
        await srv.stop()


def client_ssl_context() -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


@contextlib.asynccontextmanager
async def raw_connection(
    server: BridgeServer, *, subprotocols: list[str] | None = None
) -> AsyncIterator[Any]:
    uri = f"wss://127.0.0.1:{server.port}/ws"
    async with connect(
        uri,
        ssl=client_ssl_context(),
        subprotocols=["dsphone.v1"] if subprotocols is None else subprotocols,
        open_timeout=5,
    ) as ws:
        yield ws


class TestClient:
    """带 C→S 序号管理的测试客户端。"""

    def __init__(self, ws: Any) -> None:
        self.ws = ws
        self._seq = 0
        self.events: list[dict[str, Any]] = []

    def next_seq(self) -> int:
        self._seq += 1
        return self._seq

    async def send(self, mtype: str, **fields: Any) -> int:
        seq = self.next_seq()
        await self.ws.send(json.dumps({"type": mtype, "seq": seq, **fields}))
        return seq

    async def recv(self, timeout: float = 5.0) -> dict[str, Any]:
        raw = await asyncio.wait_for(self.ws.recv(), timeout)
        assert isinstance(raw, str), f"收到非文本帧: {raw!r}"
        return json.loads(raw)

    async def expect(self, mtype: str, timeout: float = 5.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                received = [m.get("type") for m in self.events]
                raise TimeoutError(f"等待 {mtype} 超时（已收到: {received}）")
            msg = await self.recv(timeout=remaining)
            if msg.get("type") == mtype:
                return msg
            self.events.append(msg)

    async def collect(self, mtype: str, timeout: float = 5.0) -> list[dict[str, Any]]:
        """等待直到出现 mtype，返回此前收集到的所有同类型之外的消息。"""
        await self.expect(mtype, timeout=timeout)
        return self.events


@contextlib.asynccontextmanager
async def client(server: BridgeServer) -> AsyncIterator[TestClient]:
    async with raw_connection(server) as ws:
        yield TestClient(ws)


async def hello(
    tc: TestClient, *, device_id: str = DEVICE_ID, version: int = 1
) -> dict[str, Any]:
    await tc.send(
        "hello",
        v=version,
        deviceId=device_id,
        app="dsh-phone-tests",
        os="pytest",
        ts=int(time.time()),
    )
    return await tc.expect("hello.ok")


async def pair(tc: TestClient, server: BridgeServer, *, device_id: str = DEVICE_ID) -> bytes:
    """hello 后执行配对，返回原始 deviceToken 字节。"""
    await hello(tc, device_id=device_id)
    await tc.send(
        "pair.request",
        deviceId=device_id,
        code=server.pair_code,
        deviceName=DEVICE_NAME,
        pubKey=None,
    )
    msg = await tc.expect("pair.ok")
    token = b64d(msg["deviceToken"])
    assert len(token) == 32
    return token


async def auth(
    tc: TestClient,
    token: bytes,
    *,
    device_id: str = DEVICE_ID,
    ts: int | None = None,
    nonce: str | None = None,
) -> dict[str, Any]:
    now = int(time.time()) if ts is None else int(ts)
    nonce_b64 = b64e(secrets.token_bytes(16)) if nonce is None else nonce
    await tc.send(
        "auth",
        deviceId=device_id,
        ts=now,
        nonce=nonce_b64,
        hmac=compute_hmac_b64(token, device_id, now, nonce_b64),
        v=1,
    )
    return await tc.expect("auth.ok")


async def pair_and_auth(
    tc: TestClient,
    server: BridgeServer,
    *,
    device_id: str = DEVICE_ID,
    resync: bool = True,
) -> bytes:
    token = await pair(tc, server, device_id=device_id)
    await auth(tc, token, device_id=device_id)
    if resync:
        await tc.send("resync", lastSeq=0)
        await tc.expect("resync.ok")
    return token


async def create_session(
    tc: TestClient, *, title: str = "测试会话", timeout: float = 5.0
) -> dict[str, Any]:
    await tc.send("session.create", title=title)
    msg = await tc.expect("session.create.ok", timeout=timeout)
    return msg["session"]


__all__ = [
    "DEVICE_ID",
    "DEVICE_NAME",
    "TestClient",
    "auth",
    "client",
    "client_ssl_context",
    "create_session",
    "hello",
    "make_config",
    "pair",
    "pair_and_auth",
    "raw_connection",
    "server",
]
