# CI 流水线与本地构建脚本（M4）

本目录负责 dsh-phone 的持续集成：bridge Python 测试、WebView 前端测试、Android debug APK 构建、仓库密钥扫描。
**Android 构建只在本机之外的 GitHub Actions 跑**（DECISIONS D-006：开发机没有 JDK / Android SDK）。

## 1. 目录与文件

| 文件 | 作用 |
| --- | --- |
| `.github/workflows/ci.yml` | GitHub Actions 主 workflow（4 个 job，见 §2） |
| `ci/version.json` | 版本号单一事实来源（versionName / versionCode / protocolVersion / minSdk / targetSdk） |
| `ci/bump_version.ps1` | 版本号递增 / 显式设置（默认 dry-run，`-Write` 才落盘） |
| `ci/check_workflow.py` | 本仓库 CI 配置自检（YAML 结构、job 关键步骤、版本规则、.gitignore、模块输入就绪度） |
| `ci/secrets_scan.py` | 密钥 / 敏感文件扫描（CI 的 secrets-scan job 与本地共用） |
| `ci/run_tests.ps1` | 本地一键跑 CI 能跑的部分（配置自检 + bridge + web + sync 一致性 + Android 4 个离线自检） |
| `ci/build_apk.ps1` | 本机有 JDK17 + Android SDK 时才可用；调 sync_web + gradle assembleDebug 并把 APK 拷到 `dist/` |
| `scripts/sync_web.ps1` / `scripts/sync_web.sh` | 把 `app/web/` 同步到 Android assets（clean 同步，幂等），Linux CI 用 .sh |

## 2. GitHub Actions（`.github/workflows/ci.yml`）

触发：`push` 到 `main` / `master`、`pull_request`、`workflow_dispatch`；`concurrency` 取消同 ref 的旧 run。

| job | runner | 关键步骤 | 产物 |
| --- | --- | --- | --- |
| `bridge-tests` | ubuntu-latest + windows-latest（matrix） | 检查 bridge 输入 → setup-python 3.11（pip 缓存）→ 安装 `bridge/requirements.txt`（`requirements-dev.txt` 缺失时 warning 并回退装 `pytest pytest-asyncio`）→ `python -m pytest bridge/tests -q -p no:cacheprovider --junitxml=bridge-test-results.xml` | `pytest-report-<os>` artifact（always 上传） |
| `web-tests` | ubuntu-latest | 检查 `app/web/index.html` + `app/web/tests/` → setup-node 20 → `node --test app/web/tests` | 无 |
| `android-build` | ubuntu-latest | 检查 android/web 输入（含 `app/android/tools/` 四个自检脚本）→ sync_web.sh → setup-java 17 (temurin) → 安装 SDK 组件（platforms;android-35、build-tools;35.0.0、platform-tools）→ **4 个纯 stdlib 离线自检**（`verify_protocol.py`、`check_contract.py`、`check_kotlin_static.py`、`check_gradle_config.py`，任一非 0 即 job 失败）→ 从 `gradle-wrapper.properties` 解析 Gradle 版本（缺省 8.9）→ `gradle/actions/setup-gradle@v4`（自带 Gradle 缓存）→ 缺 wrapper 时 `gradle wrapper` 生成 gradlew/jar → 从 `ci/version.json` 读版本并注入 `-Pdsphone.versionName/-Pdsphone.versionCode` → `./gradlew assembleDebug` → **`./gradlew testDebugUnitTest`（JUnit4，含 PROTOCOL §6.3 HMAC 向量，共 8 个测试类）** | `app-debug-<versionName>-<versionCode>` artifact，保留 14 天 |
| `secrets-scan` | ubuntu-latest | checkout `fetch-depth: 0` → 解析 diff 基线（PR 用 base sha，push 用 event.before）→ `python3 ci/secrets_scan.py --range <base>..<head>`（基线不可解析时回退 `--all`） | 无（发现问题 exit 1 使 job 失败） |

其他约定：
- Python 3.11 / Node 20 / JDK 17 / Gradle 8.9（版本与 `app/android` 的 AGP 8.7.3 兼容，见 ci.yml `env`）。
- 仓库**不提交** `gradle/wrapper/gradle-wrapper.jar` 与 `gradlew`，由 CI 的 `gradle wrapper --gradle-version <解析值>` 生成（已被 .gitignore 忽略）。
- CI 环境变量：`PYTHONUTF8=1`、`PYTHONIOENCODING=utf-8`（Windows runner 中文输出不乱码）。
- debug APK 由 AGP 默认 debug keystore 自动签名；release 签名走 GitHub Secrets（PRD §16 / D-004 侧载分发）。

