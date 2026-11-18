# DSH Phone（DSH 随身）

把电脑上的 DeepSeek Harness 变成手机上的「随身终端」：打字 + **拍照/传图** + 蓝牙麦语音实时输入 + 流式回复 + 审批。

- **手机端**：Android App（Kotlin 原生壳 + WebView HTML/JS UI），UI 对齐 DeepSeek 官方手机端，顶部显示 **模型 / 思考强度 / 上下文占用**
- **电脑端**：Python Bridge 进程（WSS 服务端 + 配对认证 + ASR + DSH SDK），**默认接真模型** `deepseek-flash`（DeepSeek-V4.1-Flash，1M 上下文，支持 Vision）
- **链路**：`App ⇄ wss(dsphone.v1) ⇄ Bridge ⇄ deepseek-harness-sdk ⇄ dsh --profile sdk`
- **配对**：手机**扫码**电脑端输出的二维码（`bridge/state/qr.png`）即可；同一二维码里带 `hosts=` 多地址，换 Wi-Fi / 公共网络会自动逐个尝试并记住可用地址

> 详细需求见 [docs/PRD.md](docs/PRD.md)；协议见 [docs/PROTOCOL.md](docs/PROTOCOL.md)；架构见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)；
> **交付/验收/已知坑见 [docs/HANDOVER.md](docs/HANDOVER.md)**；决策见 [docs/DECISIONS.md](docs/DECISIONS.md)。
>
> 仓库（公开）：<https://github.com/lianhuaxindi/dsh-phone> · CI：[`.github/workflows/ci.yml`](.github/workflows/ci.yml)
> （**GitHub Actions 目前被账号 billing lock 阻塞，见 [D-026](docs/DECISIONS.md)；本地用 `ci/run_tests.ps1` 验收**）

## 目录结构

```
docs/          PRD、架构、协议、测试计划、安全、决策记录
bridge/        Python Bridge（WSS/认证/协议/SDK 对接）；bridge/asr/ 为 ASR
app/android/   Kotlin 壳（录音、蓝牙路由、前台服务、JS 桥）
app/web/       WebView 内 HTML/JS UI
ci/            CI 脚本；.github/ 为 Actions 工作流
scripts/       开发/诊断/基准脚本（含 spike）
models/        sherpa-onnx 模型（不入库，脚本下载）
```

## 快速开始（电脑端 Bridge）

前置：Python 3.10+（本机使用 conda 环境 `scisim-agent`）。

**一键启动（推荐，给手机连）：双击 `scripts\start_bridge.cmd`。** 脚本会自动选 Python、自动检测本机局域网 IPv4（排除 VMware/Mihomo 等虚拟网卡，优先 WLAN，且只接受真正能 bind 的地址）、生成/更新 `bridge\config.local.toml`（不动仓库里的示例配置；**已有该文件时只更新地址/端口/配对码有效期，保留你填的 `[dsh] apiKey` 等**）、打印配对码/指纹/配对 URI 并前台运行；停止按 `Ctrl+C`，或双击 `scripts\stop_bridge.cmd`。手机需与电脑连同一 Wi-Fi。

```powershell
scripts\start_bridge.cmd                          # 双击等价；自动检测局域网 IP，配对码默认 900 秒
powershell -File scripts\start_bridge.ps1 -Port 8766 -PairCodeTtlSec 120   # 换端口 / 用正式 TTL
powershell -File scripts\start_bridge.ps1 -HostAddress 192.168.1.23        # 手动指定 IP
scripts\stop_bridge.cmd                           # 停止：按端口+命令行匹配，只杀 python -m bridge
```

等价的手动步骤：

```powershell
$py = 'D:\anaconda3\envs\scisim-agent\python.exe'

# 1) 依赖（首次）
& $py -m pip install -r bridge\requirements.txt

# 2) 自检（2.6s；无需模型/凭据，不烧 token）
& $py -m bridge --self-test

# 3) 启动 Bridge（默认只监听 127.0.0.1，手机连不上；给手机接入加 --host <本机局域网 IP>）
& $py -m bridge --config bridge\config.example.toml --host 10.5.19.39
```

启动后控制台会打印**配对码 / 64 位指纹 / 配对 URI**，并把同一份信息渲染成二维码图片 `bridge\state\qr.png`（手机 App 的「扫码配对」直接扫它）。要接真模型只需给 `[dsh] apiKey` 填值（或用环境变量 `DEEPSEEK_API_KEY`）；模型默认 `deepseek-flash`（DeepSeek-V4.1-Flash，1M 上下文）。

