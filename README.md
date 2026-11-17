# DSH Phone（DSH 随身）

把电脑上的 DeepSeek Harness 变成手机上的「随身终端」：打字 + 蓝牙麦语音实时输入 + 流式回复 + 审批。

- **手机端**：Android App（Kotlin 原生壳 + WebView HTML/JS UI）
- **电脑端**：Python Bridge 进程（WSS 服务端 + 配对认证 + ASR + DSH SDK）
- **链路**：`App ⇄ wss(dsphone.v1) ⇄ Bridge ⇄ deepseek-harness-sdk ⇄ dsh --profile sdk`

> 详细需求见 [docs/PRD.md](docs/PRD.md)；协议见 [docs/PROTOCOL.md](docs/PROTOCOL.md)；架构见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

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

```powershell
# 1) 依赖（首次）
D:\anaconda3\envs\scisim-agent\python.exe -m pip install -r bridge\requirements.txt

# 2) 启动 Bridge（默认只监听 127.0.0.1，安全默认）
D:\anaconda3\envs\scisim-agent\python.exe -m bridge --config bridge\config.example.toml

# 3) 控制台会打印配对二维码（含 host/port/证书指纹/6 位配对码）
```

## 开发状态

| 阶段 | 内容 | 状态 |
|---|---|---|
| M0 Phase0 | 仓库骨架、协议冻结、SDK spike | 进行中 |
| M1 | Bridge 骨架 + 配对认证 + WSS + 假音频 ASR | 待开始 |
| M2 | App 壳 + WebView UI + 文本聊天 | 待开始 |
| M3 | 真录音 + 蓝牙路由 + PTT + partial/定稿 | 待开始 |
| M4 | 审批/提问、重连、诊断、CI 出 APK | 待开始 |

## 开发约定

- 协议先冻结（`docs/PROTOCOL.md` v1），改动需 Lead 评审。
- 模块边界不跨写：`bridge/`、`bridge/asr/`、`app/android/`、`app/web/`、`ci/` 各归其主。
- Python 一律用 `scisim-agent` 环境；新增依赖前先查版本，只有真实冲突才升级。
- 密钥/证书/令牌/日志一律不入库（见 `.gitignore`）。
- 提交前：`python -m pytest bridge\tests -q` + `git status` 检查无敏感文件。
