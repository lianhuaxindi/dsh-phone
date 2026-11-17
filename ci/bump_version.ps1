<#
.SYNOPSIS
  版本号工具：维护 ci/version.json（versionName 1.0.0 + versionCode 递增），按 PRD §16 规则。

.DESCRIPTION
  ci/version.json 是 App 版本号的唯一事实源（source of truth）。规则：
    - versionName 严格三段 MAJOR.MINOR.PATCH（PRD §16：1.0.0+code）；
    - versionCode = major*10000 + minor*100 + patch，且必须严格递增；
    - 任何 name 变更后 versionCode 取 max(公式值, 旧 versionCode + 1)，避免回退；
    - -Part code 只递增 versionCode（构建号），不改 versionName。

  默认是 dry-run：只打印变更，不写文件。加 -Write 才落盘。
  -SyncAndroid 是可选尽力而为：若 app/android/app/build.gradle.kts 存在且含
  versionCode/versionName 字面量，则同步过去；不存在时只警告（CI 通过
  -Pdsphone.versionName / -Pdsphone.versionCode 属性注入，见 ci/README.md）。

.PARAMETER Part
  patch | minor | major | code | set。默认 patch。
  set 必须配合 -Version "X.Y.Z"（可带 +N 显式 versionCode）。

.PARAMETER Version
  仅 -Part set 使用：新的三段版本号，例如 "1.2.0" 或 "1.2.0+12"。

.PARAMETER Write
  真正写入 ci/version.json（默认 dry-run）。

.PARAMETER SyncAndroid
  尽力同步到 Android build.gradle.kts（android 模块就绪后用）。

.PARAMETER VersionFile
  覆盖 version.json 路径（测试用；默认 ci/version.json）。

.PARAMETER AndroidBuildFile
  覆盖 Android 构建文件路径（默认 app/android/app/build.gradle.kts）。

.PARAMETER Force
  允许 -Part set 设置一个小于等于当前 versionName 的版本（正常不需要）。

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File ci\bump_version.ps1
  powershell -NoProfile -ExecutionPolicy Bypass -File ci\bump_version.ps1 -Part minor -Write
  powershell -NoProfile -ExecutionPolicy Bypass -File ci\bump_version.ps1 -Part set -Version 1.2.0 -Write
