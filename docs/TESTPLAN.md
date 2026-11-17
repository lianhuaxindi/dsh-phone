# 测试计划（TESTPLAN）v1.0

| 项 | 值 |
|---|---|
| 适用 | 协议 v1.0 FROZEN、PRD v1.0、ARCHITECTURE v1.0、DECISIONS D-001…D-009 + D-010（ARCHITECTURE §3.4） |
| 用例 ID | 分配后不复用；废弃用例保留 ID 并标 `@deprecated` |
| 基线 | M0；协议变更须同 PR 修改 §3 |
| 执行 | `conda run -n scisim-agent python -m pytest bridge\tests -q`；`node --test app\web\tests`；CI `android-unit` |
## 1. 测试分层与职责

原则：协议行为在 L1/L2 断言到**具体 type、错误码、关闭码**；UI 只测逻辑不测像素；E2E 只验用户可见结果与性能；能用 fake 的绝不等模型。

| 层 | ID 前缀 | 位置与命令 | 职责 | 本机 / CI |
|---|---|---|---|---|
| L1 协议单测 | TC-PROTO/AUTH/PAIR/AUDIO | `bridge/tests/test_protocol.py`、`test_auth.py`、`test_audio.py`；`pytest -q` | 纯函数：信封、seq/replyTo、HMAC 向量、TTL/锁定、帧编解码；无 socket、无真等 | 本机 + CI |
| L2 Bridge 集成 | IT-BRIDGE | `bridge/tests/test_server.py`；`asr.mode=fake`、`dsh.enabled=false`，临时端口 + 动态证书 | 真 WebSocket：握手/认证/重放/节流/审批/背压/撤销 | 本机 + CI |
| L3 Web 逻辑 | TC-WEB | `app/web/tests/*.test.js`；`node --test app\web\tests` | 状态机、渲染、IndexedDB 适配、clientId、节流、表单校验；DOM 用轻量 stub | 本机 + CI |
| L4 Android JVM | TC-AND | `app/android`；`./gradlew testDebugUnitTest`（Robolectric/Shadow） | 帧构造、指纹校验、PTT/路由状态机、退避、Keystore 封装 | 仅 CI（D-006） |
| L5 真机 E2E | E2E / PERF | 真机 + 真 Bridge + 真耳机；按 §3.7/§3.9 执行 | 端到端时延、蓝牙路由、锁屏、断网、安装包 | 发布前 |
## 2. 需求 → 测试追踪

每个 FR 至少一个主用例。P0 用例是发布门禁；P1 未过需 Lead 签字。NFR 映射：NFR-1→PERF-01~06；NFR-2→IT-BRIDGE-12、E2E-06；NFR-3→SEC-T-01~05；NFR-4→SEC-T-04；NFR-5→E2E-03、E2E-10；NFR-7→CI 覆盖率 ≥60%。

