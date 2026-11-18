# M2/v1.1 验收证据（app/web WebView UI）

- 对应任务：task-3（M2 WebView UI，已完成）+ task-6（M2 验收硬化：TC-WEB 映射 + PRD §12 交互自检）+ **task-12（v1.1 Web UI：DeepSeek 风格改版 + 模型/思考/上下文顶栏 + 图片发送 + 真实扫码配对）**
- 负责：web-ui；写作域：`app/web/**`（未改动 `app/android/`、`bridge/`、`docs/`、`ci/`，未 git commit）
- 协议基准：`docs/PROTOCOL.md` v1.1（v1.0 冻结 + D-033 增量）；交互基准：`docs/PRD.md` §12 + `docs/DECISIONS.md` D-028…D-033；验收基准：`docs/TESTPLAN.md` §3.5
- 结论：TC-WEB-01…18 均有覆盖证据；PRD §12 所列页面与交互全部实现，v1.1 新增顶栏三要素/图片/扫码/抽屉均有逻辑与契约测试。纯渲染/手势/焦点类条目采用「逻辑测试 + 源码契约断言」覆盖，像素级与真机行为需 E2E/人工目检（见 §5）。

## 1. 运行方式

```powershell
# 规范命令（CI，ubuntu runner 直接可跑）
node --test app\web\tests

# 本机沙箱：runner 会 spawn 子进程，被沙箱拒绝（Error: spawn EPERM）
# 改用进程内隔离模式（等价执行全部测试文件）
cd app\web
node --test --test-isolation=none "tests/*.test.js"

# 单文件调试
node tests/controller.test.js
```

最后一次全量运行（`cd app\web`，Node v24.21.0）：

```
ℹ tests 144
ℹ suites 0
ℹ pass 144
ℹ fail 0
ℹ cancelled 0
ℹ skipped 0
ℹ duration_ms 415.432
```

分文件（全部绿色，共 144）：

| 测试文件 | tests | pass | fail |
| --- | ---: | ---: | ---: |
| `tests/acceptance.test.js` | 19 | 19 | 0 |
| `tests/app.test.js` | 8 | 8 | 0 |
| `tests/controller.test.js` | 22 | 22 | 0 |
| `tests/diagnostics.test.js` | 7 | 7 | 0 |
| `tests/i18n.test.js` | 7 | 7 | 0 |
| `tests/mock.test.js` | 19 | 19 | 0 |
| `tests/protocol.test.js` | 25 | 25 | 0 |
| `tests/scheduler.test.js` | 7 | 7 | 0 |
| `tests/state.test.js` | 10 | 10 | 0 |
| `tests/store.test.js` | 7 | 7 | 0 |
| `tests/transport.test.js` | 13 | 13 | 0 |

浏览器演示（mock 后端，零依赖）：

```powershell
cd app\web
python -m http.server 8123
# 打开 http://127.0.0.1:8123/index.html ，配对信息已预填，配对码 123456
```

## 2. TC-WEB-01…13 映射

“覆盖”= 有断言级证据；“部分覆盖”= 逻辑/契约已测，渲染或真机行为归 E2E；“未覆盖”= 明确说明原因。

