# DSH Phone 协议规范 v1（冻结）

| 项 | 值 |
|---|---|
| 版本 | **v1.0 FROZEN**（2026-11-17，M0 Phase0 冻结） |
| 子协议 | `dsphone.v1`（RFC 6455 token 语法，**不含 `/`**；见 D-015） |
| 适用 | 手机 App（C，客户端） ⇄ 电脑端 Bridge（S，服务端） |
| 上游依据 | `docs/PRD.md` §10 |
| 变更流程 | 见 §15；改动需 Lead 评审并同步 bridge/app 测试 |

> 约定：C = 手机 App，S = Bridge。JSON 字段一律 **camelCase**；base64 为标准 base64（带 padding）。
> PRD §10.3 示例里 `asr.partial` 的 `"seq":12` 在本规范中解释为**音频帧序号**，字段名 `aseq`；JSON 信封的 `seq` 是协议消息序号，两者不同（见 §2）。

---

## 1. 传输层

- 端点：`wss://<host>:<port>/ws`
- 子协议：`Sec-WebSocket-Protocol: dsphone.v1`；服务端必须回显该子协议，否则客户端断开。
- TLS：≥ TLS 1.2；Bridge 使用自签证书。客户端做 **SHA-256 证书指纹 pinning**：
  - `fp` = 服务器证书 DER 字节的 SHA-256，小写 hex（64 字符）。
  - App 首次配对时记录 `fp`（来自二维码或手动输入）；之后每次连接校验，不匹配即拒绝（视为中间人）。
- 帧类型：**文本帧 = JSON 控制消息**；**二进制帧 = 音频**（见 §10）。
- 单帧上限：文本帧 ≤ 64 KiB；二进制帧 ≤ 8 KiB（V1 实际 647 B）。
- 关闭 `permessage-deflate`（省 CPU、避免兼容问题）。

## 2. 消息信封与序号

所有 JSON 消息必须是对象，且包含：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `type` | string | 是 | 消息类型，见各章 |
| `seq` | int | 是 | 该方向单调递增序号，首个消息（`hello`/`hello.ok`）为 1 |
| `replyTo` | int \| null | 响应类必填 | 请求-响应型消息的响应必须回带请求的 `seq` |

规则：
- **每个方向独立计数**：C→S 与 S→C 各自从 1 开始；客户端 WebSocket 重连后计数器重置为 1。
- 二进制音频帧的序号不占用 JSON `seq`，而是帧头 `aseq`（u16，独立计数，从 1 开始，65535 后回绕到 1）。
- 服务端为 S→C 消息保留**重放缓冲**（最近 512 条或 10 分钟，取先到者），用于 `resync`。

## 3. 连接生命周期

```
C: WS connect (subprotocol dsphone.v1, TLS verify)
C→S hello
S→C hello.ok {needPair}
  needPair=true  → C→S pair.request → S→C pair.ok | pair.error
C→S auth
S→C auth.ok {sessions} | auth.error      # 通过后连接 = ONLINE
C→S resync {lastSeq}                      # 重连时必须；首次连接可省略
S→C resync.ok {fromSeq, uptoSeq, truncated}
... 业务消息 ...
WS close
```

**客户端状态机**：`Idle → Connecting → Pairing → Authenticating → Online → (Recording | WaitingForReply | ApprovalPending) → Online`

- 在 `auth.ok` 之前，服务端**只接受** `hello`、`pair.request`、`auth`、`ping`；其它消息 → `error{code:1001}` + 关闭 `4401`。
- 未认证连接的存活上限 15 秒（必须完成 auth）。

## 4. 握手

### 4.1 hello（C→S）

```json
{"type":"hello","seq":1,"v":1,"deviceId":"8f14e45f-...","app":"1.0.0","os":"android","ts":1710000000}
```

- `deviceId`：App 首次启动生成的 UUIDv4，持久化；未配对时也要发送（服务端据此判定 needPair）。
- `v`：协议主版本，V1 必须为 `1`。
- `ts`：Unix 秒（仅诊断用途，认证以 `auth.ts` 为准）。

### 4.2 hello.ok（S→C）

```json
{"type":"hello.ok","seq":1,"replyTo":1,"serverId":"srv-1a2b","bridge":"1.0.0","needPair":false,"authRequired":true,"ts":1710000000}
```