| FR | P | 主用例 | 辅助用例 | 验收锚点 |
|---|---|---|---|---|
| FR-101 证书/指纹/二维码 | P0 | IT-BRIDGE-01 | E2E-01 | 二维码四要素齐全 |
| FR-102 配对与 token | P0 | TC-PAIR-02 | TC-AND-10、E2E-01 | 重启免配对 |
| FR-103 每次连接认证 | P0 | TC-AUTH-01~04 | IT-BRIDGE-02 | HMAC + 防重放 |
| FR-104 配对码 TTL/锁定 | P0 | TC-PAIR-03、TC-PAIR-04 | — | 120s、5 次锁 5 分钟 |
| FR-105 设备撤销 | P0 | IT-BRIDGE-03 | E2E-05 | 撤销即断连 |
| FR-106 连接状态机 | P0 | TC-WEB-01 | TC-AND-09、E2E-06 | 断网 30s 内状态正确 |
| FR-107 监听地址 | P0 | SEC-T-02 | — | 0.0.0.0 启动失败 |
| FR-201 会话列表 | P0 | TC-WEB-02 | IT-BRIDGE-04 | 三字段正确排序 |
| FR-202 新建/切换/恢复 | P0 | IT-BRIDGE-04 | E2E-07 | 恢复后依赖上文 |
| FR-203 会话隔离 | P0 | TC-WEB-02 | — | IndexedDB 按 sessionId 分区 |
| FR-204 删除会话 | P0 | TC-WEB-03 | — | 二次确认且只删本地 |
| FR-205 历史来源 | P0/P1 | TC-WEB-03 | — | V1 本地；P1 JSONL 导入 |
| FR-301 输入与发送 | P0 | TC-WEB-04 | — | 空消息禁止、发送中禁用 |
| FR-302 流式显示 | P0 | TC-WEB-05 | IT-BRIDGE-05 | 增量或整段打字机 |
| FR-303 工具状态行 | P0 | TC-WEB-05 | — | running/done/error |
| FR-304 消息操作 | P0 | TC-WEB-06 | IT-BRIDGE-07 | 复制/重发/停止/滚动 |
| FR-305 clientId 幂等 | P0 | IT-BRIDGE-06 | TC-WEB-07 | 200 条窗口去重 |
| FR-306 错误展示 | P0 | TC-WEB-08 | IT-BRIDGE-07 | 1001/3001/3003 文案 |
| FR-401 按住说话 | P0 | TC-AND-03 | E2E-02 | 按下录音、松手定稿 |
| FR-402 partial 实时显示 | P0 | IT-BRIDGE-08 | TC-WEB-09 | ≤5 次/秒且防抖 |
| FR-403 定稿可编辑/自动发送 | P0 | TC-WEB-09 | — | 两种模式可用 |
| FR-404 静音 500ms 定稿 | P0 | IT-BRIDGE-09 | — | VAD 等价 audio.stop |
| FR-405 连续聆听 | P1 | E2E-12 | IT-BRIDGE-09 | 5 句正确分条 |
| FR-406 语言切换 | P0 | TC-WEB-12 | IT-BRIDGE-05 | lang 进入 audio.start |
| FR-407 音频参数 | P0 | TC-AUDIO-01、TC-AUDIO-04 | PERF-01 | 16k/16bit/mono/20ms |
| FR-408 权限/无麦引导 | P0 | TC-AND-03 | — | 跳系统设置 |
| FR-501 路由显示 | P0 | TC-AND-04 | E2E-02 | phone/hfp/le + 设备名 |
| FR-502 主动切蓝牙 | P0 | TC-AND-04 | E2E-02 | setCommunicationDevice/SCO |
| FR-503 松手释放路由 | P0 | TC-AND-05 | — | 不影响音乐播放 |
| FR-504 8s 回退 | P0 | TC-AND-06 | E2E-04 | 回退手机麦并提示 |
| FR-505 偏好设备 | P0 | TC-AND-07 | TC-WEB-12 | 选择生效 |
| FR-506 LE Audio/回落 | P0 | TC-AND-06 | E2E-03 | LE 不可用走 HFP |
| FR-601 审批弹窗/超时 | P0 | IT-BRIDGE-10 | TC-WEB-10、E2E-09 | 10 分钟默认拒绝 |
| FR-602 提问弹窗 | P0 | IT-BRIDGE-11 | TC-WEB-11、E2E-09 | 取消回传 cancelled |
| FR-603 后台审批通知 | P1 | E2E-13 | — | 通知可点回 |
| FR-701 设置 | P0 | TC-WEB-12 | — | 保存即生效 |
| FR-702 诊断/导出 | P0 | TC-WEB-13 | E2E-11 | 延迟统计 + 导出 |
| FR-703 重启桥/重连 | P0 | E2E-10 | — | 5s 内恢复 |
| FR-704 日志隐私 | P0 | SEC-T-04 | IT-BRIDGE-13 | 无 token/正文/音频 |
## 3. 用例明细

前置默认 `asr.mode=fake`、`dsh.enabled=false`；时间断言一律注入时钟（`now_fn`），禁止 `sleep` 真等。锚点列写协议章节与期望帧/码。
### 3.1 协议信封与序号

