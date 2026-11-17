"""``python -m bridge`` 入口：加载配置、启动 WSS 桥接服务、处理 Ctrl+C。"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import signal
import sys
from pathlib import Path

from . import __version__
from .config import BridgeConfig, ConfigError, load_config
from .logging_setup import setup_logging
from .server import BridgeServer


def build_parser() -> argparse.ArgumentParser:
    default_config = Path(__file__).resolve().parent / "config.example.toml"
    parser = argparse.ArgumentParser(
        prog="python -m bridge",
        description="DSH Phone Bridge：局域网 WSS 桥接服务",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=default_config,
        help=f"配置文件路径（默认 {default_config}）",
    )
    parser.add_argument("--host", default=None, help="覆盖监听地址（默认取配置文件）")
    parser.add_argument("--port", type=int, default=None, help="覆盖监听端口")
    parser.add_argument(
        "--run-for-seconds",
        type=float,
        default=None,
        help="冒烟测试用：运行指定秒数后自动优雅退出",
    )
    parser.add_argument("--version", action="version", version=f"bridge {__version__}")
    return parser


def apply_overrides(
    config: BridgeConfig, *, host: str | None, port: int | None
) -> BridgeConfig:
    server = config.server
    changed = False
    if host is not None:
        server = dataclasses.replace(server, host=host)
        changed = True
    if port is not None:
        if not 1 <= int(port) <= 65535:
            raise ConfigError(f"port 超出范围: {port}")
        server = dataclasses.replace(server, port=int(port))
        changed = True
    if not changed:
        return config
    return dataclasses.replace(config, server=server)


def install_signal_handlers(loop: asyncio.AbstractEventLoop, server: BridgeServer) -> None:
    """安装 SIGINT/SIGTERM 处理：请求停止（Windows 无 add_signal_handler）。"""

    def _handler(signum: int, frame: object) -> None:  # noqa: ARG001
        loop.call_soon_threadsafe(server.request_stop)

    for signame in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, signame, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError):  # pragma: no cover - 非主线程等情况
            pass


async def run_server(config: BridgeConfig, *, run_for_seconds: float | None) -> int:
    server = BridgeServer(config)
    await server.start()
    install_signal_handlers(asyncio.get_running_loop(), server)
    try:
        if run_for_seconds is not None and run_for_seconds > 0:
            try:
                await asyncio.wait_for(server.serve_forever(), timeout=run_for_seconds)
            except asyncio.TimeoutError:
                pass
        else:
            await server.serve_forever()
    finally:
        print("正在关闭桥接服务 ...")
        await server.stop()
        print("桥接服务已退出。")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.config)
        config = apply_overrides(config, host=args.host, port=args.port)
    except ConfigError as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 2
    setup_logging(config.log, base_dir=config.base_dir, console=True)
    try:
        return asyncio.run(
            run_server(config, run_for_seconds=args.run_for_seconds)
        )
    except KeyboardInterrupt:  # pragma: no cover - 兜底
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
