# DSH Phone（DSH 随身）

把电脑上的 DeepSeek Harness 变成手机上的「随身终端」：打字 + 蓝牙麦语音实时输入 + 流式回复 + 审批。

- **手机端**：Android App（Kotlin 原生壳 + WebView HTML/JS UI）
- **电脑端**：Python Bridge 进程（WSS 服务端 + 配对认证 + ASR + DSH SDK）
- **链路**：`App ⇄ wss(dsphone.v1) ⇄ Bridge ⇄ deepseek-harness-sdk ⇄ dsh --profile sdk`

> 详细需求见 [docs/PRD.md](docs/PRD.md)；协议见 [docs/PROTOCOL.md](docs/PROTOCOL.md)；架构见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)；
> **交付/验收/已知坑见 [docs/HANDOVER.md](docs/HANDOVER.md)**；决策见 [docs/DECISIONS.md](docs/DECISIONS.md)。
>
> 仓库（私有）：<https://github.com/lianhuaxindi/dsh-phone> · CI：[`.github/workflows/ci.yml`](.github/workflows/ci.yml)

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
$py = 'D:\anaconda3\envs\scisim-agent\python.exe'

# 1) 依赖（首次）
& $py -m pip install -r bridge\requirements.txt

# 2) 自检（2.6s；无需模型/凭据，不烧 token）
& $py -m bridge --self-test

# 3) 启动 Bridge（默认只监听 127.0.0.1，安全默认；控制台打印二维码+配对码）
& $py -m bridge --config bridge\config.example.toml
```

手机端 UI 可以先在浏览器里看（mock 模式，不需要手机）：`cd app\web; python -m http.server 8123` → 打开 <http://127.0.0.1:8123/?transport=mock>（配对码 `123456`）。
完整上手、真 ASR 验证、出 APK、已知坑与「未验证清单」见 **[docs/HANDOVER.md](docs/HANDOVER.md)**。

## 开发状态

| 阶段 | 内容 | 状态 |
|---|---|---|
| M0 Phase0 | 仓库骨架、协议冻结、SDK spike | ✅ 完成 |
| M1 | Bridge 骨架 + 配对认证 + WSS + 假/真 ASR + DSH SDK 接线 | ✅ 完成（114 tests） |
| M2 | App 壳 + WebView UI + 文本聊天 + JsBridge 契约 | ✅ 代码完成（122 web tests + 4 个静态自检） |
| M3 | 真录音 + 蓝牙路由 + PTT + partial/定稿 | ⏳ 代码完成，待真机按 runbook 验收 |
| M4 | 审批/提问、重连、诊断、CI 出 APK | ✅ 代码与 CI 配置完成，待 GitHub 首次运行 |

## 验证命令（提交前）

```powershell
$py = 'D:\anaconda3\envs\scisim-agent\python.exe'
& $py -m pytest bridge\tests -q -p no:cacheprovider        # 114 passed
& $py -m bridge --self-test                                 # 5 PASS
& $py scripts\e2e_phone_sim.py --asr fake                   # 21 checks PASS（模拟手机走完整协议）
cd app\web; node --test --test-isolation=none "tests/*.test.js"; cd ..   # 122 pass
foreach ($s in 'verify_protocol','check_contract','check_kotlin_static','check_gradle_config') {
  & $py "app\android\tools\$s.py" }                         # 全 OK
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ci\run_tests.ps1 -AllowMissing
```

## 开发约定

- 协议先冻结（`docs/PROTOCOL.md` v1），改动需 Lead 评审。
- 模块边界不跨写：`bridge/`、`bridge/asr/`、`app/android/`、`app/web/`、`ci/` 各归其主。
- Python 一律用 `scisim-agent` 环境；新增依赖前先查版本，只有真实冲突才升级。
- 密钥/证书/令牌/日志一律不入库（见 `.gitignore`）。
- 提交前：`python -m pytest bridge\tests -q` + `git status` 检查无敏感文件。
