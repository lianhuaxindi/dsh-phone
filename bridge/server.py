"""Bridge WSS 服务端：生命周期、TLS、连接管理与配对引导。"""

from __future__ import annotations

import asyncio
import logging
import ssl
import time
from pathlib import Path
from typing import Any

from websockets.asyncio.server import Server, ServerConnection, serve

from . import __version__
from .asr import StreamingASR, create_recognizer
from .auth import AuthManager, DeviceStore
from .certs import (
    cert_fingerprint,
    derive_server_id,
    load_or_create_cert,
    pair_uri,
    server_ssl_context,
)
from .config import BridgeConfig
from .connection import Connection, DeviceSession
from .dsh import DshClient, create_dsh_client
from .history import HistoryStore
from .logging_setup import setup_logging
from .qr import print_qr

logger = logging.getLogger("bridge.server")

#: 子协议名（PROTOCOL §1；RFC 6455 token 语法，见 D-015）。
SUBPROTOCOL = "dsphone.v1"
#: WSS 端点路径（PROTOCOL §1）。
WS_PATH = "/ws"
#: 文本帧上限。
MAX_MESSAGE_BYTES = 64 * 1024


def _select_subprotocol(_protocol: Any, subprotocols: list[str]) -> str | None:
    """协商子协议（D-015）。

    客户端没带或带错子协议时也允许握手完成，随后由 :class:`Connection`
    以 4400 关闭；这样错误路径与「缺少子协议」路径行为一致，且便于客户端
    收到关闭码而不是握手阶段的 HTTP 400。
    """
    if SUBPROTOCOL in subprotocols:
        return SUBPROTOCOL
    return None