| ID | P | 前置 | 步骤 | 预期 | 锚点 |
|---|---|---|---|---|---|
| TC-PROTO-01 | P0 | 双向各 3 条消息 | 发 hello→auth→ping，重连再发；另发 seq=7 的请求 | C→S 1,2,3 且重连重置为 1；S→C 跨重连继续递增；响应 `replyTo=7` | §2、§7 |
| TC-PROTO-02 | P0 | 注入未知 type/字段 | C 收 S→C `future.event`；S 收 C→S `future.cmd` | C 忽略并 `diag.unknown_type+1`；S close 4400；未知可选字段忽略 | §12、§14、R-05 |
| TC-PROTO-03 | P0 | 边界帧 | 发 65536B、65537B 文本；8193B 二进制；641B payload | 65536B 通过；其余拒绝或丢弃计数；不崩溃 | §1、§13 |
| TC-PROTO-04 | P0 | S→C 已发 seq 100…620 | 重连 `resync{lastSeq:100}` | 补 101…620；超 512 条时 `truncated=true` 且 `fromSeq=109` | §2、§7 |
| TC-PROTO-05 | P0 | 非法 JSON、缺 `type`、未知错误码 | 分别发送/接收 | 非法 JSON → close 4400；缺信封字段 → 4400；未知错误码按 5001 展示诊断 ID | §2、§12 |
### 3.2 认证与配对

| ID | P | 前置 | 步骤 | 预期 | 锚点 |
|---|---|---|---|---|---|
| TC-AUTH-01 | P0 | §6.3 向量 | 用给定 token/ts/nonce 算 canonical 与 HMAC | canonical 逐字节一致；hmac b64 为 `gYfqlKpD9PLEml5FzwSMmtd93hlDbF/JrA7lGcDBSjo=` | §6.3 |
| TC-AUTH-02 | P0 | 服务端时钟 T | ts 取 T-119、T+120、T-121、T+121 | 前两者 `auth.ok`；后两者 `auth.error{1001}` + close 4401 | §6.1 |
| TC-AUTH-03 | P0 | 同一 deviceId 的 nonce 已成功 | 10 分钟内重放；10 分钟后同 nonce 新 ts | 前者 1001；后者通过（缓存已过期） | §6.1、§13 |
| TC-AUTH-04 | P0 | 已配对 deviceId | 用错误 32B token 算 HMAC 发 `auth` | `auth.error{1001,needPair:true}`；不泄露校验细节 | §6.2 |
| TC-PAIR-01 | P0 | Bridge 已启动 | 解析二维码 URI | `dsphone://pair?v=1&host=<具体IP>&port=8765&fp=<64hex>&code=<6位>&sid=srv-...`；host 非 0.0.0.0 | §5.1 |
| TC-PAIR-02 | P0 | `needPair=true` | 发正确 `pair.request` | `pair.ok`：token 解码恰 32B、fp 64hex、`expiresAt=null`；服务端仅存 `sha256(token)` | §5.3 |
| TC-PAIR-03 | P0 | 已生成码 | 119s 配对；另起 121s 后配对 | 前者成功；后者 `pair.error{1002}` 且 `attemptsLeft` 递减 | §5.1 |
| TC-PAIR-04 | P0 | 同一 deviceId | 5 次错误码后第 6 次输正确码；锁定期满再试 | 第 5 次 close 4410；5 分钟内正确码也拒绝；期满重新生成码可配对 | §5.1 |
### 3.3 音频帧

| ID | P | 前置 | 步骤 | 预期 | 锚点 |
|---|---|---|---|---|---|
| TC-AUDIO-01 | P0 | 16k/16bit/mono | 构造 `aseq=1,tsMs=0` 首帧；再构造 48kHz 帧 | 前 7B 为 `01 01 00 00 00 00 00`；payload 640B、总长 647B；48kHz 时 payload 1920B | §10.2 |
| TC-AUDIO-02 | P0 | 已发 65535 帧 | 连发 2 帧 | `aseq` 回绕到 1、再到 2；不出现 0 | §2、§10.2 |
| TC-AUDIO-03 | P0 | 未收 `audio.ready`；已 ready | 未 ready 发 3 帧；已 ready 发 payload 639B/641B 帧与 `0x02` 帧 | 未 ready 全丢并 `audio.drop_pre_ready=3`；非法帧丢弃计数；0x02 记 `unsupported_codec` | §10.2 |
| TC-AUDIO-04 | P0 | 真机抓包 | 统计 10s 上行字节 | 实测 256kbps；记录与 FR-407 的 64kbps 偏差，V1.1 Opus 0x02 再验 | §10.2、R-05 |
### 3.4 Bridge 集成

