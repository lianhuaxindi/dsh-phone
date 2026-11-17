# dsh-phone M0 · Python SDK Spike 报告

结论全部来自**实际安装的包**，不是文档推测。文中 `api.py:NNN` 均指
`scripts\spike\.vendor\deepseek_harness\api.py`（client/models 同理）。

复现（不调模型、不需凭据、exit 0）：

```powershell
& 'D:\anaconda3\envs\scisim-agent\python.exe' 'scripts\spike\spike_sdk.py' --log 'scripts\spike\.cache\spike_sdk.out.txt'
```

## 0. 结论速览

| # | 能力 | 结论 |
|---|---|---|
| a | 发消息 | ✅ `DeepSeekHarness.run()` / `Session.run()`，**同步阻塞**，返回 `RunResult` |
| b | 流式 | ⚠️ 只有**事件级**回调 `on_notification`；**没有 token 级实时增量** |
| c | 会话 | ⚠️ 有 `start_session` / `resume_session`；**无会话列表、无历史读取** |
| d | 审批 | ✅ 可用；请求只有 `sessionId/toolName/callId/reason`，**模型里没有 options** |
| e | 提问 | ✅ 可用；handler 收 `(request, cancelled_event)`，可回 `{"cancelled": true}` |
| f | 取消 | ❌ **SDK 与协议层都缺失**，只能 `close()` 杀进程 |
| g | provider/model | ✅ 构造参数传入；CLI 用 wheel 内置 exe，**不查 PATH**，且必须有 `DSH_HOME` |
| h | 兜底 | `HarnessClient.request(method, params, response_model=...)` 可发任意协议方法 |

## 1. 环境与安装

- Python `3.11.17`，conda 环境 `scisim-agent`（`D:\anaconda3\envs\scisim-agent\python.exe`）。
- 运行期依赖 `pydantic 2.14.1` **已满足** `pydantic>=2.12,<3`，无需升级；未改动 conda 内任何包。

| 包 | 版本 | 说明 |
|---|---|---|
| `deepseek-harness-sdk` | `0.2.2rc2` | `py3-none-any`，sha256 `ad14f1b1…a623d31899` |
| `deepseek-harness-runtime-bin` | `0.2.2rc2` | `py3-none-win_amd64`，sha256 `a791af95…ee33e9aea` |
| 内置 runtime | `0.2.2-rc.2` | `…-win-x64.exe`，147,907,584 B，`--version` rc=0 |

