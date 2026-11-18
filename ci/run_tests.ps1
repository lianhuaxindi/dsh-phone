<#
.SYNOPSIS
  本地跑 CI 能跑的部分：CI 配置自检 + bridge pytest + web node --test + sync_web 一致性检查
  + Android 4 个纯 stdlib 离线自检（app/android/tools/*.py）
  + 端到端模拟（scripts/e2e_phone_sim.py --asr fake，默认 SKIP，加 -WithE2E 才跑）。
  本机自建 Android 工具链出 APK 的路径见 DECISIONS D-027 与 app/android/BUILD-LOCAL.md；
  GitHub Actions 恢复（D-026 billing lock 解除）前的本地替代流程见 ci/README.md。

.DESCRIPTION
  一次运行全部检查，最后给出汇总与退出码：
    0 = 全部通过（或 -AllowMissing 下只有“未就绪模块”被跳过）
    1 = 有检查失败
    2 = 环境问题（找不到 Python 等）

  模块未就绪（其他 teammate 尚未产出文件）时默认算失败；加 -AllowMissing 则降级为 SKIP，
  可以先跑已就绪的部分。

  每个步骤都会打印 PASS/FAIL/SKIP 与耗时；只有 FAIL 会改变退出码（有 FAIL 时 exit 1）。

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File ci\run_tests.ps1
  powershell -NoProfile -ExecutionPolicy Bypass -File ci\run_tests.ps1 -AllowMissing
  powershell -NoProfile -ExecutionPolicy Bypass -File ci\run_tests.ps1 -AllowMissing -WithE2E
#>
[CmdletBinding()]
param(
    [switch]$AllowMissing,
    [switch]$WithE2E
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$ScriptRoot = $PSScriptRoot
$RepoRoot = Split-Path -Parent $ScriptRoot

$script:Failures = @()
$script:Skips = @()

$script:StepStart = $null

function Format-Elapsed {
    if ($null -eq $script:StepStart) { return 'n/a' }
    $elapsed = (Get-Date) - $script:StepStart
    if ($elapsed.TotalSeconds -ge 60) { return ('{0:N1} min' -f $elapsed.TotalMinutes) }
    return ('{0:N1}s' -f $elapsed.TotalSeconds)
}
function Write-Step([string]$Message) {
    Write-Host ""
    Write-Host "=== $Message ===" -ForegroundColor Cyan
    $script:StepStart = Get-Date
}
function Write-Pass([string]$Message) {
    Write-Host "[run-tests][PASS] $Message（耗时 $(Format-Elapsed)）" -ForegroundColor Green
}
function Write-Fail([string]$Message) {
    Write-Host "[run-tests][FAIL] $Message（耗时 $(Format-Elapsed)）" -ForegroundColor Red
    $script:Failures += $Message
}
function Write-Skip([string]$Message) {
    Write-Host "[run-tests][SKIP] $Message（耗时 $(Format-Elapsed)）" -ForegroundColor Yellow
    $script:Skips += $Message
}

function Resolve-PythonExecutable {
    $candidates = @()
    if ($env:DSPHONE_PYTHON) { $candidates += $env:DSPHONE_PYTHON }
    $candidates += 'D:\anaconda3\envs\scisim-agent\python.exe'
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { $candidates += $cmd.Source }
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) { return $candidate }
    }
    return $null
}

function Resolve-NodeExecutable {
    $candidates = @()
    if ($env:DSPHONE_NODE) { $candidates += $env:DSPHONE_NODE }
    $candidates += 'D:\others\nodejs\node.exe'
    $cmd = Get-Command node -ErrorAction SilentlyContinue
    if ($cmd) { $candidates += $cmd.Source }
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) { return $candidate }
    }
    return $null
}

Write-Host "[run-tests] 仓库: $RepoRoot"
Write-Host "[run-tests] 模式: AllowMissing=$AllowMissing（模块未就绪时是否降级为 SKIP），WithE2E=$WithE2E（是否跑端到端模拟）"

$Python = Resolve-PythonExecutable
if (-not $Python) {
    Write-Host "[run-tests][ERROR] 找不到 Python。请设置 DSPHONE_PYTHON 或安装 Python 3.9+。" -ForegroundColor Red
    exit 2
}
$Node = Resolve-NodeExecutable
Write-Host "[run-tests] Python: $Python"
if ($Node) { Write-Host "[run-tests] Node:   $Node" } else { Write-Host "[run-tests] Node:   <未找到>" }

# ---------------------------------------------------------------------------
# 1/5 CI 配置自检（.github/workflows/ci.yml + ci/version.json + .gitignore）
# ---------------------------------------------------------------------------
Write-Step "1/6 CI 配置自检（ci/check_workflow.py）"
$checkWorkflow = Join-Path $ScriptRoot 'check_workflow.py'
if (-not (Test-Path -LiteralPath $checkWorkflow)) {
    Write-Fail "缺少 ci/check_workflow.py"
}
else {
    & $Python $checkWorkflow
    $code = $LASTEXITCODE
    if ($code -eq 2) {
        Write-Fail "缺少 Python 依赖 pyyaml：$Python -m pip install pyyaml"
    }
    elseif ($code -ne 0) {
        Write-Fail "CI 配置自检失败（exit $code），先修 .github/workflows/ci.yml / ci/version.json / .gitignore"
    }
    else {
        Write-Pass "CI 配置自检通过"
    }
}

# ---------------------------------------------------------------------------
# 2/5 bridge 测试（pytest bridge/tests，对应 CI bridge-tests job）
# ---------------------------------------------------------------------------
Write-Step "2/6 bridge 测试（pytest bridge/tests）"
$bridgeRequirements = Join-Path $RepoRoot 'bridge\requirements.txt'
$bridgeTestsDir = Join-Path $RepoRoot 'bridge\tests'
if (-not (Test-Path -LiteralPath $bridgeRequirements) -or -not (Test-Path -LiteralPath $bridgeTestsDir)) {
    $message = "bridge 模块未就绪（缺少 bridge/requirements.txt 或 bridge/tests/，bridge-core 负责补齐）"
    if ($AllowMissing) { Write-Skip $message } else { Write-Fail "$message；补齐后重跑，或加 -AllowMissing 只跑已就绪部分" }
}
else {
    $pytestVersion = & $Python -c "import pytest; print(pytest.__version__)" 2>&1
    if ($LASTEXITCODE -ne 0) {
        $message = "pytest 未安装：$Python -m pip install -r bridge\requirements-dev.txt（或 python -m pip install pytest pytest-asyncio）"
        if ($AllowMissing) { Write-Skip $message } else { Write-Fail $message }
    }
    else {
        Write-Host "[run-tests] pytest $pytestVersion"
        Push-Location -LiteralPath $RepoRoot
        try {
            & $Python -m pytest 'bridge\tests' -q -p no:cacheprovider
            $code = $LASTEXITCODE
        }
        finally {
            Pop-Location
        }
        if ($code -ne 0) { Write-Fail "bridge 测试失败（exit $code）" } else { Write-Pass "bridge 测试通过" }
    }
}

# ---------------------------------------------------------------------------
# 3/5 web 测试（node --test app/web/tests，对应 CI web-tests job）
# ---------------------------------------------------------------------------
Write-Step "3/6 web 测试（node --test app/web/tests）"
$webIndex = Join-Path $RepoRoot 'app\web\index.html'
$webTestsDir = Join-Path $RepoRoot 'app\web\tests'
if (-not (Test-Path -LiteralPath $webIndex) -or -not (Test-Path -LiteralPath $webTestsDir)) {
    $message = "web 模块未就绪（缺少 app/web/index.html 或 app/web/tests/，web-ui 负责补齐）"
    if ($AllowMissing) { Write-Skip $message } else { Write-Fail "$message；补齐后重跑，或加 -AllowMissing 只跑已就绪部分" }
}
elseif (-not $Node) {
    Write-Fail "找不到 Node.js（node --test 需要 Node 18+）：设置 DSPHONE_NODE 或把 node 加入 PATH"
}
else {
    Push-Location -LiteralPath $RepoRoot
    try {
        $webOutput = & $Node --test 'app/web/tests' 2>&1 | Out-String -Width 200
        $code = $LASTEXITCODE
        if ($webOutput) { Write-Host $webOutput.TrimEnd() }
        # 本机 DSH 沙箱禁止子进程/命名管道：node --test runner 需要 spawn 子进程，会 EPERM。
        # 此时退回逐文件直接执行（同一套 node:test 断言，in-process），保证本地仍能验证 web 测试。
        if ($code -ne 0 -and $webOutput -match 'spawn (EPERM|EACCES)') {
            Write-Host "[run-tests] node --test 无法 spawn 子进程（本机沙箱限制），改为逐文件直接执行（每文件 120s 超时）…"
            $directFailures = 0
            $testFiles = Get-ChildItem -LiteralPath $webTestsDir -Recurse -File -Filter '*.test.js' | Sort-Object FullName
            foreach ($testFile in $testFiles) {
                $rel = $testFile.FullName.Substring($RepoRoot.Length + 1)
                Write-Host "[run-tests] node $rel"
                # 用 .NET Process 而非 Start-Process：本机 PS5.1 的 Start-Process -PassThru 取不到 ExitCode。
                $psi = New-Object System.Diagnostics.ProcessStartInfo
                $psi.FileName = $Node
                $psi.Arguments = '"' + $testFile.FullName + '"'
                $psi.WorkingDirectory = $RepoRoot
                $psi.UseShellExecute = $false
                $psi.CreateNoWindow = $true
                $psi.RedirectStandardOutput = $true
                $psi.RedirectStandardError = $true
                $proc = New-Object System.Diagnostics.Process
                $proc.StartInfo = $psi
                try { $null = $proc.Start() }
                catch { Write-Host "[run-tests][FAIL] $rel 无法启动 node: $($_.Exception.Message)"; $directFailures++; continue }
                $stdoutTask = $proc.StandardOutput.ReadToEndAsync()
                $stderrTask = $proc.StandardError.ReadToEndAsync()
                $finished = $proc.WaitForExit(120000)
                if (-not $finished) {
                    try { $proc.Kill() } catch { }
                    $null = $proc.WaitForExit(5000)
                    Write-Host "[run-tests][FAIL] $rel 超过 120s 未结束（疑似未关闭的定时器/socket），已强制终止"
                    $directFailures++
                    continue
                }
                $outText = ''
                $errText = ''
                try { if ($stdoutTask.Wait(10000)) { $outText = $stdoutTask.Result } } catch { }
                try { if ($stderrTask.Wait(10000)) { $errText = $stderrTask.Result } } catch { }
                if ($proc.ExitCode -ne 0) {
                    if ($outText) { ($outText -split "`n") | Select-Object -Last 45 | ForEach-Object { Write-Host $_ } }
                    if ($errText) { ($errText -split "`n") | Select-Object -Last 15 | ForEach-Object { Write-Host $_ } }
                    Write-Host "[run-tests][FAIL] $rel (exit $($proc.ExitCode))"
                    $directFailures++
                }
                else {
                    $summary = [regex]::Matches($outText, 'tests (\d+)[\s\S]{0,120}?pass (\d+)[\s\S]{0,120}?fail (\d+)')
                    if ($summary.Count -gt 0) {
                        $m = $summary[0]
                        Write-Host "[run-tests][ok] $rel（tests=$($m.Groups[1].Value) pass=$($m.Groups[2].Value) fail=$($m.Groups[3].Value)）"
                    }
                    else {
                        Write-Host "[run-tests][ok] $rel"
                    }
                }
            }
            if ($directFailures -gt 0) { $code = 1 } else { $code = 0 }
        }
    }
    finally {
        Pop-Location
    }
    if ($code -ne 0) { Write-Fail "web 测试失败（exit $code）" } else { Write-Pass "web 测试通过" }
}

# ---------------------------------------------------------------------------
# 4/5 sync_web 一致性（CI 在 assembleDebug 前执行 bash scripts/sync_web.sh）
# ---------------------------------------------------------------------------
Write-Step "4/6 sync_web 一致性（app/web -> app/android/app/src/main/assets/web）"
$syncScript = Join-Path $RepoRoot 'scripts\sync_web.ps1'
$androidDir = Join-Path $RepoRoot 'app\android'
if (-not (Test-Path -LiteralPath $syncScript)) {
    Write-Fail "缺少 scripts/sync_web.ps1"
}
elseif (-not (Test-Path -LiteralPath $webIndex) -or -not (Test-Path -LiteralPath $androidDir)) {
    $message = "web 或 android 模块未就绪（缺少 app/web/index.html 或 app/android/），无法做 sync 一致性检查"
    if ($AllowMissing) { Write-Skip $message } else { Write-Fail "$message；补齐后重跑，或加 -AllowMissing" }
}
else {
    # 本机执行策略会拒绝直接运行未签名脚本，必须通过独立 powershell 进程 + Bypass
    & 'powershell.exe' -NoProfile -ExecutionPolicy Bypass -File $syncScript -VerifyOnly
    $code = $LASTEXITCODE
    if ($code -ne 0) { Write-Fail "sync_web 一致性检查失败（exit $code）：先运行 scripts\sync_web.ps1 同步，或检查 app/web 是否多/少文件" }
    else { Write-Pass "sync_web 一致性检查通过" }
}

# ---------------------------------------------------------------------------
# 5/5 Android：先跑与 CI 相同的 4 个纯 stdlib 离线自检，再说明 APK 本机不可构建
# ---------------------------------------------------------------------------
Write-Step "5/6-1 Android 离线静态自检（与 CI android-build 前置步骤一致）"
$androidToolsDir = Join-Path $RepoRoot 'app\android\tools'
$androidToolScripts = @('verify_protocol.py', 'check_contract.py', 'check_kotlin_static.py', 'check_gradle_config.py')
if (-not (Test-Path -LiteralPath $androidToolsDir)) {
    $message = "android 自检工具未就绪（缺少 app/android/tools/，android-shell 负责补齐）"
    if ($AllowMissing) { Write-Skip $message } else { Write-Fail "$message；补齐后重跑，或加 -AllowMissing" }
}
else {
    $toolFailures = 0
    foreach ($toolName in $androidToolScripts) {
        $toolPath = Join-Path $androidToolsDir $toolName
        if (-not (Test-Path -LiteralPath $toolPath)) {
            Write-Fail "缺少 app/android/tools/$toolName（android-shell 负责补齐）"
            $toolFailures++
            continue
        }
        Write-Host "[run-tests] python3 app/android/tools/$toolName"
        Push-Location -LiteralPath $RepoRoot
        try { & $Python $toolPath } finally { Pop-Location }
        $toolExit = $LASTEXITCODE
        if ($toolExit -ne 0) {
            Write-Fail "Android 离线自检失败: $toolName（exit $toolExit，android-shell 负责）"
            $toolFailures++
        }
    }
    if ($toolFailures -eq 0) { Write-Pass "Android 离线静态自检通过（4 个脚本）" }
}

Write-Step "5/6-2 Android APK 构建（说明）"
$java = Get-Command java -ErrorAction SilentlyContinue
$androidSdk = $env:ANDROID_HOME
if (-not $androidSdk) { $androidSdk = $env:ANDROID_SDK_ROOT }
$localToolchain = Join-Path $RepoRoot '.tools\android'
if (-not $java -or -not $androidSdk) {
    if (Test-Path -LiteralPath $localToolchain) {
        Write-Skip "当前 shell 未设置 JDK/ANDROID_HOME，但检测到自建工具链 .tools/android（DECISIONS D-027）：按 app/android/BUILD-LOCAL.md 设置环境变量后运行 ci\build_apk.ps1 出包；GitHub Actions 解锁（D-026）前这是唯一出包路径"
    }
    else {
        Write-Skip "本机无 JDK/Android SDK（DECISIONS D-006/D-027）：先按 app/android/BUILD-LOCAL.md 自建工具链出包，或等 GitHub Actions 解锁（D-026）后 Run workflow（产物 app-debug-*）"
    }
}
else {
    Write-Host "[run-tests] 检测到 java=$($java.Source) ANDROID_HOME=$androidSdk"
    Write-Host "[run-tests] 本机具备 Android 工具链，可运行: powershell -NoProfile -ExecutionPolicy Bypass -File ci\build_apk.ps1"
    Write-Skip "run_tests.ps1 不自动构建 APK（耗时且会写 app/android/build/），请用 ci\build_apk.ps1"
}

# ---------------------------------------------------------------------------
# 6/6 端到端模拟（scripts/e2e_phone_sim.py --asr fake；默认 SKIP，-WithE2E 才跑）
# ---------------------------------------------------------------------------
Write-Step "6/6 端到端模拟（scripts/e2e_phone_sim.py --asr fake）"
$e2eScript = Join-Path $RepoRoot 'scripts\e2e_phone_sim.py'
if (-not (Test-Path -LiteralPath $e2eScript)) {
    $message = "缺少 scripts/e2e_phone_sim.py（bridge-core 负责）"
    if ($AllowMissing) { Write-Skip $message } else { Write-Fail $message }
}
elseif (-not $WithE2E) {
    Write-Skip "未指定 -WithE2E，跳过端到端模拟（默认不跑：会启动临时 Bridge/本地 socket、耗时数十秒；需要时加 -WithE2E 真跑 python scripts/e2e_phone_sim.py --asr fake）"
}
else {
    # 用 .NET Process + 超时：E2E 会起临时 Bridge，异常时避免挂死整个 run_tests。
    $e2ePsi = New-Object System.Diagnostics.ProcessStartInfo
    $e2ePsi.FileName = $Python
    $e2ePsi.Arguments = '"' + $e2eScript + '" --asr fake'
    $e2ePsi.WorkingDirectory = $RepoRoot
    $e2ePsi.UseShellExecute = $false
    $e2ePsi.CreateNoWindow = $true
    $e2ePsi.RedirectStandardOutput = $true
    $e2ePsi.RedirectStandardError = $true
    $e2eProc = New-Object System.Diagnostics.Process
    $e2eProc.StartInfo = $e2ePsi
    Write-Host "[run-tests] python scripts/e2e_phone_sim.py --asr fake（超时 600s）"
    $e2eStarted = $false
    try { $e2eStarted = $e2eProc.Start() }
    catch { Write-Fail "端到端模拟无法启动：$($_.Exception.Message)" }
    if ($e2eStarted) {
        $e2eOutTask = $e2eProc.StandardOutput.ReadToEndAsync()
        $e2eErrTask = $e2eProc.StandardError.ReadToEndAsync()
        $e2eFinished = $e2eProc.WaitForExit(600000)
        if (-not $e2eFinished) {
            try { $e2eProc.Kill() } catch { }
            $null = $e2eProc.WaitForExit(5000)
            Write-Fail "端到端模拟超过 600s 未结束（已强制终止）"
        }
        else {
            $e2eOut = ''
            $e2eErr = ''
            try { if ($e2eOutTask.Wait(10000)) { $e2eOut = $e2eOutTask.Result } } catch { }
            try { if ($e2eErrTask.Wait(10000)) { $e2eErr = $e2eErrTask.Result } } catch { }
            if ($e2eProc.ExitCode -ne 0) {
                if ($e2eErr) { ($e2eErr -split "`n") | Select-Object -Last 30 | ForEach-Object { Write-Host $_ } }
                if ($e2eOut) { ($e2eOut -split "`n") | Select-Object -Last 30 | ForEach-Object { Write-Host $_ } }
                Write-Fail "端到端模拟失败（exit $($e2eProc.ExitCode)，ASR=fake；脚本属 bridge-core）"
            }
            else {
                if ($e2eOut) { ($e2eOut -split "`n") | Select-Object -Last 12 | ForEach-Object { Write-Host $_ } }
                Write-Pass "端到端模拟通过（ASR=fake）"
            }
        }
    }
}

# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "== 汇总 =="
if ($script:Skips.Count -gt 0) {
    Write-Host "[run-tests] 跳过 $($script:Skips.Count) 项：" -ForegroundColor Yellow
    foreach ($item in $script:Skips) { Write-Host "  - $item" -ForegroundColor Yellow }
}
if ($script:Failures.Count -gt 0) {
    Write-Host "[run-tests][FAIL] $($script:Failures.Count) 项失败：" -ForegroundColor Red
    foreach ($item in $script:Failures) { Write-Host "  - $item" -ForegroundColor Red }
    exit 1
}
Write-Host "[run-tests][PASS] 本地可验证项全部通过。" -ForegroundColor Green
exit 0