| ID | 期望（`docs/TESTPLAN.md` §3.5 摘要） | 覆盖证据（测试文件::用例） | 结论 |
| --- | --- | --- | --- |
| TC-WEB-01 | net.state connecting/online/离线/reconnecting 时 UI 状态与文案一致；离线显示“主机离线 + 重试” | `acceptance.test.js::TC-WEB-01 offline/reconnecting banner exists with a retry action`；`transport.test.js::NativeTransport maps bridge events to transport events`；`state.test.js`（状态机相位） | 部分覆盖：真实断网/后端重连时序需 E2E-01 |
| TC-WEB-02 | 多会话按 updatedAt 倒序；预览截断；仅显示选中会话消息；本地按 sessionId 分区 | `acceptance.test.js::TC-WEB-02 session previews are truncated and rows keep a stable shape`；`store.test.js::MemoryStore keeps sessions sorted by updatedAt…`；`controller.test.js::openSession loads local history first then server messages` | 部分覆盖：WebView 重建/真实 IndexedDB 需 E2E |
| TC-WEB-03 | 删除需二次确认；取消保留；确认仅删本地且不发协议帧；消息与草稿可恢复；窗口无 token | `acceptance.test.js::TC-WEB-03 drafts persist locally and are restored into the composer`；`store.test.js::MemoryStore stores per-session composer drafts (TC-WEB-03)`、`…deletes session messages…`；`controller.test.js::composer drafts persist locally, restore on open and clear after send` | 部分覆盖：重建恢复 IDB 需 E2E-03 |
| TC-WEB-04 | 空/纯空白不可发送；>32000 字提示；发送中禁用发送 | `acceptance.test.js::TC-WEB-04 too-long input is rejected…`；`controller.test.js::empty text is blocked with a toast, not a request`、`too-long text is rejected with chat.tooLong and no turn starts`；`chat.js::_syncSendState` 契约断言 | 覆盖（逻辑） |
| TC-WEB-05 | delta 增量追加/整段兜底；代码块等宽；tool.status 三态可见；上滑后停止跟随 | `acceptance.test.js::TC-WEB-05 streaming text, code blocks, tool states and scroll follow are implemented`；`controller.test.js::sendText streams a reply and completes the turn`；`mock.test.js::approval scenario…`（tool.status running/done） | 部分覆盖：滚动跟随为渲染行为，需真机/目检 |
| TC-WEB-06 | 复制内容一致；重新发送新 clientId；停止发送 chat.cancel；回到底部 | `acceptance.test.js::TC-WEB-06 copy/resend/stop actions exist for messages`；`controller.test.js::reconnectBridge…`/`cancelTurn` 相关；`protocol.test.js::builders produce chat.send/chat.cancel` | 部分覆盖：剪贴板与滚动位置需真机 |
| TC-WEB-07 | 每条消息 uuidv4 clientId；重试复用/新消息不同 | `acceptance.test.js::TC-WEB-07 every send gets a fresh uuid clientId`；`protocol.test.js::randomUuid returns RFC4122 v4`；`controller.test.js::sendText streams…`（`local-` 消息 id） | 覆盖 |
| TC-WEB-08 | 注入 1001/3001/3003/4001/5001 等错误码时 UI 按错误规格行动 | `acceptance.test.js::TC-WEB-08 every protocol error/close code ships a title + next-step hint`；`controller.test.js::error frames drive recovery: 1001 clears token, 3001 refreshes sessions, 4001 closes the turn`；`i18n.test.js`（error/close 文案完整性） | 覆盖 |
| TC-WEB-09 | partial ≤5/s 合并渲染；final 清空 partial；autoSend 开/关行为 | `acceptance.test.js::TC-WEB-09 ASR partial writes are coalesced per animation frame; final handled`；`controller.test.js::PTT recording drives the audio state and auto-sends the ASR final`、`autoSend off keeps the transcript editable instead of sending`；`mock.test.js`（partial→final 序列） | 部分覆盖：DOM 渲染频率需真机 |
| TC-WEB-10 | 审批弹窗默认焦点“拒绝”；命令等宽红色；倒计时归零禁用按钮并默认拒绝 | `acceptance.test.js::TC-WEB-10 approval sheet: deny focused by default, mono red command, countdown disables buttons`；`controller.test.js::approval request suspends the turn and responds with denied`、`approval deadline expiry clears the sheet locally with default deny`；`mock.test.js::approval scenario` | 部分覆盖：焦点视觉/倒计时渲染需真机 |
| TC-WEB-11 | 提问单选互斥/多选/自定义答案；必填校验；取消发 cancelled:true | `acceptance.test.js::TC-WEB-11 question sheet: single/multi/custom, required validation, cancel sends cancelled`；`controller.test.js::question request gets answers and resumes the turn`；`mock.test.js::question scenario` | 部分覆盖：控件交互需真机 |
| TC-WEB-12 | 语言/VAD/主题/首选设备持久化并即时生效；audio.start 使用新语言；偏好走 preferDevice | `acceptance.test.js::TC-WEB-12 settings persist immediately and drive language/audio preference`；`controller.test.js::preferred audio device setting is pushed to the transport (FR-505)`、`PTT recording drives…`（`audio.start.lang`）；`settings.js::_update` 契约断言 | 覆盖（逻辑） |
| TC-WEB-13 | 延迟样本与错误可见（p50/p95）；导出脱敏、无音频/token/消息正文 | `acceptance.test.js::TC-WEB-13 diagnostics exposes p50/p95 latency, recent errors, sanitized export`；`diagnostics.test.js`（sanitize/percentile/exportText）；`controller.test.js::chat error retry and diagnostics export surface controller data` | 覆盖 |