#>
[CmdletBinding()]
param(
    [ValidateSet('patch', 'minor', 'major', 'code', 'set')]
    [string]$Part = 'patch',

    [string]$Version = '',

    [switch]$Write,

    [switch]$SyncAndroid,

    [string]$VersionFile = '',

    [string]$AndroidBuildFile = '',

    [switch]$Force
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$CiRoot = $PSScriptRoot
$RepoRoot = Split-Path -Parent $CiRoot
if ([string]::IsNullOrWhiteSpace($VersionFile)) {
    $VersionFile = Join-Path $CiRoot 'version.json'
}
if ([string]::IsNullOrWhiteSpace($AndroidBuildFile)) {
    $AndroidBuildFile = Join-Path $RepoRoot 'app\android\app\build.gradle.kts'
}

$MajorWeight = 10000
$MinorWeight = 100

function Write-Info([string]$Message) { Write-Host "[version] $Message" }
function Stop-WithError([string]$Message) {
    Write-Host "[version][ERROR] $Message" -ForegroundColor Red
    exit 1
}

function Get-FormulaCode([int]$Major, [int]$Minor, [int]$Patch) {
    return ($Major * $MajorWeight + $Minor * $MinorWeight + $Patch)
}

function Assert-ComponentLimits([int]$Major, [int]$Minor, [int]$Patch) {
    if ($Minor -ge $MinorWeight) {
        Stop-WithError "minor=$Minor 超出编码上限 $($MinorWeight - 1)，公式版 versionCode 会冲突；请用 -Part set 手动指定 +N 或调整版本策略。"
    }
    if ($Patch -ge $MinorWeight) {
        Stop-WithError "patch=$Patch 超出编码上限 $($MinorWeight - 1)，公式版 versionCode 会冲突；请用 -Part set 手动指定 +N 或调整版本策略。"
    }
    if ($Major -ge $MinorWeight) {
        Stop-WithError "major=$Major 过大，major*$MajorWeight 会溢出编码规则；请调整版本策略。"
    }
}

# ---------------------------------------------------------------- 读取
if (-not (Test-Path -LiteralPath $VersionFile -PathType Leaf)) {
    Stop-WithError "版本文件不存在: $VersionFile"
}
try {
    $raw = Get-Content -LiteralPath $VersionFile -Raw -Encoding UTF8
    $data = $raw | ConvertFrom-Json
}
catch {
    Stop-WithError "无法解析 JSON: $VersionFile`n$($_.Exception.Message)"
}

foreach ($required in @('versionName', 'versionCode')) {
    if ($null -eq $data.PSObject.Properties[$required]) {
        Stop-WithError "版本文件缺少字段 '$required': $VersionFile"
    }
}

$OldName = [string]$data.versionName
$OldCode = [int]$data.versionCode
if ($OldName -notmatch '^(\d+)\.(\d+)\.(\d+)$') {
    Stop-WithError "versionName 不是三段 MAJOR.MINOR.PATCH: '$OldName'（PRD §16 要求 1.0.0+code 形式）"
}
$OldMajor = [int]$Matches[1]
$OldMinor = [int]$Matches[2]
$OldPatch = [int]$Matches[3]
if ($OldCode -lt 1) {
    Stop-WithError "versionCode 必须是 >= 1 的整数，当前为 $OldCode"
}

Write-Info "当前: versionName=$OldName versionCode=$OldCode ($VersionFile)"

# ---------------------------------------------------------------- 计算
$NewMajor = $OldMajor
$NewMinor = $OldMinor
$NewPatch = $OldPatch
$ExplicitCode = 0
$HasExplicitCode = $false

if ($Part -eq 'set') {
    if ([string]::IsNullOrWhiteSpace($Version)) {
        Stop-WithError "-Part set 必须配合 -Version，例如 -Version 1.2.0（或 1.2.0+12）。"
    }
    if ($Version -notmatch '^(\d+)\.(\d+)\.(\d+)(?:\+(\d+))?$') {
        Stop-WithError "-Version 格式非法: '$Version'（应为 X.Y.Z 或 X.Y.Z+N）"
    }
    $NewMajor = [int]$Matches[1]
    $NewMinor = [int]$Matches[2]
    $NewPatch = [int]$Matches[3]
    if ($Matches[4]) {
        $ExplicitCode = [int]$Matches[4]
        $HasExplicitCode = $true
    }
    $compare = [version]"$NewMajor.$NewMinor.$NewPatch"
    $oldCompare = [version]"$OldMajor.$OldMinor.$OldPatch"
    if (-not $Force -and $compare -le $oldCompare) {
        Stop-WithError "-Version '$Version' 不大于当前 '$OldName'；确需降级请加 -Force。"
    }
}
elseif (-not [string]::IsNullOrWhiteSpace($Version)) {
    Stop-WithError "-Version 只能与 -Part set 一起使用（当前 -Part $Part）。"
}
elseif ($Part -eq 'patch') {
    $NewPatch = $OldPatch + 1
}
elseif ($Part -eq 'minor') {
    $NewMinor = $OldMinor + 1
    $NewPatch = 0
}
elseif ($Part -eq 'major') {
    $NewMajor = $OldMajor + 1
    $NewMinor = 0
    $NewPatch = 0
}
# Part = code: versionName 不变，只递增 versionCode

Assert-ComponentLimits $NewMajor $NewMinor $NewPatch
$NewName = "$NewMajor.$NewMinor.$NewPatch"
$FormulaCode = Get-FormulaCode $NewMajor $NewMinor $NewPatch
$MinimumMonotonic = $OldCode + 1

if ($Part -eq 'code') {
    $NewCode = $MinimumMonotonic
    $codeSource = "code 构建号 +1（旧 $OldCode + 1）"
}
elseif ($HasExplicitCode) {
    if ($ExplicitCode -lt $MinimumMonotonic) {
        Stop-WithError "显式 versionCode=$ExplicitCode 小于最小允许值 $MinimumMonotonic：versionCode 必须严格递增，不允许回退（-Force 只用于 versionName 降级）。请改用 -Part code 或更大的 +N。"
    }
    $NewCode = $ExplicitCode
    $codeSource = "显式 +$ExplicitCode"
}
else {
    $NewCode = [Math]::Max($FormulaCode, $MinimumMonotonic)
    if ($NewCode -eq $FormulaCode) {
        $codeSource = "公式 major*$MajorWeight+minor*$MinorWeight+patch=$FormulaCode"
    }
    else {
        $codeSource = "公式值 $FormulaCode 低于旧值+1，取 max 后 = $NewCode（旧 $OldCode）"
    }
}

Write-Info ("变更: -Part {0} -> versionName={1} versionCode={2}（{3}）" -f $Part, $NewName, $NewCode, $codeSource)

# ---------------------------------------------------------------- 落盘
if (-not $Write) {
    Write-Info "dry-run: 未写入任何文件。确认无误后加 -Write 更新 $VersionFile。"
    $data.versionName = $NewName
    $data.versionCode = $NewCode
    Write-Info "预览 JSON:"
    Write-Host ($data | ConvertTo-Json -Depth 5)
}
else {
    $data.versionName = $NewName
    $data.versionCode = $NewCode
    $jsonText = ($data | ConvertTo-Json -Depth 5)
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($VersionFile, $jsonText + [Environment]::NewLine, $utf8NoBom)
    Write-Info "已写入: $VersionFile"
}

# ---------------------------------------------------------------- 可选：同步 Android 构建文件
if ($SyncAndroid) {
    $kts = $AndroidBuildFile
    if (-not (Test-Path -LiteralPath $kts -PathType Leaf)) {
        Write-Host "[version][WARN] 未找到 Android 构建文件: $kts" -ForegroundColor Yellow
        Write-Host "[version][WARN] android 模块尚未就绪；ci/version.json 仍是事实源，CI 会用 -Pdsphone.versionName / -Pdsphone.versionCode 注入（见 ci/README.md）。" -ForegroundColor Yellow
    }
    else {
        $original = [System.IO.File]::ReadAllText($kts)
        $updated = $original
        $codeCount = 0
        $nameCount = 0
        $updated = [regex]::Replace($updated, 'versionCode\s*=\s*\d+', {
                param($m)
                $script:codeCount++
                return "versionCode = $NewCode"
            })
        $updated = [regex]::Replace($updated, 'versionName\s*=\s*"[^"]*"', {
                param($m)
                $script:nameCount++
                return "versionName = `"$NewName`""
            })
        if ($codeCount -gt 0 -or $nameCount -gt 0) {
            [System.IO.File]::WriteAllText($kts, $updated, (New-Object System.Text.UTF8Encoding($false)))
            Write-Info "已同步 Android 构建文件: $kts（versionCode 替换 $codeCount 处，versionName 替换 $nameCount 处）"
        }
        else {
            Write-Host "[version][WARN] $kts 中未找到 versionCode/versionName 字面量；跳过同步。" -ForegroundColor Yellow
            Write-Host "[version][WARN] 请让 Android 构建读取 -Pdsphone.versionName / -Pdsphone.versionCode（CI 契约，见 ci/README.md）。" -ForegroundColor Yellow
        }
    }
}

Write-Info "完成: versionName=$NewName versionCode=$NewCode"
exit 0
