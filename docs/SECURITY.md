# 安全设计（SECURITY）v1.0

| 项 | 值 |
|---|---|
| 适用 | V1.0，协议 v1.0 FROZEN；手机 App ⇄ 局域网 Bridge |
| 边界 | 不覆盖公网直连、云端 ASR、桌面端会话共享；局域网视为不可信网络 |
| 上游 | PRD §8/§11/§15/§17、PROTOCOL §1/§5/§6/§11/§12、ARCHITECTURE §3.4、DECISIONS D-004/D-008 |
| 验证 | `docs/TESTPLAN.md` §3.8；发布前必须跑完 §5 清单 |

## 1. 威胁模型（STRIDE）

| # | 威胁 | STRIDE | 场景 | 影响 | 控制 | 残余 |
|---|---|---|---|---|---|---|
| T1 | 局域网中间人 | S/T | 同 Wi-Fi ARP 欺骗、恶意热点 | 窃听/篡改语音与回复 | TLS1.2+ + 证书 SHA-256 pinning；明文 HTTP 不监听 | 二维码被拍照替换需物理接近 |
| T2 | 暴力破解配对码 | S | 6 位码 10^6，脚本高速尝试 | 未授权配对 | TTL 120s + 5 次锁 5 分钟 + 常量时间比较 | 可轮换 deviceId 绕过按设备计数 |
| T3 | token 泄露 | I | 备份、root、日志、截图 | 长期冒充设备 | 服务端只存 `sha256(token)`；客户端 Keystore；日志脱敏 | 无 root 检测；token 长期有效 |
| T4 | 重放认证帧 | S | 抓 `auth` 帧原样重放 | 冒充已配对设备 | ts ±120s + nonce 缓存 10 分钟 | 设备时钟漂移导致误拒 |
| T5 | 未授权设备直连 | S | 知道 host:port 直接连 | 读取会话/发指令 | `needPair` + 未认证 15s 上限 + 仅 4 类消息 | 配对码肩窥 |
| T6 | 日志泄露 | I | 日志/导出含 token、正文、PCM | 隐私泄露 | 白名单字段 + 导出二次脱敏 + 7 天保留 | 崩溃转储可能含内存明文 |
| T7 | 公网暴露误配置 | E | `host=0.0.0.0` 或路由器端口映射 | 公网可达 | 启动拒绝 0.0.0.0；默认 127.0.0.1；文档警告 | 用户手动 NAT 转发无法阻止 |
| T8 | 恶意 APK 覆盖安装 | T | 同包名不同签名/钓鱼包 | 窃取 token | Android 签名不同拒绝覆盖；Releases 附 SHA-256 | 用户允许未知来源 |
| T9 | 依赖供应链投毒 | T | PyPI/npm 包被替换 | 代码执行 | 版本固定 + lockfile + gitleaks + 最小依赖 | 无 SBOM/签名验证 |
| T10 | 审批社会工程 | S | 伪造/重复审批弹窗诱导点击 | 危险命令被执行 | `defaultOutcome=denied` + deadline + 未知 id 忽略 + 默认焦点拒绝 | 用户误点允许 |

## 2. 安全设计

