# DSH Phone · Web UI (`app/web`)

DSH Phone 手机端 WebView UI。纯 HTML/CSS/ES module，**零 npm 依赖、零构建步骤**：
同一份文件既被 Android WebView 直接加载（`https://appassets.local/index.html`，
由 `scripts/sync_web.ps1` 同步到 `app/android/app/src/main/assets/web/`），
也可以在桌面浏览器里用 `python -m http.server` 直接演示。

- 设计文档：`docs/PRD.md`（§12 UI）、`docs/PROTOCOL.md`（v1.1，v1.0 冻结 + D-033 增量）、`docs/ARCHITECTURE.md`（§3.2 JsBridge 契约）、`docs/DECISIONS.md`（D-028…D-034）
- 写作用域：本目录 `app/web/**`；不要从这里直接改 `bridge/`、`app/android/`
- v1.1 界面：DeepSeek 官方手机端风格（主色 `#4D6BFE`、圆角气泡、会话抽屉、空态欢迎）；
  聊天页顶栏展示**模型 / 思考强度 / 上下文占用**；支持**图片发送**与**真实扫码配对**

## 1. 浏览器演示（Mock 后端，无需电脑端 Bridge）

在仓库根目录执行：

```powershell
cd app\web
python -m http.server 8123
```

浏览器打开 <http://127.0.0.1:8123/index.html>（手机宽度窗口更接近真机效果）。
默认传输自动选择：没有 `window.DshNative` 时使用 **mock 后端**，所有交互都可离线演示。

演示路径（配对 → 会话 → 发送 → 流式回复 → 工具状态 → 审批 → 提问 → 设置 → 诊断）：

1. 配对页已预填 mock 服务器信息，配对码 `123456`，点「连接」（也可点「扫码配对」，mock 会返回成功并自动填表 + 展示 `hosts` 候选）；
2. 自动进入会话列表（含 2 个演示会话），点任意会话进入聊天；
3. 顶部可看到**模型 chip**（如 `DeepSeek-V4.1-Flash`）、**思考强度 chip**（思考·高/最高）与**上下文占用条**（加载 `session.meta` 后显示，每轮回复后按 `assistant.done.usage` 更新）；
4. 输入文案发送，观察流式回复、工具状态行、「停止」按钮；
5. 发送含关键词的消息触发弹窗：
   - “删除文件 / bash / rm / 部署” → **工具审批**弹窗（默认焦点在「拒绝」，红色等宽命令）
   - “选择方案 / 哪个 / choose” → **追问**弹窗（单选 + 多选 + 自定义答案）
   - “报错 / 失败” → 错误气泡 + 重试
   - “长文 / 详细” → 长回复（向上滚动会暂停自动跟随，出现「回到底部」）
   - “模型 / 切换模型 / 换模型” → mock 在 `deepseek-flash` ↔ `deepseek-v4-pro` 间切换，顶栏三要素同步变化
6. 点 composer 的 `📎`（相册）或 `📷`（相机）：mock 返回内置示例图（离线可用），缩略图可删除；
   可只发图片（不填文字）。发送后消息内显示图片，重新打开会话会走 `media.get` 懒加载原图；
7. 按住麦克风按钮说话：先显示「连接耳机中…」，随后波形 + 计时 + 灰色 partial；
   松开自动发送（可在设置里关闭 autoSend），上滑取消；
8. 设置页：服务器地址、重新配对、语言（中/英）、主题（跟随系统/浅/深）、
   autoSend、VAD 静音时长、首选音频设备；
9. 诊断页：版本/连接状态/延迟（首个 partial、final、首 token、整轮，含 p50/p95）、
   连接失败时的每地址 `tried` 明细、最近错误、导出已脱敏日志（不含 token/音频/正文）、重连 Bridge。

URL 参数可强制传输：`?transport=mock` / `?transport=ws` / `?transport=native`。

### WebSocket 调试模式

浏览器无法使用 Native JsBridge，但可用 `?transport=ws` 连真实 Bridge 的 WSS 端点做联调
（自签证书需浏览器手动信任；token 仅存在 `localStorage['dsphone.debugToken']`）。
真机上 Web 侧不接触设备 token，由 native 负责认证与音频二进制帧。

## 2. 测试

```powershell
# 仓库根目录
node --test app\web\tests
```

本机若因沙箱限制报 `spawn EPERM`（Node 测试运行器需要子进程），等价地进程内运行：

```powershell
cd app\web
node --test --test-isolation=none "tests/*.test.js"
# 或单个文件：node tests/protocol.test.js
```

测试覆盖：协议（含 PROTOCOL §6.3 HMAC 测试向量与 v1.1 图片/session.meta/usage/media.get 约束）、
状态机、i18n 完整性（中英键集一致、所有错误码文案含下一步）、IndexedDB/内存存储（含草稿与图片缓存）、
诊断脱敏与 p50/p95、调度器、mock 全流程（含扫码/取图/模型切换/多地址失败）、三种传输契约、
controller 端到端演示流程，以及静态检查（相对路径、模块图、无构建产物、`t()` 字面量键都存在、
48dp 与减弱动效样式）。当前共 **144** 个测试，全部通过。

