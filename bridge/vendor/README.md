# bridge/vendor

本目录是第三方依赖的**离线兜底副本**，仅在运行环境无法通过 pip 安装时使用。

## qrcode 8.2

- 包名/版本：`qrcode==8.2`（`qrcode-8.2-py3-none-any.whl`，纯 Python）
- 来源：PyPI <https://pypi.org/project/qrcode/8.2/>（wheel 文件由
  `https://pypi.tuna.tsinghua.edu.cn/simple/qrcode/` 镜像下载）
- 许可证：BSD（见 wheel 内 `qrcode-8.2.dist-info/LICENSE`，未修改源码）
- vendor 原因：M1 开发会话的 DSH 文件沙箱禁止 `pip install` 写入
  site-packages / 临时目录（`[Errno 13] Permission denied`），而终端配对二维码
  需要 qrcode 库；此处保存 wheel 解压副本以便本地与无网环境运行。

## 加载顺序

`bridge/qr.py` 的优先级：

1. 先 `import qrcode`（使用环境已安装的版本）；
2. 仅在 `ImportError` 时把 `bridge/vendor` 前插 `sys.path` 并重试。

因此生产环境应优先用 `pip install qrcode>=8.2`（已列入
`bridge/requirements.txt`）；本目录可随时删除，删除后若无已安装 qrcode，
`bridge/qr.py` 会降级为打印配对 URI 文本 + 警告，不崩溃。

## 升级

下载新 wheel 后在本目录解压并删除旧 `qrcode` 目录：

```powershell
python -c "import zipfile; zipfile.ZipFile('qrcode-8.2-py3-none-any.whl').extractall('bridge/vendor')"
```

注意：本目录内容不参与代码审查逻辑，仅为依赖副本。