| ID | P | 前置 | 步骤 | 预期 | 锚点 |
|---|---|---|---|---|---|
| IT-BRIDGE-01 | P0 | 空 `state/` | 启动 Bridge | 生成 cert/key（0600）；日志打印 fp；QR 的 fp 等于证书 DER 的 SHA-256 | §5.1 |
| IT-BRIDGE-02 | P0 | 已配对；另起未认证连接 | hello→auth→resync；未认证发 `session.list`；另一连接静默 16s | 认证链路返回 sessions 与 `resync.ok`；未认证 `error{1001}` + close 4401；15s 关闭 | §3、§4 |
| IT-BRIDGE-03 | P0 | 设备在线 | `python -m bridge devices revoke <deviceId>` | 在线连接 5s 内 close 4401；重连 `auth` 得 1001 且 `needPair=true`；列表移除 | §5.3 |
| IT-BRIDGE-04 | P0 | 已有 2 个会话 | list→create→resume→history；resume 不存在 id；恢复后追问 | 响应 `replyTo` 正确；`resume.ok` 带 messages；不存在 3001；追问依赖上文 | §8 |
| IT-BRIDGE-05 | P0 | fake DSH：3 段 delta + 1 工具 | 发 `chat.send` | 顺序 ack(clientId)→delta(0,1,2)→tool.status(running→done)→done(completed)；seq 单调 | §9 |
| IT-BRIDGE-06 | P0 | `clientId=X` 已完成 | 重连后重发同 id；再发第 201 个新 id | 前者只回 `chat.ack`，fake DSH 调用计数仍 1；窗口外旧 id 视为新消息 | §9 |
| IT-BRIDGE-07 | P0 | 会话有进行中 turn | 再发 `chat.send`；随后 `chat.cancel`；另测 SDK 无取消能力 | 第二次 4001 `retryable=true`；cancel 后必收 `done{cancelled}`；无能力则 `3003 "cancel unsupported"` | §9 |
| IT-BRIDGE-08 | P0 | 文本每秒变 20 次 | 统计 1s 内 `asr.partial` | ≤5 条；文本未变不发；`aseq` 为最新帧序号 | §10.1、§13 |
| IT-BRIDGE-09 | P0 | `audio.ready` 后发 2.4s 音频 | 发 `audio.stop` 计时；另测静音 500ms、121s 流、重复 `audio.start` | stop 后 ≤1.5s 收到 final（空串也发）；静音 500ms 自动 final；120s 自动 stop；重复 start 2001；模型缺失 2002 | §10.1 |
| IT-BRIDGE-10 | P0 | `approval.request`（deadline=T+600） | 不答等到期；再测 approved/denied；发未知与重复 id | 到期 `approval.expired{outcome:"denied"}` 且 SDK 收到 denied；未知/重复 id 忽略只记诊断 | §11.1 |
| IT-BRIDGE-11 | P0 | `question.request` | 回答单选+自定义；取消；到期 | `question.response{cancelled:false}` 格式正确；取消/到期均 `cancelled:true` | §11.2 |
| IT-BRIDGE-12 | P0 | 已发 600 条后断线；另测 Bridge 重启 | 重连 `resync{lastSeq}` | 补发不重复；`truncated=true` 全量刷新；重启后 lastSeq>uptoSeq 触发全量；final/done/approval 不丢 | §2、§7 |
| IT-BRIDGE-13 | P0 | 注入 canary token/正文/音频 | 跑完整会话并制造背压 >256KiB | 日志无 token/正文/PCM；背压只丢 partial，final/done/approval 保留 | FR-704、§13 |
### 3.5 App Web UI 逻辑

