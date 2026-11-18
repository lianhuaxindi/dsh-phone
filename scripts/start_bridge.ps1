<#
.SYNOPSIS
    一键启动 / 停止 DSH Phone Bridge（局域网模式，供手机 App 配对连接）。

.DESCRIPTION
    做四件小事，省掉手动敲命令：
      1. 自动选择 Python（默认 D:\anaconda3\envs\scisim-agent\python.exe，可用 -Py 或环境变量 DSH_PHONE_PYTHON 覆盖）；
      2. 自动检测本机局域网 IPv4（排除 VMware / Mihomo / 回环等虚拟网卡，优先 WLAN）；
      3. 生成 bridge\config.local.toml（复制示例配置，改 host/port/配对码有效期，不改仓库里的正式示例）；
      4. 前台启动 Bridge，窗口里直接显示配对码 / 指纹 / 配对 URI；按 Ctrl+C 停止。

.PARAMETER Stop
    不启动，改为停止正在运行的 Bridge 进程（按端口与命令行匹配，只杀 python 的 -m bridge）。

.PARAMETER HostAddress
    手动指定手机要连的局域网 IP。默认自动检测。

.PARAMETER Port
    监听端口，默认 8765。

.PARAMETER PairCodeTtlSec
    配对码有效期秒数，默认 900（正式默认值 120，为了首次配对方便在这里放宽，范围 30..3600）。

.PARAMETER Config
    使用自定义配置文件路径；给了就不再生成 config.local.toml。

.PARAMETER DryRun
    只做检测与生成配置并打印将执行的命令，不真正启动。

.EXAMPLE
    scripts\start_bridge.cmd
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\start_bridge.ps1 -HostAddress 192.168.1.23
.EXAMPLE
    scripts\stop_bridge.cmd
#>
[CmdletBinding()]
param(
    [switch]$Stop,
    [string]$HostAddress = "",
    [int]$Port = 8765,
    [int]$PairCodeTtlSec = 900,
    [string]$Config = "",
    [string]$Py = "",
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$scriptRoot = $PSScriptRoot
$repoRoot = Split-Path -Parent $scriptRoot
$exampleConfig = Join-Path $repoRoot 'bridge\config.example.toml'
$lanScript = Join-Path $scriptRoot 'bridge_lan.py'
$localConfig = Join-Path $repoRoot 'bridge\config.local.toml'

function Write-Info([string]$Text) { Write-Host "[bridge] $Text" -ForegroundColor Cyan }
function Write-Note([string]$Text) { Write-Host "[bridge] $Text" -ForegroundColor DarkGray }
function Write-Warn2([string]$Text) { Write-Host "[bridge] $Text" -ForegroundColor Yellow }
function Write-Fail([string]$Text) { Write-Host "[bridge] ERROR: $Text" -ForegroundColor Red }

try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false) } catch { }
try { $Host.UI.RawUI.WindowTitle = 'DSH Phone Bridge' } catch { }

# --------------------------------------------------------------------------
# 停止模式
# --------------------------------------------------------------------------
if ($Stop) {
    $killed = @()
    $cims = @()
    try {
        $cims = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction Stop |
            Where-Object { $_.CommandLine -and ($_.CommandLine -match '-m\s+bridge') }
    } catch {
        Write-Note "无法通过 WMI 枚举命令行（$($_.Exception.Message)），改用端口匹配。"
    }
    foreach ($proc in $cims) {
        try {
            Stop-Process -Id $proc.ProcessId -Force -ErrorAction Stop
            $killed += $proc.ProcessId
        } catch {
            Write-Warn2 "无法停止 PID $($proc.ProcessId)：$($_.Exception.Message)"
        }
    }
    if ($killed.Count -eq 0) {
        # 兜底：谁在监听这个端口
        $owners = @()
        try {
            $owners = netstat -ano | Select-String ":$Port\s+.*LISTENING" |
                ForEach-Object { ($_ -split '\s+')[-1] } | Sort-Object -Unique
        } catch { }
        foreach ($owner in $owners) {
            if (-not $owner) { continue }
            $p = Get-Process -Id $owner -ErrorAction SilentlyContinue
            if ($p -and $p.ProcessName -eq 'python') {
                try {
                    Stop-Process -Id $p.Id -Force -ErrorAction Stop
                    $killed += $p.Id
                } catch {
                    Write-Warn2 "无法停止 PID $($p.Id)：$($_.Exception.Message)"
                }
            }
        }
    }
    if ($killed.Count -gt 0) {
        Write-Info ("已停止 Bridge 进程: " + (($killed | Sort-Object -Unique) -join ', '))
    } else {
        Write-Info "没有发现运行中的 Bridge 进程（端口 $Port 也未被占用）。"
    }
    return
}

