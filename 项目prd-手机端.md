
# DSH Phone 产品需求文档（PRD）v1.0

| 项目 | 内容 |
|---|---|
| 文档状态 | Draft，待评审 |
| 日期 | 2026-11-17 |
| 产品代号 | `dsh-phone`（暂定；App 中文名候选「DSH 随身」） |
| 一句话定义 | 把电脑上的 DeepSeek Harness 变成手机上的“随身终端”：打字 + 蓝牙麦语音实时输入 + 流式回复 + 审批。 |
| 本轮约束 | 只出 PRD，不写代码、不建文件；下一步换新工作区再落盘为 `docs/PRD.md` |

---

## 0. 工作区约定（换工作区后执行）

- 新仓库建议 `D:\code\dsh-phone`（独立 Git 仓库，不混入知识库项目）。
- 目录：`docs/`（本 PRD、架构、协议、测试计划）、`bridge/`（Python）、`app/android/`（Kotlin 壳）、`app/web/`（WebView UI）、`ci/`、`scripts/`。
- 分支：`main` 保护 + `feat/*`；合并前 CI 绿 + Lead review。
- 工作区切换后第一件事：把本文档落为 `docs/PRD.md`，并生成 `ARCHITECTURE.md`、`PROTOCOL.md`、`TESTPLAN.md`。

---

## 1. 背景与问题

用户已有运行 DSH 的 Windows 电脑，希望在手机上：
1. **打字**与 DSH 实时对话（流式回复、工具执行、审批）；
2. **语音实时输入**，边说边出字；
3. 语音输入使用**手机外接蓝牙麦克风**（耳机麦）。

现有社区插件（dsh-mobile、dsh-voice-mode 等）不能满足“蓝牙麦可控 + 完全自研 + 不依赖第三方插件”的组合。本产品用**自写 Android App + 自写电脑端桥接进程 + 官方 Python SDK** 实现，不依赖任何外部插件。

## 2. 产品目标与成功指标

**目标**：手机通过蓝牙耳机说一句话，≤3 秒内变成 DSH 的一条任务，并看到流式回复。

| 指标 | 目标值 | 测量方式 |
|---|---|---|
| 首次配对时间 | ≤3 分钟 | 用户测试 |
| 按住说话→文字定稿 | ≤1.5s（LAN） | 桥埋点 |
| 定稿→DSH 收到 | ≤0.5s | 桥日志 |
| 首字延迟（partial） | ≤1.0s | 桥埋点 |
| 中文识别错误率 | 20 句语料 ≤15% | 固定测试集 |
| 蓝牙麦路由成功率 | 测试机型 ≥95% | 真机矩阵 |
| 断线重连 | ≤5s | 拔网测试 |
| 崩溃率 | 无崩溃（MVP 标准）；<1% 会话 | 本地日志 |
| 1 小时语音会话耗电 | ≤10% | 真机 |
| APK 体积 | ≤25MB | 构建产物 |

## 3. 范围

**In Scope（V1.0，Android）**
- 手机↔电脑桥的配对、认证、加密传输
- 会话列表/新建/切换/恢复（DSH 侧上下文连续）
- 文本聊天 + 助手流式显示 + 工具状态行
- 语音输入：按住说话（PTT）为 P0，连续聆听 P1；partial 实时显示 + 定稿可编辑再发
- 蓝牙耳机麦克风路由与状态显示（SCO / LE Audio）
- 审批弹窗、`ask_user_question` 弹窗
- 本地消息记录、断线重连、设置页、诊断页
- CI 自动出 APK

**Out of Scope（V1.0 不做）**
- iOS（Windows 无法构建；V2 用 Mac/云 Mac）
- 与桌面端 DSH 会话共享（SDK 用独立 home；V2 研究）
- 复用官方 DSH Web UI / 修改 DSH 本体
- 公网直连（默认只允许局域网/Tailscale）
- 图片/文件上传、多设备同时在线、语音播报（TTS）、唤醒词（均 V1.1+）

## 4. 用户画像与场景

