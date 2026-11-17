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
