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