| 场景 | 描述 | 关键需求 |
|---|---|---|
| 厨房/家务 | 戴蓝牙耳机、手机放一边，口述任务 | 蓝牙麦、PTT、低延迟、锁屏可用 |
| 通勤 | 手机上快速跟进 DSH 任务 | 会话恢复、流式回复、断网重连 |
| 会议记录 | 连续说长句 | 连续模式、VAD 断句、可编辑定稿 |
| 危险操作 | DSH 要执行命令 | 审批弹窗、默认拒绝、可撤销设备 |

## 5. 术语

DSH＝DeepSeek Harness；Bridge＝电脑端桥接进程；PTT＝按住说话；partial＝流式中间识别文本；final＝定稿文本；SCO＝蓝牙通话音频；LE Audio＝低功耗音频；Typert Remote＝DSH 内部 RPC（本项目不使用）。

---

## 6. 总体架构

```
┌─────────────── Android App（Kotlin + WebView） ───────────────┐
│  Web UI（HTML/JS）: 会话列表 / 聊天 / 设置 / 诊断            │
│        ↕ WebMessage / JsBridge                                │
│  Native: 录音(AudioRecord VOICE_COMMUNICATION) / 蓝牙路由     │
│          (setCommunicationDevice) / 前台服务 / Keystore       │
│        ↕ WSS（TLS + 配对 token）                              │
└───────────────────────────┬────────────────────────────────────┘
                            │ 局域网 / Tailscale
┌───────────────────────────▼────────────────────────────────────┐
│ Bridge（Python 3.10+，单进程）                                  │
│  WSS 服务端 + 配对认证 + 协议编解码 + 背压/重连                  │
│  ASR：sherpa-onnx 流式 zipformer（partial）+ SenseVoice（final）│
│  DSH：deepseek-harness-sdk（run / on_notification /            │
│       resume_session / on_user_question / approval）            │
└───────────────────────────┬────────────────────────────────────┘
                            │ stdio JSON-RPC（SDK 内部）
                     dsh --profile sdk（子进程）
```

**端到端主流程（语音）**：手机按下 PTT → 启 SCO（预热）→ 16k PCM 20ms 帧 → WS → Bridge 缓冲并流式识别 → partial 推手机显示 → 松手/静音 500ms → final 定稿 → 手机确认或自动发送 → `session.run()` → DSH 返回通知 → assistant 增量推手机 → 完成。

**审批流**：DSH 发起审批 → Bridge `next_request()` → `approval.request` → 手机弹窗 → `approval.response` → `client.respond()`；超时 10 分钟默认拒绝。

## 7. 功能需求

优先级：P0＝MVP 必须；P1＝V1.1；P2＝后续。

### 7.1 连接与配对（P0）
| ID | 需求 | 验收 |
|---|---|---|
| FR-101 | Bridge 首次启动生成自签 TLS 证书 + 设备指纹，显示配对二维码（含 host/port/指纹/6 位配对码） | 二维码可被 App 扫描 |
| FR-102 | App 扫码或手动输入地址+配对码完成配对，获得 deviceToken 存 Keystore | 重启 App 免配对 |
| FR-103 | 每次连接 `auth` 校验 deviceToken + 时间戳 + HMAC，防重放 | 错误 token 拒绝 |
| FR-104 | 配对码 TTL 120s，失败 5 次锁定 5 分钟 | 暴力破解无效 |
| FR-105 | 设备管理：Bridge 端可列出/撤销设备；App 端可解除配对 | 撤销后立即断连 |
| FR-106 | 连接状态机：未配对/连接中/在线/重连中/主机离线；UI 有明确提示 | 断网 30s 内状态正确 |
| FR-107 | 支持配置监听地址：默认 `127.0.0.1`，开启局域网选具体 IP，或仅 Tailscale IP；禁止默认 `0.0.0.0` | 越权绑定报错 |

### 7.2 会话管理（P0）
| ID | 需求 | 验收 |
|---|---|---|
| FR-201 | 会话列表：标题、更新时间、最近一条摘要 | 列表可滚动刷新 |
| FR-202 | 新建会话、切换会话、恢复历史会话（DSH 上下文连续） | 恢复后追问依赖上文成功 |
| FR-203 | 会话切换时本地消息记录按会话隔离 | 无串消息 |
| FR-204 | 会话删除（仅删手机本地记录，不删 DSH 会话）需二次确认 | 提示清楚 |
| FR-205 | 历史消息来源：V1 本地存储；P1 支持从 `$DSH_HOME/sessions/*.jsonl` 导入 | 导入后气泡正确 |