| ID | P | 前置 | 步骤 | 预期 | 锚点 |
|---|---|---|---|---|---|
| TC-WEB-01 | P0 | 注入 `net.state` 序列 | connecting→online→offline→reconnecting | UI 状态与文案一致；离线显示主机离线与重试按钮 | ARCH §3.3 |
| TC-WEB-02 | P0 | 3 会话乱序；s1/s2 各写 2 条 | 渲染列表；切回 s1 检查 IndexedDB key | 按 `updatedAt` 降序并截断；只见 s1 消息；key 前缀 `msg:<sessionId>` | FR-201、FR-203 |
| TC-WEB-03 | P0 | 有本地会话与消息 | 删除→取消→再删除→确认；重建 WebView | 取消不删；确认只删本地不发协议帧；重建后消息与草稿恢复；`window` 无 token | FR-204、FR-205 |
| TC-WEB-04 | P0 | 输入框 | 空串、纯空白、超 32000 字、发送中再点 | 前两者禁用；超长提示；发送中按钮禁用 | FR-301、§13 |
| TC-WEB-05 | P0 | delta 3 帧；整段 1 帧；含代码块与工具 | 渲染 | 增量追加；整段打字机；`pre` 等宽；上滑后停止跟随；工具三态可见 | FR-302、FR-303 |
| TC-WEB-06 | P0 | 有消息 | 复制、重发、停止、回到底部 | 内容一致；重发生成新 clientId；停止发 `chat.cancel`；滚动恢复 | FR-304 |
| TC-WEB-07 | P0 | 新建消息 | 生成 clientId 并重连重发 | uuidv4；同条复用原 id；不同消息不同 id | FR-305 |
| TC-WEB-08 | P0 | 注入 1001/3001/3003/4001 | 渲染错误 | 1001 回配对页；3001 刷新列表；3003 文案+重试；4001 退避提示 | FR-306 |
| TC-WEB-09 | P0 | 每秒 20 次 partial；随后 final | 统计 DOM 更新；切换 autoSend | ≤5 次/秒（rAF 合并）；final 清 partial 并填入；开自动发、关仅填入 | FR-402、FR-403 |
| TC-WEB-10 | P0 | `approval.request` | 渲染 Sheet 并等超时 | 默认焦点“拒绝”；命令等宽红色；倒计时归零按钮禁用并提示默认拒绝 | FR-601 |
| TC-WEB-11 | P0 | `question.request` | 单选/多选/自定义/取消 | 单选互斥、多选可多、空自定义拦截；取消发 `cancelled:true` | FR-602 |
| TC-WEB-12 | P0 | 设置页 | 改语言/静音/主题/偏好设备并保存；切语言录音 | 立即生效并持久化；`audio.start.lang` 用新语言；偏好进入 `preferDevice` | FR-406、FR-505、FR-701 |
| TC-WEB-13 | P0 | 有延迟样本与最近错误 | 打开诊断页并导出 | 显示 p50/p95 与错误码；导出触发 `logs.exported` 且为脱敏文本 | FR-702 |
### 3.6 Android JVM

