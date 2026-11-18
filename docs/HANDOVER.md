# DSH Phone 交付说明（Handover）

> 面向：接手这个仓库的人 / 想在本机把它跑起来的用户。
> 需求见 [PRD.md](PRD.md)，协议见 [PROTOCOL.md](PROTOCOL.md)，架构见 [ARCHITECTURE.md](ARCHITECTURE.md)，
> 决策记录见 [DECISIONS.md](DECISIONS.md)，测试计划见 [TESTPLAN.md](TESTPLAN.md)，安全见 [SECURITY.md](SECURITY.md)。

## 1. 当前状态（一句话版）

**电脑端 Bridge 已完整可跑**（WSS + 配对认证 + 协议 + 真 ASR + DSH SDK 接线，114 个测试绿）；
**手机端代码完整**（Kotlin 壳 + WebView UI），**debug APK 已在本机构建成功**（`com.dsh.phone` 1.0.0，3,867,632 B，见 §3.2），但**还没有在真机上跑过**；
**CI 流水线已写好**，但 **GitHub Actions 被账号 billing lock 阻塞**（D-026），首次 CI 至今未跑。

| 里程碑 | 内容 | 状态 | 证据 |
|---|---|---|---|
| M0 Phase0 | 仓库骨架、协议冻结（`PROTOCOL.md` v1）、SDK spike | ✅ 完成 | `docs/spike/SDK-SPIKE.md`、D-001…D-027 |
| M1 Bridge | WSS + 配对 + 认证 + 协议 + 音频帧 + 假/真 ASR + 假/真 DSH | ✅ 完成 | `pytest bridge\tests` → 114 passed；`python -m bridge --self-test` → 5 PASS |
| M2 App+UI | Kotlin 壳、WebView UI、JsBridge 契约、mock 演示 | ✅ 代码完成、单测/静态验收、**已出 APK** | web 122 tests 绿；`app/android/tools/*.py` 4 个脚本全 OK；APK 见 §3.2 |
| M3 录音+蓝牙 | AudioRecord / PTT / SCO/HFP / 前台服务 | ⏳ **APK 已构建、待真机验收** | `app-debug.apk`（3.87 MB，D-027）+ `app/android/README.md` 的 M3 runbook（约 30 条可勾选场景） |
| M4 审批/诊断/CI | 审批/提问弹窗、诊断页、重连、GitHub Actions 出 APK | ✅ 代码与配置完成；CI 被 billing lock 阻塞（D-026），**APK 已改由本机构建**（D-027） | `.github/workflows/ci.yml`、`ci/`、`app/android/BUILD-LOCAL.md` |

## 2. 电脑端：马上能跑

### 2.1 前置

| 项 | 本机取值 | 说明 |
|---|---|---|
| Python | `D:\anaconda3\envs\scisim-agent\python.exe`（3.11.17） | `bridge/requirements.txt` 里的依赖已装齐 |
| ASR 模型 | `models/`（~189MB 流式 + ~228MB SenseVoice） | 已下载并 sha256 校验；`models/*` 不入库 |
| sherpa-onnx | 手动解包到 `models/.pipdeps`（gitignored） | 沙箱里 pip 装不进环境，用 `PYTHONPATH` 注入；正常机器直接 `pip install sherpa-onnx==2.0.1` |
| DSH runtime | 自动探测到 `D:\others\DSH\resources\runtime\cli\bin\dsh.cmd` | 走 D-022 解析链；SDK wheel 内置 runtime 在本机不可用 |

### 2.2 启动 Bridge

**一键（推荐，尤其给手机连）**：双击 `scripts\start_bridge.cmd`。它会：

1. 自动挑 Python（默认 `D:\anaconda3\envs\scisim-agent\python.exe`，可用 `-Py` / 环境变量 `DSH_PHONE_PYTHON` 覆盖）；
2. 用 `scripts/bridge_lan.py` 自动检测本机局域网 IPv4（跳过回环/APIPA/代理 fake-IP `198.18.*`，跳过 VMware VMnet / VirtualBox / Hyper-V / WSL / docker / Mihomo TUN 等虚拟网卡，优先 WLAN）；
3. 复制 `bridge\config.example.toml` → 生成 `bridge\config.local.toml`（改 host/port/pairCodeTtlSec，**不动**仓库里的正式示例；该文件已 gitignore）；
4. 前台启动 Bridge，窗口直接打印配对码 / 64 位指纹 / 配对 URI，过期自动轮换新码。