### 7.3 文本聊天（P0）
| ID | 需求 | 验收 |
|---|---|---|
| FR-301 | 输入框、发送、空消息禁止、发送中状态 | 正常收发 |
| FR-302 | 助手流式显示（增量追加或整段，取决于 M0 spike）；代码块等宽显示 | 长回复不卡顿 |
| FR-303 | 工具调用状态行：工具名 + 运行中/完成/失败（不渲染完整卡片） | 至少 1 种工具可见 |
| FR-304 | 消息操作：复制、重发、停止（若 SDK 支持取消）、滚动到底 | 操作可用 |
| FR-305 | 发送幂等：clientId 去重，重连后不重复发送 | 断网重连不重复 |
| FR-306 | 错误展示：鉴权失败、会话不存在、模型错误、超时，附重试按钮 | 文案可理解 |

### 7.4 语音输入（P0）
| ID | 需求 | 验收 |
|---|---|---|
| FR-401 | 按住说话：按下开始采音并显示录音浮层，松手定稿 | 3 秒短句可用 |
| FR-402 | partial 实时显示在输入框上方（≤5 次/秒刷新，防抖） | 肉眼可见“边说边出字” |
| FR-403 | 定稿后填入输入框，可编辑；配置“自动发送”开关（默认开） | 两种模式都可用 |
| FR-404 | 静音 500ms 自动断句并定稿（松手立即定稿优先） | 断句准确 |
| FR-405 | 连续聆听模式（P1）：VAD 断句、句间不退出、点击结束 | 连续说 5 句正确分条 |
| FR-406 | 语言：中文（默认）/英文/中英混合；识别语言可切换 | 中英混合可用 |
| FR-407 | 音频参数：16kHz、16bit、单声道、20ms/帧；上行带宽 ≤64kbps | 抓包验证 |
| FR-408 | 录音权限被拒时引导去系统设置；无麦时提示 | 引导正确 |

### 7.5 蓝牙麦克风（P0）
| ID | 需求 | 验收 |
|---|---|---|
| FR-501 | 显示当前输入路由：手机麦/蓝牙 HFP/LE Audio，含设备名 | 状态准确 |
| FR-502 | PTT 按下时主动切换到蓝牙通信设备（`setCommunicationDevice`，API31+；低版本 `startBluetoothSco`） | 蓝牙耳机说话被采集 |
| FR-503 | 松手后释放路由，避免影响系统音乐播放 | 播放不中断 |
| FR-504 | 蓝牙连接失败/超时 8s 回退手机麦，并提示 | 有回退与提示 |
| FR-505 | 设置页可选择“偏好设备”（默认跟随系统） | 选择生效 |
| FR-506 | 支持 HFP 8k/16k 与 LE Audio（API33+，`TYPE_BLE_HEADSET`）；不支持 LE Audio 时回落 HFP | 两种耳机均可用 |

### 7.6 审批与提问（P0）
| ID | 需求 | 验收 |
|---|---|---|
| FR-601 | 审批弹窗：工具名、命令/原因、允许/拒绝；超时 10 分钟默认拒绝 | 拒绝后 DSH 收到 |
| FR-602 | `ask_user_question` 弹窗：单选/多选/自定义输入；可取消（返回 cancelled） | 答案回传格式正确 |
| FR-603 | 后台时审批以高优先级通知提醒（P1） | 通知可点回 |

### 7.7 设置与诊断（P0）
| ID | 需求 | 验收 |
|---|---|---|
| FR-701 | 设置：服务器地址、重新配对、语言、自动发送、VAD 静音时长、模型、主题 | 保存即生效 |
| FR-702 | 诊断页：版本、连接状态、延迟统计（首字/定稿/回复）、最近错误、导出日志 | 可导出文本 |
| FR-703 | 一键重启桥/重连按钮 | 可恢复 |
| FR-704 | 隐私：日志不含音频原文与 token；导出前脱敏 | 人工检查 |

