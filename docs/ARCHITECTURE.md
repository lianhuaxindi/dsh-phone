# 架构设计（ARCHITECTURE）

> 上游：`docs/PRD.md`；协议：`docs/PROTOCOL.md` v1（冻结）；决策：`docs/DECISIONS.md`。
> 本文只描述**模块边界与数据流**，接口细节以协议与代码 docstring 为准。

## 1. 总览

```
┌──────────────────── Android App ────────────────────┐
│  WebView UI (app/web, 纯 HTML/JS/CSS)               │
│        ⇅  JsBridge(WebMessage)                       │
│  Kotlin 壳 (app/android): 录音/蓝牙/前台服务/Keystore │
│        ⇅  WSS dsphone.v1 (TLS + 指纹 pinning)        │
└───────────────────────┬─────────────────────────────┘
                        │ 局域网 / Tailscale
┌───────────────────────▼─────────────────────────────┐
│  Bridge (Python 3.10+, 单进程 asyncio)               │
│    server.py   WSS + 连接生命周期                     │
│    auth.py     配对/令牌/HMAC/nonce                   │
│    protocol/   编解码、错误码、音频帧                  │
│    dsh.py      SDK 对接（线程池，同步调用隔离）        │
│    asr/        流式 partial + 定稿 final              │
│    history.py  会话/消息持久化（本地）                 │
└───────────────────────┬─────────────────────────────┘
                        │ stdio JSON-RPC（SDK 内部）
                 dsh --profile sdk（子进程）
```

## 2. Bridge

### 2.1 目录布局

```
bridge/
  __main__.py          # CLI: python -m bridge --config config.toml
  config.py            # TOML 加载 + dataclass 校验 + 默认值
  certs.py             # 自签证书生成/加载、SHA-256 指纹、二维码 URI
  server.py            # websockets 服务端、连接注册表、广播、优雅退出
  connection.py        # 单连接状态机（handshake/auth/online）+ 消息分发
  auth.py              # 配对码生成/TTL/锁定、设备令牌存储(sha256)、HMAC 校验、nonce 缓存
  protocol/
    __init__.py
    envelope.py        # 信封校验、seq 计数器、replyTo
    messages.py        # 各消息 dataclass（与 docs/PROTOCOL.md 一一对应）
    errors.py          # 错误码枚举、ProtocolError、close code 映射
    audio.py           # 二进制帧解析/构造、20ms 校验
  dsh.py               # 唯一的 SDK 边界：起会话、run、通知转事件、审批/提问桥
  history.py           # 会话与消息持久化（bridge/state/history.sqlite3）
  qr.py                # 控制台二维码 + 可选 PNG
  logging_setup.py     # 结构化日志 + 脱敏过滤器（token/audio/文本）
  requirements.txt     # 运行依赖（版本固定）
  asr/
    __init__.py        # create_recognizer(config) 工厂
    base.py            # StreamingASR 抽象：start/frame/flush/close + 事件回调
    fake.py            # FakeASR：固定脚本，用于 M1 与 CI（无模型）
    sherpa.py          # 真实现：流式 zipformer(partial) + SenseVoice(final)，线程内推理
    models.py          # 模型清单/校验/下载（scripts/download_models.py 复用）
  tests/
    test_protocol.py   # 信封/编解码/测试向量（含 PROTOCOL §6.3）
    test_audio.py      # 帧解析边界
    test_auth.py       # HMAC/重放/TTL/锁定
    test_server.py     # 用 fake ASR + fake DSH 跑端到端协议会话
```

### 2.2 并发模型

- **单事件循环**（asyncio）承载 WebSocket I/O、协议状态、定时器（心跳/VAD/deadline）。
- **SDK 是同步阻塞的** → 所有 SDK 调用走 `ThreadPoolExecutor(max_workers=2)`（`loop.run_in_executor`），结果通过 `asyncio.Queue` 回投事件循环；**严禁在事件循环里直接调 SDK**。
- **ASR 推理**在独立线程（sherpa-onnx 为 CPU 推理），音频帧从事件循环投递到线程内队列；partial 结果回投事件循环后按 5 次/秒节流发送。
- 每连接一个 `ConnectionState`；全局 `SessionRegistry` 管理会话 ↔ SDK 会话的映射。
- 服务端保持"一个 turn 一个 task"：`assistant.delta/done` 由 SDK 通知回调驱动。

### 2.2.1 SDK 接线规范（M0 spike 定稿，取证见 `docs/spike/SDK-SPIKE.md`）

依据 `deepseek-harness-sdk 0.2.2rc2` 实测（**无 token 级流式、无 list/history、无 cancel**），`bridge/dsh.py` 按此实现：

