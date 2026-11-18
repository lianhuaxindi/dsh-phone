# 决策记录（ADR-lite）

记录 PRD §18 待决问题的落定，以及开发中不可逆/影响面大的技术决策。改决策需 Lead 评审并更新本文件。

## D-001 工作区 = 当前目录 `D:\code\apps\手机端`（替代 PRD 建议的 `D:\code\dsh-phone`）

- 状态：已定（M0）。
- 理由：本会话 DSH 文件策略（workspace-write）只允许写当前工作区；新开目录需更高权限且无收益。
- 影响：仓库根 = 当前目录。PRD §0 的目录约定照此落地。

## D-002 V1 接受「与桌面端 DSH 会话不共享」

- 状态：已定（对应 PRD §18-2）。
- 决策：Bridge 使用独立 `dsh_home`（`bridge/state/dsh_home`），不读写桌面端会话。
- 理由：SDK 会话隔离是 V1 最快路径；跨端共享需理解 DSH 内部存储格式，风险高。

## D-003 TTS 朗读不进 V1

- 状态：已定（对应 PRD §18-3）。
- 决策：V1 只做识别（ASR）不做播报；TTS 列 V1.1。

## D-004 发布渠道：GitHub Releases 侧载 APK

- 状态：已定（对应 PRD §18-4）。
- 决策：不上架商店；debug APK 由 CI 出，release 签名由用户保管（secrets 注入）。

## D-005 Phase0 允许安装 Python 依赖、下载模型

- 状态：已定（对应 PRD §18-5，用户已授权开始开发）。
- 约束：一律装进 conda 环境 `scisim-agent`；先检查已安装版本，只有真实冲突才升级（用户明确要求）。
- 模型（sherpa-onnx，约几百 MB）按需下载到 `models/`，不入库。

## D-006 最低 Android 10（API 29），target SDK 35

- 状态：已定（对应 PRD §18-6）。
- 补充：本机无 Android SDK/JDK，Android 构建走 CI（PRD §17 风险行），本机只跑 Python 测试与协议测试。

## D-007 协议 v1 以 `docs/PROTOCOL.md` 为准，先冻结再并行开发

- 状态：已定（M0）。
- 内容：JSON 信封 + `seq` 单调 + 应用层心跳 + HMAC 认证 + 二进制音频帧 + 错误码表。
- 理由：Bridge / Web UI / Android 三个模块并行开发，协议不冻结会导致返工。
- 变更流程：改 `PROTOCOL.md` → Lead 评审 → 同步 bridge/app 测试向量。

## D-008 认证不引入设备公私钥（V1）

- 状态：已定。
- 决策：TLS（自签 + 指纹 pinning）+ 32B `deviceToken` + HMAC-SHA256(ts,nonce) 防重放。`pair.request.pubKey` 字段保留但不校验。
- 理由：MVP 安全目标（局域网、默认回环绑定、可撤销）已满足；公私钥体系增加复杂度，V2 再评估。

## D-009 流式回复以 SDK 实际能力为准（spike 结论回填）

- 状态：待 spike 确认（`docs/spike/SDK-SPIKE.md`）。
- 预案：若 SDK 只有整段通知，则 `assistant.delta` 只发一帧（整段），App 用打字机动画模拟；协议字段不变。

## D-010 本地历史用 WebView IndexedDB 替代 Room/SQLite

- 状态：已定（M2）。
- 决策：会话/消息/设置存 WebView 的 IndexedDB（按 sessionId 分区）；`deviceToken` 仍只在 Native Keystore。
- 理由：UI 完全在 WebView 里，用 IndexedDB 省掉「Web → Native → Room → 回传」一层桥接与线程同步；PRD §11 的 Room 方案在纯 Web UI 下无收益。偏差已记入 ARCHITECTURE §3.4。
- 风险：清除应用数据会丢历史；导出/备份留 V1.1。

## D-011 扫码配对后置到 M3（M2 手动输入 + `scanPairing` 预留）

