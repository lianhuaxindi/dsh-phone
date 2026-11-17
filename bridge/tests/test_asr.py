"""ASR 管线单元测试（``bridge/asr/{models,sherpa}.py`` + 两个脚本）。

覆盖范围：

- ``bridge/asr/models.py``：清单一致性、路径解析、就绪检查、下载计划、
  ``.part`` 断点续传 + sha256 校验（本地 ``file://`` 与临时 HTTP Range 服务）；
- ``bridge/asr/fake.py`` 经工厂的端到端行为（partial 递增、final 必达、
  abort 静默、空流空串）；
- ``bridge/asr/sherpa.py``：用注入的假运行时（不加载真模型）覆盖 partial/
  final、空串 final、abort、AsrBusy、依赖/模型缺失 → AsrUnavailable(2002)；
- ``scripts/download_models.py`` 与 ``scripts/bench_asr.py`` 的纯函数/CLI。

设计约束：**不依赖 sherpa-onnx 与真模型**，默认 ``pytest -q`` 全绿；
真模型延迟由 ``scripts/bench_asr.py --backend sherpa`` 单独测量。
"""

from __future__ import annotations

import argparse
import array
import asyncio
import hashlib
import http.server
import importlib.util
import shutil
import sys
import threading
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from bridge.asr.base import AsrBusy, AsrUnavailable, StreamingASR  # noqa: E402
from bridge.asr.models import (  # noqa: E402
    DEFAULT_FINAL_MODEL,
    DEFAULT_STREAMING_MODEL,
    MODELS,
    MODELS_DIR_ENV,
    MODEL_ALIASES,
    DownloadError,
    ModelFile,
    ModelError,
    ModelIntegrityError,
    ModelNotFoundError,
    ModelSpec,
    UnknownModelError,
    check_model,
    default_models_dir,
    download_file,
    download_plan,
    format_bytes,
    get_spec,
    missing_files,
    model_dir,
    repo_root,
    require_model,
    resolve_models_dir,
    verify_model,
)

#: 一帧 20 ms / 640 B PCM16LE（幅度 16 的方波，非静音即可）。
FRAME = b"\x10\x00" * 320
FRAME_SAMPLES = 320


# ---------------------------------------------------------------------------
# 测试辅助
# ---------------------------------------------------------------------------