# --------------------------------------------------------------------------
# 1. 基本检查
# --------------------------------------------------------------------------
if (-not (Test-Path -LiteralPath $exampleConfig)) {
    Write-Fail "找不到示例配置: $exampleConfig —— 请在仓库根目录运行本脚本。"
    exit 2
}
if (-not (Test-Path -LiteralPath $lanScript)) {
    Write-Fail "找不到 scripts\bridge_lan.py —— 仓库文件不完整。"
    exit 2
}
if ($Port -lt 1 -or $Port -gt 65535) { Write-Fail "端口非法: $Port"; exit 2 }
if ($PairCodeTtlSec -lt 30 -or $PairCodeTtlSec -gt 3600) { Write-Fail "PairCodeTtlSec 必须在 30..3600"; exit 2 }

# --------------------------------------------------------------------------
# 2. 选 Python
# --------------------------------------------------------------------------
$pythonCandidates = @()
if ($Py) { $pythonCandidates += $Py }
if ($env:DSH_PHONE_PYTHON) { $pythonCandidates += $env:DSH_PHONE_PYTHON }
$pythonCandidates += 'D:\anaconda3\envs\scisim-agent\python.exe'
$cmdPython = Get-Command python -ErrorAction SilentlyContinue
if ($cmdPython) { $pythonCandidates += $cmdPython.Source }

$python = $null
foreach ($candidate in $pythonCandidates) {
    if ($candidate -and (Test-Path -LiteralPath $candidate)) { $python = $candidate; break }
}
if (-not $python) {
    Write-Fail "找不到 Python 解释器。请安装 conda 环境 scisim-agent，或用 -Py 指定路径 / 设置环境变量 DSH_PHONE_PYTHON。"
    exit 2
}

# --------------------------------------------------------------------------
# 3. 检测局域网 IP
# --------------------------------------------------------------------------
if ($HostAddress) {
    $parsed = $null
    if (-not [System.Net.IPAddress]::TryParse($HostAddress, [ref]$parsed)) {
        Write-Fail "HostAddress 不是合法 IPv4: $HostAddress"
        exit 2
    }
    if ($HostAddress.StartsWith('127.')) {
        Write-Warn2 "127.0.0.1 只能本机访问，手机连不上；建议去掉 -HostAddress 让脚本自动检测。"
    }
} else {
    Write-Info "检测本机局域网 IPv4 ..."
    try {
        $detected = & $python $lanScript --quiet 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $detected) {
            Write-Fail "自动检测失败。请运行 ipconfig 查看 WLAN 的 IPv4，然后用 -HostAddress 指定。"
            exit 2
        }
        $HostAddress = ($detected | Select-Object -First 1).Trim()
    } catch {
        Write-Fail "自动检测出错: $($_.Exception.Message)。请用 -HostAddress 指定局域网 IP。"
        exit 2
    }
}