| SDK 事实 | Bridge 行为 |
|---|---|
| `start_session(session_id=None) -> Session` **不发 RPC**，只生成 `session-<uuid4hex>` | Bridge 自己生成 `sessionId`（`s-<hex>`）并存 `history.sqlite3`，同时记 `sdkSessionId`；`start_session(sdkSessionId)` 后 `run` |
| `resume_session(sdkSessionId)`：不存在/不兼容抛异常，**旧事件不重放** | `session.resume` 先查自家 sqlite；SDK resume 失败 → `error 3001`（不静默新建） |
| `run(input, *, session_id=None, on_notification=None) -> RunResult` 同步阻塞到 turn 结束 | 走 `ThreadPoolExecutor`；`on_notification` 在 SDK 线程把事件投 `asyncio.Queue`，事件循环消费 |
| 通知 method 仅 `session.event`/`session.status`/`session.query.completed`/`subagent.started|finished`/`user-question.cancelled` | `session.event.payload.event`：`tool/call`→`tool.status(running)`；`tool/result`→`tool.status(ok/error)`；`assistant/message`→**单帧** `assistant.delta{index:0,text}`；`turn/end`→`assistant.done`（`finishReason`），`error`→`turn.error{3003}` |
| **无 token 级增量**，文本整段到达（每 step 一条） | `assistant.delta` 每 turn 至多 1 条且为整段；客户端打字机是**本地渲染效果**，不得声称来自模型（D-018） |
| `config.approval_requests=True` 后必须并发 drain `next_request()`，否则 turn 挂死 | 专用线程 `while: next_request()` → `approval.request{id,tool,reason,callId,sessionId}`；`respond(id,{"outcome":"allowed-once"|"rejected"|"cancelled"})` |
| 审批 payload 仅 `sessionId/toolName/callId/reason`；`IncomingRequest.payload` 是原始 JSON | `approval.request` 透传未知键到可选 `extra` 字段；**不得**因未知键解析失败 |
| 提问 handler 在 daemon 线程执行，`threading.Event` 是取消信号 | `question.request` → 用户答/超时 → `UserQuestionAnswer` 或裸 `{"cancelled": true}`；到期发 `question.expired` |
| **无 cancel/interrupt/stop**，唯一手段 `close()` 杀进程 | `chat.cancel` 回 `turn.error{code:3003,"message":"cancel unsupported (SDK 0.2.2rc2)"}`；不得谎报取消（D-019） |
| **无 list_sessions / read_history** | `session.list`/`session.history` 完全由 Bridge 的 `history.sqlite3` 提供 |
| CLI 发现**从不查 PATH**：`dsh_bin` 空 → wheel 内置 runtime exe（147MB）；否则用给定路径；必须显式 `dsh_home`/`DSH_HOME`，否则 `ValueError: …never uses ~/.dsh implicitly` | 配置 `[dsh].runtimeExe` + `[dsh].home`（默认 `state/dsh_home`，绝对化）；**禁止**回落到用户桌面 `~/.dsh`（D-002）。空值时的解析顺序（D-022）：显式配置/env → 本机已装 DSH（PATH `dsh.cmd`/`dsh.exe`、常见安装目录）→ wheel 内置 exe；每步命中路径与原因必须进日志 |
| `provider`/`model` 经 initialize 传参，无运行时改模型 API | 模型变更需新会话；配置透传 |

- 依赖：SDK 与 runtime **不进** `bridge/requirements.txt`（147MB+ 且 PyPI 为 `--pre`），单列 `bridge/requirements-sdk.txt`；CI 不装，`[dsh].enabled=false` 走 fake。

### 2.3 关键约束

- 未认证连接 15s 内必须完成 auth；认证前只允许 hello/pair/auth/ping。
- 所有 S→C 消息进重放缓冲（512 条/10 分钟）以支持 `resync`。
- `asr.final`、`assistant.done`、`approval.request` 永不丢弃；仅 `asr.partial` 可丢。
- SDK 子进程生命周期：Bridge 启动时拉起/探活，退出时优雅关闭；SDK 崩溃 → 重连一次 → 仍失败则 `error{code:5002}` 并保持 WSS 可用（可诊断）。
- 独立 `dsh_home`（`bridge/state/dsh_home`），不读桌面端会话（D-002）。
- 配置默认只监听 `127.0.0.1`；开启局域网必须显式配置具体 IP，禁止 `0.0.0.0`。

### 2.4 配置（`bridge/config.example.toml`）