### v1.1 增补（D-028…D-034）

| ID | 期望（v1.1） | 覆盖证据（测试文件::用例） | 结论 |
| --- | --- | --- | --- |
| TC-14 | 顶栏展示模型 / 思考强度 / 上下文占用；数据源 `session.meta` + `assistant.done.usage`；缺省用 1M 兜底并标“估算”；切会话重置 | `acceptance.test.js::v1.1 TC-14 top bar shows model / thinking / context…`；`controller.test.js::v1.1 top bar: session.meta and assistant.done.usage feed chatMeta`、`…mock 切换模型`；`protocol.test.js::v1.1 session.meta and usage normalizers…`；`mock.test.js::session.resume session.meta + assistant.done usage` | 覆盖（逻辑；像素级需真机） |
| TC-15 | 图片：`pickImage{source}` 取图、缩略图可删、`chat.send.images`、历史 `media.get` 懒加载与 IndexedDB 缓存、4002 回滚提示 | `acceptance.test.js::v1.1 TC-15 images…`；`controller.test.js::v1.1 images attach…`、`…4002 invalid image rolls the turn back`；`transport.test.js::pickImage/mediaGet`；`mock.test.js::pickImage→chat.send images→media.get`；`protocol.test.js::v1.1 validateChatImages…`、`…Message.images metadata…` | 覆盖（逻辑；真实相机/相册权限需真机） |
| TC-16 | 扫码配对：`scanPairing` 真实现，成功填表 + hosts 候选 + name；失败按 reason 提示；手动输入与整条 URI 粘贴保留；hosts 传给 native connect | `acceptance.test.js::v1.1 TC-16 QR pairing…`；`mock.test.js::async scan success / scanFailure hook`；`transport.test.js::v1.1 scan ok/fail events`；`protocol.test.js::v1.1 pairing QR URI carries hosts[] and name` | 覆盖（逻辑；真扫码需真机，E2E-10） |
| TC-17 | mock 离线演示可走通 扫码→图片→meta/usage | `acceptance.test.js::v1.1 TC-17 mock demo can show scan → image → meta/usage offline`；`mock.test.js` 全部场景 | 覆盖 |
| TC-18 | 多地址连接全失败时展示每地址尝试明细（host/耗时/错误），`error` 状态按“已断开”渲染（D-031/D-034） | `acceptance.test.js::v1.1 TC-18 all-addresses-failed…`；`controller.test.js::v1.1 all-addresses-failed exposes tried address diagnostics`；`transport.test.js::net.state error → FAILED with detail.tried` | 覆盖（逻辑） |

## 3. PRD §12 映射（页面/交互 → 实现位置）