- `needPair`：该 `deviceId` 无有效配对记录时为 `true`。
- `authRequired`：V1 恒为 `true`。
- 版本不支持：`error{code:1003}` + 关闭 `4402`。

## 5. 配对

### 5.1 二维码 / 手动配对信息

Bridge 启动或请求配对时打印二维码，内容为 URI：

```
dsphone://pair?v=1&host=192.168.1.10&port=8765&fp=<64hex>&code=123456&sid=srv-1a2b
```

- `host` 必须是**具体监听地址**（禁止 0.0.0.0；默认部署为 127.0.0.1，局域网需显式开启）。
- `code`：6 位数字，**TTL 120 秒**；同一 `deviceId`/来源失败 **5 次锁定 5 分钟**（关闭码 4410）。
- `fp`：服务器证书 SHA-256 指纹，客户端必须校验。
- App 也支持手动输入 host/port/fp/code（扫码失败兜底）。

### 5.2 pair.request（C→S）

```json
{"type":"pair.request","seq":2,"deviceId":"8f14e45f-...","code":"123456","deviceName":"Pixel 8","pubKey":null}
```

- `pubKey`：保留字段，V1 恒为 `null`（见 `docs/DECISIONS.md` D-008）。

### 5.3 pair.ok / pair.error（S→C）

```json
{"type":"pair.ok","seq":2,"replyTo":2,"deviceToken":"ABEiM0RV...","serverFingerprint":"<64hex>","serverId":"srv-1a2b","expiresAt":null}
{"type":"pair.error","seq":3,"replyTo":2,"code":1002,"message":"pair code expired","attemptsLeft":2,"retryable":true}
```

- `deviceToken`：32 随机字节（base64 传输）。服务端持久化两份：`sha256(token)`（设备索引/吊销校验，可安全落库与打印）+ **密文**（AES-256-GCM 封装，密钥为每份安装独立的 `bridge/state/master.key`，文件权限 0600；因 §6.1 校验 HMAC 必须用原始 token）。原始 token 严禁写入日志/诊断/导出。客户端存 Keystore（V1 用 EncryptedSharedPreferences）。
- `expiresAt: null` = 长期有效，直到被撤销。
- 撤销设备后：该 token 立即 `auth` 失败（1001），已在线连接被关闭（4401）。

## 6. 认证

### 6.1 auth（C→S）

```json
{"type":"auth","seq":3,"deviceId":"8f14e45f-...","ts":1710000000,"nonce":"bm9uY2UxMjM0NTY3OA==","hmac":"gYfqlKpD...","v":1}
```

- **规范化字符串**（UTF-8，`\n` 分隔，无末尾换行、无空格）：

```
DSPHONE1\n{deviceId}\n{ts}\n{nonce}
```

- **HMAC**：`base64(HMAC-SHA256(key = deviceToken 原始 32 字节, msg = 规范化字符串))`
- `ts`：Unix 秒；与服务端时间偏差 > 120 秒 → 1001。
- `nonce`：16 随机字节 base64；服务端按 deviceId 缓存最近 10 分钟已用 nonce，重复即拒绝（防重放）→ 1001。

### 6.2 auth.ok / auth.error（S→C）

```json
{"type":"auth.ok","seq":3,"replyTo":3,"serverId":"srv-1a2b","sessions":[<Session>...]}
{"type":"auth.error","seq":4,"replyTo":3,"code":1001,"message":"bad token","needPair":true}
```

### 6.3 测试向量（`bridge/tests` 必须覆盖）

```
deviceToken (hex)   = 00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff
deviceToken (b64)   = ABEiM0RVZneImaq7zN3u/wARIjNEVWZ3iJmqu8zd7v8=
deviceId            = dev-abc123
ts                  = 1710000000
nonce (b64)         = bm9uY2UxMjM0NTY3OA==
canonical           = DSPHONE1\ndev-abc123\n1710000000\nbm9uY2UxMjM0NTY3OA==
hmac (b64)          = gYfqlKpD9PLEml5FzwSMmtd93hlDbF/JrA7lGcDBSjo=
hmac (hex)          = 8187ea94aa43f4f2c49a5e45cf048c9ad77dde19436c5fc9ac0ee519c0c14a3a
sha256(canonical)   = 4c1b758da310551fca45aeb91ed7cdb302fe09627d50421e0cd0aac59597c6e1
```

