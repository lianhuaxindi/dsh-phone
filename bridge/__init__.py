"""DSH Phone 电脑端 Bridge。

Bridge 是手机 App（C）与电脑端 DSH SDK（S 侧内部）之间的 WebSocket 服务端：
协议见 ``docs/PROTOCOL.md`` v1（冻结），架构见 ``docs/ARCHITECTURE.md`` §2。

入口：``python -m bridge --config bridge/config.example.toml``。
"""

__all__ = ["__version__"]

#: Bridge 版本（写入 ``hello.ok.bridge``，见 PROTOCOL §4.2/§14）
__version__ = "1.0.0"