| PRD §12 条目 | 实现位置 | 现状 |
| --- | --- | --- |
| 配对页（host/port/fp/code，扫码真实现，支持粘贴 `dsphone://pair` URI） | `js/views/pairing.js`（表单 `:16`，`_applyScanResult` 处理 `scanPairing.result`）；`js/controller.js` `connectPairing`/`scanPairing`；`js/transport.js` NativeTransport `scanPairing`/`pickImage` | 已实现（v1.1 扫码真实现，D-030；失败按 reason 提示并保留手动输入） |
| 会话列表页（标题/时间/预览，新建/刷新/切换） | `js/views/sessions.js:14`；预览截断 `js/views/sessions.js:12` | 已实现 |
| 会话页（消息流、输入、发送、停止） | `js/views/chat.js:37`；输入区 `js/views/chat.js:131`、`:200` | 已实现 |
| 设置页（服务器/重配对/语言/主题/autoSend/VAD/首选设备） | `js/views/settings.js:14` | 已实现 |
| 诊断页（版本/连接/延迟/最近错误/导出/清空/重连 Bridge） | `js/views/diagnostics.js:22`；`exportLogs` `:117` | 已实现 |
| 蓝牙状态栏（当前音频路由/设备名） | `js/app.js:166`（route-chip）、`:315` `_onAudioRoute`；`js/views/chat.js:570` `_onAudioState` 传入 route/device | 已实现（task-6 补齐，依赖 native `audio.route` 事件） |
| 录音浮层（波形 + 计时 + 上滑取消 + “连接耳机中…”） | `js/ui/recording-overlay.js:26`；波形 `:13`；计时 `:10`；预热点 `:128`；路由文案 `:146`；手势 `js/views/chat.js:196` | 已实现 |
| 审批弹窗（工具/原因、红色危险按钮、等宽命令、默认焦点“拒绝”、倒计时） | `js/ui/approval-sheet.js:40`；默认焦点 `:74`；命令块 `:53`；超时禁用 `:107` | 已实现 |
| 提问弹窗（单选/多选/自定义/必填/取消） | `js/ui/question-sheet.js:20`；单选/多选 `:35`；必填校验 `:72`；取消 `:91` | 已实现 |
| 错误 toast（含“下一步怎么做”） | `js/ui/toast.js:23`；`js/app.js:308` `_onError`；文案 `js/i18n.js:799` `errorCopy` | 已实现 |
| 流式回复光标跟随；用户上滑后停止自动跟随 | `js/views/chat.js:219`（距离阈值跟随）、`:122` 回到底部按钮 | 已实现（渲染行为需真机目检） |
| partial 灰色预览位于输入框上方；final 可编辑；autoSend 可关 | `js/views/chat.js:131`（composer 内 partial 在输入行之前）、`:522` `_renderPartial`；`:256` `_onAsrFinal`；`:158` `_toggleAutoSend` | 已实现 |
| 状态机 Idle→Connecting→Pairing→Online→Recording→WaitingForReply→ApprovalPending | `js/state.js:10` `Phase`；`:104` `reduce` | 已实现 |
| 深色/浅色跟随系统 | `css/tokens.css:81` `prefers-color-scheme: dark`；`js/app.js:552` `_applyTheme`（matchMedia 监听） | 已实现 |
| 触控目标 ≥48dp | `css/tokens.css:15` `--touch: 48px`；`css/components.css:13` `min-height: var(--touch)` | 已实现（像素级需真机） |
| 中英双语 | `js/i18n.js:18` zh、`:366` en（键集完全对齐，i18n 测试强制） | 已实现 |
| 错误文案必须包含“下一步怎么做” | `js/i18n.js:299` 起 `error.<code>.next`；`errorCopy` `:799`；`i18n.test.js` 逐码断言 | 已实现 |
| TalkBack/屏幕阅读器标签 | `js/views/chat.js:74` 等 `aria-label`；`i18n.js` `a11y.*` 键；`role="log"/"status"/"dialog"` | 已实现（需真机 TalkBack 目检） |
| 审批/提问弹窗返回键可关闭 | `window.DshBridge.onBack` 链 → `js/app.js` `handleBack()` + `closeTopSheet()`（`js/ui/sheet.js`） | 已实现 |
| v1.1 顶栏：模型 / 思考强度 / 上下文占用 | `js/views/chat.js` `_renderChatMeta`（chip--model / chip--reasoning / chat__context）；`js/controller.js` `chatMetaFor`；`js/protocol.js` `MEDIA.modelLabels`、`normalizeSessionMeta/normalizeUsage`；`js/i18n.js` `chat.thinkingLow|High|Max`、`chat.contextDetail` | 已实现（D-032；缺省 1M 标“估算”） |
| v1.1 图片发送与历史图片 | composer `📎/📷` → `js/controller.js` `attachImage`；`js/views/chat.js` `_renderPendingImages`/`_renderImages`；`js/transport.js` `pickImage`/`mediaGet`；`js/store.js` media 缓存；`js/mock.js` `MOCK_SAMPLE_IMAGES` | 已实现（D-029；≤4 张/≤2MiB/≤6MiB 本地拦截，4002 回滚） |
| v1.1 会话抽屉 + chat 全屏 + 空态欢迎 | `js/app.js` `openDrawer/closeDrawer`、`show()` 隐藏 topbar/tabbar；`css/components.css` `.drawer*`、`.app--chat`；`js/views/chat.js` 空态 | 已实现（D-032） |
| v1.1 多地址连接失败诊断 | `js/controller.js` `_connectDiagnostics` + `getDiagnostics`；`js/views/pairing.js` `_showConnectDiagnostics`；`js/views/diagnostics.js` `connectDiagList`；`js/transport.js` `error → FAILED` | 已实现（D-031/D-034，展示 tried host/耗时/错误） |