| ID | P | 前置 | 步骤 | 预期 | 锚点 |
|---|---|---|---|---|---|
| TC-AND-01 | P0 | 20ms PCM 缓冲 | 组 3 帧 | `aseq` 1,2,3；`tsMs` 单调；7B 头 + 640B payload | §10.2 |
| TC-AND-02 | P0 | 已知证书 DER 与 fp | 正确 fp 连接；改 1 位 hex 重连 | 前者通过；后者抛 `FingerprintMismatchException`，不发 `auth` | §1、SEC-T-01 |
| TC-AND-03 | P0 | Idle；权限被拒；无麦 | 按下→松手→快速按放两次；触发无权限录音 | 进 Recording 并发 `audio.start`；松手发 `audio.stop`；无双流；权限引导设置页；无麦提示 | FR-401、FR-408 |
| TC-AND-04 | P0 | 三种路由回调；API31+/29 | 读取路由；按下 PTT | 返回 phone/hfp/le + 设备名；31+ 调 `setCommunicationDevice`，29-30 调 `startBluetoothSco` | FR-501、FR-502 |
| TC-AND-05 | P0 | 正在使用蓝牙路由 | 松手 | 调 `clearCommunicationDevice`/`stopBluetoothSco`；5s 内不重复 | FR-503 |
| TC-AND-06 | P0 | 路由回调不返回；API33 无 LE | 等待；读取路由 | 8s 后手机麦且 `audio.route` 带 `fallback=true`；LE 不可用回落 HFP | FR-504、FR-506 |
| TC-AND-07 | P0 | 偏好设备 A 在线/离线 | 各测一次 | 在线优先 A；离线回系统默认并提示 | FR-505 |
| TC-AND-08 | P0 | 注入连接失败 | 连续失败 6 次后成功 | 退避 1/2/4/8/15/15 秒 ±20%；成功后清零 | §7 |
| TC-AND-09 | P0 | 已连接 | 观察 15s ping；服务端静默 46s | 每 15s 发 `ping`；45s 无 S 消息判定断线并重连 | §7 |
| TC-AND-10 | P0 | 空偏好 | 写读 token；检查 SharedPreferences XML | 读写一致；XML 无明文 token；卸载重装后不存在 | §5.3 |
### 3.7 真机 E2E

| ID | P | 前置 | 步骤 | 预期 | 锚点 |
|---|---|---|---|---|---|
| E2E-01 | P0 | 全新安装 | 扫码配对→重启；卸载重装走手动 host/port/fp/code | ≤3 分钟；重启免配对；手动兜底成功 | §5.1 |
| E2E-02 | P0 | HFP 耳机 | 按住说 3s→松手→自动发送 | partial 可见；final 填入并发送；回复流式；记录首字/定稿 | FR-401、FR-502 |
| E2E-03 | P0 | LE Audio 耳机 | 同链路；关闭 LE 仅留 HFP | 两耳机均可用；LE 不可用回落 HFP 且状态正确 | FR-506 |
| E2E-04 | P0 | 耳机断电/超距 | 按 PTT | 8s 内回退手机麦，UI 提示，录音可继续 | FR-504 |
| E2E-05 | P0 | 设备在线 | Bridge 执行 revoke | App 5s 内断连回配对页；重连无法 auth | FR-105 |
| E2E-06 | P0 | 对话中 | 拔网 10s 再恢复；切热点 | ≤5s 自动重连；消息不重复；final 不丢；状态提示正确 | §7、NFR-2 |
| E2E-07 | P0 | 两个会话 | A 问上下文→切 B→回 A 追问 | A 回答依赖上文；无串消息 | FR-202 |
| E2E-08 | P0 | 在线 | 发长问题（>2000 字回复） | 流式不卡顿；工具状态行出现；完成后可复制 | FR-302、FR-303 |
| E2E-09 | P0 | DSH 请求审批与提问 | 拒绝审批；回答单选+自定义；再取消提问 | DSH 收 denied；答案格式正确；取消回传 cancelled | FR-601、FR-602 |
| E2E-10 | P0 | 录音中锁屏；Bridge 运行中 | 锁屏 5 分钟返回；点重启桥/重连；kill Bridge 后重启 | 前台服务存活可继续；按钮触发重连；5s 内恢复 | FR-703、NFR-5 |
| E2E-11 | P0 | 有历史与错误 | 诊断页导出并检查 | 文件可读，含版本/延迟/错误码；无 token/正文/PCM | FR-702、FR-704 |
| E2E-12 | P1 | 连续模式 | 连续说 5 句后结束 | VAD 分 5 条，句间不退出，均可编辑 | FR-405 |
| E2E-13 | P1 | App 切后台 | 触发审批 | 高优先级通知出现，点击回审批 Sheet | FR-603 |
| E2E-14 | P0 | CI 产物 | 安装并启动；量体积 | APK ≤25MB；minSdk 29、target 35；冷启动无崩溃 | PRD §2 |
### 3.8 安全与隐私