```powershell
scripts\start_bridge.cmd                       # 双击等价；默认配对码 900 秒、端口 8765
scripts\stop_bridge.cmd                        # 停止（WMI 不可用时按端口回退匹配，只杀 python -m bridge）
powershell -File scripts\start_bridge.ps1 -DryRun                     # 只看检测结果与将执行的命令
powershell -File scripts\start_bridge.ps1 -Port 8766 -PairCodeTtlSec 120
powershell -File scripts\start_bridge.ps1 -HostAddress 192.168.1.23   # 手动指定 IP
```

等价手动步骤：

```powershell
$py = 'D:\anaconda3\envs\scisim-agent\python.exe'

# 0) 自检（2.6s；不烧 token、不需要模型）
& $py -m bridge --self-test

# 1) 启动（控制台打印 ASCII 二维码 + 配对码，默认只监听 127.0.0.1:8765）
& $py -m bridge --config bridge\config.example.toml

# 2) 想联真实模型：先编辑 bridge/config.toml（复制 config.example.toml）
#    [dsh] enabled = true / apiKey = "..." / provider / model
#    （首次真调模型前，可先跑 `& $py -m bridge --self-test --smoke` 验证 runtime 能起）
```

### 2.3 用真实 ASR（不接手机也能验证）

```powershell
$env:PYTHONPATH = 'D:\code\apps\手机端\models\.pipdeps'   # 已装 sherpa-onnx 的机器不需要
$env:PYTHONIOENCODING = 'utf-8'
& 'D:\anaconda3\envs\scisim-agent\python.exe' bridge\state\real_asr_e2e.py
```

本机实测（`models/test_wavs/zh.wav`，真实 sherpa-onnx + SenseVoice）：

```
partials: 7 条   +1937 ms 开放时间 / +2906 ms 开放时间早 / +3219 ms 开放时间早上
final_text: '开饭时间早上9点至下午5点。'
final_after_stop: 515 ms（NFR-2 目标 ≤1500ms）
RESULT: PASS
```

> 若用 `scripts/samples/speech_16k.wav`（合成样例）会得到空结果——那是给 bench 的占位样本，不是语音。

### 2.4 一条命令端到端自证（不用手机）

`scripts/e2e_phone_sim.py` 用 Python 客户端**扮演 Android App**，走完整 `dsphone.v1` 协议：hello → pair → auth(HMAC) → session.* → chat.send → audio.start → 20ms 帧 → asr.partial/final → 心跳 → 边界（错误子协议 4400、未认证 1001+4401）。默认进程内起真实 Bridge（端口 0、自签 TLS、独立 `bridge/state/e2e_phone_home`，不碰 `~/.dsh`），也可 `--url wss://… --pair-code …` 连已启动的 Bridge。

```powershell
$py = 'D:\anaconda3\envs\scisim-agent\python.exe'
& $py scripts\e2e_phone_sim.py --asr fake        # CI 口径，约 20s
$env:PYTHONPATH = 'D:\code\apps\手机端\models\.pipdeps'
& $py scripts\e2e_phone_sim.py --asr sherpa      # 真模型，约 26s
```

本机实测：

| 模式 | 结果 | 关键指标 |
|---|---|---|
| `--asr fake` | **PASS，21 checks，exit 0**（19.8s） | pair→auth 2.1ms、chat.send→done 4.8ms、partials 7 条、final aseq=150 |
| `--asr sherpa` | **PASS，22 checks，exit 0**（26.5s） | 模型加载 ~5.8s、audio.start→首 partial **1397.9ms**、audio.stop→final **314.2ms** |

### 2.5 性能基准（NFR 复核）

```powershell
$env:PYTHONPATH = 'D:\code\apps\手机端\models\.pipdeps'
& 'D:\anaconda3\envs\scisim-agent\python.exe' scripts\bench_asr.py --backend sherpa --repeat 1 --assert-nfr
```

本机实测：first partial **p50 969–1000ms**（NFR ≤1000ms，余量很薄 → 风险 **R-09**）、final p50 313–500ms、partial 2.0/s、RTF 1.1×。