## 3. 版本号规则（`ci/version.json`）

```json
{ "schemaVersion": 1, "versionName": "1.0.0", "versionCode": 10000,
  "protocolVersion": "1", "minSdk": 29, "targetSdk": 35 }
```

- `versionName` 必须是 semver（`major.minor.patch`），对用户在「设置-关于」中展示为 `1.0.0+<versionCode>`。
- `versionCode` 公式：`major*10000 + minor*100 + patch`，且**必须严格递增**（取 `max(公式值, 旧值+1)`，永不回退）。
- CI 在构建前校验 `versionCode >= 公式值`，不满足直接失败；`protocolVersion` / `minSdk` / `targetSdk` 也参与自检。
- 递增：`powershell -NoProfile -ExecutionPolicy Bypass -File ci\bump_version.ps1 -Part patch|minor|major|code -Write`
  - `-Part code` 只递增 build 号；`-Part set -Version 1.2.0` 显式设置（不允许降 versionName）；
  - `-SyncAndroid` 尽量同步改 `app/android/app/build.gradle.kts` 的 `versionCode/versionName`（缺文件只 WARN）；
  - 默认 dry-run，只打印将变成什么，不写文件；`-Force` 只放开 versionName 降级，versionCode 永不放宽。

### Gradle 版本注入契约（给 android-shell）

CI 构建命令带 `-Pdsphone.versionName=<x> -Pdsphone.versionCode=<n>`；`app/android/app/build.gradle.kts` 用
`findProperty("dsphone.versionName")` / `findProperty("dsphone.versionCode")` 读取，缺省回退字面量。
CI job 名里的版本从 `ci/version.json` 来，因此 bump 版本后无需改 Gradle 文件也能出对应版本 APK。

## 4. 本地复现（Windows / PowerShell 5.1）

> 本机执行策略禁止直接运行未签名 .ps1：必须用
> `powershell -NoProfile -ExecutionPolicy Bypass -File <script>`（本仓库所有本地命令都按此写法）。

```powershell
# 一键本地检查（默认 strict：缺模块即失败；只跑已就绪部分用 -AllowMissing）
powershell -NoProfile -ExecutionPolicy Bypass -File ci\run_tests.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File ci\run_tests.ps1 -AllowMissing

# CI 配置自检（需要 pyyaml；本机 python 在 D:\anaconda3\envs\scisim-agent\python.exe）
D:\anaconda3\envs\scisim-agent\python.exe ci\check_workflow.py --strict

# 密钥扫描（默认 --working-tree；也支持 --all / --range BASE..HEAD）
D:\anaconda3\envs\scisim-agent\python.exe ci\secrets_scan.py --working-tree
D:\anaconda3\envs\scisim-agent\python.exe ci\secrets_scan.py --all
D:\anaconda3\envs\scisim-agent\python.exe ci\secrets_scan.py --range HEAD~1..HEAD

# Web -> Android assets 同步（clean，目标目录被 .gitignore）
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\sync_web.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\sync_web.ps1 -VerifyOnly
bash scripts/sync_web.sh             # Linux/macOS/Git Bash
bash scripts/sync_web.sh --verify-only

# Android 纯 stdlib 离线自检（与 CI android-build 前置步骤完全一致；本机可跑）
D:\anaconda3\envs\scisim-agent\python.exe app\android\tools\verify_protocol.py
D:\anaconda3\envs\scisim-agent\python.exe app\android\tools\check_contract.py
D:\anaconda3\envs\scisim-agent\python.exe app\android\tools\check_kotlin_static.py
D:\anaconda3\envs\scisim-agent\python.exe app\android\tools\check_gradle_config.py

# 版本号
powershell -NoProfile -ExecutionPolicy Bypass -File ci\bump_version.ps1 -Part patch          # dry-run
powershell -NoProfile -ExecutionPolicy Bypass -File ci\bump_version.ps1 -Part patch -Write   # 真写
```

### Android 本地构建

本机（D-006）没有 JDK / Android SDK，`ci\build_apk.ps1` 会直接以中文提示退出（exit 2）。需要 JDK17 + Android SDK 的机器上：

```powershell
$env:ANDROID_HOME = 'C:\Android\Sdk'   # 或 ANDROID_SDK_ROOT
powershell -NoProfile -ExecutionPolicy Bypass -File ci\build_apk.ps1
# 产物：app/android/app/build/outputs/apk/debug/app-debug.apk
#       另复制一份到 dist/dsphone-<versionName>-<versionCode>-debug.apk
```

CI 产物：Actions run 的 Artifacts 里下载 `app-debug-<versionName>-<versionCode>`。