```toml
[server]
host = "127.0.0.1"      # 禁止 0.0.0.0；局域网填具体网卡 IP
port = 8765
maxConnections = 4

[tls]
certFile = "state/certs/bridge.crt"
keyFile  = "state/certs/bridge.key"

[dsh]
enabled = true           # false = 假 DSH（CI/联调）
home = "state/dsh_home"
provider = ""
model = ""

[asr]
mode = "fake"            # fake | sherpa
lang = "zh"
partialRatePerSec = 5
silenceMs = 500

[audio]
sampleRate = 16000
maxStreamSeconds = 120

[security]
pairCodeTtlSec = 120
maxPairFailures = 5
lockMinutes = 5
authSkewSec = 120

[log]
level = "INFO"
dir = "state/logs"
```

## 3. App

### 3.1 模块边界

| 层 | 位置 | 职责 | 不做什么 |
|---|---|---|---|
| UI | `app/web/` | 会话/聊天/设置/诊断、状态机、i18n、审批与提问 Sheet、录音浮层 | 不做原始录音、不碰 TLS/Keystore |
| 桥 | `app/android/.../bridge/DshBridge.kt` | WebView ⇄ Native 的 `postMessage` 通道（JSON，单一 `@JavascriptInterface post()` 按 `op` 分发） | 不放业务规则 |
| 网络 | `.../net/` | WSS 客户端、TLS 指纹校验、重连退避、协议编解码（Kotlin） | 不做 UI |
| 音频 | `.../audio/` | AudioRecord 16k/16bit/mono 20ms、PTT、前台服务 | 不做 ASR |
| 蓝牙 | `.../audio/BtRouter.kt` | `setCommunicationDevice`(31+)/`startBluetoothSco`(29-30)、8s 超时回退 | 不决定 UI 文案 |
| 存储 | `.../storage/` | Keystore/EncryptedSharedPreferences 存 token；其余交 WebView IndexedDB | 不存音频 |

**UI 存放位置**：`app/web/` 源码构建产物同步到 `app/android/app/src/main/assets/web/`（CI 与本地脚本 `scripts/sync_web.ps1` 负责；仓库不提交重复产物）。

### 3.2 JsBridge 契约（Native ⇄ Web）

Web → Native（`window.DshNative.post(msg)`）：

| `op` | 参数 | 返回事件 |
|---|---|---|
| `connect` | `{host,port,fp,deviceId}` | `net.state` |
| `disconnect` | `{ }` | `net.state`（断开且**停止自动重连**；best-effort，用于切换配对/退出登录。非协议冻结面，M2 实现引入） |
| `pair` | `{code,deviceName}` | `pair.result` |
| `auth` | `{}` | `auth.result` |
| `send` | `{json}` 协议消息 | `proto.message` |
| `startAudio` | `{sampleRate,lang,mode}` | `audio.ready` / `audio.error` |
| `stopAudio` | `{}` | `audio.stopped` |
| `cancelAudio` | `{}` | — |
| `getToken` / `setToken` | `{token?}` | `token`（仅内部配对用，UI 不展示） |
| `routeInfo` | `{}` | `audio.route`（`phone|hfp|le` + 设备名） |
| `preferDevice` | `{address?}` | `settings.audioRoute` |
| `notify` | `{title,text}` | —（审批后台提醒，P1） |
| `exportLogs` | `{}` | `logs.exported`（脱敏） |
| `reconnect` | `{}` | `net.state`（诊断页「重连 Bridge」；断开并重建连接，**不**重启 Bridge 进程，见 D-013） |
| `scanPairing` | `{ }` | `scanPairing.result`（V1 固定返回 `{supported:false,reason:"m3"}`，见 D-011；Web 另兼容旧别名 scan.result，M3 可清理） |

Native → Web（`window.DshBridge.onEvent(json)`）：`net.state` / `proto.message`（协议原样透传）/ `audio.state` / `audio.route` / `audio.level`（波形）/ `error`。

**客户端本地 `audio.error` 码表**（Native 判定、经 `audio.error` 上报；`retryable` 表示 UI 可直接提供「重试」）：

| code | 触发条件 | retryable |
|---|---|---|
| `permission_denied` | `RECORD_AUDIO` 未授权 | false（引导去系统设置） |
| `audio_busy` | 已有音频流处于 STARTING/RECORDING/STOPPING | false |
| `offline` | `startAudio` 时 WSS 未在线 | true |
| `recorder_failed` | `AudioRecord` 启动失败、读循环错误达 5 次、路由切换后重建失败 | true |
| `route_failed` | 蓝牙/SCO 路由切换失败 | true |
| `audio_timeout` | `audio.start` 后 3s 内未收到 `audio.ready`（`ACK_TIMEOUT_MS=3000`） | true |
| `max_duration` | 单流 120s 上限 | false |
| `connection_lost` | 录音过程中 WSS 断开 | true |