## 3. 手机端：怎么看到 UI / 怎么出 APK

### 3.1 现在就能看的 UI（mock 模式，不需要手机）

```powershell
cd app\web
python -m http.server 8123
# 浏览器打开 http://127.0.0.1:8123/?transport=mock
# mock 配对码：123456
```

`?transport=mock` 是完整交互演示（会话/流式回复/工具状态/审批/提问/录音浮层），数据在内存与 IndexedDB。
`?transport=ws` 才会连真实 Bridge（需要浏览器信任自签证书，V1 不推荐）；`native` 只在 App 里有效。

### 3.2 出 APK（本机已装好工具链，一条命令出包）

**APK 已经在本机构建成功**（2026-11-18）：`app/android/app/build/outputs/apk/debug/app-debug.apk`
→ 3,867,632 B，`package com.dsh.phone`、`versionCode 10000` / `versionName 1.0.0`、minSdk 29 / targetSdk 35、内含 26 个 `assets/web/*` 资源。

本机工具链（无需系统安装、全部在工作区 `.tools/` 下，已 gitignore）：JDK 17 + Android cmdline-tools（`platform-tools` / `platforms;android-35` / `build-tools;35.0.0`）+ Gradle 8.9。首次安装与可复制命令见 **`app/android/BUILD-LOCAL.md`**（12 章，含环境变量、单测替代跑法、非 ASCII 路径开关、故障排查表）。

日常重新出包（PowerShell）：

```powershell
$t = 'D:\code\apps\手机端\.tools\android'
$env:JAVA_HOME = "$t\jdk"; $env:ANDROID_HOME = "$t\sdk"; $env:ANDROID_SDK_ROOT = "$t\sdk"
$env:PATH = "$t\jdk\bin;$t\sdk\platform-tools;$env:PATH"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\sync_web.ps1      # Web UI → assets
& "$t\gradle-8.9\bin\gradle.bat" -p app\android assembleDebug --no-daemon --console=plain `
  -Pdsphone.versionName=1.0.0 -Pdsphone.versionCode=10000
