<#
.SYNOPSIS
  在具备 JDK 17 + Android SDK 的机器上构建 debug APK（本地复现 CI android-build job）。
  本机（DECISIONS D-006：无 JDK/Android SDK）会给出明确提示并退出码 2。

.DESCRIPTION
  流程：检查 JDK/SDK -> sync_web（app/web -> app/android/app/src/main/assets/web）
        -> gradlew assembleDebug（注入 ci/version.json 的版本号）-> 定位/复制 APK。

  版本注入契约：Gradle 侧需读取 -Pdsphone.versionName / -Pdsphone.versionCode
  （android-shell 的 app/build.gradle.kts 负责；缺省时回退字面量，不影响构建成功）。

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File ci\build_apk.ps1
  powershell -NoProfile -ExecutionPolicy Bypass -File ci\build_apk.ps1 -SkipSync -Task assembleDebug
#>
[CmdletBinding()]
param(
    [switch]$SkipSync,
    [string]$Task = 'assembleDebug',
    [string]$OutputDir = 'dist'
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$ScriptRoot = $PSScriptRoot
$RepoRoot = Split-Path -Parent $ScriptRoot
$AndroidDir = Join-Path $RepoRoot 'app\android'
$VersionFile = Join-Path $ScriptRoot 'version.json'
$SyncScript = Join-Path $RepoRoot 'scripts\sync_web.ps1'

function Write-Info([string]$Message) { Write-Host "[build-apk] $Message" }
function Write-Fail([string]$Message, [int]$Code = 1) {
    Write-Host "[build-apk][ERROR] $Message" -ForegroundColor Red
    exit $Code
}

Write-Info "仓库: $RepoRoot"

# ---------------------------------------------------------------------------
# 1) JDK / Android SDK
# ---------------------------------------------------------------------------
$javaCommand = Get-Command java -ErrorAction SilentlyContinue
$androidSdk = $env:ANDROID_HOME
if (-not $androidSdk) { $androidSdk = $env:ANDROID_SDK_ROOT }

if (-not $javaCommand -or -not $androidSdk) {
    Write-Host ""
    Write-Host "[build-apk][SKIP] 本机没有 JDK 17 / Android SDK（DECISIONS D-006：构建只在 CI 跑）。" -ForegroundColor Yellow
    if (-not $javaCommand) { Write-Host "  - 未找到 java（JAVA_HOME / PATH）；CI 用 actions/setup-java temurin 17。" -ForegroundColor Yellow }
    if (-not $androidSdk) { Write-Host "  - 未设置 ANDROID_HOME / ANDROID_SDK_ROOT；CI runner 自带 SDK 并装 platforms;android-35、build-tools;35.0.0。" -ForegroundColor Yellow }
    Write-Host "  手动出包：GitHub Actions -> DSH Phone CI -> Run workflow，Artifacts 里下 app-debug-<versionName>-<versionCode>。" -ForegroundColor Yellow
    exit 2
}
Write-Info "java: $($javaCommand.Source)"
Write-Info "ANDROID_HOME: $androidSdk"

# ---------------------------------------------------------------------------
# 2) 读取版本
# ---------------------------------------------------------------------------
if (-not (Test-Path -LiteralPath $VersionFile)) {
    Write-Fail "缺少版本文件: $VersionFile"
}
$versionData = Get-Content -LiteralPath $VersionFile -Raw -Encoding UTF8 | ConvertFrom-Json
$versionName = [string]$versionData.versionName
$versionCode = [int]$versionData.versionCode
if ($versionName -notmatch '^\d+\.\d+\.\d+$' -or $versionCode -lt 1) {
    Write-Fail "ci/version.json 版本号非法: versionName=$versionName versionCode=$versionCode"
}
Write-Info "版本: versionName=$versionName versionCode=$versionCode"

# ---------------------------------------------------------------------------
# 3) sync_web（本机执行策略会拒绝直接调用 ps1，改用独立进程 + Bypass）
# ---------------------------------------------------------------------------
if (-not $SkipSync) {
    if (-not (Test-Path -LiteralPath $SyncScript)) {
        Write-Fail "缺少 scripts/sync_web.ps1"
    }
    if (-not (Test-Path -LiteralPath (Join-Path $RepoRoot 'app\web\index.html'))) {
        Write-Fail "缺少 app/web/index.html（web-ui 模块未就绪），无法同步 WebView 资源"
    }
    Write-Info "同步 web 资源 -> app/android/app/src/main/assets/web"
    & 'powershell.exe' -NoProfile -ExecutionPolicy Bypass -File $SyncScript
    if ($LASTEXITCODE -ne 0) {
        Write-Fail "sync_web 失败（exit $LASTEXITCODE）"
    }
}
else {
    Write-Info "-SkipSync：跳过 sync_web"
}

# ---------------------------------------------------------------------------
# 4) gradlew 构建
# ---------------------------------------------------------------------------
if (-not (Test-Path -LiteralPath $AndroidDir)) {
    Write-Fail "缺少 app/android/（android-shell 模块未就绪）"
}
$gradlew = Join-Path $AndroidDir 'gradlew'
if (-not (Test-Path -LiteralPath $gradlew)) {
    Write-Fail "缺少 app/android/gradlew（Gradle wrapper 未生成；CI 会自动 gradle wrapper 生成，本机请先安装 Gradle 8.9+ 并执行 `gradle wrapper`）"
}

Write-Info "执行: gradlew $Task（-Pdsphone.versionName=$versionName -Pdsphone.versionCode=$versionCode）"
Push-Location -LiteralPath $AndroidDir
try {
    & $gradlew $Task --no-daemon --console=plain --stacktrace `
        "-Pdsphone.versionName=$versionName" `
        "-Pdsphone.versionCode=$versionCode"
    $code = $LASTEXITCODE
}
finally {
    Pop-Location
}
if ($code -ne 0) {
    Write-Fail "Gradle $Task 失败（exit $code）"
}

# ---------------------------------------------------------------------------
# 5) 定位 / 复制 APK
# ---------------------------------------------------------------------------
$apkPath = Join-Path $AndroidDir 'app\build\outputs\apk\debug\app-debug.apk'
if (-not (Test-Path -LiteralPath $apkPath)) {
    $found = @(Get-ChildItem -Path $AndroidDir -Recurse -Filter '*.apk' -File -ErrorAction SilentlyContinue)
    if ($found.Count -eq 0) {
        Write-Fail "构建结束但未找到 APK（预期 $apkPath）"
    }
    $apkPath = $found[0].FullName
    Write-Info "预期路径不存在，使用找到的 APK: $apkPath"
}
$apkInfo = Get-Item -LiteralPath $apkPath
Write-Info "APK: $($apkInfo.FullName)（$([Math]::Round($apkInfo.Length / 1MB, 1)) MB）"

if (-not [string]::IsNullOrWhiteSpace($OutputDir)) {
    $targetDir = Join-Path $RepoRoot $OutputDir
    if (-not (Test-Path -LiteralPath $targetDir)) {
        New-Item -ItemType Directory -Path $targetDir -Force | Out-Null
    }
    $copyName = "dsphone-$versionName-$versionCode-debug.apk"
    $copyPath = Join-Path $targetDir $copyName
    Copy-Item -LiteralPath $apkPath -Destination $copyPath -Force
    Write-Info "已复制: $copyPath（dist/ 已被 .gitignore 忽略；非需要时不要提交）"
}

Write-Info "完成: $apkPath"
exit 0