## 4. 契约偏差 / 需 Lead 决策

逐项核对 `docs/ARCHITECTURE.md` §3.2 / §3.2.1（v1.1）：

1. **`scanPairing` 事件**：v1.1 文档已写明 `scanPairing.result`（成功 `{ok:true,uri,host,port,fp,code,sid,hosts?,name?}`，失败 `{ok:false,reason}`），与 native/Web 实现一致；v1.0 时期文档写 `pair.scanned` 的历史偏差已随 v1.1 契约统一。Web 侧已删除 legacy `scan.result` 监听（check_contract 无 WARN），仅保留 `scanPairing.result`。
2. **多地址连接（D-031/D-034）**：`connect{... hosts:[...]}`；全失败时 native 发 `net.state{state:"error", reason:"all_addresses_failed", message, tried:[{host,port,elapsedMs,error}]}`（注意是 `error` 不是 `failed`），Web 将 `error` 与未知 state 渲染为“已断开”，诊断页/配对页展示 tried 明细。已与 ARCHITECTURE §3.2 一致。
3. **图片（D-029）**：`pickImage{source}` → `image.picked{data,mimeType,name,width,height}` / `image.pickFailed{reason}`；`chat.send.images[]` ≤4 张/单张 ≤2MiB/合计 ≤6MiB，Web 本地 `validateChatImages` 拦截；历史 `Message.images[]` 仅元数据，`media.get` 懒加载 + IndexedDB 缓存。与 PROTOCOL §8.1/§9 一致。
4. **`session.meta` / `usage`（D-032）**：缺失键省略不造 0；`usedTokens = totalTokens ?? input+output`；顶栏 `used/contextWindow`；`contextWindow` 缺失用 1,000,000 兜底并标“估算”。
5. `notify`（后台审批通知）op 标注 P1；v1.1 Web 仍未调用（native 侧负责通知，不在本轮范围）。无冲突。
6. 其余均与文档一致：`connect/pair/auth/send/startAudio/stopAudio/cancelAudio/getToken/setToken/routeInfo/preferDevice/exportLogs/reconnect/scanPairing/pickImage`；事件 `net.state/pair.result/auth.result/proto.message/audio.*/token/logs.exported/error/scanPairing.result/image.picked/image.pickFailed`；`seq` 由 native 独占（Web 发送不带 seq）；`_reqId` → native 回填 `_reqTo`；`resync` 由 native 在 auth 后自动发（Web 不发）；`window.DshBridge.onBack()` 返回是否消费；WebView origin `https://appassets.local`、相对路径 `./js`/`./css`、MIME 约定。

## 5. 已知限制