## 7. 心跳、超时与重连

- C **每 15 秒**发 `{"type":"ping","seq":n,"ts":...}`；S 回 `{"type":"pong","seq":m,"replyTo":n}`。
- S 若 **60 秒**未收到 C 的任何消息（含二进制音频帧）→ 关闭 `4408 heartbeat timeout`。
- C 若 **45 秒**未收到 S 的任何消息（含 pong）→ 判定断线，主动重连。
- 重连退避：`1,2,4,8,15,15,...` 秒（上限 15s，±20% 抖动）。
- 重连后流程：`hello → auth → resync{lastSeq}`，`lastSeq` = 本连接已处理的最大 S→C `seq`；断线时未完成的消息由 resync 补发。
- 客户端不应在重连时重发 `chat.send`：幂等由 `clientId` 保证（见 §9）。

## 8. 会话

### 8.1 数据结构

```json
Session = {"sessionId":"s-1","title":"厨房待办","updatedAt":1710000000,"lastPreview":"帮你查一下..."}
Message = {"messageId":"m-1","sessionId":"s-1","role":"user|assistant|system","text":"...","status":"done|streaming|error","createdAt":1710000000}
```

### 8.2 消息

| 请求（C→S） | 响应（S→C） |
|---|---|
| `{"type":"session.list","seq":n}` | `{"type":"session.list.ok","replyTo":n,"sessions":[Session]}` |
| `{"type":"session.create","seq":n,"title":"可选"}` | `{"type":"session.create.ok","replyTo":n,"session":Session}` |
| `{"type":"session.resume","seq":n,"sessionId":"s-1"}` | `{"type":"session.resume.ok","replyTo":n,"session":Session,"messages":[Message]}` |
| `{"type":"session.history","seq":n,"sessionId":"s-1","limit":50,"before":null}` | `{"type":"session.history.ok","replyTo":n,"messages":[Message],"hasMore":false}` |

- 所有响应都带 `seq`（服务端自己的序号）。
- 会话不存在 / 已失效：`error{code:3001}`。
- 删除会话只删手机本地记录（PRD FR-204），不消耗协议消息。

## 9. 对话

```json
C→S {"type":"chat.send","seq":10,"clientId":"<uuidv4>","sessionId":"s-1","text":"帮我查待办"}
S→C {"type":"chat.ack","seq":20,"replyTo":10,"clientId":"<uuidv4>","messageId":"m-2"}
S→C {"type":"assistant.delta","seq":21,"sessionId":"s-1","messageId":"m-3","text":"好的","index":0}
S→C {"type":"tool.status","seq":22,"sessionId":"s-1","toolCallId":"t-1","name":"bash","state":"running","title":"执行 ls"}
S→C {"type":"assistant.done","seq":23,"sessionId":"s-1","messageId":"m-3","finishReason":"completed"}
S→C {"type":"turn.error","seq":24,"sessionId":"s-1","code":3003,"message":"model error"}
```

规则：
- `assistant.delta.text` 是**增量**片段；若 SDK 只提供整段（spike 结论），则只发一帧 `index=0` 的整段文本，App 用打字机动画呈现。
- `tool.status.state`：`running | done | error`。
- `finishReason`：`completed | error | cancelled`。
- **幂等**：服务端按 `clientId` 去重（每会话保留最近 200 个）。重复 `chat.send` 只回 `chat.ack`（不再触发执行）。
- **并发**：同一会话同时只允许 1 个进行中的 turn；忙 → `error{code:4001,"retryable":true}`，客户端排队或禁用发送按钮。
- **停止生成**：`C→S {"type":"chat.cancel","seq":n,"sessionId":"s-1"}`；服务端尽力取消 → 最终必须发 `assistant.done{finishReason:"cancelled"}`；若 SDK 无取消能力（spike 结论），回 `turn.error{code:3003,"message":"cancel unsupported"}`。

## 10. 音频与 ASR

### 10.1 控制消息