- 状态：已定（M2 决策，PRD FR-102 的 P0 扫码降级）。
- 决策：M2 配对页做**手动输入** host/port/fp(64 hex)/code(6 位)，并接受粘贴整条 `dsphone://pair...` URI；`DshBridge` 保留 op `scanPairing`，V1 返回 `{supported:false,reason:"m3"}`；M3 用 ZXing embedded（约 1 MB，不用 Play Services）实现真扫码。
- 理由：本机无 Android SDK/JDK，Android 侧无法本地编译验证，M2 先不加新 native 依赖以降低「首次 CI 构建失败」风险；手动输入已能满足配对闭环。
- 备选（已否决）：WebView `getUserMedia` + JS 自写解码器——WebView 内授权/释放不可靠，且需自维护 QR 解码器。

## D-012 `seq` 由 Native 独占（Web 不生成 seq）

- 状态：已定（M2，契约冻结，见 ARCHITECTURE §3.2.1）。
- 决策：Web 的 `send` 不带/不依赖 `seq`，Native 覆盖注入并记账两个方向；Web 用本地字段 `_reqId` 做请求-响应关联，Native 在 `replyTo` 命中时回填 `_reqTo`（`_reqId` 不出网）。
- 理由：Native 同时发 hello/pair/auth/ping/audio.*/resync，若 Web 与 Native 各自计数必然与协议「每方向单调」冲突。

## D-013 FR-703「重启 Bridge」在 V1 实现为「重连 Bridge」

- 状态：已定（M2）。
- 决策：诊断页按钮文案为「重连 Bridge / Reconnect」，调用 JsBridge `reconnect` op（Native 断开并按退避重建 WSS），不新增 `restartBridge`；进程级重启留 M4/P2，需 Bridge 侧 admin 协议 + 安全评审。
- 理由：手机端无权也不应远程重启电脑端 Python 进程；把已有能力（重连）暴露给诊断页即可覆盖用户的真实用途（连接卡死时恢复）。

## D-014 音频编解码：V1 用 PCM 16 kHz/16-bit/mono（256 kbps），Opus 留 V1.1

- 状态：已定（M1，回应 PRD 内部矛盾）。
- 背景：PRD FR-407 写「64 kbps 目标」，但 §10.2 帧格式与 640 B/20 ms 帧头已冻结为未压缩 PCM16，恰好 256 kbps；同一文档两处冲突。
- 决策：以 §10.2 帧格式为准，V1 用 PCM16（局域网上行 256 kbps 可接受，且免去编解码延迟与 NDK 依赖）；帧头 `[0x00]` 保留 PCM、`[0x02]` 预留 Opus，V1.1 若需 64 kbps 再加 Opus 分支。
- 记录：TESTPLAN TC-AUDIO-04 已按实测 256 kbps 记录偏差（R-05/R-06 系列）。

## D-015 WebSocket 子协议由 `dsphone/1` 改为 `dsphone.v1`（RFC 6455 合法性修正）

- 状态：已定（M1，协议 v1.0 → v1.0.1；**不带 `/`**，其余协议内容不变，属文档/握手层修正而非不兼容变更）。
- 事实：RFC 6455 的子协议值是 HTTP `token`，allowed 字符不含 `/`。实测 websockets 17.4 在 `serve(subprotocols=["dsphone/1"])` 与客户端 `connect(subprotocols=["dsphone/1"])` 均直接 `ValueError: invalid subprotocol`；浏览器 `new WebSocket(url, ["dsphone/1"])` 与 Android 客户端同样会按 token 语法拒绝——即原写法在真实手机端根本无法握手。
- 决策：`Sec-WebSocket-Protocol: dsphone.v1`（`.` 是合法 token 字符，且保留「版本 1」语义）；未来不兼容升级用 `dsphone.v2`。`dsphone://pair?...` 配对 URI 与 JSON 信封的 `v:1` 不受影响。
- 影响面：`docs/PROTOCOL.md`（§1/§2/§14 + 示例）、`docs/ARCHITECTURE.md`、`README.md`、`bridge/server.py` 常量与测试、`app/android/.../protocol/Protocol.kt`、`app/web/js/protocol.js`。
- 说明：`docs/PRD.md` 与根目录 `项目prd-手机端.md` 是需求原件，**故意不改**，本决策即为偏差记录。

## D-016 `deviceToken` 服务端只存 sha256 与 §6.1 HMAC 需要原始 token 的矛盾 → 采用「hash 索引 + AES-GCM 密文」

