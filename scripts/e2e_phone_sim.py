"""E2E 手机模拟器：没有手机也能验证 DSH Phone Bridge 的完整 WSS 链路。

用纯 Python + ``websockets`` 扮演 Android App，按 ``docs/PROTOCOL.md`` v1 走一遍：

    hello → pair.request → pair.ok → auth → auth.ok → resync
      → session.create / session.list / session.resume / session.history
      → chat.send → chat.ack → assistant.delta* → assistant.done
      → audio.start → 20ms PCM 帧 → asr.partial* → audio.stop → asr.final
      → 边界断言（错误子协议、未认证请求、心跳）＋ 耗时统计

两种运行方式：

1. 默认：**进程内**启动真实 ``BridgeServer``（端口 0 由系统分配、自签 TLS、
   ``[dsh].enabled=false`` 走 fake DSH），状态文件全部落在 gitignored 的
   ``bridge/state/e2e_phone_sim/`` 下，不碰 ``~/.dsh``。
2. ``--url wss://127.0.0.1:8765/ws --pair-code 123456``：连接一个**已经运行**的
   Bridge（例如 ``python -m bridge`` 起的那个），脚本不做服务端生命周期管理。

语音默认使用 ``models/test_wavs/zh.wav``（16k mono 16-bit），按 20ms 节奏发帧。

用法::

    # CI / 无模型：fake ASR，最小依赖
    python scripts/e2e_phone_sim.py --asr fake

    # 本机真模型（需要 models/ 与 models/.pipdeps；脚本会自动把 .pipdeps 加进 sys.path）
    python scripts/e2e_phone_sim.py --asr sherpa

    # 机器可读结果（JSON 一行，前缀 E2E_PHONE_SIM_JSON）
    python scripts/e2e_phone_sim.py --asr fake --json

退出码：0 = PASS 或明确的 SKIP（例如 sherpa 模型缺失）；1 = FAIL；2 = 参数错误。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import secrets
import shutil
import ssl
import struct
import sys
import time
import wave
from pathlib import Path
from typing import Any, Iterator, Sequence

# CI/管道场景统一用 UTF-8 输出，避免 Windows 控制台代码页把中文摘要变成乱码；
# 交互式终端保留原生编码（Windows 控制台 API 本身能正确显示中文）。
if not sys.stdout.isatty():
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):  # pragma: no cover - 防御
            pass

# ---------------------------------------------------------------------------
# 路径引导（脚本必须能从仓库外任意 cwd 启动）
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
# sherpa 依赖（sherpa_onnx 等）由 scripts/install 或 models/.pipdeps 提供；
# 先放进 sys.path，再 import bridge.asr（真正 import sherpa_onnx 在后台线程里）。
_PIPDEPS = REPO_ROOT / "models" / ".pipdeps"
if _PIPDEPS.is_dir():
    sys.path.insert(0, str(_PIPDEPS))

import numpy as np  # noqa: E402
from websockets.asyncio.client import connect  # noqa: E402
from websockets.exceptions import ConnectionClosed  # noqa: E402

from bridge.asr.base import AsrUnavailable  # noqa: E402
from bridge.auth import b64d, b64e, compute_hmac_b64  # noqa: E402
from bridge.config import (  # noqa: E402
    AsrConfig,
    AudioConfig,
    BridgeConfig,
    DshConfig,
    LogConfig,
    SecurityConfig,
    ServerConfig,
    TlsConfig,
)
from bridge.logging_setup import setup_logging  # noqa: E402
from bridge.protocol.audio import FRAME_TYPE_PCM16  # noqa: E402
from bridge.server import BridgeServer  # noqa: E402

SUBPROTOCOL = "dsphone.v1"
DEFAULT_WAV = REPO_ROOT / "models" / "test_wavs" / "zh.wav"
DEFAULT_DEVICE_ID = "dev-e2e-phone-sim"
FRAME_MS = 20
FRAME_SAMPLES = 16000 * FRAME_MS // 1000  # 320
FRAME_BYTES = FRAME_SAMPLES * 2  # 640
SILENCE_RMS = 120.0
#: 真实录音（zh.wav）起始段有低电平呼吸/底噪，VAD 阈值只有 120；
#: 用更高阈值找语音起点，避免开头 0.5s 静音触发服务端 VAD 提前定稿。
SPEECH_RMS = 400.0


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def client_ssl_context() -> ssl.SSLContext:
    """局域网自签证书：客户端 pinning 由指纹断言完成，这里不做 CA 校验。"""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def close_code(exc: BaseException) -> int | None:
    """取 :class:`ConnectionClosed` 的关闭码（兼容 websockets 17.4）。"""
    if not isinstance(exc, ConnectionClosed):
        return None
    received = getattr(exc, "rcvd", None)
    if received is not None and getattr(received, "code", None) is not None:
        return int(received.code)
    code = getattr(exc, "code", None)
    return int(code) if code is not None else None


def ws_peer_fingerprint(ws: Any) -> str | None:
    """TLS 对端证书 SHA-256（小写 hex），非 TLS 返回 None。"""
    transport = getattr(ws, "transport", None)
    ssl_object = transport.get_extra_info("ssl_object") if transport is not None else None
    if ssl_object is None:
        return None
    try:
        der = ssl_object.getpeercert(binary_form=True)
    except Exception:  # pragma: no cover - 防御
        return None
    return hashlib.sha256(der).hexdigest() if der else None


def load_wav_pcm16(path: Path, sample_rate: int = 16000) -> np.ndarray:
    """读 wav → 单声道 16-bit（能重采样到目标采样率）。"""
    with wave.open(str(path), "rb") as wf:
        channels = wf.getnchannels()
        width = wf.getsampwidth()
        rate = wf.getframerate()
        raw = wf.readframes(wf.getnframes())
    if width != 2:
        raise ValueError(f"wav 必须是 16-bit PCM（当前 sampwidth={width}）")
    samples = np.frombuffer(raw, dtype="<i2").astype(np.float32)
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    if rate != sample_rate:
        n_out = int(round(samples.size * sample_rate / float(rate)))
        x_old = np.linspace(0.0, 1.0, num=samples.size, endpoint=False)
        x_new = np.linspace(0.0, 1.0, num=n_out, endpoint=False)
        samples = np.interp(x_new, x_old, samples)
    return np.clip(np.rint(samples), -32768, 32767).astype("<i2")


def trim_leading_silence(samples: np.ndarray, *, threshold: float = SPEECH_RMS) -> np.ndarray:
    """去掉开头静音帧，避免 VAD 在首个字之前就定稿空串。"""
    for offset in range(0, max(0, samples.size - FRAME_SAMPLES) + 1, FRAME_SAMPLES):
        frame = samples[offset : offset + FRAME_SAMPLES]
        if frame.size < FRAME_SAMPLES:
            break
        rms = float(np.sqrt(np.mean(frame.astype(np.float64) ** 2)))
        if rms >= threshold:
            return samples[offset:]
    return samples


def pcm_frames(samples: np.ndarray) -> Iterator[bytes]:
    """按 20ms/640B 切帧（不足补零）。"""
    for offset in range(0, samples.size, FRAME_SAMPLES):
        frame = samples[offset : offset + FRAME_SAMPLES]
        if frame.size < FRAME_SAMPLES:
            frame = np.pad(frame, (0, FRAME_SAMPLES - frame.size))
        yield frame.tobytes()


def build_audio_frame(aseq: int, ts_ms: int, pcm: bytes) -> bytes:
    """7 字节帧头 + 640B PCM16LE（PROTOCOL §10）。"""
    if len(pcm) != FRAME_BYTES:
        raise ValueError(f"PCM 帧必须是 {FRAME_BYTES}B，收到 {len(pcm)}B")
    return struct.pack(f"<BHI", FRAME_TYPE_PCM16, aseq & 0xFFFF, ts_ms & 0xFFFFFFFF) + pcm


# ---------------------------------------------------------------------------
# 记录/上报
# ---------------------------------------------------------------------------


class Reporter:
    """收集 PASS/FAIL/SKIP 检查项并输出人类可读摘要（可选 JSON）。"""

    def __init__(self) -> None:
        self.checks: list[dict[str, Any]] = []
        self.timings: dict[str, float] = {}
        self.meta: dict[str, Any] = {}

    def check(self, name: str, ok: bool, detail: str = "", *, ms: float | None = None) -> None:
        entry = {
            "name": name,
            "status": "PASS" if ok else "FAIL",
            "detail": detail,
        }
        if ms is not None:
            entry["ms"] = round(ms, 2)
        self.checks.append(entry)
        suffix = f" ({detail})" if detail else ""
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}{suffix}")

    def skip(self, name: str, detail: str) -> None:
        self.checks.append({"name": name, "status": "SKIP", "detail": detail})
        print(f"  [SKIP] {name} ({detail})")

    def timing(self, name: str, seconds: float) -> None:
        self.timings[name] = round(seconds * 1000.0, 2)

    def failed(self) -> bool:
        return any(item["status"] == "FAIL" for item in self.checks)

    def passed_count(self) -> int:
        return sum(1 for item in self.checks if item["status"] == "PASS")

    def emit(self, *, as_json: bool, result: str, skip_reason: str = "") -> None:
        if as_json:
            payload = {
                "result": result,
                "skipReason": skip_reason or None,
                "checks": self.checks,
                "timings": self.timings,
                "meta": self.meta,
                "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            }
            print("E2E_PHONE_SIM_JSON " + json.dumps(payload, ensure_ascii=False))


# ---------------------------------------------------------------------------
# 协议客户端（独立于 pytest conftest，可单独运行）
# ---------------------------------------------------------------------------


class PhoneClient:
    """扮演手机端的 WSS 客户端：管理 C→S seq、断言 S→C seq 单调。"""

    def __init__(self, ws: Any) -> None:
        self.ws = ws
        self.seq = 0
        self.last_s2c_seq = 0
        self.events: list[dict[str, Any]] = []

    async def send(self, mtype: str, **fields: Any) -> int:
        self.seq += 1
        payload = {"type": mtype, "seq": self.seq, **fields}
        await self.ws.send(json.dumps(payload, ensure_ascii=False))
        return self.seq

    async def recv(self, timeout: float = 10.0) -> dict[str, Any]:
        raw = await asyncio.wait_for(self.ws.recv(), timeout)
        if not isinstance(raw, str):
            raise AssertionError(f"期望文本 JSON 帧，收到二进制 {raw!r}")
        message = json.loads(raw)
        if not isinstance(message, dict) or "seq" not in message:
            raise AssertionError(f"非法 S→C 帧: {message!r}")
        seq = int(message["seq"])
        if seq <= self.last_s2c_seq:
            raise AssertionError(f"S→C seq 未单调递增: {seq} <= {self.last_s2c_seq}")
        self.last_s2c_seq = seq
        return message

    async def expect(self, mtype: str, timeout: float = 10.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                types = [m.get("type") for m in self.events]
                raise TimeoutError(f"等待 {mtype} 超时（已收到: {types}）")
            message = await self.recv(timeout=remaining)
            if message.get("type") == mtype:
                return message
            self.events.append(message)

    async def collect_until(
        self,
        wanted: Sequence[str],
        *,
        timeout: float = 10.0,
        stop_at: Sequence[str] = (),
    ) -> dict[str, list[dict[str, Any]]]:
        """读取消息直到收齐 wanted 中每种类型各一条（或命中 stop_at）。"""
        collected: dict[str, list[dict[str, Any]]] = {name: [] for name in wanted}
        deadline = time.monotonic() + timeout
        while any(not collected[name] for name in wanted):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                missing = [name for name in wanted if not collected[name]]
                raise TimeoutError(f"等待 {missing} 超时")
            message = await self.recv(timeout=remaining)
            mtype = str(message.get("type"))
            if mtype in collected:
                collected[mtype].append(message)
            elif mtype in stop_at and any(collected[name] for name in wanted):
                break
            else:
                self.events.append(message)
        return collected


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="e2e_phone_sim.py",
        description=(
            "没有手机也能验证 DSH Phone Bridge：用 Python 扮演 Android App，"
            "走完整 WSS 协议（hello→pair→auth→session/chat→audio→边界断言）。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  python scripts/e2e_phone_sim.py --asr fake\n"
            "  python scripts/e2e_phone_sim.py --asr sherpa\n"
            "  python scripts/e2e_phone_sim.py --url wss://127.0.0.1:8765/ws "
            "--pair-code 123456\n"
        ),
    )
    parser.add_argument(
        "--asr",
        choices=("fake", "sherpa"),
        default="fake",
        help="ASR 后端（默认 fake，CI 用；sherpa 需要 models/ 真模型）",
    )
    parser.add_argument(
        "--url",
        default=None,
        help="连接已有 Bridge（wss://host:port/ws）；省略则进程内启动一个临时 Bridge",
    )
    parser.add_argument(
        "--pair-code",
        default=None,
        help="配合 --url 使用：已运行 Bridge 的 6 位配对码；进程内模式自动生成",
    )
    parser.add_argument("--device-id", default=DEFAULT_DEVICE_ID, help="模拟手机 deviceId")
    parser.add_argument(
        "--wav",
        type=Path,
        default=DEFAULT_WAV,
        help=f"16k mono 16-bit wav 路径（默认 {DEFAULT_WAV}）",
    )
    parser.add_argument(
        "--audio-seconds",
        type=float,
        default=3.0,
        help="发送语音的秒数上限（默认 3.0；0 表示发完整文件）",
    )
    parser.add_argument(
        "--heartbeat-seconds",
        type=float,
        default=15.0,
        help="心跳观察窗口秒数（默认 15，验证客户端 15s 心跳规则；0 跳过）",
    )
    parser.add_argument(
        "--fingerprint",
        default=None,
        help="可选：期望的 TLS 证书 SHA-256 指纹（64 位小写 hex），用于外部 Bridge 校验",
    )
    parser.add_argument("--json", action="store_true", help="额外输出一行结构化 JSON 结果")
    parser.add_argument(
        "--no-network",
        action="store_true",
        help="离线声明：只允许本机 loopback，不访问任何外部网络（脚本本身从不外联）",
    )
    parser.add_argument("--verbose", action="store_true", help="打印 Bridge/ASR 的详细日志")
    return parser.parse_args(argv)


async def _wait_asr_ready(server: BridgeServer, timeout: float = 180.0) -> float:
    """等待 sherpa 模型就绪（fake 无 ensure_ready，直接返回 0）。"""
    recognizer = server.asr
    if recognizer is None:
        return 0.0
    ensure_ready = getattr(recognizer, "ensure_ready", None)
    if ensure_ready is None:
        return 0.0
    started = time.perf_counter()
    await ensure_ready(timeout=timeout)
    return time.perf_counter() - started


async def _start_audio(
    phone: "PhoneClient",
    session_id: str,
    expected_mode: str,
    args: argparse.Namespace,
) -> dict[str, Any] | None:
    """audio.start，返回 audio.ready；外部 sherpa 模式会重试 2002 直到模型加载完。"""
    deadline = time.monotonic() + (180.0 if (args.url and args.asr == "sherpa") else 15.0)
    while True:
        await phone.send(
            "audio.start",
            sampleRate=16000,
            channels=1,
            bitDepth=16,
            lang="zh",
            mode="ptt",
            sessionId=session_id,
        )
        try:
            message = await phone.recv(timeout=10.0)
        except TimeoutError:
            return None
        if message.get("type") == "audio.ready":
            return message
        phone.events.append(message)
        retryable_loading = (
            args.url
            and args.asr == "sherpa"
            and message.get("type") == "error"
            and int(message.get("code", 0)) == 2002
            and time.monotonic() < deadline
        )
        if not retryable_loading:
            return None
        print("    … sherpa 模型仍在加载，2s 后重试 audio.start")
        await asyncio.sleep(2.0)


async def run_e2e(args: argparse.Namespace) -> int:
    reporter = Reporter()
    server: BridgeServer | None = None
    started = time.perf_counter()
    reporter.meta.update(
        {
            "asr": args.asr,
            "mode": "external" if args.url else "in-process",
            "proto": "dsphone.v1",
        }
    )

    async def _fail(message: str) -> int:
        print(f"  [FAIL] {message}")
        reporter.check("fatal", False, message)
        reporter.emit(as_json=args.json, result="FAIL")
        print("\nE2E_PHONE_SIM: FAIL")
        return 1

    try:
        # -------------------------------------------------------------- 启动
        if args.url:
            uri = args.url
            pair_code = args.pair_code
            if not pair_code:
                print("错误：--url 模式必须同时提供 --pair-code", file=sys.stderr)
                return 2
            if args.no_network and not any(
                token in uri for token in ("127.0.0.1", "localhost", "[::1]")
            ):
                print(f"错误：--no-network 只允许 loopback，收到 {uri!r}", file=sys.stderr)
                return 2
            tls_fingerprint = None
            reporter.check("连接目标（外部 Bridge）", True, uri)
        else:
            run_dir = (
                REPO_ROOT
                / "bridge"
                / "state"
                / "e2e_phone_sim"
                / f"run-{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}"
            )
            run_dir.mkdir(parents=True)
            dsh_home = REPO_ROOT / "bridge" / "state" / "e2e_phone_home"
            dsh_home.mkdir(parents=True, exist_ok=True)
            config = BridgeConfig(
                server=ServerConfig(host="127.0.0.1", port=0, max_connections=4),
                tls=TlsConfig(
                    cert_file="state/certs/bridge.crt",
                    key_file="state/certs/bridge.key",
                ),
                dsh=DshConfig(enabled=False, home=str(dsh_home)),
                asr=AsrConfig(
                    mode=args.asr,
                    lang="zh",
                    partial_rate_per_sec=20,
                    silence_ms=500,
                    models_dir=str(REPO_ROOT / "models"),
                ),
                audio=AudioConfig(sample_rate=16000, max_stream_seconds=120),
                security=SecurityConfig(),
                log=LogConfig(level="INFO" if args.verbose else "WARNING", dir="state/logs"),
                base_dir=run_dir,
            )
            setup_logging(config.log, base_dir=run_dir, console=False, force=True)
            server = BridgeServer(config, announce=False)
            await server.start()
            port = server.port
            pair_code = server.pair_code
            uri = f"wss://127.0.0.1:{port}/ws"
            tls_fingerprint = server.fingerprint
            reporter.meta.update(
                {
                    "serverId": server.server_id,
                    "fingerprint": server.fingerprint,
                    "port": port,
                    "stateDir": str(run_dir),
                }
            )
            reporter.check("进程内启动 Bridge（端口 0 + 自签 TLS）", True, uri)
            if args.asr == "sherpa":
                try:
                    load_seconds = await _wait_asr_ready(server, timeout=180.0)
                except AsrUnavailable as exc:
                    reporter.skip("sherpa 模型就绪", f"不可用（SKIP）: {exc}")
                    reporter.emit(as_json=args.json, result="SKIP", skip_reason=str(exc))
                    print(f"\nE2E_PHONE_SIM: SKIP (sherpa unavailable: {exc})")
                    return 0
                reporter.check("sherpa 模型就绪", True, f"加载 {load_seconds * 1000:.0f}ms")
        assert pair_code

        # ------------------------------------------------- 主连接：配对与认证
        ssl_ctx = client_ssl_context()
        print(f"→ 连接 {uri}（子协议 {SUBPROTOCOL}）")
        async with connect(
            uri,
            ssl=ssl_ctx,
            subprotocols=[SUBPROTOCOL],
            open_timeout=10,
            max_size=1 << 20,
        ) as ws:
            phone = PhoneClient(ws)
            reporter.check("TLS/子协议握手", ws.subprotocol == SUBPROTOCOL, ws.subprotocol or "")
            if tls_fingerprint:
                actual = ws_peer_fingerprint(ws)
                reporter.check(
                    "TLS 指纹 pinning",
                    actual == tls_fingerprint,
                    f"SHA-256 {actual}",
                )
            elif args.fingerprint:
                reporter.check(
                    "TLS 指纹 pinning",
                    ws_peer_fingerprint(ws) == args.fingerprint.lower(),
                    f"SHA-256 {ws_peer_fingerprint(ws)}",
                )

            # hello
            hello_seq = await phone.send(
                "hello",
                v=1,
                deviceId=args.device_id,
                app="e2e-phone-sim",
                os="windows",
                ts=int(time.time()),
            )
            hello_ok = await phone.expect("hello.ok")
            reporter.check(
                "hello → hello.ok",
                hello_ok.get("replyTo") == hello_seq
                and hello_ok.get("authRequired") is True
                and isinstance(hello_ok.get("needPair"), bool)
                and bool(hello_ok.get("serverId")),
                f"serverId={hello_ok.get('serverId')} needPair={hello_ok.get('needPair')}",
            )
            server_id = str(hello_ok.get("serverId") or "")

            # pair
            t_auth0 = time.perf_counter()
            await phone.send(
                "pair.request",
                deviceId=args.device_id,
                code=pair_code,
                deviceName="E2E Phone Sim",
                pubKey=None,
            )
            pair_ok = await phone.expect("pair.ok")
            token = b64d(str(pair_ok.get("deviceToken") or ""))
            pair_ok_checks = [
                len(token) == 32,
                pair_ok.get("serverId") == server_id,
                isinstance(pair_ok.get("serverFingerprint"), str),
                pair_ok.get("expiresAt") is None,
            ]
            if tls_fingerprint:
                pair_ok_checks.append(pair_ok.get("serverFingerprint") == tls_fingerprint)
            reporter.check(
                "pair.request → pair.ok（32B deviceToken，指纹一致）",
                all(pair_ok_checks),
                f"token={len(token)}B fp={(pair_ok.get('serverFingerprint') or '')[:16]}…",
            )

            # auth（HMAC canonical: DSPHONE1\\ndeviceId\\nts\\nnonce）
            nonce = b64e(secrets.token_bytes(16))
            ts = int(time.time())
            auth_seq = await phone.send(
                "auth",
                deviceId=args.device_id,
                ts=ts,
                nonce=nonce,
                hmac=compute_hmac_b64(token, args.device_id, ts, nonce),
                v=1,
            )
            auth_ok = await phone.expect("auth.ok")
            reporter.timing("pair→auth", time.perf_counter() - t_auth0)
            reporter.check(
                "auth → auth.ok",
                auth_ok.get("replyTo") == auth_seq
                and auth_ok.get("serverId") == server_id
                and isinstance(auth_ok.get("sessions"), list),
                f"sessions={len(auth_ok.get('sessions') or [])}",
            )

            # resync
            resync_seq = await phone.send("resync", lastSeq=0)
            resync_ok = await phone.expect("resync.ok")
            reporter.check(
                "resync lastSeq=0 → resync.ok",
                resync_ok.get("replyTo") == resync_seq
                and int(resync_ok.get("fromSeq", 0)) >= 1
                and int(resync_ok.get("uptoSeq", 0)) >= int(resync_ok.get("fromSeq", 0)) - 1
                and bool(resync_ok.get("truncated")) in (True, False),
                f"from={resync_ok.get('fromSeq')} upto={resync_ok.get('uptoSeq')}",
            )

            # ---------------------------------------------- 会话管理
            await phone.send("session.create", title="E2E 手机模拟会话")
            create_ok = await phone.expect("session.create.ok")
            session = create_ok.get("session") or {}
            session_id = str(session.get("sessionId") or "")
            reporter.check(
                "session.create → session.create.ok",
                bool(session_id)
                and session.get("title") == "E2E 手机模拟会话"
                and isinstance(session.get("updatedAt"), int)
                and "lastPreview" in session,
                f"sessionId={session_id}",
            )

            await phone.send("session.list")
            list_ok = await phone.expect("session.list.ok")
            sessions = list_ok.get("sessions") or []
            reporter.check(
                "session.list 包含新会话",
                any(item.get("sessionId") == session_id for item in sessions),
                f"sessions={len(sessions)}",
            )

            await phone.send("session.resume", sessionId=session_id)
            resume_ok = await phone.expect("session.resume.ok")
            reporter.check(
                "session.resume → session + messages",
                (resume_ok.get("session") or {}).get("sessionId") == session_id
                and isinstance(resume_ok.get("messages"), list),
                f"messages={len(resume_ok.get('messages') or [])}",
            )

            await phone.send("session.history", sessionId=session_id, limit=50)
            history_ok = await phone.expect("session.history.ok")
            reporter.check(
                "session.history → messages + hasMore",
                isinstance(history_ok.get("messages"), list)
                and isinstance(history_ok.get("hasMore"), bool),
                f"messages={len(history_ok.get('messages') or [])} hasMore={history_ok.get('hasMore')}",
            )

            # ---------------------------------------------- 文本对话
            client_id = f"c-{secrets.token_hex(6)}"
            t_chat = time.perf_counter()
            await phone.send(
                "chat.send",
                clientId=client_id,
                sessionId=session_id,
                text="你好，这是一条端到端验收消息",
            )
            ack = await phone.expect("chat.ack")
            report_ack = ack.get("clientId") == client_id and bool(ack.get("messageId"))
            deltas = 0
            first_delta: dict[str, Any] | None = None
            t_first_delta = None
            done: dict[str, Any] | None = None
            deadline = time.monotonic() + 15.0
            while done is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("等待 assistant.done 超时")
                message = await phone.recv(timeout=remaining)
                mtype = message.get("type")
                if mtype == "assistant.delta":
                    deltas += 1
                    if first_delta is None:
                        first_delta = message
                        t_first_delta = time.perf_counter()
                elif mtype == "assistant.done":
                    done = message
                else:
                    phone.events.append(message)
            reporter.timing("chat.send→首 delta", (t_first_delta or time.perf_counter()) - t_chat)
            reporter.timing("chat.send→done", time.perf_counter() - t_chat)
            reporter.check(
                "chat.ack（clientId/messageId 回显）",
                report_ack,
                f"messageId={ack.get('messageId')}",
            )
            reporter.check(
                "assistant.delta ≥1 且 messageId 与 ack 区分",
                deltas >= 1
                and first_delta is not None
                and bool(first_delta.get("messageId"))
                and first_delta.get("messageId") != ack.get("messageId")
                and first_delta.get("sessionId") == session_id,
                f"deltas={deltas} text={str((first_delta or {}).get('text'))[:24]!r}",
            )
            reporter.check(
                "assistant.done completed",
                done is not None
                and done.get("finishReason") == "completed"
                and done.get("sessionId") == session_id
                and bool(done.get("messageId")),
                f"finishReason={done.get('finishReason')}",
            )

            # ---------------------------------------------- 语音
            wav_path = Path(args.wav)
            if not wav_path.is_file():
                reporter.skip("语音链路", f"wav 不存在: {wav_path}")
            else:
                samples = trim_leading_silence(load_wav_pcm16(wav_path, 16000))
                if args.audio_seconds and args.audio_seconds > 0:
                    samples = samples[: int(16000 * args.audio_seconds)]
                frames = list(pcm_frames(samples))
                if not frames:
                    reporter.skip("语音链路", "wav 无有效音频帧")
                else:
                    expected_mode = "real" if args.asr == "sherpa" else "fake"
                    t_audio = time.perf_counter()
                    ready = await _start_audio(phone, session_id, expected_mode, args)
                    if ready is None:
                        last = phone.events[-1] if phone.events else {}
                        reporter.check(
                            "audio.start → audio.ready",
                            False,
                            f"未就绪（最近 {last.get('type')}: {last.get('message')}）",
                        )
                    elif not (
                        (ready.get("asrMode") == expected_mode or bool(args.url))
                        and ready.get("lang") == "zh"
                        and "pcm16" in (ready.get("codecs") or [])
                    ):
                        reporter.check(
                            "audio.start → audio.ready",
                            False,
                            f"asrMode={ready.get('asrMode')} codecs={ready.get('codecs')}",
                        )
                    else:
                        reporter.check(
                            "audio.start → audio.ready",
                            True,
                            f"asrMode={ready.get('asrMode')} codecs={ready.get('codecs')}",
                        )
                        partials: list[dict[str, Any]] = []
                        finals: list[dict[str, Any]] = []
                        first_partial_at: list[float] = []

                        async def _read_audio_events() -> None:
                            while not finals:
                                message = await phone.recv(timeout=30.0)
                                mtype = message.get("type")
                                if mtype == "asr.partial":
                                    partials.append(message)
                                    if not first_partial_at:
                                        first_partial_at.append(time.perf_counter())
                                elif mtype == "asr.final":
                                    finals.append(message)
                                    return
                                else:
                                    phone.events.append(message)

                        reader_task = asyncio.create_task(_read_audio_events())
                        # 20ms 节奏发送 PCM（首帧 aseq=1, ts=0）
                        t_stop = time.perf_counter()
                        try:
                            for index, pcm in enumerate(frames):
                                aseq = index + 1
                                ts_ms = index * FRAME_MS
                                await phone.ws.send(build_audio_frame(aseq, ts_ms, pcm))
                                await asyncio.sleep(FRAME_MS / 1000.0)
                            t_stop = time.perf_counter()
                            await phone.send("audio.stop", aseq=len(frames))
                            try:
                                await asyncio.wait_for(reader_task, timeout=10.0)
                            except (asyncio.TimeoutError, ConnectionClosed):
                                pass
                        finally:
                            if not reader_task.done():
                                reader_task.cancel()
                                await asyncio.gather(reader_task, return_exceptions=True)
                        if first_partial_at:
                            reporter.timing(
                                "audio.start→首 partial", first_partial_at[0] - t_audio
                            )
                        reporter.check(
                            "asr.partial ≥1",
                            len(partials) >= 1,
                            f"partials={len(partials)}",
                        )
                        final = finals[0] if finals else None
                        reporter.timing("audio.stop→final", time.perf_counter() - t_stop)
                        final_ok = (
                            final is not None
                            and isinstance(final.get("text"), str)
                            and len(str(final.get("text"))) > 0
                            and int(final.get("aseq", 0)) > 0
                            and int(final.get("durationMs", 0)) > 0
                            and final.get("lang") == "zh"
                        )
                        reporter.check(
                            "asr.final 文本非空",
                            final_ok,
                            f"text={str((final or {}).get('text'))[:30]!r} "
                            f"aseq={(final or {}).get('aseq')} frames={len(frames)}",
                        )

            # ---------------------------------------------- 心跳
            if args.heartbeat_seconds and args.heartbeat_seconds > 0:
                interval = args.heartbeat_seconds / 3.0
                alive = True
                for _ in range(3):
                    await asyncio.sleep(interval)
                    try:
                        ping_seq = await phone.send("ping", ts=int(time.time()))
                        pong = await phone.expect("pong", timeout=5.0)
                        alive = alive and pong.get("replyTo") == ping_seq
                    except Exception:
                        alive = False
                        break
                reporter.check(
                    f"心跳 {args.heartbeat_seconds:.0f}s 不断线（每 {interval:.1f}s 一次 ping/pong）",
                    alive,
                )

        # ---------------------------------------------- 边界：错误子协议
        tls_or_plain = args.url or uri  # type: ignore[possibly-undefined]
        try:
            async with connect(
                tls_or_plain,
                ssl=ssl_ctx,
                subprotocols=["bogus.v1"],
                open_timeout=10,
            ) as bad_ws:
                reporter.check("错误子协议握手被接受但不协商", bad_ws.subprotocol is None, "")
                close_exc: BaseException | None = None
                try:
                    await asyncio.wait_for(bad_ws.recv(), timeout=5.0)
                except ConnectionClosed as exc:
                    close_exc = exc
                except Exception as exc:  # noqa: BLE001 - 连接被服务端关闭
                    close_exc = exc
                reporter.check(
                    "错误子协议 → close 4400",
                    close_code(close_exc) == 4400 if close_exc is not None else False,
                    f"closeCode={close_code(close_exc) if close_exc else None}",
                )
        except Exception as exc:  # noqa: BLE001 - InvalidStatus 等
            reporter.check("错误子协议 → close 4400", False, f"握手异常: {exc}")

        # ---------------------------------------------- 边界：未认证业务请求
        try:
            async with connect(
                tls_or_plain,
                ssl=ssl_ctx,
                subprotocols=[SUBPROTOCOL],
                open_timeout=10,
            ) as unauth_ws:
                guest = PhoneClient(unauth_ws)
                await guest.send(
                    "hello",
                    v=1,
                    deviceId=f"{args.device_id}-guest",
                    app="e2e-phone-sim",
                    os="windows",
                    ts=int(time.time()),
                )
                await guest.expect("hello.ok")
                await guest.send(
                    "chat.send",
                    clientId="c-unauth",
                    sessionId=session_id,
                    text="未认证不应该成功",
                )
                error_msg: dict[str, Any] | None = None
                close_exc = None
                try:
                    message = await guest.recv(timeout=5.0)
                    while message.get("type") != "error":
                        message = await guest.recv(timeout=5.0)
                    error_msg = message
                    try:
                        await guest.recv(timeout=3.0)
                    except ConnectionClosed as exc:
                        close_exc = exc
                except ConnectionClosed as exc:
                    close_exc = exc
                # PROTOCOL §3：auth.ok 之前只允许 hello/pair.request/auth/ping；
                # 其余类型→error{1001} 并以 4401 关闭。
                reporter.check(
                    "未认证 chat.send → error 1001 + close 4401",
                    error_msg is not None
                    and int(error_msg.get("code", 0)) == 1001
                    and close_code(close_exc) == 4401,
                    f"code={(error_msg or {}).get('code')} "
                    f"closeCode={close_code(close_exc) if close_exc else None}",
                )
        except Exception as exc:  # noqa: BLE001 - 防御
            reporter.check("未认证 chat.send → error 1001 + close 4401", False, str(exc))

        # ---------------------------------------------- 汇总
        if reporter.failed():
            reporter.emit(as_json=args.json, result="FAIL")
            print("\nE2E_PHONE_SIM: FAIL")
            return 1
        reporter.timing("总耗时", time.perf_counter() - started)
        reporter.emit(as_json=args.json, result="PASS")
        print("\n耗时统计:")
        for name, value in reporter.timings.items():
            print(f"  - {name}: {value:.1f} ms")
        print(f"\nE2E_PHONE_SIM: PASS ({reporter.passed_count()} checks)")
        return 0
    finally:
        if server is not None:
            try:
                await asyncio.wait_for(server.stop(), timeout=15.0)
            except Exception as exc:  # noqa: BLE001 - 清理失败不掩盖结果
                print(f"警告：停止 Bridge 失败: {exc}", file=sys.stderr)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.audio_seconds < 0:
        print("错误：--audio-seconds 不能为负", file=sys.stderr)
        return 2
    try:
        return asyncio.run(run_e2e(args))
    except KeyboardInterrupt:
        print("\nE2E_PHONE_SIM: FAIL (interrupted)", file=sys.stderr)
        return 1
    except AsrUnavailable as exc:
        print(f"E2E_PHONE_SIM: SKIP (sherpa unavailable: {exc})")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