## 5. sync_web 规则

- 源：`app/web/`；目标：`app/android/app/src/main/assets/web/`（WebView 用 `file:///android_asset/web/index.html`）。
- 排除：`tests/`、`node_modules/`、`*.md`（任意路径的 Markdown 文档，如 README.md / ACCEPTANCE.md）、`package.json`（仅给 `node --test` 识别 ESM 用的前端工具文件，运行时不需要）。
- clean 同步：复制新增/变更文件 → 删除目标里多余文件 → 清理空目录；二次运行只打印「未变 N」，幂等。
- 目标目录被 `.gitignore` 忽略，仓库不提交；CI 在 assembleDebug 前用 bash 版同步，本地用 PowerShell 版。
- 源目录缺 `index.html` 或目标缺 `app/android` 时给出明确中文报错并 exit 1。

## 6. 密钥扫描规则（ci/secrets_scan.py）

- 模式：`--working-tree`（默认，未跟踪+已修改文件）、`--all`（全部 tracked 文件）、`--range BASE..HEAD`（commit diff，CI 用）。
- 文件名规则（BLOCK）：`local.properties`、`keystore.properties`、`google-services.json`、`id_rsa*` 等；
  `*.jks/*.keystore/*.p12/*.pfx/*.pem/*.key/*.crt/*.cer/*.der/*.apk/*.aab/*.onnx/*.gguf/*.safetensors/*.pt/*.pth/*.sqlite3`；
  `.env*`（模板除外）；生成物目录 `app/android/app/src/main/assets/web/`、`bridge/state/`、`bridge/certs/`、`models/`（`.gitkeep` 除外）。
- 内容高置信（BLOCK）：私钥块、PEM 证书块、`AKIA…`、`ghp_/gho_/…`、`xox[abprs]-`、`sk-…`、`AIza…`；
  中置信：64 位 hex token、`token=…`/`password=…` 赋值。
- 豁免：`docs/**`、`*.md` 整体豁免内容规则（协议文档含 64hex 测试向量）；`tests/` 与 mock/fixture 数据高置信只 WARN；
  与 `docs/**/*.md` 中**逐字节相同**的 64 位 hex（如 PROTOCOL.md §6.3 冻结的 deviceToken/hmac 测试向量，各端实现会复制）自动豁免；
  digest 上下文（行含 `sha256/sha1/md5/checksum/digest/hash`、裸 64hex 常量、`models.py` 等清单文件）不误报；脚本自身豁免。
- 输出会把命中的敏感片段遮蔽（masked）；有 BLOCK 时 exit 1，用法/基线错误 exit 2。CI 的 secrets-scan job 红即表示需要处理。

## 7. 已知本地环境限制（DSH 沙箱 / D-006）

| 现象 | 原因 | 本地对策 |
| --- | --- | --- |
| `ci\build_apk.ps1` exit 2 | 本机无 JDK/Android SDK（D-006） | 只能等 CI 出 APK |
| `node --test app/web/tests` → `Error: spawn EPERM` | 沙箱禁止子进程/命名管道，node test runner 需 spawn | `node app/web/tests/protocol.test.js` 逐文件直接跑（`run_tests.ps1` 已自动回退） |
| pytest `tmp_path` → `PermissionError WinError 5` | 沙箱临时目录 ACL | 加 `--basetemp=ci\_tmp -p no:cacheprovider`（CI 无此问题） |
| `bash` 不可用 / Git Bash `couldn't create signal pipe` | 沙箱限制 | `scripts/sync_web.sh` 只能靠 CI（ubuntu）验证，本地用 .ps1 等价验证 |
| 直接 `& script.ps1` 被拒 | PowerShell 执行策略 | 用 `powershell -NoProfile -ExecutionPolicy Bypass -File` |

## 8. 排障分工

| CI 失败 job | 先看 | 责任模块 |
| --- | --- | --- |
| `bridge-tests` | pytest 失败/收集错误 | bridge-core（把完整报错贴给它） |
| `web-tests` | `node --test` 失败 | web-ui |
| `android-build` | Gradle/Kotlin/AGP/SDK 报错（CI 失败时会打印 SDK 与 build output）；若 4 个离线自检之一失败，看对应脚本输出 | android-shell（把完整报错贴给它） |
| `secrets-scan` | `ci/secrets_scan.py` 输出 | 引入密钥/文件的模块 |
| workflow 结构本身 | `python ci/check_workflow.py --strict` | ci-build（本目录） |

新增/修改 CI 步骤后务必跑一次 `python ci\check_workflow.py --strict`，确保关键 job、矩阵、版本规则与 .gitignore 没被改坏。