- 状态：已定（M1，回应 bridge-core 发现的协议矛盾）。
- 背景：PROTOCOL §5.3 原写「服务端只存 sha256(token)」，但 §6.1 规定 HMAC key = token 原始 32 字节，服务端必须拿到原始 token 才能验签，两处矛盾。
- 决策（已写入 PROTOCOL §5.3）：服务端持久化 `sha256(token)`（索引/吊销/可打印）与 **AES-256-GCM 封装的密文**（密钥 = 每安装独立的 `bridge/state/master.key`，文件 0600）；原始 token 绝不进日志/诊断/导出。
- 备选（已否决）：把 HMAC key 改为 `sha256(token)`——服务端仍要存该值才能验签，安全增益为零却需重算全部测试向量。

## D-017 ASR 真模型实测：延迟数据、NFR-1 风险登记与安装方式

- 状态：已定（M1，asr-pipeline 交付后的实测结论；NFR 目标值不变，风险登记）。
- **实测环境**：本机 CPU（无 NPU），Python 3.11.17（scisim-agent），sherpa-onnx 2.0.1，模型 = 流式 zipformer 中英双语 int8（encoder 173.5 MiB / decoder 12.5 MiB / joiner 3.1 MiB，合计 ~189 MiB）+ SenseVoice int8（~228 MiB）；样例 = 中文 4.88 s 语音（`scripts/samples/speech_16k.wav`，裁剪 700 ms 前导静音后），repeat=3。
- **数据（p50）**：first partial **969 ms**（968/969/984）、final **313 ms**（313/313/344）、partial 速率 **2.0/s**（≤5 ✓）、RTF **1.1×**、当前流 chunk 解码 380–398 ms/chunk、SenseVoice 整段离线 289–319 ms/流。
- **根因**：流式 zipformer 约需 1.7 s 音频上下文才吐首个非空 token（probe：audio=1700 ms 时首现文本）；样例前 0.7 s 是静音，故生产中 VAD 从开口起算首字 ≈0.97 s。
- **风险 R-09（登记，不阻塞 V1）**：NFR-1「首字 ≤1.0 s」实测余量仅 16–31 ms，真机噪声/冷启动/后台争用下可能偶发超；V1.1 优化方向 = 更小 chunk（320→200/160 ms）、更轻量化模型档、或模型切换。NFR 目标值 **不因本机 CPU 实测而下调**。
- **安装方式（重要）**：本会话沙箱禁止 pip 写 site-packages/temp（OSError Errno 13），故 sherpa-onnx 未装进 conda 环境；改为把 wheel 手动解包到仓库内 `models/.pipdeps/`（`models/*` 已 gitignore），`bridge/asr/sherpa.py` 自动把该目录注入 sys.path。**生产/CI 仍应按常规 `pip install sherpa-onnx==2.0.1`**（CI workflow 里作为可选 ASR extra 安装），仓库不提交任何二进制。
- **运维坑（诊断页/README 提示）**：PATH 上 conda base 的旧 `onnxruntime.dll`（1.17.1）会遮盖 pip 安装的 1.32，报 `requested API version [28] ... Current ORT Version is: 1.17.1`；修复 = 把 `site-packages/onnxruntime/capi/onnxruntime.dll` 复制到 `sherpa_onnx/lib/` 同目录。
- **识别质量**：ITN 生效（示例输出 `开饭时间早上9点至下午5点。`）；偶有近音误识（"开放"→"开饭"），V1.1 通过换模型/热词/后处理改进，V1 接受。
- 模型下载：hf-mirror 108 s 完成，`scripts/download_models.py --verify` 两个模型 sha256 全部 OK。

## D-018 `assistant.delta` 的语义：单帧整段 + 客户端本地打字机

- 状态：已定（M0 spike 结论，`docs/spike/SDK-SPIKE.md` §6.1）。
- 事实：SDK 0.2.2rc2 **没有任何 token 级增量事件**；文本只在 `assistant/message` 事件里整段到达（每 step 一条），`data.stream` 是结算后的记录、不能实时。
- 决定：`assistant.delta` 保留，但语义 = 「文本追加片段」；本实现每 turn 只发 **1 条 `index=0` 的整段**（PROTOCOL §8 已注明）。客户端打字机动画是**本地渲染效果**，不是模型流式；UI/日志/诊断不得标称为「实时 token 流」。
- 否决：Bridge 侧把整段按字符切片伪装成多帧 delta——违反「不伪造增量」，且会破坏 `index` 单调语义与重放。
- 影响：M1 验收里「首字延迟」按「Bridge 收到 assistant/message → 客户端开始显示」计，不按 token 到达计。