---

## 8. 非功能需求

| ID | 类别 | 要求 |
|---|---|---|
| NFR-1 | 性能 | 首字 ≤1.0s；定稿 ≤1.5s；App 内存 ≤250MB；Bridge ≤600MB（含模型） |
| NFR-2 | 可靠性 | 断线自动重连（指数退避 1/2/4/8/15s）；音频不重复、不丢 final；会话状态可重同步 |
| NFR-3 | 安全 | TLS1.2+ + 证书 pinning；token 存 Keystore；不裸暴露公网；审批默认拒绝；设备可撤销；防重放 |
| NFR-4 | 隐私 | 音频不上传公网（本地 ASR）；日志脱敏；卸载可清除全部本地数据 |
| NFR-5 | 兼容 | Android 10+（API29），target 35；WebView ≥ Chrome 110；蓝牙 HFP/LE Audio |
| NFR-6 | 可用性 | 单手可完成发送/语音；弱网有明确反馈；首次使用有 3 步引导 |
| NFR-7 | 可维护 | 模块化、协议有测试向量、CI 必过、关键路径单测覆盖 ≥60% |
| NFR-8 | 可观测 | 结构化日志、错误码统一、诊断页可导出 |
| NFR-9 | 国际化 | 中英双语资源；文案不硬编码在代码 |
| NFR-10 | 无障碍 | 触控目标 ≥48dp；TalkBack 可读关键控件；深浅色跟随系统 |

---

## 9. 技术方案要点

### 9.1 客户端
- Kotlin 原生壳 + WebView 内 HTML/JS UI（迭代快、agent 友好），`WebMessage`/JsBridge 通信。
- 前台服务类型 `microphone`；权限：`RECORD_AUDIO`、`BLUETOOTH_CONNECT`、`POST_NOTIFICATIONS`、`FOREGROUND_SERVICE_MICROPHONE`、`INTERNET`、`ACCESS_NETWORK_STATE`。
- WebView 需重写 `onPermissionRequest` 允许音频采集。
- 构建：Gradle；CI 在 GitHub Actions（ubuntu + JDK17）出 debug APK；release 签名密钥由用户保管。

### 9.2 桥接进程
- Python 3.10+，`deepseek-harness-sdk` + `sherpa-onnx`（均为 PyPI 安装）。
- 单进程 asyncio：`websockets` 服务端 + SDK 调用放线程池（SDK 是同步阻塞）。
- 配置 `config.toml`：端口、绑定地址、模型路径、静音阈值、自动发送、日志级别、会话 home。
- 启动即生成证书/二维码，控制台提示；可选开机自启（P2）。

### 9.3 ASR 策略
- partial：sherpa-onnx 流式 zipformer（中英双语），chunk 320ms，CPU 推理。
- final：松手/静音 500ms 后用 SenseVoice（sherpa-onnx offline）对整段复判 + ITN。
- 降级：模型缺失 → 提示/自动下载；识别失败 → 保留音频文件供重试（可选）。
- 语言切换影响识别模型与 ITN。

### 9.4 DSH 集成映射（M0 必须 spike 验证）

| 能力 | 方案 | 风险 |
|---|---|---|
| 发消息 | `harness.run(text, session_id, on_notification)` | 低 |
| 流式助手文本 | `on_notification` 事件（是否含 token 增量待验证） | 中；无增量则整段 |
| 新建/恢复会话 | `start_session(id)` / `resume_session(id)` | 低 |
| 提问 | `on_user_question` 回调 | 低 |
| 审批 | `HarnessClient(approval_requests=True)` + `next_request()/respond()` | 中；高层封装是否暴露待验证 |
| 取消生成 | 待验证 SDK 是否有 cancel 接口 | 中 |
| 模型选择 | `DeepSeekHarness(provider=..., model=...)` | 低 |
| 权限预设 | `/permission <preset>` 通过 run() 发送 | 低 |
| 历史记录 | `$DSH_HOME/sessions/*.jsonl` 解析 | 中；格式内部约定 |