| 控制 | 实现 | 参数/约束 | 验证 |
|---|---|---|---|
| 传输加密 | `ssl.SSLContext(PROTOCOL_TLS_SERVER)`，`minimum_version=TLSv1_2`；关闭 `permessage-deflate` 与重协商 | 仅 TLS1.2/1.3；自签 ECDSA P-256，有效期 825 天 | SEC-T-01 |
| 证书 pinning | `fp = sha256(cert DER)` 小写 hex；OkHttp 自定义 `X509TrustManager` 比对 `chain[0].encoded`，`MessageDigest.isEqual` 常量时间 | 首次由二维码/手动输入写入并锁定；不匹配即断，不发 `auth` | SEC-T-01、TC-AND-02 |
| 监听面 | `config.server.host` 白名单：`127.0.0.1`、具体网卡 IP、Tailscale `100.64.0.0/10` | 拒绝 `0.0.0.0`/`::`/空串；默认 `127.0.0.1:8765`；`maxConnections=4` | SEC-T-02 |
| 配对码 | `secrets.randbelow(10**6)` 补零 6 位；一次性；成功或锁定后立即作废 | TTL 120s；失败 5 次锁 5 分钟（close 4410），锁定期拒绝所有 `pair.request` | TC-PAIR-03/04 |
| 设备令牌 | `secrets.token_bytes(32)`；base64 传输；服务端存 `sha256(token).hexdigest()`，`hmac.compare_digest` 比较 | 客户端存 EncryptedSharedPreferences（AES256-GCM，Keystore 主密钥）；WebView 不可见 | TC-AUTH-04、TC-AND-10 |
| 认证/防重放 | canonical `DSPHONE1\n{deviceId}\n{ts}\n{nonce}`（UTF-8，无末尾换行）；HMAC-SHA256，key 为 token 原始 32B | ts ±120s；nonce 16B base64，LRU 缓存 10 分钟；失败统一 1001，不区分原因 | TC-AUTH-01~03 |
| 未认证连接 | 仅接受 `hello`、`pair.request`、`auth`、`ping`；15s 未认证关闭 | 其它消息 `error{1001}` + close 4401 | IT-BRIDGE-02 |
| 设备撤销 | 删除设备记录并关闭在线连接；App 解除配对照删 Keystore 项 | 在线 close 4401；后续 `auth` 1001 且 `needPair=true` | IT-BRIDGE-03 |
| 审批默认拒绝 | `defaultOutcome=denied`；`deadline=now+600`；过期发 `approval.expired{denied}`；未知/重复 id 忽略 | UI 默认焦点“拒绝”；不提供自动批准 API | IT-BRIDGE-10、SEC-T-05 |
| 日志脱敏 | JSON 行白名单：`ts,level,event,connId,sessionId,code,latency,textLen,textSha256_8,aseq`；`logging_setup.py` 过滤 + 导出再过滤 | 禁止 token/hmac/nonce 原值、正文、PCM；滚动 7 天 | SEC-T-04 |
| 音频边界 | 本地 ASR，不上云；默认不落盘；重试保留 ≤24h 且 0600，final 后删除 | 公网仅 TLS 承载 PCM，无应用层加密（见 R1） | SEC-T-04 |
| Android 备份 | `android:allowBackup="false"` + `dataExtractionRules` 排除 token/消息/日志 | 防 `adb backup` 带走 | TC-AND-10 |

原则：默认拒绝（未认证、未知消息、未知审批 id、非白名单绑定地址）；错误信息不区分“token 错/过期/重放”，统一 1001。

## 3. 密钥与凭据管理

| 凭据 | 生成/保管 | 存放 | 轮换/失效 |
|---|---|---|---|
| release keystore `.jks` | 用户本地 `keytool` 生成，离线保管 | 禁止入库（`.gitignore` `*.jks`）；CI secrets `ANDROID_KEYSTORE_BASE64` | 丢失则无法升级；泄露即作废并换签名 |
| keystore 口令/alias | 用户密码管理器 | GitHub Environment secret，发布 job 需 reviewer | 每年或人员变动 |
| deviceToken | Bridge 生成 | 服务端仅 sha256；客户端 Keystore | 撤销即失效；重新配对生成新 token |
| Bridge TLS 私钥 | 首次启动生成 | `bridge/state/certs/bridge.key`（0600），目录已被忽略 | 重装需重新配对；泄露立即重配 |
| CI token | GitHub 托管 | repo/environment secrets | 90 天 |
| 模型文件 | 公开下载，无凭据 | `models/`（不入库） | 按版本重新下载 |

禁止提交：`.env*`、`*.jks`、`*.keystore`、`*.pem`、`*.key`、`*.p12`、`keystore.properties`、`bridge/state/`、`models/`。`.gitignore` 已覆盖大部分，`*.p12`/`*.bks` 需补；预提交 `gitleaks protect --staged`。

泄露处置：① `devices revoke` 撤销全部设备；② 删 `bridge/state/certs` 重新生成并重配；③ release key 泄露则公告、换签名，旧包不可撤回；④ 轮换 CI secrets；⑤ 审计最近 7 天日志。

## 4. 隐私