## D-019 `chat.cancel` 语义：SDK 不支持取消，必须如实报错

- 状态：已定（spike §6.3）。
- 事实：SDK 与协议层都没有 cancel/interrupt/stop，唯一手段是 `HarnessClient.close()`（shutdown + terminate/kill 整个运行时进程）。
- 决定：V1 收到 `chat.cancel` 一律回 `turn.error{code:3003,"message":"cancel unsupported (SDK 0.2.2rc2)"}`（PROTOCOL §8 已注明），**不得**静默忽略、不得谎报 `assistant.done{gaveReason:"cancelled"}`。UI 侧「停止生成」按钮文案改为「停止（可能无效）」，并在诊断页记录该限制。
- 未来：若 SDK 增加 cancel，或改用 Host API，再升级协议（`dsphone.v2`）实现真取消。

## D-020 审批/提问字段映射与未知键透传

- 状态：已定（spike §4.3/§6.2）。
- 审批：`IncomingRequest{id, method:"approval/request", payload{sessionId,toolName,callId,reason}}` → `approval.request{id,tool,reason,callId,sessionId}`；回 `respond(id,{"outcome":"allowed-once"|"rejected"|"cancelled"})`。SDK **没有 options 字段**、唯一批准值是 `allowed-once`（没有「总是允许」）→ 协议不提供「记住选择」，V1 每次都要弹窗。
- 未知键：`IncomingRequest.payload` 是原始 `JsonObject`，SDK 只建模 4 个字段；Bridge 必须**透传未建模键**到 `approval.request.extra`（可选 map），且不得因未知键解析失败。
- 提问：`UserQuestionRequest{requestId,sessionId,questions[{id,question,options?}]}` → `question.request`；答 `{"answers":[{"id","selected":[],"custom"?}]}`；取消回**裸 dict** `{"cancelled": true}`（pydantic 模型无 cancelled 字段）；到期未答 = 取消结案并发 `question.expired`。
- 审批超时 10 分钟默认拒绝（PROTOCOL §9），回 `{"outcome":"rejected"}` 而不是 `cancelled`。

## D-021 SDK 运行时发现与独立 `dsh_home`（强化 D-002）

- 状态：已定（spike §5）。
- 事实：SDK **从不搜 PATH**；`dsh_bin` 为空时用 wheel 内置 `deepseek-harness-sdk-runtime-win-x64.exe`（147,907,584 B，自报 `0.2.2-rc.2`），否则 resolve 给定路径；必须显式给 `dsh_home` 或 env `DSH_HOME`，否则 `ValueError: HarnessConfig requires an explicit dsh_home or non-empty DSH_HOME; the Python SDK never uses ~/.dsh implicitly`；argv = `(base…, "--profile", "sdk")`。
- 决定：配置 `[dsh].runtimeExe`（空 = 自动探测 wheel 内置 exe；填路径则覆盖，例如本机 `D:\others\DSH\resources\runtime\cli\bin\dsh.cmd`）+ `[dsh].home`（默认 `state/dsh_home`，启动时绝对化并 mkdir）。**禁止**回落到用户桌面 `~/.dsh` 或 `C:\Users\15653\.dsh`（不能用桌面端凭据/会话，D-002）；因此 Bridge 首次真正调模型需要用户自己在 `bridge/config.toml` 里配 `api_key`/`provider`/`model`。
- 依赖：`deepseek-harness-sdk==0.2.2rc2`（`--pre`）+ `deepseek-harness-runtime-bin==0.2.2rc2` 单列 `bridge/requirements-sdk.txt`；CI 不装（走 fake），避免 147MB 下载。

## D-022 runtime 来源解析链与「真运行时不进 CI」的边界（补强 D-021）