### 9.5 音频与蓝牙
- 采集：`AudioRecord(MIC/VOICE_COMMUNICATION)`，16k/16bit/mono，20ms 帧，`AudioRecord.Builder` 低延迟模式。
- 路由：API31+ `setCommunicationDevice()`；API29-30 `startBluetoothSco()`；LE Audio 用 `TYPE_BLE_HEADSET`。
- PTT 设计吸收 SCO 冷启动（实测 1.7–3.5s）：按下即建链，界面显示“预热中”。
- 回声/打断：MVP 不启用外放；建议戴耳机（PRD 明确提示）。

### 9.6 历史记录
V1 本地 SQLite/文件存消息（按会话）；P1 从 DSH JSONL 导入（只读，不改 DSH 数据）。

---

## 10. 协议规范 v1（草案）

### 10.1 传输
- 单端点 `wss://host:port/ws`，子协议 `dsphone/1`；文本帧 = JSON，二进制帧 = 音频。
- 心跳：客户端 15s `ping`，服务端 `pong`；60s 无响应断开重连。
- 顺序：每条消息带 `seq`（单调递增）；重连后 `resync{lastSeq}` 补发未确认事件。

### 10.2 握手与配对
```json
C→S {"type":"hello","v":1,"deviceId":"...","app":"1.0.0"}
S→C {"type":"hello.ok","serverId":"...","bridge":"1.0.0","needPair":true}
C→S {"type":"pair.request","code":"123456","deviceName":"Pixel 8","pubKey":"base64"}
S→C {"type":"pair.ok","deviceToken":"base64(32B)","serverFingerprint":"sha256"}
C→S {"type":"auth","deviceId":"...","ts":1710000000,"nonce":"...","hmac":"..."}
S→C {"type":"auth.ok","sessions":[...]}
```
失败：`pair.error{code}` / `auth.error{code}`（见 10.6）。

### 10.3 音频与 ASR
```json
C→S {"type":"audio.start","sampleRate":16000,"lang":"zh","mode":"ptt"}
S→C {"type":"audio.ready"}
C→S 二进制帧: [0x01][seq:u16][tsMs:u32][pcm16le...]   // 20ms=640B
S→C {"type":"asr.partial","text":"帮我查","stability":0.8,"seq":12}
S→C {"type":"asr.final","text":"帮我查一下今天的待办","seq":13,"durationMs":2400}
C→S {"type":"audio.stop"}
```
规则：partial ≤5 次/秒；final 必须送达（服务端缓冲，客户端可补拉）。

### 10.4 对话
```json
C→S {"type":"chat.send","clientId":"uuid","sessionId":"s1","text":"..."}
S→C {"type":"chat.ack","clientId":"uuid","messageId":"m1"}
S→C {"type":"assistant.delta","sessionId":"s1","messageId":"m2","text":"..."}
S→C {"type":"tool.status","sessionId":"s1","name":"bash","state":"running"}
S→C {"type":"assistant.done","sessionId":"s1","messageId":"m2","finishReason":"completed"}
S→C {"type":"turn.error","sessionId":"s1","code":3003,"message":"model error"}
```

### 10.5 会话
`session.list` / `session.create{title?}` / `session.resume{sessionId}` / `session.history{sessionId,limit}` → `session.history.ok{messages[]}`。

### 10.6 审批/提问
```json
S→C {"type":"approval.request","id":"a1","tool":"bash","reason":"rm -rf ...","deadline":1710000600}
C→S {"type":"approval.response","id":"a1","outcome":"approved"}
S→C {"type":"question.request","id":"q1","questions":[...]}
C→S {"type":"question.response","id":"q1","answers":[...]}
```

### 10.7 错误码
| 码 | 含义 | 可重试 |
|---|---|---|
| 1001 | 认证失败 | 否（重新配对） |
| 1002 | 配对码过期/错误 | 是 |
| 1003 | 协议版本不匹配 | 否 |
| 2001 | 音频设备忙/占用 | 是 |
| 2002 | ASR 不可用 | 是 |
| 3001 | 会话不存在 | 否 |
| 3002 | 审批超时 | 是 |
| 3003 | 模型/DSH 错误 | 视情况 |
| 4001 | 服务端忙（背压） | 是 |
| 5001 | 内部错误 | 是 |
| 5002 | SDK 协议错误 | 否（上报诊断） |