# 产物：app\android\app\build\outputs\apk\debug\app-debug.apk
```

- 实测：首次 `BUILD SUCCESSFUL in 54s`，增量 21s。
- 本机单测：Gradle 的 fork 测试 worker 被 DSH 沙箱禁管道拦住 → 用 `BUILD-LOCAL.md` §7.2 的直连 JUnit4 命令（**53 tests OK**），或 §7.3 的 ASCII 临时副本跑官方 `testDebugUnitTest`（JUnit XML 8 套件 / 53 tests / 0 failures）。
- 装到手机：本机没有 `adb`，把 `app-debug.apk` 传到手机安装即可（debug 签名，需允许「安装未知来源」）。
- 备选路径（CI）：仓库 `main` 已推送 <https://github.com/lianhuaxindi/dsh-phone>，`.github/workflows/ci.yml` 已通过 `actionlint`；`android-build` job 会 sync Web 资源 → 4 个离线自检 → gradle wrapper → `assembleDebug` → `testDebugUnitTest` → 上传 `app-debug-<versionName>-<versionCode>`。但**目前被账号 billing lock 阻塞**（见 §3.3 / D-026）。

### 3.3 GitHub 仓库与 CI 现状（2026-11-18）

- 仓库：<https://github.com/lianhuaxindi/dsh-phone>（**public**，`main` HEAD `fecc151`）。
- **CI 现在跑不了，但原因不在代码**：所有 job 的 check-run annotation 是
  `The job was not started because your account is locked due to a billing issue.`
  ⇒ **账号级 billing lock**（详见 D-026）。`actionlint` 已通过；最小 smoke workflow 同样被拦；同账号公开/私有一次性仓库 A/B 结果一致。
- 解锁步骤：GitHub `Settings → Billing` 处理支付方式/欠费/账单横幅 → 打开仓库 `Actions → DSH Phone CI → Run workflow` 重跑；成功后 `android-build` 产物即 APK。
- 未解锁期间的替代验证：`ci\run_tests.ps1 -AllowMissing`（bridge 114 / web 122 / 4 个 Android 静态自检 / sync；APK 跳过）。
- **APK 不再依赖 CI**：本机工具链已就绪并已出包（§3.2 / D-027），CI 只是「换机器可复现 + 回归」的加速器。
- 诊断残留待清理：一次性仓库 `dsh-phone-ci-probe`（私有）与 `dsh-phone-ci-probe-pub`（公开）需手动删除（当前 token 无 `delete_repo` scope）。

## 4. 已验证 / 未验证（诚实清单）

**已在本机真跑过并通过**

| 项 | 命令 | 结果 |
|---|---|---|
| Bridge 单测 | `python -m pytest bridge\tests -q` | **114 passed** |
| Bridge 启动 | `python -m bridge --config bridge\config.example.toml --run-for-seconds 3` | banner+二维码+配对 URI+优雅退出，exit 0 |
| Bridge 自检 | `python -m bridge --self-test` / `--smoke` | 5 PASS / 6 PASS（含真 runtime start/close） |
| 真 ASR | `bridge/state/real_asr_e2e.py` | PASS（见 §2.3） |
| **端到端（模拟手机）** | `scripts\e2e_phone_sim.py --asr fake` / `--asr sherpa` | **21 checks / 22 checks PASS，exit 0**（见 §2.4） |
| ASR 基准 | `scripts\bench_asr.py --backend sherpa` | NFR PASS |
| Web 单测 | `cd app\web; node --test --test-isolation=none "tests/*.test.js"` | 122 tests 全绿 |
| **Android 构建（本机）** | `.tools` 工具链 + `gradle assembleDebug`（见 `app/android/BUILD-LOCAL.md`） | **BUILD SUCCESSFUL**，`app-debug.apk` 3,867,632 B，SHA256 `72C34DBA…718CA408`，badging `com.dsh.phone` 1.0.0 / 10000 |
| **Android 单测** | 直连 JUnit4（`BUILD-LOCAL.md` §7.2）／ASCII 副本 `testDebugUnitTest`（§7.3） | **53 tests / 0 failures**（8 个测试类） |
| Android 静态 | `app\android\tools\{verify_protocol,check_contract,check_kotlin_static,check_gradle_config}.py` | 69 / 16+16 / 20 files / 64 checks 全 OK |
| CI 配置 | `python ci/check_workflow.py --strict` + pyyaml 解析 | exit 0 / 4 jobs |
| 本地 CI 复现 | `ci\run_tests.ps1 -AllowMissing` | exit 0（仅 APK 因无 SDK 跳过；APK 已由本机构建补上） |

**未验证（需要你 / 需要环境）**

1. **真机**：录音、蓝牙路由、前台服务、Keystore、WebView `appassets.local` —— 按 `app/android/README.md` 的 M3 runbook 逐条验收。
2. **GitHub CI 首跑**：Kotlin 编译、APK 产物、JUnit 在 JDK17 上跑、bash 版 `sync_web.sh`。（当前被账号 billing lock 阻塞，见 D-026 / §3.3）
3. **真实模型调用**：Bridge 侧 SDK 接线只验证到 `start/initialize/start_session/close`；`session/prompt` 真调用需要你配 `apiKey` 后自测（注意 D-025 的 runtime 版本偏斜：本机 installed 是 `0.2.2-rc.1`，SDK 是 `0.2.2rc2`）。

## 5. 沙箱/本机已知坑（踩过就记下来）

| 坑 | 症状 | 处理 |
|---|---|---|
| DSH 沙箱 ACL | `pwsh` 全部失败（`SetNamedSecurityInfoW failed (Win32 5)` / `temp grant materialization failed`） | 已修：给工作区与 `D:\others\DSH\temp` 加当前用户可继承 FullControl（见 DECISIONS/.acl-recovery） |
| 沙箱禁止 pip 写 site-packages | `sherpa-onnx` 装不进 conda 环境（Errno 13） | 手动解包到 `models/.pipdeps` + `PYTHONPATH`；正常机器直接 pip |
| conda base 旧 onnxruntime.dll 抢 PATH | `requested API version [28] ... Current ORT Version is: 1.17.1` | 把 `site-packages/onnxruntime/capi/onnxruntime.dll` 复制到 `sherpa_onnx/lib/`（D-017） |
| PowerShell 5.1 读中文乱码 | 库里中文显示成 `鏈壘鍒?` | 文件是 UTF-8，用编辑器/`-Encoding UTF8`；别以为是文件坏了 |
| 沙箱内 `node --test` 全量跑 | `Error: spawn EPERM` | 用 `node --test --test-isolation=none "tests/*.test.js"`；ubuntu CI 不受影响 |
| Windows 控制台 GBK | Bridge 启动时 `UnicodeEncodeError` 崩 | 已修：二维码自动降级 ASCII、banner 用 ASCII 边框 |
| 工作区有 24 个删不掉的临时目录 | `pytest-cache-files-*` / `.tmp` / `.pip-tmp`（DSH deny ACE） | 已 gitignore，不影响仓库；无需强行清理 |
| 沙箱禁止子进程管道 → Gradle 测试 worker 起不来 | `testDebugUnitTest` 报 `ClassNotFoundException: GradleWorkerMain` / `管道正在被关闭` | 用 `BUILD-LOCAL.md` §7.2 直连 JUnit4，或 §7.3 在 ASCII 临时副本里跑官方任务 |
| 工程路径含中文（`D:\code\apps\手机端`） | AGP 直接拒绝：「Your project path contains non-ASCII characters」；`aapt2` 日志里路径变乱码 | `app/android/gradle.properties` 已设 `android.overridePathCheck=true`（CI 英文路径不受影响） |
| 干净机器缺 Build Tools 34.0.0 | AGP 8.7.x 默认要 34.0.0，而 SDK 只装 35.0.0 → `Failed to find Build Tools revision 34.0.0` | `app/android/app/build.gradle.kts` 已显式钉 `buildToolsVersion = "35.0.0"`（D-027） |

## 6. 建议的下一步（按优先级）

1. **真机验收（M3）**：把 `app/android/app/build/outputs/apk/debug/app-debug.apk`（3.87 MB，1.0.0）装到 Android 10+ 手机 → 照 `app/android/README.md` runbook 走 A/B/C/D/E 五组场景，把失败项记到 issue/README 的 gap 表。
2. **CI 首跑**：先按 D-026 解除账号 billing lock；再在仓库 `Actions → DSH Phone CI → Run workflow` 重跑，先看 `bridge-tests` + `web-tests`，再看 `android-build`；红了先看 job 里失败 step 的日志（`Show build diagnostics on failure` 会打印 SDK 路径）。
3. **真模型联调**：在 `bridge/config.toml` 配 `apiKey/provider/model` + `[dsh].enabled=true`，用 App 或 mock 之外的真连接发一轮 `chat.send`，确认 `assistant.delta`/`tool.status`/审批弹窗。
4. **M3 两个已接受缺口**（D-024）：G3（LE Audio 失败不回退 HFP）、G4（`MicService.onDestroy` 不清录音态）——真机复现后修。
5. **性能余量**（R-09）：首字 969–1000ms 贴着 NFR 1000ms；V1.1 可换更小 chunk / 更轻模型。

## 7. 关键文件索引

```
docs/PROTOCOL.md            协议 v1（冻结；子协议 dsphone.v1）
docs/ARCHITECTURE.md        三端架构、JsBridge 契约、配置样例
docs/DECISIONS.md           D-001…D-027 全部决策与理由
docs/TESTPLAN.md            69 条用例 + PRD 39 个 FR 映射
docs/SECURITY.md            STRIDE + 12 项安全设计 + 7 项安全测试
docs/spike/SDK-SPIKE.md     SDK 0.2.2rc2 能力与限制（推翻了 PRD 的部分假设）
bridge/__main__.py          Bridge 入口（--self-test / --smoke）
bridge/connection.py        WSS 连接状态机、音频帧、VAD、重放/resync
bridge/dsh_sdk.py           真 SDK 适配器（D-018…D-022）
bridge/asr/sherpa.py        流式 zipformer + SenseVoice 定稿
app/web/                    WebView UI（零依赖零构建；mock 可独立演示）
app/android/README.md       Kotlin 工程说明 + 离线自检 + M3 真机 runbook
app/android/BUILD-LOCAL.md  本机 JDK17+SDK 安装、出 APK、单测替代跑法（D-027）
.github/workflows/ci.yml    4 个 job：bridge-tests / web-tests / android-build / secrets-scan
```