def _load_script(name: str):
    """按文件路径加载 ``scripts/<name>.py``（scripts 不是包）。"""
    path = REPO_ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_under_test_{name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def bench_mod():
    return _load_script("bench_asr")


@pytest.fixture(scope="module")
def download_mod():
    return _load_script("download_models")


@pytest.fixture
def tmp_path(request):
    """仓库内的测试临时目录。

    CI/沙箱里系统临时目录不可写（DSH workspace-write 只允许仓库内），
    pytest 自带的 ``tmp_path`` 会在 ``PermissionError`` 上失败；这里改用
    ``models/.pytest-tmp/``（``models/*`` 已 gitignore），结束即删。
    """
    key = hashlib.sha1(request.node.nodeid.encode("utf-8")).hexdigest()[:12]
    base = REPO_ROOT / "models" / ".pytest-tmp" / key
    shutil.rmtree(base, ignore_errors=True)
    base.mkdir(parents=True, exist_ok=True)
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


def _tiny_spec(tmp_path: Path, *, data: bytes = b"abcd") -> ModelSpec:
    """构造一个只含一个文件的小模型清单（用于校验逻辑测试）。"""
    digest = hashlib.sha256(data).hexdigest()
    spec = ModelSpec(
        name="tiny-test",
        kind="streaming",
        description="单元测试用小模型",
        files=(ModelFile("tiny.bin", len(data), digest),),
    )
    root = tmp_path / spec.dir_name()
    root.mkdir(parents=True, exist_ok=True)
    (root / "tiny.bin").write_bytes(data)
    return spec


async def _drain_events(stream, *, timeout: float = 10.0) -> list:
    """消费事件直到 final（含 final）；超时抛 ``TimeoutError``。"""
    events: list = []
    while True:
        event = await asyncio.wait_for(stream.events.get(), timeout=timeout)
        events.append(event)
        if event.type == "final":
            return events


# ---------------------------------------------------------------------------
# models.py：清单与路径
# ---------------------------------------------------------------------------


def test_models_manifest_is_consistent() -> None:
    assert set(MODELS) >= {DEFAULT_STREAMING_MODEL, DEFAULT_FINAL_MODEL}
    for spec in MODELS.values():
        assert spec.kind in ("streaming", "offline")
        assert spec.description and spec.files
        for item in spec.files:
            assert item.size > 0
            assert len(item.sha256) == 64
            int(item.sha256, 16)  # 必须是十六进制
            assert not item.rel_path.startswith(("/", "\\"))
        assert spec.total_size == sum(item.size for item in spec.files)
        assert spec.hf_repo, f"{spec.name} 必须有 hf_repo 才能下载"
        bases = spec.hf_url_bases()
        assert any("huggingface.co" in b for b in bases)
        assert any("hf-mirror.com" in b for b in bases)
        urls = spec.file_urls(spec.files[0].rel_path)
        assert urls and urls[0].startswith("https://huggingface.co/")


def test_model_aliases_resolve() -> None:
    assert MODEL_ALIASES  # 至少一份别名
    for alias, canonical in MODEL_ALIASES.items():
        assert get_spec(alias).name == canonical
    assert get_spec(DEFAULT_STREAMING_MODEL).kind == "streaming"
    assert get_spec(DEFAULT_FINAL_MODEL).kind == "offline"
    with pytest.raises(UnknownModelError):
        get_spec("definitely-not-a-model")


def test_resolve_models_dir_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(MODELS_DIR_ENV, raising=False)
    assert resolve_models_dir() == default_models_dir()
    assert repo_root() == REPO_ROOT

    monkeypatch.setenv(MODELS_DIR_ENV, str(tmp_path))
    assert resolve_models_dir() == tmp_path.resolve()

    explicit = tmp_path / "explicit"
    assert resolve_models_dir(explicit) == explicit.resolve()
    # 显式参数优先于环境变量
    assert resolve_models_dir(explicit) == explicit.resolve()


def test_check_model_reports_missing(tmp_path: Path) -> None:
    check = check_model(DEFAULT_STREAMING_MODEL, tmp_path)
    assert not check.ok and not check.present
    assert len(check.missing) == len(MODELS[DEFAULT_STREAMING_MODEL].files)
    assert check.problem_lines()
    assert missing_files(DEFAULT_STREAMING_MODEL, tmp_path) == list(check.missing)


def test_require_model_error_carries_protocol_code(tmp_path: Path) -> None:
    with pytest.raises(ModelNotFoundError) as excinfo:
        require_model(DEFAULT_STREAMING_MODEL, tmp_path)
    err = excinfo.value
    assert err.code == 2002 and err.retryable is True
    message = str(err)
    assert "download_models.py" in message
    assert DEFAULT_STREAMING_MODEL in message


def test_check_model_size_and_hash(tmp_path: Path) -> None:
    spec = _tiny_spec(tmp_path)
    check = check_model(spec, tmp_path)
    assert check.ok and check.present
    assert check.problem_lines() == []
    assert verify_model(spec, tmp_path).ok

    # 大小正确但内容被篡改 → 只有 sha256 校验能发现
    (tmp_path / spec.dir_name() / "tiny.bin").write_bytes(b"abce")
    assert check_model(spec, tmp_path).ok
    broken = verify_model(spec, tmp_path)
    assert not broken.ok and broken.bad_hash == ("tiny.bin",)
    assert "sha256" in broken.problem_lines()[0]

    # 大小不符 → 即使不校验哈希也能发现
    (tmp_path / spec.dir_name() / "tiny.bin").write_bytes(b"abcdefgh")
    size_bad = check_model(spec, tmp_path)
    assert not size_bad.ok and size_bad.bad_size == ("tiny.bin",)
    with pytest.raises(ModelError):
        require_model(spec, tmp_path)


def test_download_plan_skips_present_files(tmp_path: Path) -> None:
    spec = ModelSpec(
        name="plan-test",
        kind="offline",
        description="plan",
        files=(ModelFile("a.bin", 3), ModelFile("b.bin", 4)),
    )
    assert len(download_plan(spec, tmp_path)) == 2
    root = model_dir(spec, tmp_path)
    root.mkdir(parents=True)
    (root / "a.bin").write_bytes(b"abc")
    plan = download_plan(spec, tmp_path)
    assert [item.file.rel_path for item in plan] == ["b.bin"]
    assert len(download_plan(spec, tmp_path, force=True)) == 2
    urls = download_plan(spec, tmp_path, base_url="https://example.com/base")[0].urls
    assert urls[0] == "https://example.com/base/b.bin"


def test_format_bytes() -> None:
    assert format_bytes(0) == "0 B"
    assert format_bytes(512) == "512 B"
    assert format_bytes(1024) == "1.0 KiB"
    assert format_bytes(1536) == "1.5 KiB"
    assert format_bytes(1024 * 1024) == "1.0 MiB"
    assert format_bytes(1024**3) == "1.0 GiB"


# ---------------------------------------------------------------------------
# models.py：下载与断点续传
# ---------------------------------------------------------------------------


def test_download_file_from_local_path(tmp_path: Path) -> None:
    data = bytes(range(256)) * 8
    src = tmp_path / "src.bin"
    src.write_bytes(data)
    dest = tmp_path / "nested" / "copy.bin"
    progress: list[tuple[int, int]] = []

    got = download_file(
        str(src),
        dest,
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        progress=lambda done, total: progress.append((done, total)),
    )
    assert got == dest
    assert dest.read_bytes() == data
    assert not dest.with_name(dest.name + ".part").exists()
    assert progress and progress[-1][0] == len(data)


def test_download_file_from_file_url(tmp_path: Path) -> None:
    data = b"model-bytes" * 100
    src = tmp_path / "src.bin"
    src.write_bytes(data)
    dest = tmp_path / "copy.bin"
    download_file(
        src.as_uri(),
        dest,
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    )
    assert dest.read_bytes() == data


def test_download_file_checksum_mismatch_cleans_part(tmp_path: Path) -> None:
    src = tmp_path / "src.bin"
    src.write_bytes(b"hello world")
    dest = tmp_path / "copy.bin"
    with pytest.raises(ModelIntegrityError):
        download_file(str(src), dest, size=11, sha256="0" * 64)
    assert not dest.exists()
    assert not dest.with_name(dest.name + ".part").exists()


class _RangeHandler(http.server.BaseHTTPRequestHandler):
    payload = b""

    def do_GET(self) -> None:  # noqa: N802 - http.server 约定
        start = 0
        header = self.headers.get("Range")
        if header and header.startswith("bytes="):
            start = int(header.split("=", 1)[1].split("-", 1)[0])
        body = type(self).payload[start:]
        self.send_response(206 if header else 200)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:  # 静音测试输出
        pass


def test_download_file_resumes_from_part_via_http_range(tmp_path: Path) -> None:
    payload = bytes(range(256)) * 20  # 5120 B
    handler = type("Handler", (_RangeHandler,), {"payload": payload})
    server = http.server.HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        url = f"http://127.0.0.1:{port}/model.bin"
        dest = tmp_path / "model.bin"
        part = dest.with_name(dest.name + ".part")
        part.parent.mkdir(parents=True, exist_ok=True)
        part.write_bytes(payload[:2000])  # 模拟已下载一半

        download_file(
            url,
            dest,
            size=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
            resume=True,
            retries=0,
        )
        assert dest.read_bytes() == payload
        assert not part.exists()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_download_file_reports_total_failure(tmp_path: Path) -> None:
    dest = tmp_path / "nope.bin"
    with pytest.raises(DownloadError):
        download_file(str(tmp_path / "missing-src.bin"), dest, retries=0)


# ---------------------------------------------------------------------------
# scripts/download_models.py CLI
# ---------------------------------------------------------------------------


def test_download_models_list(download_mod, capsys: pytest.CaptureFixture[str]) -> None:
    assert download_mod.main(["--list"]) == 0
    out = capsys.readouterr().out
    assert DEFAULT_STREAMING_MODEL in out
    assert DEFAULT_FINAL_MODEL in out


def test_download_models_dry_run(download_mod, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = download_mod.main(["--model", "streaming", "--models-dir", str(tmp_path), "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "dry-run" in out.lower()
    assert "encoder" in out[-4000:]  # 下载计划里列出文件


def test_download_models_unknown_name(download_mod, capsys: pytest.CaptureFixture[str]) -> None:
    assert download_mod.main(["--model", "no-such-model"]) == 2
    assert "未知" in capsys.readouterr().err


def test_download_models_verify_missing_models(download_mod, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = download_mod.main(["--verify", "--models-dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "FAIL" in out


# ---------------------------------------------------------------------------
# fake 后端（工厂路径）
# ---------------------------------------------------------------------------


def test_fake_backend_full_stream() -> None:
    from bridge.asr import create_recognizer
    from bridge.asr.fake import DEFAULT_FINAL_TEXT
    from bridge.config import AsrConfig

    recognizer = create_recognizer(AsrConfig(mode="fake"))
    assert isinstance(recognizer, StreamingASR)
    assert recognizer.mode == "fake" and recognizer.available

    async def run():
        stream = await recognizer.create_stream("fake-full")
        for index in range(100):  # 100 × 20 ms = 2 s
            stream.accept_pcm(FRAME, ts_ms=index * 20, aseq=index + 1)
        stream.stop(aseq=100)
        events = await _drain_events(stream)
        await recognizer.aclose()
        return stream, events

    stream, events = asyncio.run(run())
    partials = [event for event in events if event.type == "partial"]
    finals = [event for event in events if event.type == "final"]
    assert finals and finals[0].text == DEFAULT_FINAL_TEXT
    assert finals[0].aseq == 100
    assert 0 < len(partials) <= 5 * 2.0  # PROTOCOL：≤5 次/秒
    assert [event.text for event in partials] == sorted(
        {event.text for event in partials}, key=lambda text: len(text)
    )
    assert stream.duration_ms == 2000


def test_fake_backend_empty_stream_emits_empty_final() -> None:
    from bridge.asr import create_recognizer
    from bridge.config import AsrConfig

    recognizer = create_recognizer(AsrConfig(mode="fake"))

    async def run():
        stream = await recognizer.create_stream("fake-empty")
        stream.stop(aseq=7)
        events = await _drain_events(stream)
        await recognizer.aclose()
        return events

    events = asyncio.run(run())
    assert [event.type for event in events] == ["final"]
    assert events[0].text == "" and events[0].aseq == 7


def test_fake_backend_abort_is_silent() -> None:
    from bridge.asr import create_recognizer
    from bridge.config import AsrConfig

    recognizer = create_recognizer(AsrConfig(mode="fake"))

    async def run():
        stream = await recognizer.create_stream("fake-abort")
        stream.abort()  # abort 之后：帧被忽略、stop 不产生 final
        stream.accept_pcm(FRAME, ts_ms=0, aseq=1)
        stream.stop(aseq=1)
        await asyncio.sleep(0.05)
        silent = stream.events.empty()
        await recognizer.aclose()
        return silent

    assert asyncio.run(run()) is True


# ---------------------------------------------------------------------------
# sherpa.py：注入假运行时（不加载真模型）
# ---------------------------------------------------------------------------


class FakeSherpaRuntime:
    """最小 sherpa 运行时替身：不改接口，只统计/输出确定性文本。"""

    has_offline = True
    version = "fake-runtime"

    def __init__(self) -> None:
        self.offline_calls = 0
        self.closed = False
        self.streams_created = 0

    def online_new_stream(self):
        self.streams_created += 1
        return {"samples": 0, "text": "", "finished": False}

    def online_accept(self, stream, samples) -> None:
        stream["samples"] += int(samples.size)

    def online_decode(self, stream) -> None:
        samples = stream["samples"]
        if samples >= 4800:
            stream["text"] = "你好"
        if samples >= 9600:
            stream["text"] = "你好世界"

    def online_result(self, stream) -> str:
        return stream["text"]

    def online_input_finished(self, stream) -> None:
        stream["finished"] = True

    def offline_decode(self, samples) -> str:
        self.offline_calls += 1
        return f"整段复判:{int(samples.size)}"

    def close(self) -> None:
        self.closed = True


def _make_sherpa(runtime: FakeSherpaRuntime):
    from bridge.asr.sherpa import SherpaASR

    return SherpaASR(runtime=runtime)


def test_sherpa_partial_and_final_with_fake_runtime() -> None:
    runtime = FakeSherpaRuntime()
    recognizer = _make_sherpa(runtime)

    async def run():
        stream = await recognizer.create_stream("sherpa-ok")
        for index in range(60):  # 1.2 s = 19200 samples；边喂边让 worker 解码
            stream.accept_pcm(FRAME, ts_ms=index * 20, aseq=index + 1)
            if index % 8 == 7:
                await asyncio.sleep(0.01)
        # 等 worker 真正解出前几个 chunk，partial 才不会被 stop 后的 _stopped 抑制
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 5.0
        while stream.stats["chunks"] < 3 and loop.time() < deadline:
            await asyncio.sleep(0.01)
        stream.stop(aseq=60)
        events = await _drain_events(stream)
        await recognizer.aclose()
        return stream, events

    stream, events = asyncio.run(run())
    partials = [event for event in events if event.type == "partial"]
    finals = [event for event in events if event.type == "final"]
    assert partials, "必须有 partial"
    assert partials[0].text == "你好"
    assert finals and finals[0].text == "整段复判:19200"
    assert finals[0].aseq == 60 and finals[0].duration_ms == 1200
    assert runtime.offline_calls == 1
    assert runtime.closed is True
    assert stream.samples_received == 60 * FRAME_SAMPLES
    assert stream.stats["chunks"] >= 3


def test_sherpa_offline_failure_falls_back_to_stream_text() -> None:
    class BrokenOffline(FakeSherpaRuntime):
        def offline_decode(self, samples) -> str:  # noqa: ARG002
            raise RuntimeError("sensevoice boom")

    recognizer = _make_sherpa(BrokenOffline())

    async def run():
        stream = await recognizer.create_stream("sherpa-fallback")
        for index in range(60):
            stream.accept_pcm(FRAME, ts_ms=index * 20, aseq=index + 1)
        stream.stop(aseq=60)
        events = await _drain_events(stream)
        await recognizer.aclose()
        return events

    finals = [event for event in asyncio.run(run()) if event.type == "final"]
    assert finals and finals[0].text == "你好世界"  # 回退到流式结果


def test_sherpa_empty_stream_emits_empty_final() -> None:
    runtime = FakeSherpaRuntime()
    recognizer = _make_sherpa(runtime)

    async def run():
        stream = await recognizer.create_stream("sherpa-empty")
        stream.stop(aseq=3)
        events = await _drain_events(stream)
        await recognizer.aclose()
        return events

    events = asyncio.run(run())
    assert [event.type for event in events] == ["final"]
    assert events[0].text == "" and events[0].aseq == 3
    assert runtime.offline_calls == 0  # 无音频不走离线复判


def test_sherpa_abort_produces_no_final() -> None:
    recognizer = _make_sherpa(FakeSherpaRuntime())

    async def run():
        stream = await recognizer.create_stream("sherpa-abort")
        for index in range(20):
            stream.accept_pcm(FRAME, ts_ms=index * 20, aseq=index + 1)
        stream.abort()
        await asyncio.to_thread(stream.wait_done, 3.0)
        await asyncio.sleep(0.05)
        silent = stream.events.empty()
        await recognizer.aclose()
        return silent

    assert asyncio.run(run()) is True


def test_sherpa_only_one_stream_at_a_time() -> None:
    recognizer = _make_sherpa(FakeSherpaRuntime())

    async def run():
        first = await recognizer.create_stream("sherpa-1")
        with pytest.raises(AsrBusy) as busy:
            await recognizer.create_stream("sherpa-2")
        assert busy.value.code == 2001
        first.stop(aseq=1)
        await _drain_events(first)
        await asyncio.to_thread(first.wait_done, 5.0)
        second = await recognizer.create_stream("sherpa-3")
        second.stop(aseq=2)
        events = await _drain_events(second)
        await recognizer.aclose()
        return events

    events = asyncio.run(run())
    assert events[-1].type == "final" and events[-1].text == ""


def test_sherpa_missing_models_maps_to_2002(tmp_path: Path) -> None:
    from bridge.asr.sherpa import SherpaASR

    missing_dir = tmp_path / "no-models"
    recognizer = SherpaASR(
        models_dir=str(missing_dir), load_in_background=False, final_enabled=False
    )
    assert recognizer.available is False
    assert recognizer.unavailable_reason
    with pytest.raises(AsrUnavailable) as excinfo:
        asyncio.run(recognizer.create_stream("sherpa-missing"))
    assert excinfo.value.code == 2002
    assert excinfo.value.retryable is True
    asyncio.run(recognizer.aclose())


def test_sherpa_wrong_sample_rate_rejected() -> None:
    recognizer = _make_sherpa(FakeSherpaRuntime())
    with pytest.raises(AsrUnavailable):
        asyncio.run(recognizer.create_stream("sherpa-8k", sample_rate=8000))


def test_sherpa_probe_reports_status() -> None:
    from bridge.asr.sherpa import probe

    info = probe()
    assert "models_dir" in info
    assert DEFAULT_STREAMING_MODEL in info
    assert DEFAULT_FINAL_MODEL in info
    assert isinstance(info["ready"], bool)


def test_sherpa_factory_never_raises(tmp_path: Path) -> None:
    from bridge.asr import create_recognizer
    from bridge.config import AsrConfig

    recognizer = create_recognizer(
        AsrConfig(mode="sherpa", models_dir=str(tmp_path / "absent"))
    )
    assert isinstance(recognizer, StreamingASR)
    asyncio.run(recognizer.aclose())


# ---------------------------------------------------------------------------
# scripts/bench_asr.py
# ---------------------------------------------------------------------------


def test_bench_frame_helpers(bench_mod) -> None:
    assert bench_mod.SAMPLE_RATE == 16000
    assert bench_mod.FRAME_BYTES == 640
    assert bench_mod.NFR_FIRST_PARTIAL_MS == 1000.0
    assert bench_mod.NFR_FINAL_MS == 1500.0


def test_bench_trim_leading_silence(bench_mod) -> None:
    silence = array.array("h", [0] * (320 * 10)).tobytes()  # 200 ms
    tone = array.array("h", [5000] * (320 * 5)).tobytes()
    trimmed, trimmed_ms = bench_mod.trim_leading_silence(silence + tone)
    assert trimmed_ms == 200
    assert len(trimmed) == len(tone)
    assert array.array("h", trimmed)[0] == 5000

    # 整段静音 / 直接开口 → 不裁剪
    whole_silence = array.array("h", [0] * 3200).tobytes()
    assert bench_mod.trim_leading_silence(whole_silence) == (whole_silence, 0)
    assert bench_mod.trim_leading_silence(tone) == (tone, 0)


def test_bench_generate_and_read_wav(bench_mod, tmp_path: Path) -> None:
    path = bench_mod.generate_wav(tmp_path / "sample.wav", seconds=0.2)
    rate, pcm = bench_mod.read_wav(path)
    assert rate == 16000
    assert len(pcm) == int(0.2 * 16000) * 2


def test_bench_summarize(bench_mod) -> None:
    assert bench_mod.summarize([])["count"] == 0
    stats = bench_mod.summarize([10.0, 20.0, 30.0, 40.0])
    assert stats["count"] == 4
    assert stats["min"] == 10.0 and stats["max"] == 40.0
    assert stats["p50"] == 25.0


def test_bench_run_once_with_fake_backend(bench_mod) -> None:
    from bridge.asr.fake import DEFAULT_FINAL_TEXT, FakeASR

    pcm = FRAME * 100  # 2 s
    result = asyncio.run(
        bench_mod.run_once(FakeASR(), pcm, stream_id="bench-fake", paced=False, timeout=5.0)
    )
    assert result["audio_ms"] == 2000
    assert result["final_text"] == DEFAULT_FINAL_TEXT
    assert result["first_partial_ms"] is not None
    assert result["partial_rate"] <= 5.0
    assert result["final_ms"] >= 0.0
    assert result["stats"] == {}  # fake 流没有 stats


def test_bench_nfr_checks(bench_mod) -> None:
    assert bench_mod._nfr_checks({"first_partial_ms": 500.0, "final_ms": 100.0, "partial_rate": 2.0}) == {
        "first_partial": True,
        "final": True,
        "partial_rate": True,
    }
    bad = bench_mod._nfr_checks({"first_partial_ms": 1500.0, "final_ms": 2000.0, "partial_rate": 6.0})
    assert bad == {"first_partial": False, "final": False, "partial_rate": False}
    # 没有 partial（如完全静音）不单独判失败，由 final 体现
    assert bench_mod._nfr_checks({"first_partial_ms": None, "final_ms": 10.0, "partial_rate": 0.0})[
        "first_partial"
    ]


def test_bench_cli_parser_defaults(bench_mod) -> None:
    args = bench_mod.build_parser().parse_args([])
    assert isinstance(args, argparse.Namespace)
    assert args.backend == "fake"
    assert args.repeat == 1
    assert args.unpaced is False
    assert args.trim_silence is True
    assert args.load_timeout == 180.0