class BridgeServer:
    """桥接服务端。

    使用方法::

        server = BridgeServer(config)
        await server.start()      # 绑定端口、打印配对二维码
        await server.serve_forever()
        await server.stop()

    ``announce=False`` 时不在 stdout 打印配对信息（测试用）。
    """

    def __init__(self, config: BridgeConfig, *, announce: bool = True) -> None:
        self.config = config
        self.announce = announce
        self.history: HistoryStore | None = None
        self.auth: AuthManager | None = None
        self.dsh: DshClient | None = None
        self.asr: StreamingASR | None = None
        self.fingerprint = ""
        self.server_id = ""
        self.host = config.server.host
        self.port = int(config.server.port)
        self._device_store: DeviceStore | None = None
        self._server: Server | None = None
        self._connections: set[Connection] = set()
        self._device_sessions: dict[str, DeviceSession] = {}
        self._stop_event: asyncio.Event | None = None
        self._pair_refresh_task: asyncio.Task[None] | None = None
        self._started = False

    # -- 属性 -------------------------------------------------------------

    @property
    def started(self) -> bool:
        return self._started

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"

    @property
    def pair_code(self) -> str:
        if self.auth is None:
            raise RuntimeError("server not started")
        return self.auth.ensure_pair_code().code

    def pairing_uri(self) -> str:
        """当前配对 URI（二维码内容）。"""
        if self.auth is None:
            raise RuntimeError("server not started")
        return pair_uri(
            host=self.host,
            port=self.port,
            fingerprint=self.fingerprint,
            code=self.auth.ensure_pair_code().code,
            server_id=self.server_id,
        )

    def get_device_session(self, device_id: str) -> DeviceSession:
        session = self._device_sessions.get(device_id)
        if session is None:
            session = DeviceSession(device_id, self)
            self._device_sessions[device_id] = session
        return session

    # -- 生命周期 ---------------------------------------------------------

    async def start(self) -> "BridgeServer":
        if self._started:
            return self
        loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()

        # 目录
        state_dir = self.config.state_dir
        state_dir.mkdir(parents=True, exist_ok=True)
        self.config.log_dir.mkdir(parents=True, exist_ok=True)
        self.config.cert_file.parent.mkdir(parents=True, exist_ok=True)
        self.config.dsh_home.mkdir(parents=True, exist_ok=True)

        # 证书 / 指纹 / serverId
        cert = load_or_create_cert(
            self.host, self.config.cert_file, self.config.key_file
        )
        self.fingerprint = cert_fingerprint(cert)
        self.server_id = derive_server_id(self.fingerprint)
        ssl_context = server_ssl_context(self.config.cert_file, self.config.key_file)

        # 持久层
        self.history = HistoryStore(self.config.history_db)
        self._device_store = DeviceStore(state_dir / "devices.sqlite3")
        self.auth = AuthManager(
            self.config.security,
            self._device_store,
            server_id=self.server_id,
        )

        # DSH / ASR
        self.dsh = create_dsh_client(self.config.dsh, home=self.config.dsh_home)
        await self.dsh.start()
        self.asr = create_recognizer(self.config.asr)

        # WSS
        try:
            self._server = await serve(
                self._handle_connection,
                self.host,
                self.port,
                ssl=ssl_context,
                subprotocols=[SUBPROTOCOL],
                select_subprotocol=_select_subprotocol,
                compression=None,
                max_size=MAX_MESSAGE_BYTES,
                ping_interval=None,
                ping_timeout=None,
                logger=logging.getLogger("bridge.ws"),
            )
        except OSError as exc:
            await self._shutdown_resources()
            raise RuntimeError(f"无法监听 {self.host}:{self.port}: {exc}") from exc

        sockets = self._server.sockets or []
        if sockets:
            self.port = int(sockets[0].getsockname()[1])
        self._started = True
        self._pair_refresh_task = asyncio.create_task(
            self._pair_refresh_loop(), name="pair-refresh"
        )
        logger.info(
            "server.started",
            extra={
                "event": "server.started",
                "fields": {
                    "host": self.host,
                    "port": self.port,
                    "serverId": self.server_id,
                    "fingerprint": self.fingerprint,
                    "bridge": __version__,
                },
            },
        )
        if self.announce:
            self.print_pairing()
        return self

    async def serve_forever(self) -> None:
        """阻塞直到 :meth:`stop` 被调用。"""
        if self._stop_event is None:
            raise RuntimeError("server not started")
        await self._stop_event.wait()

    def request_stop(self) -> None:
        """从信号处理器等非协程上下文请求停止（线程安全）。"""
        if self._stop_event is not None:
            self._stop_event.set()

    async def stop(self) -> None:
        if not self._started:
            return
        logger.info("server.stopping", extra={"event": "server.stopping"})
        self._started = False
        if self._stop_event is not None:
            self._stop_event.set()
        if self._pair_refresh_task is not None:
            self._pair_refresh_task.cancel()
            self._pair_refresh_task = None
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:  # pragma: no cover - 防御
                logger.exception("server.close_failed", extra={"event": "server.close_failed"})
            self._server = None
        for conn in list(self._connections):
            await conn._close(int(1000), "server shutdown")
        self._connections.clear()
        for session in list(self._device_sessions.values()):
            await session.aclose()
        self._device_sessions.clear()
        await self._shutdown_resources()
        logger.info("server.stopped", extra={"event": "server.stopped"})

    async def _shutdown_resources(self) -> None:
        if self.asr is not None:
            try:
                await self.asr.aclose()
            except Exception:  # pragma: no cover - 防御
                logger.exception("asr.close_failed", extra={"event": "asr.close_failed"})
            self.asr = None
        if self.dsh is not None:
            try:
                await self.dsh.aclose()
            except Exception:  # pragma: no cover - 防御
                logger.exception("dsh.close_failed", extra={"event": "dsh.close_failed"})
            self.dsh = None
        if self.auth is not None:
            try:
                self.auth.close()
            except Exception:  # pragma: no cover - 防御
                pass
            self.auth = None
        if self._device_store is not None:
            try:
                self._device_store.close()
            except Exception:  # pragma: no cover - 防御
                pass
            self._device_store = None
        if self.history is not None:
            try:
                self.history.close()
            except Exception:  # pragma: no cover - 防御
                pass
            self.history = None

    # -- 连接处理 ---------------------------------------------------------

    async def _handle_connection(self, ws: ServerConnection) -> None:
        request = getattr(ws, "request", None)
        path = getattr(request, "path", None)
        if path != WS_PATH:
            await ws.close(int(4400), "invalid path")
            return
        if ws.subprotocol != SUBPROTOCOL:
            await ws.close(int(4400), f"subprotocol {SUBPROTOCOL} required")
            return
        if len(self._connections) >= int(self.config.server.max_connections):
            logger.warning(
                "connection.rejected",
                extra={"event": "connection.rejected", "fields": {"reason": "maxConnections"}},
            )
            await ws.close(int(1011), "too many connections")
            return
        connection = Connection(ws, self)
        self._connections.add(connection)
        try:
            await connection.run()
        finally:
            self._connections.discard(connection)

    # -- 配对引导 ---------------------------------------------------------

    def print_pairing(self, *, code: str | None = None) -> None:
        """在终端打印配对信息与二维码（编码不支持时降级为 ASCII 文案）。"""
        if self.auth is None:
            return
        pair_code = self.auth.ensure_pair_code()
        uri = pair_uri(
            host=self.host,
            port=self.port,
            fingerprint=self.fingerprint,
            code=code if code is not None else pair_code.code,
            server_id=self.server_id,
        )
        ttl = self.config.security.pair_code_ttl_sec
        try:
            print("\n".join(self._pairing_lines(pair_code.code, ttl)))
            print_qr(uri, indent="  ")
            print(f"  配对 URI: {uri}")
            print()
        except (UnicodeEncodeError, OSError):  # pragma: no cover - 极端终端
            try:
                print("\n".join(self._pairing_lines(pair_code.code, ttl, ascii_only=True)))
                print(f"  Pairing URI: {uri}")
                print()
            except OSError:
                logger.warning("pairing.print_failed", extra={"event": "pairing.print_failed"})

    def _pairing_lines(self, code: str, ttl: int, *, ascii_only: bool = False) -> list[str]:
        """配对信息横幅（只用 ASCII 边框，避免 GBK 控制台崩溃）。"""
        if ascii_only:
            return [
                "",
                "=" * 60,
                "  DSH Phone Bridge",
                f"  serverId : {self.server_id}",
                f"  listen   : {self.address}",
                f"  fp       : {self.fingerprint[:32]}",
                f"             {self.fingerprint[32:]}",
                f"  pairCode : {code} (valid {ttl}s)",
                "=" * 60,
                "",
            ]
        return [
            "",
            "=" * 60,
            "  DSH Phone Bridge",
            f"  serverId : {self.server_id}",
            f"  监听     : {self.address}",
            f"  指纹     : {self.fingerprint[:32]}",
            f"             {self.fingerprint[32:]}",
            f"  配对码   : {code}（{ttl} 秒内有效）",
            "=" * 60,
            "",
        ]

    async def _pair_refresh_loop(self) -> None:
        ttl = max(15, int(self.config.security.pair_code_ttl_sec))
        while True:
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=float(ttl))  # type: ignore[union-attr]
                return
            except asyncio.TimeoutError:
                pass
            if self._stop_event is not None and self._stop_event.is_set():
                return
            if self.auth is None or not self._started:
                return
            self.auth.refresh_pair_code()
            if self.announce:
                try:
                    print("\n[配对码已轮换]")
                    self.print_pairing()
                except (UnicodeEncodeError, OSError):  # pragma: no cover - 极端终端
                    logger.warning(
                        "pairing.rotate_print_failed",
                        extra={"event": "pairing.rotate_print_failed"},
                    )


async def serve_bridge(config: BridgeConfig, *, announce: bool = True) -> BridgeServer:
    """便捷函数：启动一个 :class:`BridgeServer` 并返回。"""
    server = BridgeServer(config, announce=announce)
    return await server.start()