- **本机无浏览器渲染验证**：沙箱内 Chrome/Edge 启动即失败
  `FATAL:mojo\public\cpp\platform\platform_channel.cc:112] Check failed: . : 拒绝访问。(0x5)`（含 `--headless=new --no-sandbox` 等参数均被拒）。因此焦点顺序、滑动手势、48dp 实际尺寸、深浅色像素、滚动跟随等采用**逻辑测试 + 源码契约断言**覆盖，最终需真机/人工目检（对应 TESTPLAN E2E-02/09/10）。
  可验证的部分已做：`python -m http.server 8123`（`app/web` 目录）下 index.html + 全部 css/js（26 个运行时资源）均返回 200，JS MIME 为 `text/javascript`，可确认相对路径与资源加载在浏览器同源环境下成立。
- **本机测试命令差异**：`node --test app\web\tests` 在沙箱报 `spawn EPERM`，本机改用 `--test-isolation=none`；CI（ubuntu）使用规范命令。
- **IndexedDB**：Node 环境无 `indexedDB`，`store.js` 以 MemoryStore 跑通全部逻辑，并对 IndexedDbStore 的失败降级做了测试；真实 WebView IndexedDB 持久化（App 重启恢复会话/草稿）需真机 E2E。
- **真实 WebSocket 后端**：`bridge/` 由 bridge-core 提供，本仓库 Web 测试使用 FakeWebSocket 验证握手/子协议/序号/`replyTo` 关联；真实端到端需 Bridge 进程（端口/证书已由 bridge 模块负责）。
- **音频/蓝牙真实链路**：采集、SCO 路由、ASR 均为 native 能力；Web 侧只验证状态机与 UI（`audio.state/audio.route/audio.level` 事件的消费与展示）。
- **通知**：审批/提问后台通知为 P1，M2 未实现。

## 6. Mock 演示场景（`js/mock.js`，`?transport=mock`）

配对：预填 `127.0.0.1:8765` + fp，**配对码 `123456`**；错误码返回 `1002`，attemptsLeft=4。

在会话页发送以下关键词可触发对应场景：

| 关键词（任一命中） | 场景 |
| --- | --- |
| 删除文件 / 执行命令 / rm / bash / deploy / 部署 / 审批 / approve | `tool.status` + `approval.request`（默认拒绝、10 分钟倒计时；响应后继续流式回复） |
| 选择 / 方案 / 请问 / 哪个 / choose / option / ask | `question.request`（单选+多选+自定义；取消发 `cancelled:true`） |
| 报错 / 失败 / 异常 / 错误 / fail / error / crash | 流式输出后 `turn.error{code:3003}` + `assistant.done finishReason:error`（可重试） |
| 长文 / 详细 / 展开 / 完整 / long / detail | 长回复（用于验证滚动跟随/上滑停止） |
| 语音 / 录音 / 耳机 / audio / voice | 语音演示提示（PTT 按钮同样可触发录音浮层预热点） |
| 模型 / 切换模型 / v4-pro / deepseek-v4-pro / 换模型 / model | `session.meta` 在 `deepseek-flash`（DeepSeek-V4.1-Flash，high，1M）与 `deepseek-v4-pro`（DeepSeek-V4-Pro，max，128K）间切换，顶栏三要素随之变化 |
| 其他文本 | 普通流式回复（多段 delta） |

v1.1 演示补充：

| 操作 | 演示内容 |
| --- | --- |
| 点击 `📎` / `📷` | `pickImage{source}` → 内嵌示例图（gallery/camera 各 1 张 64×48 PNG，无网络也可用）→ 缩略图可删；发送后 mock 回 `chat.send.images` 的视觉场景 |
| 发送带图消息后 | 历史消息只存 `images[].mediaId`；重新打开会话时 `media.get` 懒加载原图（IndexedDB 缓存） |
| 配对页「扫码配对」 | mock 180ms 后返回成功（`192.168.1.10` + hosts `10.5.19.39` + `KQ-PC`），自动填表并展示 hosts 候选；也可手动粘贴完整 `dsphone://pair?...` URI |
| 连接全失败 | native 模式下 `net.state{state:"error",reason:"all_addresses_failed",tried[]}` → 配对页/诊断页展示每地址耗时与错误（mock 可用 `transport` 注入模拟） |
| 顶部三要素 | 首次 `session.resume` 即返回 `session.meta`；每轮 `assistant.done.usage` 后上下文占用百分比更新（1.2K/1M ≈ 0.1%） |