**为什么不是 pip 安装**：本机 `pip install` 走 resolver 会无输出挂起（>10 min）；且本会话沙箱
禁止写 conda env（`PermissionError: [Errno 13]`）与 user site（`[WinError 5]`）。
故改为下载 PyPI wheel 后按 zip 解压到 `scripts\spike\.vendor\`，由脚本把该目录注入 `sys.path`
（wheel 解压布局 = 安装布局）。正式部署请用 `bridge\requirements-sdk.txt`。

安装位置：`scripts\spike\.vendor\`（`deepseek_harness\`、`deepseek_harness_runtime\` + 两个 dist-info）。
`.cache\`/`.vendor\`/`.tmp\` 是构建产物，不应进版本库。

## 2. 导出面（`__init__.py:1-28`，`__all__` 共 18 项）

`DeepSeekHarness, DeepSeekHarnessConfig, Session, RunResult, HarnessClient, HarnessConfig,
UserQuestionHandler, UserQuestionRequest, UserQuestionAnswer, UserQuestionAnswerItem,
SdkProtocolError, ApprovalRequestParams, ApprovalRequestResult, IncomingRequest,
InitializeResponse, JsonObject, Notification, ServerInfo`

`office.py` 存在（`render_images:155` / `convert:187` / `recalculate:206`，跑 runtime CLI，不建会话）
但**未导出**，用 `from deepseek_harness import office`。

## 3. 逐能力结论

| 能力 | 真实 API | 真实签名（证据） | 结论 | 风险 |
|---|---|---|---|---|
| a 发消息 | `DeepSeekHarness.run` | `run(self, input: str \| list[JsonObject], *, session_id: str \| None = None, on_notification: Callable[[Notification], None] \| None = None) -> RunResult`（api.py:188-196） | 同步阻塞直到 turn 结束 | 长 turn 不可中断（见 f），UI 必须放线程 |
| a 返回值 | `RunResult` | `RunResult(session_id, final_response, finish_reason, events: list[JsonObject], notifications: list[Notification])`（api.py:44-53） | `final_response` 由 `assistant/message` 的 text 块拼接（api.py:298-315）；`finish_reason`=`turn/end.data.reason.kind`（api.py:318-335） | 无 text 块时 `final_response` 为空串 |
| b 流式 | `on_notification` | `Notification(method: str, payload: JsonObject)`（models.py:13-17） | **事件级**，不是 token 级 | 见 §6.1 |
| c 会话-建 | `start_session` | `start_session(self, session_id: str \| None = None) -> Session`（api.py:175-177） | **不发 RPC**，仅 `session-{uuid4().hex}` 或原样用传入 id | id 合法性由运行时校验，SDK 不校验 |
| c 会话-恢复 | `resume_session` | `resume_session(self, session_id: str) -> Session`（api.py:179-186 → client.py:220-226 `session/resume`） | 复用已有会话 | 不存在/不可读/不兼容会抛异常，**不会新建替代会话** |
| c 列表/历史 | —— | 无 | ❌ 缺失 | Bridge 必须自建索引（见 §6.4） |
| d 审批-开 | `approval_requests` | `DeepSeekHarnessConfig.approval_requests: bool = False`（api.py:41）；`initialize(..., approval_requests=False)` → `"approvalRequests": True`（client.py:151-173） | 开开关即可 | 开了不 drain 会把 turn 挂死（client.py:161-166） |
| d 审批-收 | `next_request` | `next_request(self) -> IncomingRequest`（client.py:310-314，阻塞 `queue.get`）；`IncomingRequest(id: str \| int, method: str, payload: JsonObject)`（models.py:19-23），`method="approval/request"` | 同步阻塞取队列 | 单 reader 模型，必须与 `session_prompt` 等待循环并发 |
| d 审批-回 | `respond` | `respond(self, request_id: str \| int, result: JsonValue) -> None`（client.py:316-317）；`respond_error(request_id, *, code: int, message: str, data: JsonValue \| None = None)`（client.py:319-330） | `result` 直接用 `ApprovalRequestResult.model_dump(by_alias=True)` | 回错 id 静默丢弃 |
| e 提问 | `on_user_question` | `UserQuestionHandler = Callable[[UserQuestionRequest, threading.Event], UserQuestionAnswer \| JsonObject]`（client.py:22）；`HarnessConfig.on_user_question`（client.py:39） | handler 在 **daemon 线程**执行，`Event` 是取消信号 | handler 异常回 `-32603`；重复 requestId 回 `-32602` |
| f 取消 | —— | 无 | ❌ 缺失 | 见 §6.3 |
| g 模型 | `provider`/`model` | `DeepSeekHarnessConfig.provider="deepseek-official"`, `model="deepseek-v4-flash"`, `reasoning_effort=None`, `max_tokens=None`, `base_url`, `api_key`（api.py:14-41） | 经 `initialize` payload 传给运行时 | 无运行时改模型 API；换模型需新会话/重启（未实测） |

`Session` 上还有：`run(...)`（api.py:214-234）、`get_working_directory()`/`set_working_directory(path)`
（api.py:204-212 → `session/working-directory/get|set`）。

## 4. 事件与请求对象字段清单

### 4.1 通知信封（SDK 层，`models.py`）

- `Notification(method: str, payload: JsonObject)`；`method` 取值仅：
  `session.event`、`session.status`（`{"sessionId", "status": "running"|"idle"}`）、
  `session.query.completed`、`subagent.started`、`subagent.finished`、`user-question.cancelled`。
- `IncomingRequest(id: str|int, method: str, payload: JsonObject)`；`method`：
  `approval/request`、`user-question/ask`。

### 4.2 会话事件（`session.event` 的 `payload.event`，SDK 不建模、纯透传）

`{type, seq, time, data}`，`type` 全集（据协议/会话词汇表）：
`turn/start`、`turn/end{reason:{kind}}`、`step/start`、`step/end`、`user/message`、
`assistant/message{turn,step,message,stream[],usage?,interrupted?}`、`assistant/attempt`、
`tool/call{turn,step,callId,name,arguments}`、`tool/result{turn,step,message,error?{name,code,reason?},meta?}`、
`developer/message`、`system/message`、`request/header`、`request/context`、`session/end-seed`。
**没有任何 `assistant/delta` 之类的事件类型。**

### 4.3 审批 / 提问对象

```
ApprovalRequestParams  {sessionId: str, toolName: str, callId: str|None, reason: str|None}
ApprovalRequestResult  {outcome: "allowed-once" | "rejected" | "cancelled"}   # 唯一批准值 allowed-once
UserQuestionRequest    {requestId: str, sessionId: str, questions: [{id, question, options?, ...}]}  # min_length=1
UserQuestionAnswer     {answers: [{id: str, selected: [str], custom: str|None}]}
取消提问               裸 dict {"cancelled": true}（模型里没有 cancelled 字段）
```

离线构造实测（spike 输出）：

```
ApprovalRequestParams -> {"sessionId": "session-demo", "toolName": "shell", "callId": "call-1", "reason": "write outside workspace"}
IncomingRequest      -> {"id": 3, "method": "approval/request", "payload": {...同上...}}
UserQuestionRequest  -> {"requestId": "q-1", "sessionId": "session-demo", "questions": [{"id": "scope", "question": "要改哪些文件？", "options": [{"label": "仅 docs"}, {"label": "全部"}]}]}
UserQuestionAnswer   -> {"answers": [{"id": "scope", "selected": ["仅 docs"]}, {"id": "note", "selected": [], "custom": "先给计划"}]}
```

## 5. `dsh` CLI 发现机制（`client.py:588-616`）

1. `dsh_bin` 为空 → `deepseek_harness_runtime.resolve_bundled_launch_args()`，用 wheel 内置
   `runtime\deepseek-harness-sdk-runtime-win-x64.exe`（要求 `-rg.exe` sidecar 存在）；否则用
   `(Path(dsh_bin).expanduser().resolve(),)`。**从不搜索 PATH。**
2. `dsh_home` 非空 → 注入 `env["DSH_HOME"]`；否则要求环境变量 `DSH_HOME` 非空，否则：
   `ValueError: HarnessConfig requires an explicit dsh_home or non-empty DSH_HOME; the Python SDK never uses ~/.dsh implicitly`
3. 参数拼接 `(*base, "--profile", profile, *(--patch, p)…)`，`profile` 默认 `"sdk"`。

本机实测：`shutil.which("dsh")` → `None`（不在 PATH）；`D:\others\DSH\resources\runtime\cli\bin\dsh.cmd`
存在（桌面版启动器），传 `dsh_bin=` 后 argv 正确变成
`('D:\others\DSH\resources\runtime\cli\bin\dsh.cmd', '--profile', 'sdk')`；当前 shell 已有
`DSH_HOME=C:\Users\15653\.dsh`，故默认可直接启动。`DSH_RUNTIME_MODE` 未设（`exe`|`node`，node 模式需系统 node≥24.9）。

## 6. 对 PROTOCOL v1 的直接影响

### 6.1 `assistant.delta` 不存在 → 必须改设计

SDK 没有 token 级增量；文本在 `assistant/message` 事件里**整段**到达（每个 step 一条）。
`assistant/message.data.stream` 是结算时附带的 `AssistantStreamRecord[]` 定时记录，**事后可回放、
不能实时**，且 SDK 不建模其字段（纯 JSON 透传，本报告不猜其结构）。
→ v1 方案：Bridge 只发 `assistant.message`（整段）+ `turn.end`；UI 打字机效果由客户端本地按字符
节奏播放（可标注为"渲染效果"）。**不要伪造 `assistant.delta` 声称来自模型。**

### 6.2 审批字段映射

`approval/request` → `{id: IncomingRequest.id, tool: payload.toolName, reason: payload.reason,
callId: payload.callId, sessionId: payload.sessionId}`；`respond(id, {"outcome": "allowed-once"|"rejected"|"cancelled"})`。
**注意**：SDK 的 pydantic 模型只声明 4 个字段，但 `IncomingRequest.payload` 是原始 `JsonObject`，
未建模键是否出现**未实测**（见 §8.2）；v1 侧字段应做"可选 + 透传未知键"处理。

### 6.3 cancel 不可用

SDK 无 cancel/interrupt/stop；协议 README 明确"无取消与会话关闭方法——客户端放弃轮次的方式是关闭
运行时进程"。唯一手段 `HarnessClient.close()`（`client.py:103-149`：发 `shutdown` 后 terminate/kill）。
→ v1 若保留 `session.cancel`，语义只能是"终止该会话进程"，UI 文案必须如实；桌面版另有 Host API
`@Remote('cancel')`，但不经 Python SDK 暴露。

### 6.4 会话列表 / 历史

SDK 无 list/history；`session/resume` 只恢复持久化历史，**旧事件不重放**（通知只报新提交事件）。
→ Bridge 必须自己持久化事件流（订阅侧落盘）并维护 sessionId→元数据索引；`DSH_HOME` 内的会话存储
布局未验证，不要依赖它。

### 6.5 其它

- `session/wait` 协议里有、SDK 未封装 → 需要时用 `client.request("session/wait", …)`。
- `session/query` 是唯一 prompt 入口（`_query`，client.py:228-241），字符串会包成
  `[{"type": "text", "text": …}]`，支持 native slash command，返回 `str`（messageId）或 `CommandExecution`。

## 7. 未解决问题清单

1. `AssistantStreamRecord` 字段结构未取证（SDK 不建模；GitHub API 限流）。v1 不要依赖它。
2. 审批 payload 是否有未建模的额外键（如权限范围/options）——需真机实测抓 `IncomingRequest.payload`。
3. 无凭据，未做真实端到端（`initialize`/`session/prompt`/审批全链路）；首次真跑放在 Bridge 阶段。
4. `dsh --profile sdk` 与内置 exe 是否等价（profile 注入的 patch 集合）未比较。
5. 运行时冷启动耗时、常驻内存未测（本 spike 只跑 `--version`）。
6. `resume_session` 的"不可读/不兼容/被占用"具体错误码未枚举。
7. `DSH_HOME` 目录布局与历史存储格式未验证（若要自建列表/历史需要它）。

## 8. 附录

### 8.1 spike 关键输出（节选，完整见 `scripts\spike\.cache\spike_sdk.out.txt`）

```
== 1. package metadata / versions
  deepseek-harness-sdk version       0.2.2rc2
  deepseek-harness-sdk location      D:\code\apps\手机端\scripts\spike\.vendor
  deepseek-harness-sdk requires      deepseek-harness-runtime-bin==0.2.2rc2, pydantic<3,>=2.12
  pydantic (runtime)                 2.14.1