# --------------------------------------------------------------------------
# 4. 端口占用检查
# --------------------------------------------------------------------------
$busy = $null
try {
    $busy = netstat -ano | Select-String ":$Port\s+.*LISTENING" | Select-Object -First 1
} catch { }
if ($busy) {
    Write-Warn2 "端口 $Port 已被占用（可能是上一次没关掉的 Bridge）。"
    Write-Note "可以先执行 scripts\stop_bridge.cmd 停止旧进程，或加 -Port 8766 换端口。"
    if (-not $DryRun) {
        $answer = Read-Host "继续尝试启动吗？(y/N)"
        if ($answer -notmatch '^(y|yes)$') { Write-Info "已取消。"; exit 1 }
    }
}

# --------------------------------------------------------------------------
# 5. 生成 config.local.toml
# --------------------------------------------------------------------------
$configPath = $Config
if (-not $configPath) {
    Write-Info "生成 $localConfig ..."
    $text = Get-Content -LiteralPath $exampleConfig -Raw -Encoding UTF8
    $text = [regex]::Replace($text, '(?m)^host\s*=\s*".*?"\s*$', 'host = "' + $HostAddress + '"')
    $text = [regex]::Replace($text, '(?m)^port\s*=\s*\d+\s*$', 'port = ' + $Port)
    $text = [regex]::Replace($text, '(?m)^pairCodeTtlSec\s*=\s*\d+\s*$', 'pairCodeTtlSec = ' + $PairCodeTtlSec)
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($localConfig, $text, $utf8NoBom)
    $configPath = $localConfig
} else {
    if (-not (Test-Path -LiteralPath $configPath)) {
        Write-Fail "配置文件不存在: $configPath"
        exit 2
    }
    $configPath = (Get-Item -LiteralPath $configPath).FullName
}

# --------------------------------------------------------------------------
# 6. 打印说明并启动
# --------------------------------------------------------------------------
Write-Host ''
Write-Host '==================== DSH Phone Bridge ====================' -ForegroundColor Green
Write-Info ("手机要连的地址 : " + $HostAddress + ':' + $Port)
Write-Info ("配对码有效期   : " + $PairCodeTtlSec + ' 秒（过期后控制台会自动打印新码）')
Write-Info ("配置文件       : " + $configPath)
Write-Info ("Python         : " + $python)
Write-Host ''
Write-Note '手机端怎么填：配对页把下面控制台打印的「配对 URI」整条粘进主机输入框，或手填'
Write-Note '  主机 / 端口 / 64 位指纹 / 6 位配对码；手机需与本机连同一个 Wi-Fi。'
Write-Note '停止：本窗口按 Ctrl+C，或另开一个窗口执行 scripts\stop_bridge.cmd。'
Write-Note ('连不上先查：同一 Wi-Fi？Windows 防火墙是否放行 python 入站 ' + $Port + '？')
Write-Host '===========================================================' -ForegroundColor Green
Write-Host ''

$bridgeArgs = @('-u', '-m', 'bridge', '--config', $configPath)
if ($DryRun) {
    Write-Info ("DryRun：将执行  `"" + $python + '" ' + ($bridgeArgs -join ' ') + "`"")
    Write-Info ("        工作目录 " + $repoRoot)
    exit 0
}

$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUNBUFFERED = '1'
$env:PYTHONUTF8 = '1'

Push-Location -LiteralPath $repoRoot
$exitCode = 0
try {
    & $python @bridgeArgs
    $exitCode = $LASTEXITCODE
} catch {
    Write-Fail "启动失败: $($_.Exception.Message)"
    $exitCode = 1
} finally {
    Pop-Location
}

Write-Host ''
if ($exitCode -eq 0) {
    Write-Info 'Bridge 已正常退出。'
} else {
    Write-Warn2 ("Bridge 以退出码 " + $exitCode + " 结束，常见原因：端口被占用 / 配置错误 / 缺少依赖。")
    Write-Note '诊断：D:\anaconda3\envs\scisim-agent\python.exe -m bridge --self-test'
}
exit $exitCode