---

## 11. 数据模型（本地）

| 实体 | 字段 |
|---|---|
| Server | id, name, host, port, fingerprint, lastConnectedAt |
| Pairing | deviceId, deviceToken(Keystore), serverId, createdAt |
| SessionLink | localId, dshSessionId, title, updatedAt, lastPreview |
| Message | id, sessionId, role, text, status, createdAt, clientId |
| Settings | lang, autoSend, silenceMs, theme, preferAudioDevice, model |
| Diagnostics | lastError, latencyStats, appVersion, bridgeVersion |

存储：设备令牌 → Android Keystore；其余 → Room/SQLite；日志滚动保留 7 天。

---

## 12. UI/UX 规范

**页面清单**：启动/配对页、会话列表、聊天页、设置页、诊断页、蓝牙状态条、录音浮层、审批 Sheet、提问 Sheet、错误 Toast。

**关键交互**
- 录音浮层：波形 + 计时 + “上滑取消”；按下即显示“连接耳机中…”直到 SCO 就绪。
- partial 显示在输入框上方灰色文字；定稿后变黑并可编辑；自动发送倒计时可取消（可选）。
- 助手流式：光标跟随；长回复自动滚动但用户上滑后停止跟随。
- 审批：红色危险按钮 + 命令原文等宽展示；默认焦点在“拒绝”（安全）。
- 状态机：`Idle → Connecting → Pairing → Online → Recording → WaitingForReply → ApprovalPending`。

**深色/浅色**跟随系统；触控目标 ≥48dp；文案中英双语；错误文案必须含“下一步怎么做”。

---

## 13. 里程碑与排期

| 阶段 | 内容 | 工期 | 人工检查点 |
|---|---|---|---|
| M0 Phase0 | 修沙箱、建仓库骨架、协议冻结、SDK spike | 1–2 天 | H0：PRD/架构/协议评审 |
| M1 | Bridge 骨架 + 配对认证 + WSS + 假音频 ASR | 3–4 天 | H1：电脑说一句话→出字→DSH 回复 |
| M2 | App 壳 + WebView UI + 文本聊天（模拟数据→真桥） | 4–6 天 | H2：APK 装机可打字聊天 |
| M3 | 真录音 + 蓝牙路由 + PTT + partial/定稿 | 4–6 天 | H3：蓝牙耳机实测可用 |
| M4 | 审批/提问、重连、锁屏保活、诊断、CI 出包 | 3–5 天 | H4：安全评审 + 验收 |
| 合计 | | 约 2–3 周 | |

---

## 14. 团队分工（agent 可执行）

| 角色 | 职责 | 写作用域 | 交付 |
|---|---|---|---|
| Lead | 协议冻结、review、集成、验收、发布 | `docs/`、`ci/` | 版本、验收报告 |
| bridge-core | WSS、认证、协议、SDK 对接、重连 | `bridge/`（除 asr） | bridge 可运行 |
| asr-pipeline | 流式/定稿识别、延迟基准、模型下载 | `bridge/asr/`、`scripts/` | ASR 服务 + 基准 |
| android-shell | Kotlin 壳、录音、蓝牙、前台服务、JS 桥 | `app/android/` | APK |
| web-ui | 聊天/设置/诊断 UI、状态机、i18n | `app/web/` | 前端资源 |
| ci-build | Actions 构建、测试流水线、版本号 | `.github/`、`ci/` | 每提交出包 |

**协作规则**：协议先冻结；模块边界不跨写；每模块带单测；合并前 CI 绿 + Lead review；密钥不上仓库。

---

## 15. 测试计划与验收

**测试层次**：协议单测（测试向量）、Bridge 集成（假音频/样例 WAV）、App UI（WebView 内 DOM 测试/手工）、真机 E2E。

**真机矩阵**：Android 12 / 14 / 15 各 1 台；HFP 耳机 1 个、LE Audio 耳机 1 个；网络：家庭 Wi-Fi、手机热点、Tailscale、断网。