（这些是**本地**码，不在 PROTOCOL §13 的服务端错误码表里；服务端错误仍走 `error{code:1001/2001/2002/...}`。Web 对 `audio.error.code` 不透明透传。）

规则：Web 侧不直接持有 token；音频二进制帧由 Native 直接组帧发送（不经 WebView，避免 JS 拷贝抖动）。

### 3.2.1 seq 权威与本地关联字段（冻结）

- **`seq` 由 Native 独占**（D-012）：Web 侧 `send` 不生成、不依赖 `seq`，Native 解析 JSON 后覆盖式注入本方向下一个 `seq` 再上行；入站 S→C `seq` 也由 Native 记账（重连 `resync{lastSeq}` 用 Native 记录值）。理由：Native 同时发握手/心跳/音频控制帧，Web 与 Native 各自计数必然撞号，而协议要求每方向单调。
- **`_reqId` / `_reqTo`**：Web 可在消息里带本地字段 `_reqId`（任意字符串），Native **剥离后不出网**；Native 收到服务端消息若其 `replyTo` 命中某个已发出的 `seq`，把对应 `_reqId` 回填为 `_reqTo` 一并转发给 Web。Web 匹配响应优先用 `_reqTo`，其次 `clientId`（`chat.ack`）、`id`（审批/提问）。Mock 传输必须模拟同一行为，保证 mock 与真机一致。
- **Web 资源 origin**：`https://appassets.local/`，由 `shouldInterceptRequest` 从 `assets/web/` 读。必须给正确 MIME（`.js`→`text/javascript`、`.json`→`application/json`、`.css`→`text/css`），否则 ES module 加载失败；`index.html` 内一律用相对路径（`./js/...`），保证 `python -m http.server` 也能调试。
- **返回键**：`window.DshBridge.onBack(): boolean`，返回 `true` 表示已由 UI 处理（关闭 Sheet / 返回列表）；`false` 交给系统。
- **重连**：由 Native 自动负责（退避 1/2/4/8/15s），Web 只消费 `net.state`；诊断页「重连 Bridge」= `reconnect` op（D-013）。
- **音频**：二进制帧由 Native 直发，不经 WebView。
- **扫码**：M2 手动输入配对（host/port/fp/code，或粘贴整条 `dsphone://pair...` URI）；`scanPairing` 为预留 op（D-011）。

### 3.3 UI 状态机

`Idle → Connecting → Pairing → Authenticating → Online → Recording → WaitingForReply → ApprovalPending → Online`（与协议 §3 对齐）。

### 3.4 本地存储（V1）

- `deviceToken` → Android Keystore（EncryptedSharedPreferences）。
- 会话/消息/设置 → WebView **IndexedDB**（按 sessionId 分区）。
- 日志 → Native 滚动文件（7 天，脱敏），导出时再次过滤。
- 偏差说明：PRD §11 写的是 Room/SQLite；WebView UI 下改用 IndexedDB 更省一层桥接，记 `docs/DECISIONS.md`（D-010）。

## 4. 数据流（语音主链路）

```
PTT 按下 → Native 切 SCO(setCommunicationDevice) → audio.start
        → AudioRecord 20ms 帧 → 二进制帧(WSS) → Bridge.asr 线程
        → asr.partial(节流 5/s) → UI 灰色预览
松手/静音 500ms → audio.stop → 整段复判(SenseVoice) → asr.final
        → UI 填入输入框（可编辑，自动发送默认开）
        → chat.send → chat.ack → dsh.run() → 通知流
        → assistant.delta / tool.status → assistant.done
        （过程中审批/提问 → approval.request / question.request → 弹窗 → response）
```

## 5. 可观测性

- 结构化日志（JSON 行）：`ts, level, event, connId, sessionId, code, latency{}`。
- 埋点：`asr.first_partial_ms`、`asr.final_ms`、`chat.send_to_delta_ms`、`reconnect_count`。
- 诊断页读取最近错误与延迟统计（来自 Native 汇总 + Bridge 在 `hello.ok`/`diagnostics` 之外附带的版本信息）。
- 脱敏：token/音频/完整消息正文不进日志（只记长度与 hash 前缀）。

## 6. 构建与测试

| 产物 | 命令 | CI |
|---|---|---|
| Bridge 测试 | `python -m pytest bridge\tests -q` | Linux + Windows |
| 协议一致性 | `python -m pytest bridge\tests\test_protocol.py` | 每 PR |
| Web UI 单测 | `node --test app\web\tests` | 每 PR |
| APK | `cd app\android; ./gradlew assembleDebug` | ubuntu + JDK17 |
| 模型 | `python scripts\download_models.py`（不入库） | 手动/发布前 |

本机无 JDK/Android SDK → APK 只在 CI 构建（见 `docs/DECISIONS.md` D-006）。
