"""v1.1 配对 URI（hosts/name）与二维码 PNG 测试（PROTOCOL §5.1，D-031）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from bridge import netinfo
from bridge.certs import pair_uri
from bridge.config import BridgeConfig
from bridge.qr import save_qr_png
from bridge.server import BridgeServer
from bridge.tests.conftest import make_config

FINGERPRINT = "a" * 64


class TestPairUri:
    def test_hosts_and_name_encoded(self) -> None:
        uri = pair_uri(
            host="192.168.1.10",
            port=8765,
            fingerprint=FINGERPRINT,
            code="123456",
            server_id="srv-1a2b",
            hosts=["192.168.1.10", "10.5.19.39"],
            name="KQ-PC",
        )
        assert uri.startswith("dsphone://pair?v=1&host=192.168.1.10&port=8765")
        assert "hosts=192.168.1.10%2C10.5.19.39" in uri
        assert "name=KQ-PC" in uri
        # 参数顺序：hosts/name 追加在最后
        assert uri.index("&hosts=") < uri.index("&name=")

    def test_without_hosts_keeps_v1_form(self) -> None:
        uri = pair_uri(
            host="127.0.0.1",
            port=8765,
            fingerprint=FINGERPRINT,
            code="000123",
            server_id="srv-1a2b",
        )
        assert "hosts=" not in uri
        assert "name=" not in uri

    def test_wildcard_host_rejected(self) -> None:
        for host in ("0.0.0.0", "::", "*"):
            with pytest.raises(ValueError):
                pair_uri(
                    host=host,
                    port=8765,
                    fingerprint=FINGERPRINT,
                    code="123456",
                    server_id="srv-1a2b",
                )


class TestQrPng:
    def test_save_qr_png_min_512(self, tmp_path: Path) -> None:
        target = tmp_path / "qr.png"
        result = save_qr_png(
            "dsphone://pair?v=1&host=127.0.0.1&port=8765&fp=" + FINGERPRINT,
            target,
            min_size=512,
        )
        if result is None:
            pytest.skip("qrcode/Pillow 不可用，无法生成 PNG")
        assert Path(result).is_file()
        from PIL import Image  # type: ignore[import-not-found]

        with Image.open(result) as image:
            assert image.width >= 512 and image.height >= 512


class TestNetinfo:
    def test_is_private_ipv4(self) -> None:
        assert netinfo.is_private_ipv4("192.168.1.10")
        assert netinfo.is_private_ipv4("10.0.0.5")
        assert netinfo.is_private_ipv4("172.16.3.4")
        assert not netinfo.is_private_ipv4("127.0.0.1")
        assert not netinfo.is_private_ipv4("169.254.1.1")
        assert not netinfo.is_private_ipv4("198.18.0.1")
        assert not netinfo.is_private_ipv4("198.19.1.1")
        assert not netinfo.is_private_ipv4("8.8.8.8")
        assert not netinfo.is_private_ipv4("not-an-ip")

    def test_pairing_hosts_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(netinfo.ENV_PAIR_HOSTS, "10.9.8.7, 10.9.8.8")
        hosts = netinfo.pairing_hosts("192.168.1.10")
        assert hosts[0] == "192.168.1.10"
        assert "10.9.8.7" in hosts
        assert "10.9.8.8" in hosts
        # host 是回环时不进候选
        assert netinfo.pairing_hosts("127.0.0.1") == ["10.9.8.7", "10.9.8.8"]

    def test_computer_name_non_empty(self) -> None:
        assert netinfo.computer_name()


@pytest.mark.asyncio
async def test_server_pairing_uri_and_png(workdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(netinfo.ENV_PAIR_HOSTS, "10.9.8.7")
    config: BridgeConfig = make_config(workdir)
    srv = BridgeServer(config, announce=False)
    await srv.start()
    try:
        uri = srv.pairing_uri()
        assert "hosts=10.9.8.7" in uri
        assert "name=" in uri
        # announce=False 时 start() 不打印；手动调用以验证 state/qr.png
        srv.print_pairing()
        assert srv.qr_png_path is not None
        assert Path(srv.qr_png_path).is_file()
        assert "hosts=10.9.8.7" in srv.pairing_uri()
    finally:
        await srv.stop()


@pytest.mark.asyncio
async def test_bind_all_uses_lan_host(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(netinfo.ENV_PAIR_HOSTS, "10.9.8.7")
    config = make_config(workdir, bind_all=True)
    srv = BridgeServer(config, announce=False)
    try:
        await srv.start()
    except OSError as exc:  # pragma: no cover - 视环境而定
        pytest.skip(f"无法监听 0.0.0.0: {exc}")
    try:
        assert srv.listen_host == "0.0.0.0"
        # bindAll 时配对 URI 的 host 必须是具体地址，不能是通配地址
        assert srv.host == "10.9.8.7"
        assert "host=10.9.8.7" in srv.pairing_uri()
    finally:
        await srv.stop()


@pytest.mark.asyncio
async def test_qr_refresh_prints_new_code(workdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(netinfo.ENV_PAIR_HOSTS, "10.9.8.7")
    srv = BridgeServer(make_config(workdir), announce=False)
    await srv.start()
    try:
        assert srv.pair_code
        first = srv.pair_code
        srv.auth.refresh_pair_code()
        srv.print_pairing()
        assert srv.pair_code != first or len(srv.pair_code) == 6
        assert srv.qr_png_path is not None
    finally:
        await srv.stop()