**MVP 验收清单**
- [ ] 扫码配对 ≤3 分钟；重启免配对；撤销设备立即断连
- [ ] 手机打字→DSH 回复流式可见
- [ ] 蓝牙耳机 PTT 说话→partial→定稿→发送→回复
- [ ] 断网 5s 内自动重连，消息不重复
- [ ] 审批弹窗可拒绝，DSH 收到拒绝；问题弹窗答案正确
- [ ] 锁屏/切后台 5 分钟返回仍可用（前台服务）
- [ ] 诊断页可导出脱敏日志
- [ ] 首字/定稿延迟达标
- [ ] 全链路无 token/音频出现在日志

---

## 16. 发布与运维

- 分发：GitHub Releases 侧载 APK（不上架商店）；版本号 `1.0.0+code`。
- Bridge：`pip` 安装/独立 zip；提供 `start.bat` 与配置示例（V1 不做服务化）。
- 签名：debug 用 CI 自动签名；release keystore 由用户生成保管，CI 通过 secrets 注入。
- 崩溃/日志：本地文件 + 诊断页导出，不上传（V1）。
- 升级：App 检查 bridge 版本，协议不兼容时提示升级。

---

## 17. 风险登记

| 风险 | 概率/影响 | 缓解 |
|---|---|---|
| SDK 无 token 级流式 | 中/中 | M0 spike；无则整段返回 + 打字机模拟 |
| SDK 审批/cancel 接口不符预期 | 中/中 | 用底层 HarnessClient；退回“无审批”模式则 MVP 降级并标注 |
| 蓝牙路由厂商差异 | 高/高 | 8s 超时回退；状态显示；真机矩阵；文档列已知机型 |
| 本机沙箱/shell 故障 | 已发生/高 | Phase0 修沙箱或批准 danger-full-access；CI 云构建兜底 |
| Android 工具链缺失 | 中/中 | CI 出 APK，本机只跑 Python 测试 |
| 模型下载慢/大 | 中/低 | 镜像、预下载脚本、可配置路径 |
| 中文 IME/组合输入 bug | 中/中 | 自定义输入框 + composition 处理；参考已知 issue |
| 安全误用（公网暴露） | 低/高 | 默认回环绑定；文档红色警告；配对+撤销 |
| 会话与桌面端不共享 | 确定/中 | PRD 明示 V1 不共享 |

---

## 18. 待决问题（需要你拍板）

1. 新工作区路径确认：`D:\code\dsh-phone`？
2. V1 是否接受“与桌面端 DSH 会话不共享”（独立 dsh_home）？
3. TTS 朗读是否进 V1，还是 V1.1？
4. 发布渠道：GitHub Releases 侧载可以吗？
5. 是否允许 Phase 0 修沙箱/装 Python 依赖/联网下模型（约几百 MB）？
6. 最低 Android 版本 10（API29）是否可接受？

## 19. 交付物清单

`docs/PRD.md`（本文）、`docs/ARCHITECTURE.md`、`docs/PROTOCOL.md`、`docs/TESTPLAN.md`、`docs/SECURITY.md`、`README.md`、可运行 bridge、APK、验收报告。

## 附录 A：关键参考

- Python SDK：https://deepseek-harness.github.io/deepseek-harness/guide/python-sdk.md
- SDK 参考（通知/审批/提问）：https://github.com/deepseek-ai/deepseek-harness/blob/master/python/sdk/README.zh.md
- 插件/扩展点：https://deepseek-harness.github.io/deepseek-harness/develop/basic/index.md
- 蓝牙 SCO 参考实现：https://github.com/jooray/android-bluetooth-microphone

## 附录 B：需求追踪样例

| 需求 | 设计 | 测试 | 状态 |
|---|---|---|---|
| FR-501 蓝牙路由 | 9.5 | E2E-03 | 待开发 |
| FR-302 流式回复 | 9.4 | IT-05 | 待 spike |

---

**本轮未创建任何文件。** 你确认上面 6 个待决问题（至少 1、2、5）后，下一步在新工作区我先落 `docs/PRD.md`，再按 M0 开始推进。