- 状态：已定（M1 SDK 真适配器冒烟实测，2026-11-17）。
- 实测事实：
  - 用 SDK wheel **内置** runtime exe（默认路径）在本机**不可用**：runtime 启动 2.6s 后退出，stderr 为
    `Error: dsh: host preparation failed: No usable native binding found for node-addon-require-builtin-win32-x64-msvc (auto)`，
    适配器侧表现为 `TransportClosedError: DeepSeek Harness runtime stdout closed`。
  - 改用已安装 DSH 的 runtime（`D:\others\DSH\resources\runtime\cli\bin\dsh.cmd`）后，经 `bridge/dsh_sdk.py` 的
    `start → initialize → start_session → close` **全链路成功**：start 5496.9ms（含 initialize 冷启动）、session 成功
    （sdkSessionId `session-12008033c3074c42aabbfc374f30c448`）、close 110.8ms、无 `*runtime*` 残留进程；
    全程未调用 `run()`/`session/prompt`（不烧 token）、无 api_key；`DSH_HOME=bridge/state/dsh_home_e2e`（独立，不碰桌面 `~/.dsh`）。
- 决定：`[dsh].runtimeExe` 为空时按 **a) 显式配置/`DSH_PHONE_RUNTIME_EXE` 环境变量 → b) 本机已装 DSH（PATH 的 `dsh.cmd`/`dsh.exe`、常见安装目录候选）→ c) SDK wheel 内置 exe** 的顺序解析；**每步的候选路径、命中结果、失败原因都要写日志**。全部失败时报可读错误并给出下一步（安装 DSH 桌面版 / 配置 runtimeExe / 见本决策）。
- 理由：产品是「手机端连用户电脑上的 DSH」，用户电脑本就装有 DSH；wheel 内置 runtime 只在「纯净 Python 环境」有意义，且当前版本在本机有原生绑定缺陷。两者都保留，避免把产品绑死在某一安装形态上。
- 边界：**CI 永远不装 SDK/runtime、不跑真 runtime**（147MB+、`--pre`、且需要凭据），M1/M2 走 `[dsh].enabled=false` fake；真 runtime 只在用户机器上由 `--self-test` 冒烟（start/initialize/close，不发 prompt）。
- 影响：`bridge --self-test` 必须能在无 SDK、无 runtime、无模型、无凭据的机器上给出 PASS/SKIP/FAIL 结论而非崩溃；错误文案须含「下一步怎么做」。

## D-023 断线检测与「重连 ≤5s」的验收口径

- 状态：已定（M2/M3 验收硬化，android-shell 提出 D3 场景歧义）。
- 事实：协议心跳为服务端每 15s ping、60s 无响应断开（PROTOCOL §7）；客户端在「正常断线」与「黑洞（TCP 不 RST）」两种故障下的检测时延量级不同。
- 决定：
  1. **正常断线**（飞行模式切换、Wi-Fi 掉线、Bridge 进程被杀、TLS 握手失败）：OkHttp `onFailure` 立即触发 → 退避重连 1/2/4/8/15s → 网络恢复后 **≤5s 内回到 online**。PRD §15 的「断线重连 ≤5s」按**此口径**验收。
  2. **黑洞**（网络物理断开但连接不报错）：靠 WebSocket ping/pong。`WssClient` 的 OkHttpClient **必须**配置 `pingInterval(15, TimeUnit.SECONDS)`（OkHttp 在连续无 pong 后主动关连接 → `onFailure` → 重连）；应用层 45s 无入站消息 watchdog 仅作兜底，**不得**作为正常重连路径。
  3. 黑洞检测时延单独记实测值（预期 30–45s），不作为 NFR 失败；若真机测试认为不可接受，再评估把 pingInterval 降到 10s。
- 理由：5s 目标来自「网络恢复后快速回到会话」的产品诉求，而不是要求黑洞秒级发现；把两者混为一谈会导致无谓的短心跳（耗电、断流风险）。
- 影响：`app/android/README.md` 的 M3 runbook D3 必须写清两种口径与预期日志；TESTPLAN 的 NFR 用例按口径 1 判定。

## D-024 已知缺口（M3 真机复现前接受）