```json
C→S {"type":"audio.start","seq":11,"sampleRate":16000,"channels":1,"bitDepth":16,"lang":"zh","mode":"ptt|continuous","sessionId":"s-1"}
S→C {"type":"audio.ready","seq":30,"replyTo":11,"asrMode":"real|fake","lang":"zh","codecs":["pcm16"]}
C→S {"type":"audio.stop","seq":12,"aseq":200}
S→C {"type":"asr.partial","seq":31,"text":"帮我查","stability":0.8,"aseq":12}
S→C {"type":"asr.final","seq":32,"text":"帮我查一下今天的待办","aseq":200,"durationMs":2400,"lang":"zh"}
```

- 已有音频流进行中 → `error{code:2001}`；ASR 不可用（模型缺失/初始化失败）→ `error{code:2002,"retryable":true}`。
- `codecs`：V1 只支持 `pcm16`；`opus` 为 V1.1 保留（届时客户端用 MediaCodec 编码）。
- `asr.partial` 频率上限 **5 次/秒**（服务端节流，只发最新文本）；文本未变化不发。
- `asr.final` **必须送达**：服务端缓冲，客户端可用 `resync` 补拉；收到 `audio.stop` 后 1.5 秒内必须发出（空串也要发）。
- 服务端 VAD：静音 ≥ 500ms（默认，可配置）自动定稿，等价于一次 `audio.stop`。
- 单条音频流时长上限 120 秒，超时服务端按 `audio.stop` 处理。

### 10.2 二进制音频帧（C→S，仅 `audio.ready` 之后）

| 偏移 | 长度 | 含义 |
|---|---|---|
| 0 | 1 | 帧类型：`0x01` = PCM16LE（`0x02` = Opus 保留） |
| 1 | 2 | `aseq` u16 **小端**，从 1 递增，65535 后回绕到 1 |
| 3 | 4 | `tsMs` u32 **小端**，相对 `audio.start` 的采集时刻（毫秒） |
| 7 | 640 | PCM：16 kHz / 16 bit / 单声道，20 ms = 640 字节 |

- 首帧帧头示例（`aseq=1, ts=0`）：`01 01 00 00 00 00 00`（7 字节，其后 640 字节 PCM）。
- 采样率不同时，payload 长度 = `sampleRate × channels × bitDepth/8 × 0.02`（必须整除 20ms）。
- 若 `audio.ready` 前收到二进制帧 → 丢弃并计数（不报错，防抖动）。
- 原始码率 256 kbps（16k×16bit）。PRD FR-407 要求 ≤64kbps → **V1 在局域网直传 PCM**（在 `docs/DECISIONS.md` 记录偏差），V1.1 引入 Opus（帧类型 0x02）满足带宽要求。

## 11. 审批与提问

### 11.1 审批

```json
S→C {"type":"approval.request","seq":40,"id":"ap-1","sessionId":"s-1","tool":"bash","reason":"执行 rm -rf build","argsPreview":"rm -rf build","deadline":1710000600,"defaultOutcome":"denied"}
C→S {"type":"approval.response","seq":13,"id":"ap-1","outcome":"approved|denied"}
S→C {"type":"approval.expired","seq":41,"id":"ap-1","outcome":"denied"}
```

- `deadline`：Unix 秒（默认 10 分钟）；到期服务端按 `defaultOutcome`（默认 `denied`）结案并发 `approval.expired`。
- 重复 `approval.response` 忽略；未知/已过期 `id` 忽略并记诊断日志（不返回错误帧，避免打断 UI）。
- 客户端 UI 默认焦点在「拒绝」（安全，PRD §12）。

### 11.2 提问

```json
S→C {"type":"question.request","seq":50,"id":"q-1","sessionId":"s-1","questions":[{"id":"q1","question":"选哪个方案?","multiSelect":false,"required":true,"allowCustom":false,"options":[{"id":"o1","label":"方案 A"},{"id":"o2","label":"方案 B"}]}],"deadline":1710000600}
C→S {"type":"question.response","seq":14,"id":"q-1","cancelled":false,"answers":[{"questionId":"q1","selected":["o1"],"custom":null}]}
S→C {"type":"question.expired","seq":51,"id":"q-1","cancelled":true}
```