| 数据 | 是否采集 | 存放 | 保留 | 导出 |
|---|---|---|---|---|
| 语音 PCM | 仅录音期间内存 | 内存（重试模式落盘） | 结束即弃；重试 ≤24h | 否 |
| 识别/消息文本 | 是 | 手机 IndexedDB；桥 `history.sqlite3` | 用户删除或卸载 | 仅长度与 hash 前缀 |
| deviceToken | 是 | 服务端 sha256；客户端 Keystore | 至撤销 | 否 |
| deviceId/设备名 | 是 | 服务端 devices 表 | 至撤销 | deviceId 前 8 位 |
| 日志 | 元数据 | 手机文件；`bridge/state/logs` | 7 天滚动 | 脱敏后 |
| 延迟统计 | 是 | 本地 | 7 天 | 数值 |
| 崩溃 | 否（不上传） | — | — | — |

不采集：通讯录、定位、短信、其他 App 列表、剪贴板、输入法内容。ASR 全本地，无遥测，唯一出网是模型下载。

卸载：Android 卸载清除 App 数据与 Keystore 别名；Bridge 侧执行 `Remove-Item -Recurse -Force bridge\state` 清除历史、token 哈希与证书。导出前弹确认并列出字段清单，不自动上传。

## 5. 安全测试清单（可执行）

| # | 检查 | 命令/操作 | 预期 | 用例 |
|---|---|---|---|---|
| 1 | 指纹不匹配必须拒绝 | 把 App 记录的 fp 改 1 位 hex 后连接（或跑 `pytest bridge/tests/test_server.py -k fingerprint_mismatch`） | TLS 校验失败，UI 报“证书指纹不匹配”，不发 `auth`，日志 `event=tls.pin_mismatch` | SEC-T-01 |
| 2 | 错误 token | `pytest bridge/tests/test_auth.py -k wrong_token` | `auth.error{code:1001,needPair:true}` + close 4401 | SEC-T-03 |
| 3 | 重放 nonce | `pytest bridge/tests/test_auth.py -k replay` | 第二次同 nonce 得 1001 + close 4401 | SEC-T-03 |
| 4 | 0.0.0.0 必须启动失败 | `conda run -n scisim-agent python -m bridge --config bridge/tests/fixtures/config_bind_all.toml` | 退出码 2，stderr 含 `server.host must not be 0.0.0.0` | SEC-T-02 |
| 5 | 日志无 token/音频 | 注入 canary 后 `rg -n "ABEiM0RV\|CANARY_(TOKEN\|AUDIO\|TEXT)" bridge/state/logs` | 0 命中（事件名除外） | SEC-T-04 |
| 6 | 审批超时默认拒绝 | `deadline=now+5` 的 `approval.request`，不响应等待 | `approval.expired{outcome:"denied"}`，SDK 收到 denied | SEC-T-05 |
| 7 | 撤销设备即时断连 | 设备在线时 `python -m bridge devices revoke <deviceId>` | 5s 内 close 4401；重连 `auth` 得 1001 且 `needPair=true` | IT-BRIDGE-03 |

## 6. 残留风险与 V1 接受度

| # | 风险 | V1 接受度 | 缓解 | V1.1 计划 |
|---|---|---|---|---|
| R1 | 局域网 PCM 仅 TLS 保护，无应用层加密 | 接受 | pinning + 禁降级 + 默认回环；不鼓励公网 | Opus 压缩 + 可选 AES-GCM 帧（类型 0x03） |
| R2 | 无设备公私钥，token 泄露即可完全冒充 | 接受（D-008） | 32B 高熵、可撤销、单设备绑定、失败告警 | 每设备 EC P-256 挑战签名 |
| R3 | 限速只按 deviceId，轮换 id 可绕过 | 有条件接受 | 全局失败计数；锁定期拒绝所有 `pair.request` | 按源 IP 令牌桶 + 指数退避 |
| R4 | 自签证书无吊销/在线轮换 | 接受 | 重配即换 fp；证书 825 天 | 双指纹过渡窗口 |
| R5 | 侧载 APK 无商店签名校验 | 接受（D-004） | Releases 附 SHA-256 与版本号；禁自动更新 | 应用内更新验签 |
| R6 | 无 SBOM、无依赖签名验证 | 接受 | 固定版本 + hash + gitleaks + Dependabot | pip-audit + CycloneDX SBOM |
| R7 | 崩溃转储可能含内存明文 | 接受 | 不集成崩溃上报；日志脱敏 | 本地 crash 文件脱敏后再导出 |
| R8 | 已 root/物理接触设备 | 接受 | Keystore 硬件保护；音频不落盘 | V2 评估 root 检测 |

上述风险在 M4 安全评审逐条确认；新增风险必须同时给出缓解与 V1.1 条目，否则不发布。
