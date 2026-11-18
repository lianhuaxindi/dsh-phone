"""``bridge.netinfo`` 的解析/过滤测试（D-031、D-035）。

D-035 的背景：``ipconfig`` 的「默认网关」折行后会孤立成一行（只有缩进 + 一个 IP），
旧解析把它当成了网卡地址，Bridge 启动时报 ``无法监听 10.15.0.1:8765``。
"""

from __future__ import annotations

import subprocess

from bridge import netinfo


_FAKE_IPCONFIG_EN = """Windows IP Configuration


Wireless LAN adapter WLAN:

   Connection-specific DNS Suffix  . :
   IPv6 Address. . . . . . . . . . . : 2001:da8:a800:815:f05b:3c13:5b6:4e31
   Link-local IPv6 Address . . . . . : fe80::b53:4cb9:af:3388%7
   IPv4 Address. . . . . . . . . . . : 10.15.187.127
   Subnet Mask . . . . . . . . . . . : 255.255.0.0
   Default Gateway . . . . . . . . . : fe80::9ece:88ff:fe4f:146d%7
                                       10.15.0.1
Ethernet adapter VMware Network Adapter VMnet8:

   IPv4 Address. . . . . . . . . . . : 192.168.148.1
   Subnet Mask . . . . . . . . . . . : 255.255.255.0
   Default Gateway . . . . . . . . . :
"""

_FAKE_IPCONFIG_ZH = """Windows IP 配置


无线局域网适配器 WLAN:

   IPv4 地址 . . . . . . . . . . . . : 192.168.31.66
   子网掩码  . . . . . . . . . . . . : 255.255.255.0
   默认网关. . . . . . . . . . . . . : 192.168.31.1
"""


class _FakeProc:
    def __init__(self, text: str) -> None:
        self.stdout = text.encode("utf-8")


def _patch_ipconfig(monkeypatch, text: str) -> None:
    def fake_run(*args, **kwargs):  # noqa: ANN002, ANN003 - 替身不关心参数
        return _FakeProc(text)

    monkeypatch.setattr(netinfo.subprocess, "run", fake_run)
    monkeypatch.setattr(netinfo.sys, "platform", "win32")


class TestIpconfigParsing:
    def test_skips_gateway_continuation_line(self, monkeypatch) -> None:
        """D-035：折行的默认网关不能被当成网卡 IPv4。"""
        _patch_ipconfig(monkeypatch, _FAKE_IPCONFIG_EN)
        entries = netinfo._from_ipconfig()
        assert ("Wireless LAN adapter WLAN", "10.15.187.127") in entries
        assert all(ip != "10.15.0.1" for _, ip in entries), entries
        # 子网掩码同样不能被当成地址
        assert all(ip != "255.255.0.0" for _, ip in entries), entries

    def test_parses_localized_label(self, monkeypatch) -> None:
        """中文 ``IPv4 地址`` 标签也要认。"""
        _patch_ipconfig(monkeypatch, _FAKE_IPCONFIG_ZH)
        entries = netinfo._from_ipconfig()
        assert ("无线局域网适配器 WLAN", "192.168.31.66") in entries
        assert all(ip != "192.168.31.1" for _, ip in entries), entries

    def test_non_windows_returns_empty(self, monkeypatch) -> None:
        monkeypatch.setattr(netinfo.sys, "platform", "linux")
        assert netinfo._from_ipconfig() == []


class TestBindable:
    def test_local_address_is_bindable(self) -> None:
        assert netinfo.is_bindable_ipv4("127.0.0.1")

    def test_foreign_address_is_not_bindable(self) -> None:
        # TEST-NET-3（203.0.113.0/24）与随机的私网地址都不应能 bind 成功
        assert not netinfo.is_bindable_ipv4("203.0.113.7")
        assert not netinfo.is_bindable_ipv4("10.99.98.97")

    def test_invalid_string_is_not_bindable(self) -> None:
        assert not netinfo.is_bindable_ipv4("not-an-ip")

    def test_collect_candidates_drops_unbindable(self, monkeypatch) -> None:
        """D-035：解析出但本机 bind 不了的地址不得进入候选。"""
        monkeypatch.setattr(
            netinfo,
            "_from_ipconfig",
            lambda: [
                ("Wireless LAN adapter WLAN", "10.99.98.97"),
                ("VirtualBox Host-Only Network", "192.168.56.1"),
            ],
        )
        assert netinfo.collect_candidates() == []

    def test_pairing_hosts_never_contains_unbindable(self, monkeypatch) -> None:
        monkeypatch.setattr(
            netinfo,
            "_from_ipconfig",
            lambda: [("Wireless LAN adapter WLAN", "10.99.98.97")],
        )
        hosts = netinfo.pairing_hosts("10.99.98.97")
        # host 参数本身会被保留（调用者显式指定），但自动探测的地址不得混入
        assert "10.99.98.97" in hosts
        assert len(hosts) == 1


assert subprocess  # 保留导入，避免 lint 提示（subprocess 在 netinfo 内被 monkeypatch）