手机端 UI 可以先在浏览器里看（mock 模式，不需要手机）：`cd app\web; python -m http.server 8123` → 打开 <http://127.0.0.1:8123/?transport=mock>（配对码 `123456`）。
完整上手、真 ASR 验证、出 APK、已知坑与「未验证清单」见 **[docs/HANDOVER.md](docs/HANDOVER.md)**。

## 出 APK（本机已出包，不需要 CI）

本机工具链已装在工作区 `.tools/`（JDK 17 + Android SDK 35 + Gradle 8.9，均已 gitignore），**debug APK 已构建成功**：

```
app\android\app\build\outputs\apk\debug\app-debug.apk
→ 4,578,710 B · sha256 A45C164E…E726704601 · com.dsh.phone 1.1.0 (10100) · minSdk 29 / targetSdk 35
→ 内含 assets/web 26 个文件（sync_web 同步的 WebView UI）
```

安装：`adb install -r app\android\app\build\outputs\apk\debug\app-debug.apk`（或把 APK 传到手机点击安装，需允许「安装未知来源」）。

重新出包 / 第一次在新机器上装工具链 / 沙箱下跑单测的完整步骤见 **[app/android/BUILD-LOCAL.md](app/android/BUILD-LOCAL.md)**（12 章，含可复制命令与故障排查表）；决策记录见 `docs/DECISIONS.md` 的 D-027。

配真实模型只需要一步：编辑 `bridge\config.local.toml`（一键脚本生成的；填好后再次双击启动不会被覆盖）或复制 `bridge\config.example.toml` 后填 `[dsh] apiKey = "sk-..."`；也可以设环境变量 `DEEPSEEK_API_KEY` / `DSH_PHONE_API_KEY`（详见 D-028）。

## 开发状态

| 阶段 | 内容 | 状态 |
|---|---|---|
| M0 Phase0 | 仓库骨架、协议冻结、SDK spike | ✅ 完成 |
| M1 | Bridge 骨架 + 配对认证 + WSS + 假/真 ASR + 真模型/图片/用量 + DSH SDK 接线 | ✅ 完成（146 tests） |
| M2 | App 壳 + WebView UI + 文本聊天 + JsBridge 契约 + v1.1 扫码/取图/顶栏三要素 | ✅ 代码完成（144 web tests + 4 个静态自检 + APK 1.1.0） |
| M3 | 真录音 + 蓝牙路由 + PTT + partial/定稿 | ⏳ APK 已构建（71 单测绿），待真机按 runbook 验收 |
| M4 | 审批/提问、重连、诊断、CI 出 APK | ✅ 代码与 CI 配置完成；APK 已改由本机构建；GitHub Actions 被账号 billing lock 阻塞（D-026） |

## 验证命令（提交前）

```powershell
$py = 'D:\anaconda3\envs\scisim-agent\python.exe'
& $py -m pytest bridge\tests -q -p no:cacheprovider        # 146 passed
& $py -m bridge --self-test                                 # 6 PASS / 1 SKIP（apiKey 未配）
& $py scripts\e2e_phone_sim.py --asr fake                   # 21 checks PASS（模拟手机走完整协议）
cd app\web; node --test --test-isolation=none "tests/*.test.js"; cd ..   # 144 pass
foreach ($s in 'verify_protocol','check_contract','check_kotlin_static','check_gradle_config') {
  & $py "app\android\tools\$s.py" }                         # 101 checks / 17 ops / 25 files / 67 checks 全 OK
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ci\run_tests.ps1 -AllowMissing
# Android 构建 / 单测：见 app\android\BUILD-LOCAL.md（本机 71 单测绿、APK 4.58 MB）
```

## 开发约定

- 协议先冻结（`docs/PROTOCOL.md` v1），改动需 Lead 评审。
- 模块边界不跨写：`bridge/`、`bridge/asr/`、`app/android/`、`app/web/`、`ci/` 各归其主。
- Python 一律用 `scisim-agent` 环境；新增依赖前先查版本，只有真实冲突才升级。
- 密钥/证书/令牌/日志一律不入库（见 `.gitignore`）。
- 提交前：`python -m pytest bridge\tests -q` + `git status` 检查无敏感文件。