- `cancelled: true` 表示用户取消 → 服务端把「取消」回传 SDK。
- 到期未答：服务端发 `question.expired{cancelled:true}` 并以取消结案。

## 12. 错误与关闭码

通用错误帧：

```json
{"type":"error","seq":60,"replyTo":10,"code":3003,"message":"model error","retryable":true,"detail":null}
```

| 码 | 含义 | 可重试 | 客户端处理 |
|---|---|---|---|
| 1001 | 认证失败 / 未认证 | 否 | 清除 token，回配对页 |
| 1002 | 配对码错误/过期 | 是 | 重新输入配对码 |
| 1003 | 协议版本不匹配 | 否 | 提示升级 App/Bridge |
| 2001 | 音频设备忙 / 已有音频流 | 是 | 等当前流结束再录 |
| 2002 | ASR 不可用 | 是 | 提示检查电脑端模型，可重试 |
| 3001 | 会话不存在 | 否 | 刷新会话列表 |
| 3002 | 审批超时 | 是 | 提示已默认拒绝 |
| 3003 | 模型 / DSH 错误 | 视情况 | 展示 `message` + 重试按钮 |
| 4001 | 服务端忙 / 背压 | 是 | 延迟重试（指数退避） |
| 5001 | 内部错误 | 是 | 展示诊断 ID |
| 5002 | SDK 协议错误 | 否 | 上报诊断页 |

**WebSocket 关闭码**：

| 码 | 含义 |
|---|---|
| 1000 | 正常关闭 |
| 1011 | 服务端内部错误 |
| 4400 | 协议错误（非法 JSON / 缺信封字段 / 字段类型错误；**不含**未知 `type`，见下条） |
| 4401 | 认证失败或未认证 |
| 4402 | 协议版本不匹配 |
| 4408 | 心跳超时 |
| 4410 | 配对尝试过多，已锁定 |

**未知 `type` 的处理（与 §14 一致，澄清）**：接收方遇到不认识的 `type` 必须**忽略并记诊断日志**，不得断开或回 4400（前向兼容）；只有结构性错误（非法 JSON、缺 `v`/`type`/`seq`、字段类型错误、帧过大）才用 4400 断开。§14 的兼容规则优先于本表中的 4400 说明。

## 13. 背压与限制

| 项 | 限制 |
|---|---|
| 文本消息长度 | ≤ 32,000 字符 |
| JSON 帧 | ≤ 64 KiB |
| 音频帧 payload | 恰好 640 B（16k/16bit/mono/20ms） |
| `asr.partial` 频率 | ≤ 5 次/秒 |
| 服务端事件重放缓冲 | 512 条 / 10 分钟 |
| 未确认事件队列 | 256 条（超出时**丢 partial、保 final**） |
| 单流音频时长 | ≤ 120 秒 |
| 每连接并发 turn | 1（同会话） |
| 配对码 | TTL 120s，失败 5 次锁 5 分钟 |
| 认证时间偏差 | ±120s，nonce 缓存 10 分钟 |

背压行为：服务端发送缓冲 > 256 KiB 时，丢弃 `asr.partial`；`assistant.delta` 合并为 ≤100ms 一帧；`asr.final` / `assistant.done` / `approval.request` 永不丢弃。

## 14. 版本与兼容

- `v` 为主版本号；任何不兼容改动 → 子协议升级为 `dsphone.v2`。
- `hello.ok.bridge` 报 Bridge 版本；App 发现协议不兼容时提示升级（PRD FR-xx / §16）。
- 新增**可选**字段不升版本；接收方必须忽略未知字段与未知 `type`（记录诊断，不崩溃）。

## 15. 变更流程

1. 提 PR 修改本文件，标题 `protocol: <变更摘要>`；
2. Lead 评审通过后，同步更新：
   - `bridge/tests/test_protocol.py`（测试向量 + 编解码单测）
   - `app/web/js/protocol.js` 与 `app/android/.../Protocol.kt` 的消息定义
3. 不兼容变更必须同时升 `v` 与子协议，并在 `docs/DECISIONS.md` 记一条 ADR。

---

**变更记录**

| 版本 | 日期 | 内容 |
|---|---|---|
| v1.0 | 2026-11-17 | 冻结：传输/信封/握手/配对/认证/会话/对话/音频/审批/错误/限制 |
