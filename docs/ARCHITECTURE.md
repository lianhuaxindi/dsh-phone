# 架构设计（ARCHITECTURE）

> 上游：`docs/PRD.md`；协议：`docs/PROTOCOL.md` v1（冻结）；决策：`docs/DECISIONS.md`。
> 本文只描述**模块边界与数据流**，接口细节以协议与代码 docstring 为准。

## 1. 总览

```
┌──────────────────── Android App ────────────────────┐
│  WebView UI (app/web, 纯 HTML/JS/CSS)               │
│        ⇅  JsBridge(WebMessage)                       │
│  Kotlin 壳 (app/android): 录音/蓝牙/前台服务/Keystore │
│        ⇅  WSS dsphone/1 (TLS + 指纹 pinning)         │
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
| 桥 | `app/android/.../JsBridge.kt` | WebView ⇄ Native 的 `postMessage` 通道（JSON） | 不放业务规则 |
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

Native → Web（`window.DshBridge.onEvent(json)`）：`net.state` / `proto.message`（协议原样透传）/ `audio.state` / `audio.route` / `audio.level`（波形）/ `error`。

规则：Web 侧不直接持有 token；音频二进制帧由 Native 直接组帧发送（不经 WebView，避免 JS 拷贝抖动）。

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
