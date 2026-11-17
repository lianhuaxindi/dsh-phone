# DSH SDK runtime 说明（D-021 / D-022）

Bridge 在 `[dsh].enabled = true` 时通过 Python SDK 拉起 DeepSeek Harness runtime。
runtime **不是**由 Bridge 内置的 Node 进程，而是一个独立的 CLI 可执行文件
（`dsh.cmd` / `dsh.exe`），由 SDK 以子进程方式启动。

## 解析顺序（D-022）

`bridge/runtime.py` 按以下顺序解析，每一步都会写日志
（`dsh.runtime.candidate`，字段：`source` / `path` / `exists` / `detail`）：

| 优先级 | 来源 | 说明 |
| --- | --- | --- |
| 1 | 环境变量 `DSH_PHONE_RUNTIME_EXE` | CI / 冒烟测试用，覆盖配置 |
| 2 | `[dsh].runtimeExe`（`bridge/config.toml`） | 显式路径，最高配置优先级 |
| 3 | 本机已安装 DSH | `%PATH%` 中的 `dsh.cmd` / `dsh.exe`，加上候选列表（默认安装位置；可用 `DSH_PHONE_RUNTIME_CANDIDATES` 以 `;` 分隔追加） |
| 4 | SDK wheel 内置 runtime | `deepseek_harness_runtime` 的 bundled exe |

- **显式来源（1/2）指向不存在的文件时不会静默回退**，而是直接报错，避免配置写错却悄悄用了别的 runtime。
- 全部候选都不可用时抛出 `RuntimeUnavailable`，错误文案包含每个候选的探测结果
  （路径 + 是否存在 + 失败原因）以及下一步操作建议。
- 选中来源为 `bundled` 时，`dsh_bin` 交给 SDK 自己按 wheel 内路径解析；其余来源
  以绝对路径传给 SDK `HarnessConfig.dsh_bin`。
- `[dsh].home` 必须显式（默认 `bridge/state/dsh_home`），**不会**回落到桌面
  `~/.dsh`（D-021）。

## 本机实测（2026-11-17，Windows 11 / Python 3.11.17）

用 `SdkDshClient` 做 `start → initialize → create_session → close`，**不调用
`run()` / 不发 prompt / 不消耗 token**：

| runtime 来源 | 结果 | 关键数据 |
| --- | --- | --- |
| SDK wheel 内置 exe（`runtimeExe=""`） | **失败** | `TransportClosedError: DeepSeek Harness runtime stdout closed`；runtime stderr：`Error: dsh: host preparation failed: No usable native binding found for node-addon-require-builtin-win32-x64-msvc (auto)` |
| 已安装 DSH runtime（`D:\others\DSH\resources\runtime\cli\bin\dsh.cmd`） | **成功** | start/initialize 冷启动 **5496.9 ms**；`sdkSessionId=session-12008033c3074c42aabbfc374f30c448`；close **110.8 ms**；close 后无 `*runtime*` 残留进程 |

结论：本机直接使用 wheel 内置 runtime 不可用（runtime 打包 / 原生绑定环境问题，
与 Bridge 无关）；把 `[dsh].runtimeExe` 指向已安装 DSH 的 `dsh.cmd` 即可正常拉起。
M1 阶段 `[dsh].enabled=false` 仍走 `FakeDsh`，不依赖 runtime。

DSH_HOME 使用独立的 `bridge/state/dsh_home`（已在 `.gitignore` 中忽略）；
实测确认运行时只写该目录，**不会**触碰桌面 `~/.dsh`。

## 自检命令

```powershell
# 无副作用：配置/TLS/ASR/runtime 解析与 --version/日志脱敏
D:\anaconda3\envs\scisim-agent\python.exe -m bridge --self-test

# 额外做一次真实 SDK start/initialize/close（冷启动约 5s，不发 prompt）
D:\anaconda3\envs\scisim-agent\python.exe -m bridge --self-test --smoke
```

输出为人类可读摘要 + 一行 `SELF_TEST_JSON {...}`（便于 CI 解析）。
退出码：`0` = 无致命失败；`2` = 有致命失败（配置/监听地址、TLS、日志脱敏；
`[dsh].enabled=true` 时 runtime 解析失败也算致命）。

## 配置示例

```toml
[dsh]
enabled = false                 # M1 阶段：FakeDsh，不依赖 runtime
home = "state/dsh_home"         # 独立 DSH_HOME，禁止 ~/.dsh
runtimeExe = ""                 # 空 = 自动解析（依次尝试已安装 DSH → wheel 内置）
# 指向已安装 DSH（本机实测可用）：
# runtimeExe = "D:/others/DSH/resources/runtime/cli/bin/dsh.cmd"
```

## 故障排查

- **`No usable native binding found for node-addon-require-builtin-win32-x64-msvc`**：
  wheel 内置 runtime 在本机无法启动，改用已安装 DSH 的 `dsh.cmd`（见上表）。
- **`无法找到可用的 DSH SDK runtime`**：看错误里每个候选的探测结果；安装 DSH 桌面版，
  或把 `[dsh].runtimeExe` 指向 `dsh.cmd`；也可设置 `DSH_PHONE_RUNTIME_EXE` 临时覆盖。
- **`SDK 未安装`**：`python -m pip install -r bridge/requirements-sdk.txt --pre`；
  开发期可设置 `DSH_PHONE_SDK_PATH` 指向已解压的 SDK 目录（如 `scripts/spike/.vendor`）。
- **SDK 启动超时 / 无响应**：确认 `dsh.cmd --version` 可直接运行；检查
  `bridge/state/dsh_home` 是否可写。