| ID | P | 前置 | 步骤 | 预期 | 锚点 |
|---|---|---|---|---|---|
| SEC-T-01 | P0 | 已知正确 fp | 用错误 fp 连接 | 握手后校验失败，UI 报“证书指纹不匹配”，不发 `auth` | SECURITY §5 |
| SEC-T-02 | P0 | 配置 `host=0.0.0.0`/`::`/空 | 启动 | 退出码 2，stderr 含 `server.host must not be 0.0.0.0`；回环与具体 IP 正常 | FR-107 |
| SEC-T-03 | P0 | 已配对并有成功 auth | 错误 token；10 分钟内重放 nonce | 均 `auth.error{1001}` + close 4401 | §6 |
| SEC-T-04 | P0 | 注入 canary | `rg -n "ABEiM0RV\|CANARY_(TOKEN\|AUDIO\|TEXT)" bridge/state/logs`；再导出诊断 | 0 命中；导出仅长度、hash 前缀、时间戳 | FR-704 |
| SEC-T-05 | P0 | deadline=now+5 的审批 | 不响应等待 | `approval.expired{outcome:"denied"}`，SDK 收到 denied | §11.1 |
### 3.9 性能

| ID | P | 指标 | 方法 | 埋点字段 | 门槛 |
|---|---|---|---|---|---|
| PERF-01 | P0 | 首字延迟 | 20 次 3s PTT 取 p95，必须 `asr.mode=sherpa` | `asr.first_partial_ms` | ≤1000ms |
| PERF-02 | P0 | 定稿延迟 | stop→final 取 p95 | `asr.final_ms` | ≤1500ms |
| PERF-03 | P0 | 定稿→DSH | `chat.send` 入站减 final 发出，p95 | `chat.final_to_dsh_ms` | ≤500ms |
| PERF-04 | P0 | 断线重连 | 20 次拔网/切热点，close→`auth.ok` p95 | `net.reconnect_total_ms` | ≤5000ms |
| PERF-05 | P0 | APK 体积 | CI `stat -c%s .../app-debug.apk` | 构建产物 | ≤26214400B |
| PERF-06 | P0 | 内存 | `adb shell dumpsys meminfo` PSS；桥 `psutil` 采样 | `app.pss_mb`、`bridge.rss_mb` | App ≤250MB；桥 ≤600MB |

> 新增 `chat.final_to_dsh_ms`、`app.pss_mb`、`bridge.rss_mb` 三个埋点，需同步 `bridge/logging_setup.py` 与诊断页。
## 4. 测试环境与数据
### 4.1 本机（Windows，无 JDK/Android SDK）

| 项 | 值 |
|---|---|
| Python | conda 环境 `scisim-agent`（3.11），命令一律 `conda run -n scisim-agent python ...` |
| Node | 24（内置 `node --test`） |
| 可跑 | L1/L2/L3：`pytest bridge\tests -q`、`node --test app\web\tests` |
| 不可跑 | `gradlew`、`adb`、真机 E2E；APK 只由 CI 产出（D-006） |
| 模型 | 默认 `asr.mode=fake`；真模型只做手动基准 |
### 4.2 CI（GitHub Actions）

| Job | Runner | 命令 | 门禁 |
|---|---|---|---|
| `bridge-test` | ubuntu + Python 3.11 | `pytest bridge/tests -q --cov=bridge --cov-fail-under=60` | 必绿 |
| `web-test` | ubuntu + Node 24 | `node --test app/web/tests` | 必绿 |
| `android` | ubuntu + JDK17 | `./gradlew testDebugUnitTest assembleDebug` + 体积门禁 | 必绿、≤25MB |
| `secret-scan` | ubuntu | `gitleaks detect --no-banner` | 0 命中 |
### 4.3 真机矩阵（PRD §15）

