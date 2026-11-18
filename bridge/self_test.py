"""``python -m bridge --self-test``：无副作用自检（D-022）。

设计目标：**不启动 WSS、不发 prompt、不烧 token**，只做本地可判定检查：

- 配置解析与监听地址合法性（拒绝 ``0.0.0.0`` / ``::`` / ``*`` 通配地址）
- TLS 证书：已存在则加载；不存在则在 ``state/self-test/`` 下临时生成（不覆盖正式证书），报告 SHA-256 指纹
- ASR：识别器是否可用、模型目录、不可用时的 ``error 2002`` 说明
- DSH runtime：按 D-022 解析链给出选中路径/来源，并做 ``--version`` 轻量探测；
  仅 ``--smoke`` 时才做 ``start → initialize → close``（默认不做，避免冷启动耗时）
- 日志脱敏自检：假 token/hmac/nonce/音频验证是否被掩码

退出码：``0`` = 无致命失败（全 PASS 或仅有非致命 SKIP/FAIL）；``2`` = 有致命 FAIL。
致命项：配置/监听地址、TLS、日志脱敏；ASR 不可用与 runtime 探测失败在
``[dsh].enabled=false`` 时按非致命处理（M1 默认 fake）。
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from dataclasses import dataclass, field
from typing import Any, TextIO

from . import __version__
from .asr import create_recognizer
from .certs import cert_fingerprint, load_certificate, load_or_create_cert
from .config import BridgeConfig, ConfigError, _validate_host
from .logging_setup import redact_fields
from .runtime import probe_version, resolve_runtime

PASS = "PASS"
FAIL = "FAIL"
SKIP = "SKIP"


@dataclass
class CheckResult:
    """单个自检项结果。"""

    name: str
    status: str
    detail: str
    fatal: bool = False
    data: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "fatal": self.fatal,
            "detail": self.detail,
            "data": self.data,
        }


def _one_line(text: str, limit: int = 600) -> str:
    collapsed = " | ".join(line.strip() for line in str(text).splitlines() if line.strip())
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1] + "…"


# ---------------------------------------------------------------------------
# 各检查项
# ---------------------------------------------------------------------------


def _check_config(config: BridgeConfig) -> CheckResult:
    server = config.server
    try:
        host = _validate_host(server.host)
        wildcard_rejected = False
        try:
            _validate_host("0.0.0.0")
        except ConfigError:
            wildcard_rejected = True
        if not wildcard_rejected:
            return CheckResult(
                name="config",
                status=FAIL,
                detail="监听地址校验规则未拒绝 0.0.0.0（安全策略失效）",
                fatal=True,
            )
        if not 1 <= int(server.port) <= 65535:
            return CheckResult(
                name="config",
                status=FAIL,
                detail=f"port 越界: {server.port}",
                fatal=True,
            )
        if int(server.max_connections) < 1:
            return CheckResult(
                name="config",
                status=FAIL,
                detail=f"maxConnections 非法: {server.max_connections}",
                fatal=True,
            )
    except ConfigError as exc:
        return CheckResult(name="config", status=FAIL, detail=f"配置非法: {exc}", fatal=True)
    except Exception as exc:  # pragma: no cover - 防御
        return CheckResult(name="config", status=FAIL, detail=f"配置检查异常: {exc}", fatal=True)
    return CheckResult(
        name="config",
        status=PASS,
        detail=(
            f"监听 {host}:{server.port}，maxConnections={server.max_connections}，"
            f"bindAll={server.bind_all}，通配地址拒绝规则生效"
        ),
        data={
            "source": str(config.source_path) if config.source_path else None,
            "host": host,
            "port": server.port,
            "maxConnections": server.max_connections,
            "bindAll": server.bind_all,
            "baseDir": str(config.base_dir),
        },
    )


def _check_tls(config: BridgeConfig) -> CheckResult:
    cert_path = config.cert_file
    key_path = config.key_file
    try:
        if cert_path.is_file() and key_path.is_file():
            cert = load_certificate(cert_path)
            mode = "已存在证书"
            paths = {"certFile": str(cert_path), "keyFile": str(key_path)}
        else:
            # 临时生成到 state/self-test/，绝不覆盖正式证书
            temp_dir = config.state_dir / "self-test"
            temp_dir.mkdir(parents=True, exist_ok=True)
            cert_file = temp_dir / "bridge.crt"
            key_file = temp_dir / "bridge.key"
            cert = load_or_create_cert(config.server.host, cert_file, key_file)
            mode = "临时生成（state/self-test，未覆盖正式证书）"
            paths = {"certFile": str(cert_file), "keyFile": str(key_file)}
        fingerprint = cert_fingerprint(cert)
    except Exception as exc:
        return CheckResult(name="tls", status=FAIL, detail=f"证书不可用: {exc}", fatal=True)
    if len(fingerprint) != 64 or any(ch not in "0123456789abcdef" for ch in fingerprint):
        return CheckResult(
            name="tls", status=FAIL, detail=f"指纹格式非法: {fingerprint!r}", fatal=True
        )
    return CheckResult(
        name="tls",
        status=PASS,
        detail=f"{mode}；SHA-256 指纹 {fingerprint}",
        data={"fingerprint": fingerprint, **paths},
    )


def _resolve_models_dir(config: BridgeConfig) -> str:
    try:
        from .asr.models import resolve_models_dir

        return str(resolve_models_dir(config.asr.models_dir))
    except Exception as exc:  # pragma: no cover - 模型模块可选
        return f"(无法解析: {exc})"


def _check_asr(config: BridgeConfig) -> CheckResult:
    try:
        recognizer = create_recognizer(config.asr)
        available = bool(getattr(recognizer, "available", False))
        mode = str(getattr(recognizer, "mode", config.asr.mode))
        reason = str(getattr(recognizer, "unavailable_reason", ""))
        models_dir = _resolve_models_dir(config)
    except Exception as exc:  # pragma: no cover - 工厂设计为不抛
        return CheckResult(
            name="asr", status=FAIL, detail=f"ASR 工厂异常: {exc}", fatal=False
        )
    data = {"mode": mode, "available": available, "modelsDir": models_dir}
    if available:
        return CheckResult(
            name="asr",
            status=PASS,
            detail=f"mode={mode}，模型目录 {models_dir}",
            data=data,
        )
    return CheckResult(
        name="asr",
        status=SKIP,
        detail=(
            f"ASR 不可用（mode={mode}）：{reason or '未知原因'}；"
            "音频请求将返回 error 2002（retryable），Bridge 仍可启动。"
            f"模型目录 {models_dir}"
        ),
        data=data,
    )


def _check_runtime(config: BridgeConfig) -> CheckResult:
    resolution = resolve_runtime(config.dsh)
    data = resolution.as_dict()
    if resolution.selected is None:
        fatal = bool(config.dsh.enabled)
        return CheckResult(
            name="dsh.runtime",
            status=FAIL if fatal else SKIP,
            detail=(
                "runtime 解析失败（[dsh].enabled=false，M1 fake 不依赖 runtime）："
                if not fatal
                else "runtime 解析失败（[dsh].enabled=true，无法启动真实 DSH）："
            )
            + _one_line(resolution.error or "未知错误"),
            fatal=fatal,
            data=data,
        )
    probe = probe_version(resolution.selected)
    if probe.ok:
        version = _one_line(probe.output, 160) or "(无输出)"
        return CheckResult(
            name="dsh.runtime",
            status=PASS,
            detail=f"来源 {resolution.source}: {resolution.selected}；--version: {version}",
            data={**data, "version": probe.as_dict()},
        )
    return CheckResult(
        name="dsh.runtime",
        status=FAIL,
        detail=(
            f"来源 {resolution.source}: {resolution.selected}；"
            f"--version 探测失败：{_one_line(probe.error or '未知错误')}"
        ),
        fatal=bool(config.dsh.enabled),
        data={**data, "version": probe.as_dict()},
    )


def _check_dsh_config(config: BridgeConfig) -> CheckResult:
    """D-028：真模型配置的只读检查（不泄露 apiKey 内容）。"""
    dsh = config.dsh
    api_key_configured = bool(dsh.api_key)
    data = {
        "enabled": dsh.enabled,
        "provider": dsh.provider,
        "model": dsh.model,
        "reasoningEffort": dsh.reasoning_effort,
        "maxTokens": dsh.max_tokens,
        "contextWindowTokens": dsh.context_window_tokens,
        "baseUrl": dsh.base_url,
        "apiKeyConfigured": api_key_configured,
    }
    key_state = "已配置" if api_key_configured else "未配置"
    detail = (
        f"enabled={dsh.enabled}，provider={dsh.provider or '(默认)'}，"
        f"model={dsh.model or '(默认)'}，reasoningEffort={dsh.reasoning_effort}，"
        f"maxTokens={dsh.max_tokens}，contextWindowTokens={dsh.context_window_tokens}，"
        f"apiKey {key_state}"
    )
    if not dsh.enabled:
        return CheckResult(
            name="dsh.config", status=SKIP, detail=detail + "；[dsh].enabled=false（fake DSH，不连真模型）",
            data=data,
        )
    if not api_key_configured:
        return CheckResult(
            name="dsh.config",
            status=SKIP,
            detail=detail + "；未配置 apiKey（可启动，真 prompt 将返回 turn.error 3003 并提示填写）",
            data=data,
        )
    return CheckResult(name="dsh.config", status=PASS, detail=detail, data=data)


def _check_media(config: BridgeConfig) -> CheckResult:
    """v1.1：state/media/ 目录可写（图片落盘依赖，D-029）。"""
    media_dir = config.state_dir / "media"
    probe = media_dir / f".self-test-{__version__}"
    try:
        media_dir.mkdir(parents=True, exist_ok=True)
        probe.write_bytes(b"self-test")
        writable = probe.read_bytes() == b"self-test"
        probe.unlink(missing_ok=True)
    except OSError as exc:
        return CheckResult(
            name="media",
            status=FAIL,
            detail=f"state/media 不可写: {exc}；图片消息将返回 error 4002",
            data={"mediaDir": str(media_dir)},
        )
    if not writable:
        return CheckResult(
            name="media", status=FAIL, detail="state/media 写入校验失败", data={"mediaDir": str(media_dir)}
        )
    return CheckResult(
        name="media",
        status=PASS,
        detail=f"state/media 可写：{media_dir}",
        data={"mediaDir": str(media_dir)},
    )


async def _check_smoke(config: BridgeConfig) -> CheckResult:
    """``--smoke``：真实启动 SDK runtime 做 start/initialize/session/close。"""
    try:
        from .dsh_sdk import SdkDshClient

        home = config.dsh_home
        home.mkdir(parents=True, exist_ok=True)
        client = SdkDshClient(config.dsh, home=str(home))
        t0 = time.perf_counter()
        await asyncio.wait_for(client.start(), timeout=180.0)
        start_ms = (time.perf_counter() - t0) * 1000
        t1 = time.perf_counter()
        session = await asyncio.wait_for(
            client.create_session("self-test-smoke"), timeout=30.0
        )
        session_ms = (time.perf_counter() - t1) * 1000
        t2 = time.perf_counter()
        await asyncio.wait_for(client.aclose(), timeout=60.0)
        close_ms = (time.perf_counter() - t2) * 1000
    except asyncio.TimeoutError:
        return CheckResult(name="dsh.smoke", status=FAIL, detail="SDK 启动/关闭超时")
    except Exception as exc:
        return CheckResult(
            name="dsh.smoke",
            status=FAIL,
            detail=f"SDK 冒烟失败: {type(exc).__name__}: {_one_line(str(exc))}",
        )
    resolution = client.runtime_resolution
    return CheckResult(
        name="dsh.smoke",
        status=PASS,
        detail=(
            f"start/initialize {start_ms:.1f}ms，session {session.session_id}"
            f"（{session_ms:.1f}ms），close {close_ms:.1f}ms；未调用 run()/prompt"
        ),
        data={
            "startMs": round(start_ms, 1),
            "sessionMs": round(session_ms, 1),
            "closeMs": round(close_ms, 1),
            "sdkSessionId": session.session_id,
            "runtime": resolution.as_dict() if resolution else None,
            "home": str(config.dsh_home),
        },
    )


def _check_redaction() -> CheckResult:
    fake = {
        "token": "fake-token-should-not-leak",
        "deviceToken": "ZmFrZS10b2tlbi1zaG91bGQtbm90LWxlYWs=",
        "hmac": "deadbeef" * 8,
        "nonce": "bm9uY2UxMjM0NTY3OA==",
        "pcm": b"\x00" * 640,
        "text": "这是一段不应该完整出现在日志里的正文。" * 10,
        "data": "iVBORw0KGgo" * 50,
        "images": [{"mediaId": "a" * 64, "mimeType": "image/png"}],
        "safeKey": "visible-value",
    }
    redacted = redact_fields(fake)
    problems: list[str] = []
    for key in ("token", "deviceToken", "hmac", "nonce", "data"):
        if redacted.get(key) != "***":
            problems.append(f"{key} 未被掩码")
    if redacted.get("pcm") != "<640B>":
        problems.append("pcm 未按长度记录")
    if redacted.get("images") != "<1 images>":
        problems.append("images 未按数量记录")
    text_value = str(redacted.get("text", ""))
    if text_value == fake["text"] or "…" not in text_value or len(text_value) > 90:
        problems.append("长文本未截断")
    if redacted.get("safeKey") != "visible-value":
        problems.append("普通字段被误掩码")
    if problems:
        return CheckResult(
            name="logging.redaction",
            status=FAIL,
            detail="日志脱敏自检失败：" + "；".join(problems),
            fatal=True,
            data={"redacted": {k: str(v) for k, v in redacted.items()}},
        )
    return CheckResult(
        name="logging.redaction",
        status=PASS,
        detail="假 token/hmac/nonce/data 均被掩码为 ***，音频按字节数记录，images 按数量记录，长文本截断",
    )


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


async def run_self_test(
    config: BridgeConfig, *, smoke: bool = False, stream: TextIO | None = None
) -> int:
    out = stream if stream is not None else sys.stdout
    checks: list[CheckResult] = [
        _check_config(config),
        _check_tls(config),
        _check_asr(config),
        _check_dsh_config(config),
        _check_runtime(config),
    ]
    if smoke:
        checks.append(await _check_smoke(config))
    checks.append(_check_media(config))
    checks.append(_check_redaction())

    passed = sum(1 for c in checks if c.status == PASS)
    failed = sum(1 for c in checks if c.status == FAIL)
    skipped = sum(1 for c in checks if c.status == SKIP)
    fatal_failures = [c for c in checks if c.status == FAIL and c.fatal]
    exit_code = 2 if fatal_failures else 0

    report = {
        "tool": "bridge-self-test",
        "version": __version__,
        "config": str(config.source_path) if config.source_path else None,
        "smoke": smoke,
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "fatalFailures": [c.name for c in fatal_failures],
        "exitCode": exit_code,
        "checks": [c.as_dict() for c in checks],
    }

    print(f"DSH Phone Bridge 自检（bridge {__version__}）", file=out)
    print(f"配置文件：{config.source_path or '(默认)'}", file=out)
    width = max(len(c.name) for c in checks) + 2
    for check in checks:
        print(
            f"  {check.status:<4} {check.name:<{width}} {_one_line(check.detail)}",
            file=out,
        )
    print(
        f"结果：{passed} passed / {failed} failed / {skipped} skipped"
        f"；致命失败 {len(fatal_failures)}"
        + ("；退出码 2" if exit_code else "")
        + ("；非致命失败不影响退出码" if failed and not fatal_failures else ""),
        file=out,
    )
    print(
        "SELF_TEST_JSON "
        + json.dumps(report, ensure_ascii=False, separators=(",", ":")),
        file=out,
    )
    return exit_code


__all__ = ["CheckResult", "run_self_test"]