验收证据（TC-WEB-01…18 与 PRD §12 的逐项映射、契约偏差、已知限制、mock 场景清单）见
[`ACCEPTANCE.md`](./ACCEPTANCE.md)。

## 3. 文件结构

```
app/web/
  index.html            # 唯一入口（相对路径 ./js ./css）
  css/tokens.css        # 设计变量、深浅色（prefers-color-scheme）、减弱动效
  css/app.css           # 外壳布局：topbar / screen / tabbar / 浮层根节点
  css/components.css    # 按钮、表单、弹窗、聊天、录音浮层、审批、设置、诊断
  js/config.js          # 版本、设置默认值、可选项、重连退避
  js/protocol.js        # PROTOCOL v1.1：常量/错误码/编解码/HMAC/帧头/图片与 usage 规范化
  js/transport.js       # WS + Native JsBridge 两种真实传输（统一 Transport 接口，含 pickImage/mediaGet）
  js/mock.js            # 演示用 Mock 后端（模拟 native 事件 + 协议消息 + 配对/扫码/取图/审批/ASR）
  js/state.js           # UI 状态机（纯 reducer，非法迁移抛错）
  js/store.js           # IndexedDB（无则内存）会话/消息/设置/草稿/图片缓存存储
  js/diagnostics.js     # 脱敏日志环 + 延迟统计（p50/p95）+ 导出文本
  js/scheduler.js       # 真实/手动时钟（测试用虚拟时间）
  js/controller.js      # DOM 无关的编排：配对/认证/会话/聊天/图片/审批/追问/录音/重连
  js/i18n.js            # 中英词条、错误文案（含“下一步”）、相对时间
  js/app.js             # DOM 引导：外壳、路由、主题/语言、会话抽屉、事件绑定、自动启动
  js/views/*.js         # pairing / sessions / chat（顶栏三要素 + 附件）/ settings / diagnostics
  js/ui/*.js            # dom 工具、toast、sheet、审批、追问、录音浮层
  tests/*.test.js       # node --test
```

## 4. 传输与 Native 契约（ARCHITECTURE §3.2）

Web → Native：`window.DshNative.post(JSON.stringify({op, ...}))`
（`connect` / `pair` / `auth` / `send` / `startAudio` / `stopAudio` / `cancelAudio` /
`getToken` / `setToken` / `routeInfo` / `preferDevice` / `exportLogs` / `scanPairing` /
`pickImage` / `reconnect`）。`connect` 可带 `hosts:[...]`（host→hosts 每地址 3s 回退，D-031）。

Native → Web：`window.DshBridge.onEvent(jsonString)`，事件
`net.state` / `pair.result` / `auth.result` / `proto.message` / `audio.*` / `token` /
`logs.exported` / `error` / `scanPairing.result` / `image.picked` / `image.pickFailed`。

要点：

- 认证与重连由 native 负责：pair.ok 后 native 自动 `auth`，auth.ok 后 native 自动 `resync`；
  Web 只发业务消息（不带 `seq`），连接用 `connect{host,port,fp,deviceId,hosts?}`。
- 请求可用本地字段 `_reqId` 关联响应：native 发网前剥掉，命中 `replyTo` 时回填 `_reqTo`
  （`media.get` 同样用该机制关联 `media.data`）。
- `window.DshBridge.onBack()` 返回 `true` 表示 Web 消费了返回键（关弹窗/抽屉/返回上一屏）。
- 录音只走 `startAudio`/`stopAudio`/`cancelAudio`，PCM 帧由 native 直发，WebView 不接触 token 与音频字节。
- `scanPairing`：v1.1 真实现，回 `scanPairing.result{ok,host,port,fp,code,sid,hosts?,name?}`
  或 `{ok:false,reason}`；Web 填表并把 hosts 传给 native connect（D-030）。
- `pickImage{source:"camera"|"gallery"}`：native 缩放长边 ≤1600 / JPEG q0.8 / ≤2MiB 后回
  `image.picked{data,mimeType,name,width,height}`；发送时 `chat.send.images[]` ≤4 张/合计 ≤6MiB（D-029）。
- 多地址全失败：`net.state{state:"error",reason:"all_addresses_failed",tried:[{host,port,elapsedMs,error}]}`
  （D-034：是 `error` 不是 `failed`）；Web 按“已断开”渲染并在配对页/诊断页展示 tried 明细。

## 5. 约定

- 所有用户可见文案走 `js/i18n.js`（NFR-9），新增 key 中英都要有；`node --test` 会检查键集一致。
- 错误/关闭码文案必须包含「下一步怎么做」（`error.<code>.next` / `close.<code>.next`）。
- 触控目标 ≥ 48dp（`--touch`），深浅色跟随系统，减弱动效尊重 `prefers-reduced-motion`。
- 不引入 npm 依赖或构建步骤；改协议先改 `docs/PROTOCOL.md` 并同步 `js/protocol.js`（PROTOCOL §15）。