- 状态：已定（M2 静态复盘，android-shell 的 `app/android/README.md` 有逐条明细与修法）。
- 已**修复**（M2 收尾）：G1 录音启动失败无 `audio.error`、G2 路由切换后 recorder 重启失败被忽略、G5 Activity finishing 时录音不停、G6 无 `audio.ready` 超时导致静默丢帧、G7 读失败缺 `DshPhone` 结构化日志。
- **M3 真机复现后再处理/接受**：G3（LE Audio 失败不回退 HFP）、G4（`MicService.onDestroy` 不清录音态）。
- 理由：这两项的正确行为依赖真实耳机/ROM 的时序与厂商实现，纯静态推断容易「改错方向」；V1 先记录、真机复现后修，避免把未知风险伪装成已解决。

## D-025 runtime 版本偏斜（SDK 0.2.2rc2 ↔ installed runtime 0.2.2-rc.1）

- 状态：已定（M1 冒烟实测）。
- 事实：`deepseek-harness-sdk==0.2.2rc2` 自带的 wheel 内置 runtime 报 `0.2.2-rc.2`（本机因原生绑定缺陷不可用）；本机已安装 DSH 的 runtime 报 `0.2.2-rc.1`；用 rc.1 跑 `start → initialize → start_session → close` 成功（冷启动 4.1–5.5s、close 94–111ms），但**未验证 `session/prompt` 真模型调用**。
- 决定：V1 **不强制** runtime 版本等于 SDK 版本；`--self-test` 与启动日志打印所选 runtime 的 `--version`，当与 SDK 声明版本不一致时记 WARNING（不阻断）。真 prompt 路径由用户在配置 `api_key` 后自测，若 rc.1 出现协议不兼容再要求安装与 SDK 匹配的 runtime。
- 理由：产品定位是「连用户电脑上已装的 DSH」，用户机器上的 runtime 版本不受我们控制；把版本号写死会制造无法满足的前置条件。同时保留 upgraded 提示，避免 silently 用错版本。
- 影响：`bridge/RUNTIME.md` 记录版本偏斜与本机实测；CI 不涉及（fake）。

## D-026 GitHub 仓库公开 + Actions 被账号级 billing lock 阻塞（2026-11-18）

- 状态：已定（交付收尾）。
- 现状：仓库 `https://github.com/lianhuaxindi/dsh-phone`，`main` HEAD=`fecc151`（本地工作区干净）。仓库按用户选择改为 **public**；`ci.yml` 已通过 `actionlint` v1.7.7（修掉一处真问题：job 级 `name:` 里用了 `${{ env.NODE_VERSION }}`，改为字面量）。
- 故障现象：所有 run 都 `startup_failure`（私有期间 `path=BuildFailed`、0 job；改 public 后 job 会创建但立刻失败、steps 为空）。
- **根因（决定性证据）**：job 级 check-run annotation 原文 —— `The job was not started because your account is locked due to a billing issue.` ⇒ 账号级 billing lock，与 workflow YAML / 仓库内容无关。
- 已排除：① A/B 对照：同账号新建的一次性公开/私有仓库跑同一个 5 行 smoke workflow，同样被拦（annotation 一致），故与 `allowed_actions`、仓库可见性、工作流注册无关；② 关闭再开启仓库 Actions 权限（重建注册）无效；③ `actionlint` 已无告警。
- 决定：
  1. **公开仓库不能绕过 billing lock**，仍须用户在 GitHub `Settings → Billing`（支付方式/欠费/账单横幅）处理；处理完重跑 `workflow_dispatch` 即可。
  2. 未解锁前，CI 的唯一执行路径是本地 `ci\run_tests.ps1`（已全绿）；`.github/workflows/ci.yml` 保留，额度恢复即可用。
  3. token 不落仓库；推送用临时 tokenized remote URL，推送后立即 `set-url` 复原（沙箱内 credential helper 会因 `sh.exe` 建管道被拒而失败）。
- 用户待办：① 处理 Billing 锁定；② 手动删除诊断用一次性仓库 `dsh-phone-ci-probe`（私有）与 `dsh-phone-ci-probe-pub`（公开；当前 token 无 `delete_repo` scope，API 删不掉）；③ 解锁后确认 4 个 job + APK 产物；④ 收尾建议 revoke 当前 PAT。
- 影响：APK 仍无法产出（本机无 JDK/SDK 是 D-006，GitHub 端又被 lock 阻塞）；真机验收（M3）与真模型联调（需 apiKey）仍待用户参与。