== 2. import + exported surface
  __all__ count                      18          __all__ all resolvable  yes
== 9. capability matrix
  -- DeepSeekHarness   cancel MISSING / list_sessions MISSING / read_history MISSING /
                       model_selection MISSING / token_stream MISSING / session_wait MISSING
== 10. dsh CLI discovery logic
  shutil.which('dsh')                NOT FOUND on PATH
  resolve_bundled_launch_args()      ('…\.vendor\deepseek_harness_runtime\runtime\deepseek-harness-sdk-runtime-win-x64.exe',)
  launch args w/o DSH_HOME           ValueError: HarnessConfig requires an explicit dsh_home or non-empty DSH_HOME; …
  launch args with DSH_HOME          [exe, '--profile', 'sdk']
  dsh_bin override argv              ('D:\others\DSH\resources\runtime\cli\bin\dsh.cmd', '--profile', 'sdk')
== 11. runtime health check (no model call)
  runtime --version rc 0 / out 0.2.2-rc.2
[OK] spike completed: 26 sections, exit 0
```

### 8.2 脚本做了什么 / 没做什么

做了：`sys.path` 引导 vendor → 导入 → 打印 `__all__`、`inspect.signature`、dataclass/pydantic 字段
（含 alias）→ 扫描已装源码里的 JSON-RPC 方法名 → 离线构造审批/提问/通知对象并 `model_dump(by_alias=True)`
→ 能力矩阵 → CLI 发现逻辑（含强制移除 `DSH_HOME` 的反例）→ `runtime --version` 健康检查 → JSON 汇总。
没做：任何 `start()`/`initialize()`/`session/prompt`，即**不启动运行时、不调模型、不需要 key**。
`--no-probe` 可跳过 `--version` 子进程（纯静态）。