| 机位 | Android | 耳机 | 网络 | 必跑 |
|---|---|---|---|---|
| 1 | 12（API31） | HFP | 家庭 Wi-Fi | E2E-02、04、05、06、14 |
| 2 | 14（API34） | LE Audio | 手机热点 | E2E-03、06、07、10 |
| 3 | 15（API35） | HFP + LE | Tailscale | E2E-01、08、09、11 |
| 断网位 | 任一台 | — | 飞行模式/拔路由 | E2E-06 |
### 4.4 假数据与固定件

| 文件 | 内容 | 用途 |
|---|---|---|
| `bridge/tests/fixtures/fake_asr_script.json` | 按 `aseq` 区间返回 partial/final，含空 final、重复文本 | IT-BRIDGE-08/09 |
| `bridge/tests/fixtures/zh_16k_mono.wav` | 3s、16k/16bit/mono 合成语音“帮我查一下今天的待办” | 帧解析、定稿 |
| `bridge/tests/fixtures/corpus_zh_20/` | 20 句语料 + 参考文本 + WAV | WER ≤15% 人工基准 |
| `bridge/tests/fixtures/replay_600.json` | 600 条 S→C 事件 | resync/truncated |
| `bridge/tests/fixtures/cert_fp.json` | 测试证书 DER 与期望 fp | 指纹 pinning |
| fake DSH | `dsh.enabled=false` 时按脚本回 delta/tool/approval | L2 全部 |

夹具不得含真实用户录音；WAV 用 TTS 合成或书面授权录制；单文件 ≤1MB。
## 5. 缺陷管理与准入/准出

| 等级 | 定义 | 修复时限 | 回归 |
|---|---|---|---|
| S1 | 崩溃、数据丢失、token/音频泄露、未授权可配对 | 24h | 必须新增/更新用例 |
| S2 | P0 功能不可用、错误码错配导致流程卡死 | 3 天 | 同上 |
| S3 | P1 功能异常、UI 错误、性能未达标 | 下迭代 | 关联用例 |
| S4 | 文案/样式 | 排期 | 可无用例 |

缺陷单必填：bridge/app 版本、协议版本、环境、复现步骤、期望/实际、错误码或关闭码、脱敏日志、复现率。
准入：协议版本一致；CI 全绿；fixtures 已版本化；Bridge 可启动；E2E 阶段真机与耳机可用。
准出：P0 用例 100% 通过；P1 未过有 Lead 签字；CI 全绿且覆盖率 ≥60%；无 S1/S2；SEC-T 全过；PERF 达标或有批准偏差（如 FR-407）；APK ≤25MB；日志 grep 干净。
## 6. 已知测试风险

| ID | 风险 | 概率/影响 | 缓解 |
|---|---|---|---|
| R-01 | 本机无 JDK/Android SDK，L4 不能本地跑 | 高/中 | 纯 Kotlin 逻辑下沉 JVM 单测；CI 每 PR 跑；本地用 L1/L3 覆盖 |
| R-02 | 无真机/耳机，E2E 无法在 PR 阶段执行 | 高/高 | E2E 仅作发布门禁；模拟器覆盖 UI；集中“真机日”执行 §4.3 |
| R-03 | 无模型时真实 ASR 延迟/WER 不可测 | 高/中 | fake 覆盖协议；`asr.mode=sherpa` 手动基准写入验收报告 |
| R-04 | 时间/网络用例易 flaky | 中/中 | 注入 `now_fn`、固定 seed、禁用真实 sleep、`pytest-timeout` |
| R-05 | §12 与 §14 对未知 type 口径不一致 | 中/中 | 按 §3.1 分工执行（C 忽略、S 4400），提交协议澄清 PR |
| R-06 | FR-407 的 64kbps 与 V1 PCM 256kbps 冲突 | 确定/低 | 用例记录实测偏差；V1.1 Opus（0x02）再验 |
| R-07 | CI 无 release 签名，升级覆盖不可验 | 中/低 | debug 签名验证安装；release 手工冒烟并核对 sha256 |
| R-08 | Node 无原生 IndexedDB | 低/低 | `app/web/tests/helpers/fake-idb.js` 内存适配器，接口对齐浏览器 |