其他：demo 工具条提供审批/提问快捷发送、断开、重连、重置；`simulateClose(4408)` 可验证重连退避与恢复；`expirePendingApproval()` 验证 10 分钟自动拒绝；`?transport=ws` 用于调试真实 `dsphone.v1` WebSocket（token 仅存 `localStorage['dsphone.debugToken']`，生产 WebView 由 native 持有 token）。

## 7. task-6 变更记录

- 新增：`tests/acceptance.test.js`（TC-WEB/PRD §12 映射与契约断言）、本文件 `ACCEPTANCE.md`
- 修改（均在 `app/web/**`）：`js/diagnostics.js`（`percentile`/p50/p95/样本窗口）、`js/views/diagnostics.js`（p50/p95 列）、`js/store.js`（`normalizeDraft` + 草稿持久化）、`js/controller.js`（草稿 CRUD、超长/空输入提示、`preferDevice`）、`js/views/chat.js`（`parseMessageSegments`、草稿回填、resend、rAF partial、audio route 传浮层）、`js/app.js`（离线横幅、蓝牙状态条、审批/提问超时关弹层）、`js/ui/approval-sheet.js`（倒计时归零禁用按钮）、`js/views/sessions.js`（预览截断）、`js/i18n.js`（`a11y.resend`、`settings.paired`）、`css/app.css`（topbar 状态条布局）、`css/components.css`（`.conn-banner`）、`tests/{store,controller,diagnostics,acceptance}.test.js`、`README.md`（指向本文件）

## 8. task-12（v1.1）变更记录

- 协议/契约层：`js/protocol.js`（`MEDIA` 常量、`SESSION_META`/`MEDIA_GET`/`MEDIA_DATA`、4002 规格、`buildPairUri/parsePairUri` hosts+name、`validateChatImages`、`buildMediaGet`、`normalizeSessionMeta/Usage/MediaData`、`Message.images` 归一化）；`js/transport.js`（`pickImage`/`mediaGet`、`image.picked`/`image.pickFailed`、`scanPairing.result` 真实现、`net.state error → FAILED` + `tried` 详情、删除 legacy `scan.result`）。
- 状态层：`js/controller.js`（`chatMetaFor`/`attachImage`/`loadMedia`/`removeImage`、`sendText` 支持纯图片、`session.meta`/`usage` 处理、4002 回滚、`hosts` 连接候选、`connectDiagnostics`、`sendText` 改为 async）；`js/mock.js`（`MOCK_SCAN_RESULT`/`MOCK_SAMPLE_IMAGES`/`MOCK_SESSION_META`、`session.meta`/`usage`/`images`/`media.get`、模型切换与图片场景、扫码/取图 hooks、mock 延迟）；`js/store.js`（图片缓存 `getMedia/saveMedia/deleteMedia`）。
- UI 层：`js/views/chat.js`（顶栏三要素 + 上下文条、`📎/📷`、待发送缩略图、消息图片懒加载、抽屉入口、纯图片发送）；`js/views/pairing.js`（扫码结果填表/hosts/name、失败 reason、连接 tried 诊断）；`js/views/diagnostics.js`（p50/p95 + 连接 tried 明细）；`js/app.js`（DeepSeek 风格 shell、会话抽屉、chat 屏隐藏 topbar/tabbar）；`js/i18n.js`（v1.1 新增键，zh/en 逐一对齐）；`css/tokens.css`（主色 `#4D6BFE`、深浅主题气泡色）；`css/components.css`（圆角气泡、顶栏 chip/上下文条、附件与图片网格、抽屉、全屏 chat）。
- 测试：`protocol.test.js`（+6）、`mock.test.js`（+4）、`transport.test.js`（+3）、`controller.test.js`（+4）、`acceptance.test.js`（+5）；总 **144/144**。
- 未覆盖/说明：像素级视觉与真实相机/相册权限、ZXing 扫码、真机音频路由归 M3 E2E（TESTPLAN E2E-02/09/10）；后台 `notify` 仍为 P1 未调用。
