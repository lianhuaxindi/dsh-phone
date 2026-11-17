#!/usr/bin/env python
"""ASR 管线延迟基准（partial 首字 / final 定稿 / partial 频率）。

对应 NFR-1（docs/PRD.md）：首字 ≤ 1.0 s、定稿 ≤ 1.5 s；PROTOCOL §10：
``asr.partial`` ≤ 5 次/秒。本脚本按**真实节奏**（20 ms/帧、asyncio.sleep
模拟麦克风）把 PCM 喂给识别器，测量：

- ``first_partial_ms``：从第一帧音频投递到收到第一个 partial 的时间；
- ``final_ms``：从 ``stop()`` 到收到 ``final`` 的时间（空串也计）；
- ``partial_rate``：partial 条数 / 音频秒数（协议上限 5/s）；
- ``rtf``：喂完音频 + 定稿的总墙钟 / 音频时长；
- 若后端是 sherpa，还附带 ``stream.stats``（chunk 解码耗时、离线复判耗时）。

用法::

    python scripts\\bench_asr.py                      # fake 后端（无模型/依赖，CI 可用）
    python scripts\\bench_asr.py --backend sherpa     # 真 sherpa-onnx（需先下载模型）
    python scripts\\bench_asr.py --backend sherpa --wav models\\test_wavs\\zh.wav --repeat 3
    python scripts\\bench_asr.py --json               # 机器可读结果
    python scripts\\bench_asr.py --assert-nfr        # 超 NFR 时退出码 1

真后端依赖（sherpa-onnx + 模型）见 README / ``scripts/download_models.py``。
本脚本会自动把 ``models/.pipdeps``（沙箱内手动解包的 sherpa-onnx）加入
``sys.path``，方便本地验证。若导入 sherpa_onnx 时报
``The requested API version [28] is not available ... Current ORT Version is: 1.17.x``，
说明 PATH 上 conda base 的旧 ``onnxruntime.dll`` 遮盖了 pip onnxruntime：把
``site-packages/onnxruntime/capi/onnxruntime.dll`` 复制到 ``sherpa_onnx/lib/``
同目录（运维备注见 docs/DECISIONS.md D-017）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import statistics
import sys
import wave
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

#: 协议固定参数（PROTOCOL §10.2）：16 kHz / 16 bit / 单声道 / 20 ms 帧。
SAMPLE_RATE = 16000
FRAME_MS = 20
FRAME_BYTES = SAMPLE_RATE * FRAME_MS // 1000 * 2  # 640
#: NFR-1 阈值（ms）。
NFR_FIRST_PARTIAL_MS = 1000.0
NFR_FINAL_MS = 1500.0
#: PROTOCOL §10.1：partial ≤ 5 次/秒。
MAX_PARTIAL_RATE = 5.0
#: 默认合成样例时长（秒）。
DEFAULT_SECONDS = 5.0
#: 优先使用的真实样例（存在则自动使用）。
DEFAULT_WAVS = (
    REPO_ROOT / "models" / "test_wavs" / "zh.wav",
    REPO_ROOT / "scripts" / "samples" / "speech_16k.wav",
)


def _ensure_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass


def _ensure_sherpa_importable() -> None:
    """沙箱/便携环境：把 ``models/.pipdeps`` 加入 sys.path（若存在）。"""
    try:
        import sherpa_onnx  # noqa: F401

        return
    except Exception:
        pass
    pipdeps = REPO_ROOT / "models" / ".pipdeps"
    if pipdeps.is_dir() and str(pipdeps) not in sys.path:
        sys.path.insert(0, str(pipdeps))


# ---------------------------------------------------------------------------
# WAV 工具
# ---------------------------------------------------------------------------


def generate_wav(path: Path, *, seconds: float = DEFAULT_SECONDS, sample_rate: int = SAMPLE_RATE) -> Path:
    """生成 16 kHz / 单声道 / 16 bit 的合成样例（不依赖网络与版权素材）。

    内容为带幅度包络的双音 + 轻噪声：对 fake 后端足够“有声音”，
    对真模型不构成有效语音，仅用于测量管线延迟。
    """
    import array
    import random

    path.parent.mkdir(parents=True, exist_ok=True)
    rng = random.Random(20261117)
    total = int(sample_rate * seconds)
    samples = array.array("h")
    for i in range(total):
        t = i / sample_rate
        envelope = 0.5 + 0.5 * math.sin(2 * math.pi * 0.7 * t)
        value = (
            0.28 * math.sin(2 * math.pi * 220.0 * t)
            + 0.16 * math.sin(2 * math.pi * 330.0 * t)
            + 0.02 * (rng.random() * 2.0 - 1.0)
        ) * envelope
        samples.append(max(-32768, min(32767, int(value * 32767))))
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(samples.tobytes())
    return path


def read_wav(path: Path) -> tuple[int, bytes]:
    """读取 16 kHz/16 bit/单声道 WAV，返回 ``(sample_rate, pcm_bytes)``。"""
    with wave.open(str(path), "rb") as wf:
        channels = wf.getnchannels()
        width = wf.getsampwidth()
        rate = wf.getframerate()
        frames = wf.readframes(wf.getnframes())
    if channels != 1 or width != 2:
        raise SystemExit(f"不支持的 WAV（需要单声道/16 bit）: {path} channels={channels} width={width}")
    if rate != SAMPLE_RATE:
        raise SystemExit(f"不支持的采样率（需要 {SAMPLE_RATE} Hz）: {path} rate={rate}")
    return rate, frames


def default_wav() -> Path:
    for candidate in DEFAULT_WAVS:
        if candidate.is_file() and candidate.stat().st_size > 1000:
            return candidate
    return generate_wav(REPO_ROOT / "scripts" / "samples" / "speech_16k.wav")


def trim_leading_silence(
    pcm: bytes,
    *,
    silence_threshold: float = 200.0,
    max_trim_ms: int = 3000,
    frame_ms: int = FRAME_MS,
) -> tuple[bytes, int]:
    """裁掉前导静音，返回 ``(裁剪后的 PCM, 裁剪时长 ms)``。

    真机链路里 VAD/静音检测在 bridge 的 connection 层完成，喂给识别器的
    音频从“用户开口”开始；样例 WAV 往往带 0.5~1 s 前导静音，直接计入
    ``first_partial_ms`` 会高估首字延迟。默认按 20 ms 帧 RMS 找到第一段
    有声帧；整段都静音时原样返回。
    """
    from bridge.protocol.audio import pcm16_rms

    frame_bytes = SAMPLE_RATE * frame_ms // 1000 * 2
    limit = min(len(pcm) // frame_bytes, max(0, max_trim_ms // frame_ms))
    for index in range(limit):
        start = index * frame_bytes
        if pcm16_rms(pcm[start:start + frame_bytes]) >= silence_threshold:
            return pcm[start:], index * frame_ms
    return pcm, 0


# ---------------------------------------------------------------------------
# 基准核心
# ---------------------------------------------------------------------------


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * q
    lower = math.floor(pos)
    upper = math.ceil(pos)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (pos - lower)


def summarize(values: list[float]) -> dict[str, float]:
    """统计 min/mean/p50/p95/max（空列表返回全 0）。"""
    if not values:
        return {"count": 0, "min": 0.0, "mean": 0.0, "p50": 0.0, "p95": 0.0, "max": 0.0}
    return {
        "count": len(values),
        "min": round(min(values), 3),
        "mean": round(statistics.fmean(values), 3),
        "p50": round(_percentile(values, 0.5), 3),
        "p95": round(_percentile(values, 0.95), 3),
        "max": round(max(values), 3),
    }


async def run_once(
    recognizer: Any,
    pcm: bytes,
    *,
    stream_id: str = "bench",
    paced: bool = True,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """跑一条完整音频流并返回单次测量结果（毫秒）。

    partial 的时间戳由**并发消费者**在事件到达时记录，因此
    ``first_partial_ms`` 是真实的“第一帧音频 → 第一个 partial”延迟；
    喂完音频后才 ``stop()``，``final_ms`` 是“stop → final”延迟。
    """
    loop = asyncio.get_running_loop()
    stream = await recognizer.create_stream(stream_id)
    frames = [
        pcm[i:i + FRAME_BYTES]
        for i in range(0, len(pcm), FRAME_BYTES)
        if len(pcm[i:i + FRAME_BYTES]) == FRAME_BYTES
    ]
    duration_ms = len(frames) * FRAME_MS

    partials: list[tuple[str, float]] = []
    finals: list[Any] = []

    async def consume() -> None:
        while True:
            event = await stream.events.get()
            now = loop.time()
            if event.type == "partial":
                partials.append((event.text, now))
            else:
                finals.append(event)
                return

    aseq = 0
    t_start = loop.time()
    consumer = asyncio.create_task(consume())
    next_frame_at = t_start
    for payload in frames:
        aseq += 1
        stream.accept_pcm(payload, ts_ms=(aseq - 1) * FRAME_MS, aseq=aseq)
        if paced:
            next_frame_at += FRAME_MS / 1000.0
            delay = next_frame_at - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
        else:
            await asyncio.sleep(0)
    t_audio_end = loop.time()
    stream.stop(aseq=aseq)
    t_stop = loop.time()
    try:
        await asyncio.wait_for(consumer, timeout=timeout)
    except asyncio.TimeoutError:
        consumer.cancel()
        raise TimeoutError(f"等待 final 超时（>{timeout:.1f}s）") from None
    t_final = loop.time()

    final = finals[0]
    first_partial_ms = round((partials[0][1] - t_start) * 1000.0, 2) if partials else None
    final_ms = round((t_final - t_stop) * 1000.0, 2)
    total_ms = (t_final - t_start) * 1000.0
    audio_seconds = duration_ms / 1000.0
    result: dict[str, Any] = {
        "stream_id": stream_id,
        "audio_ms": duration_ms,
        "paced": paced,
        "first_partial_ms": first_partial_ms,
        "final_ms": final_ms,
        "total_ms": round(total_ms, 2),
        "partial_count": len(partials),
        "partial_rate": round(len(partials) / audio_seconds, 3) if audio_seconds else 0.0,
        "rtf": round(total_ms / 1000.0 / audio_seconds, 3) if audio_seconds else 0.0,
        "feed_wall_ms": round((t_audio_end - t_start) * 1000.0, 2),
        "final_text": final.text,
        "final_duration_ms": getattr(final, "duration_ms", 0),
        "stats": dict(getattr(stream, "stats", {}) or {}),
    }
    try:
        await stream.aclose()
    except Exception:
        pass
    return result


def _nfr_checks(result: dict[str, Any]) -> dict[str, bool]:
    fp = result.get("first_partial_ms")
    return {
        "first_partial": fp is None or fp <= NFR_FIRST_PARTIAL_MS,
        "final": result["final_ms"] <= NFR_FINAL_MS,
        "partial_rate": result["partial_rate"] <= MAX_PARTIAL_RATE,
    }


async def run_bench(args: argparse.Namespace) -> dict[str, Any]:
    from bridge.asr.base import AsrUnavailable, StreamingASR
    from bridge.config import AsrConfig

    wav = Path(args.wav) if args.wav else default_wav()
    _, pcm = read_wav(wav)
    trimmed_leading_ms = 0
    if args.trim_silence:
        pcm, trimmed_leading_ms = trim_leading_silence(
            pcm, silence_threshold=args.silence_threshold
        )
    if args.seconds:
        max_bytes = int(args.seconds * SAMPLE_RATE) * 2 // FRAME_BYTES * FRAME_BYTES
        pcm = pcm[:max_bytes]

    _ensure_sherpa_importable()
    asr_config = AsrConfig(mode=args.backend, models_dir=args.models_dir or "")
    from bridge.asr import create_recognizer

    recognizer = create_recognizer(asr_config)
    if not isinstance(recognizer, StreamingASR):  # pragma: no cover - 防御
        raise SystemExit("识别器工厂返回非法对象")
    ensure_ready = getattr(recognizer, "ensure_ready", None)
    if callable(ensure_ready):
        try:
            await ensure_ready(args.load_timeout)
        except AsrUnavailable as exc:
            raise SystemExit(f"后端不可用: {exc}") from exc
    if not getattr(recognizer, "available", True):
        raise SystemExit(f"后端不可用: {getattr(recognizer, 'unavailable_reason', '')}")

    runs: list[dict[str, Any]] = []
    try:
        for index in range(max(1, args.repeat)):
            runs.append(
                await run_once(
                    recognizer,
                    pcm,
                    stream_id=f"bench-{index + 1}",
                    paced=not args.unpaced,
                    timeout=args.timeout,
                )
            )
    finally:
        close = getattr(recognizer, "aclose", None)
        if callable(close):
            try:
                await close()
            except Exception:
                pass

    first_partials = [r["first_partial_ms"] for r in runs if r["first_partial_ms"] is not None]
    finals = [r["final_ms"] for r in runs]
    summary = {
        "backend": args.backend,
        "wav": str(wav),
        "audio_ms": runs[0]["audio_ms"] if runs else 0,
        "repeat": len(runs),
        "paced": not args.unpaced,
        "trimmed_leading_ms": trimmed_leading_ms,
        "first_partial_ms": summarize(first_partials),
        "final_ms": summarize(finals),
        "partial_rate": summarize([r["partial_rate"] for r in runs]),
        "rtf": summarize([r["rtf"] for r in runs]),
        "feed_wall_ms": summarize([r["feed_wall_ms"] for r in runs]),
        "nfr": {
            "first_partial_ms_limit": NFR_FIRST_PARTIAL_MS,
            "final_ms_limit": NFR_FINAL_MS,
            "partial_rate_limit": MAX_PARTIAL_RATE,
            "all_pass": all(all(_nfr_checks(r).values()) for r in runs),
        },
        "runs": runs,
    }
    return summary


def _print_summary(summary: dict[str, Any]) -> None:
    def fmt(stat: dict[str, float], unit: str = "ms") -> str:
        if not stat.get("count"):
            return "n/a"
        return f"p50={stat['p50']:.1f}{unit} p95={stat['p95']:.1f}{unit} max={stat['max']:.1f}{unit}"

    print(f"\n=== ASR 基准（backend={summary['backend']}, repeat={summary['repeat']}, "
          f"audio={summary['audio_ms']} ms, paced={summary['paced']}）===")
    if summary.get("trimmed_leading_ms"):
        print(f"  已裁剪前导静音 {summary['trimmed_leading_ms']} ms（模拟 VAD 从开口起送流）")
    print(f"  first partial : {fmt(summary['first_partial_ms'])}   (NFR ≤ {NFR_FIRST_PARTIAL_MS:.0f} ms)")
    print(f"  final         : {fmt(summary['final_ms'])}   (NFR ≤ {NFR_FINAL_MS:.0f} ms)")
    print(f"  partial rate  : {fmt(summary['partial_rate'], '/s')}   (PROTOCOL ≤ {MAX_PARTIAL_RATE:.0f}/s)")
    print(f"  rtf           : {fmt(summary['rtf'], '×')}")
    for run in summary["runs"]:
        stats = run.get("stats") or {}
        extra = ""
        if stats.get("offline_ms") is not None:
            extra = (f", chunks={stats.get('chunks')}, chunk_decode={stats.get('decode_ms', 0):.1f} ms"
                     f", offline={stats.get('offline_ms'):.1f} ms")
        print(f"  - {run['stream_id']}: first_partial={run['first_partial_ms']} ms, "
              f"final={run['final_ms']} ms, partials={run['partial_count']}{extra}")
        print(f"    text={run['final_text']!r}")
    nfr = summary["nfr"]
    print(f"  NFR 结论: {'PASS' if nfr['all_pass'] else 'FAIL'}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="ASR 管线延迟基准（NFR-1 / PROTOCOL §10）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--backend", choices=("fake", "sherpa"), default="fake",
                   help="识别后端（默认 fake，无需模型）")
    p.add_argument("--wav", default=None, help="输入 WAV（16 kHz/16 bit/单声道）；缺省用 models/test_wavs/zh.wav 或合成样例")
    p.add_argument("--models-dir", default=None, help="模型根目录（sherpa 后端，默认 $DSH_PHONE_MODELS_DIR 或 <repo>/models）")
    p.add_argument("--seconds", type=float, default=None, help="只截取前 N 秒音频")
    p.add_argument("--repeat", type=int, default=1, help="重复次数（默认 1）")
    p.add_argument("--unpaced", action="store_true", help="不按 20 ms 实时节奏投递（测吞吐/积压，非 NFR）")
    p.add_argument("--no-trim-silence", dest="trim_silence", action="store_false",
                   help="不裁剪前导静音（默认裁剪，模拟 VAD 从开口起送流）")
    p.add_argument("--silence-threshold", type=float, default=200.0,
                   help="前导静音判定的 16 bit RMS 阈值（默认 200）")
    p.add_argument("--timeout", type=float, default=30.0, help="单次等待 final 超时（秒）")
    p.add_argument("--load-timeout", type=float, default=180.0, help="等待模型加载超时（秒）")
    p.add_argument("--json", action="store_true", help="输出 JSON")
    p.add_argument("--out", default=None, metavar="FILE", help="把 JSON 结果写入文件")
    p.add_argument("--assert-nfr", action="store_true", help="NFR 不达标时退出码 1")
    p.add_argument("--gen-wav", action="store_true", help="只生成默认合成样例并退出")
    return p


def main(argv: list[str] | None = None) -> int:
    _ensure_utf8_stdio()
    args = build_parser().parse_args(argv)
    if args.gen_wav:
        path = generate_wav(REPO_ROOT / "scripts" / "samples" / "speech_16k.wav")
        print(f"已生成合成样例: {path}")
        return 0
    summary = asyncio.run(run_bench(args))
    payload = json.dumps(summary, ensure_ascii=False, indent=2)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(payload, encoding="utf-8")
    if args.json:
        print(payload)
    else:
        _print_summary(summary)
    if args.assert_nfr and not summary["nfr"]["all_pass"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
