<#
.SYNOPSIS
  把 app/web 的静态资源同步进 Android 壳的 WebView 资源目录。

.DESCRIPTION
  源目录  : app/web/                              （web-ui 模块负责产出）
  目标目录: app/android/app/src/main/assets/web/  （生成物，.gitignore 已忽略）
  排除项  : tests/、node_modules/、*.md、package.json、*.tmp（前端工具/编辑器临时文件，不进 APK）
  幂等    : 复制新增/变更文件、删除目标目录中的陈旧文件；重复执行结果一致。

  行为约定：
  - 源目录不存在 / 为空 / 缺少 index.html  -> 清晰报错 exit 1（提示哪个模块待补）；
  - 默认目标位置要求 app/android 已存在（Android 壳就绪后才允许同步）；
  - -VerifyOnly 只比较、不写盘：目标目录与源不一致时 exit 1，并列出差异。

.PARAMETER VerifyOnly
  只校验目标目录是否与 app/web 一致（CI 自检 / 本地检查用），不复制文件。

.PARAMETER SourcePath
  覆盖源目录（测试或非默认布局用；默认 scripts/..\app\web）。

.PARAMETER DestinationPath
  覆盖目标目录（测试或非默认布局用；默认 scripts/..\app\android\app\src\main\assets\web）。

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\sync_web.ps1

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\sync_web.ps1 -VerifyOnly
#>
[CmdletBinding()]
param(
    [switch]$VerifyOnly,
    [string]$SourcePath = "",
    [string]$DestinationPath = ""
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$RepoRoot = Split-Path -Parent $PSScriptRoot
$UsingDefaultSource = [string]::IsNullOrWhiteSpace($SourcePath)
$UsingDefaultDestination = [string]::IsNullOrWhiteSpace($DestinationPath)

if ($UsingDefaultSource) {
    $SourcePath = Join-Path $RepoRoot 'app\web'
}
if ($UsingDefaultDestination) {
    $DestinationPath = Join-Path $RepoRoot 'app\android\app\src\main\assets\web'
}

$ExcludedDescription = 'tests/, node_modules/, *.md, package.json, *.tmp'

function Write-Step([string]$Message) {
    Write-Host "[sync_web] $Message"
}

function Stop-WithError([string]$Message) {
    Write-Host "[sync_web][ERROR] $Message" -ForegroundColor Red
    exit 1
}

function Test-ExcludedPath([string]$RelativePath) {
    $p = $RelativePath -replace '\\', '/'
    # Markdown 文件（README.md / ACCEPTANCE.md / 任意子目录 *.md）只给人看，不进 APK assets。
    if ($p -match '\.md$') { return $true }
    # package.json 只是给 node --test 识别 ESM 用的前端工具文件，运行时不需要。
    if ($p -match '(^|/)package\.json$') { return $true }
    # 编辑器原子写残留的 *.tmp（如 .transport.js.<pid>.<guid>.tmp）不是运行时资源，
    # 且可能在扫描与复制之间消失（web-ui 并发编辑会触发 Copy-Item 竞态），必须跳过。
    if ($p -match '\.tmp$') { return $true }
    if ($p -match '^tests/') { return $true }
    if ($p -match '(^|/)node_modules/') { return $true }
    return $false
}

function Get-RelativePath([string]$BasePrefix, [string]$FullPath) {
    if (-not $FullPath.StartsWith($BasePrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "internal error: '$FullPath' is not under '$BasePrefix'"
    }
    return $FullPath.Substring($BasePrefix.Length)
}

function Join-DestinationPath([string]$Root, [string]$RelativePath) {
    return (Join-Path $Root ($RelativePath -replace '/', '\'))
}

function Test-SameContent([string]$PathA, [string]$PathB) {
    $a = Get-Item -LiteralPath $PathA
    $b = Get-Item -LiteralPath $PathB
    if ($a.Length -ne $b.Length) { return $false }
    $ha = (Get-FileHash -LiteralPath $PathA -Algorithm SHA256).Hash
    $hb = (Get-FileHash -LiteralPath $PathB -Algorithm SHA256).Hash
    return ($ha -eq $hb)
}

# ---------------------------------------------------------------- 输入检查
if (-not (Test-Path -LiteralPath $SourcePath -PathType Container)) {
    Stop-WithError ("源目录不存在: $SourcePath`n  -> app/web 由 web-ui 模块产出；等该模块落盘后重试（或用 -SourcePath 指定）。")
}
if ($UsingDefaultDestination -and -not (Test-Path -LiteralPath (Join-Path $RepoRoot 'app\android') -PathType Container)) {
    Stop-WithError ("Android 工程目录不存在: $(Join-Path $RepoRoot 'app\android')`n  -> Android 壳由 android 模块产出；本脚本只负责把 app/web 复制到 assets/web。")
}
$SourceIndex = Join-Path $SourcePath 'index.html'
if (-not (Test-Path -LiteralPath $SourceIndex -PathType Leaf)) {
    Stop-WithError ("缺少入口文件: $SourceIndex`n  -> WebView 加载 assets/web/index.html，缺该文件 APK 无 UI，拒绝同步。")
}

$SourceFull = (Resolve-Path -LiteralPath $SourcePath).Path
$SourcePrefix = $SourceFull.TrimEnd('\', '/') + [System.IO.Path]::DirectorySeparatorChar
$DestinationFull = [System.IO.Path]::GetFullPath($DestinationPath).TrimEnd('\', '/')

# ---------------------------------------------------------------- 收集文件
$Files = New-Object System.Collections.ArrayList
$SkippedCount = 0
$SkippedMdCount = 0
$SkippedPackageJsonCount = 0
$SkippedTmpCount = 0
foreach ($item in (Get-ChildItem -LiteralPath $SourceFull -Recurse -File -Force | Sort-Object -Property FullName)) {
    $relative = Get-RelativePath $SourcePrefix $item.FullName
    $relative = $relative -replace '\\', '/'
    if (Test-ExcludedPath $relative) {
        $SkippedCount++
        if ($relative -match '\.md$') { $SkippedMdCount++ }
        if ($relative -match '(^|/)package\.json$') { $SkippedPackageJsonCount++ }
        if ($relative -match '\.tmp$') { $SkippedTmpCount++ }
        continue
    }
    [void]$Files.Add([pscustomobject]@{
            Relative = $relative
            FullName = $item.FullName
            Length   = $item.Length
        })
}

if ($Files.Count -eq 0) {
    Stop-WithError ("源目录没有可同步的静态资源: $SourceFull`n  -> 检查 app/web 是否为空（web-ui 模块可能尚未产出 index.html/js/css）。")
}

# ---------------------------------------------------------------- 只校验模式
if ($VerifyOnly) {
    $kept = @{}
    foreach ($file in $Files) { $kept[$file.Relative.ToLowerInvariant()] = $true }

    $differences = New-Object System.Collections.ArrayList
    if (-not (Test-Path -LiteralPath $DestinationFull -PathType Container)) {
        foreach ($file in $Files) { [void]$differences.Add("missing: $($file.Relative)") }
    }
    else {
        $destinationPrefix = $DestinationFull + [System.IO.Path]::DirectorySeparatorChar
        foreach ($file in $Files) {
            $target = Join-DestinationPath $DestinationFull $file.Relative
            if (-not (Test-Path -LiteralPath $target -PathType Leaf)) {
                [void]$differences.Add("missing: $($file.Relative)")
            }
            elseif (-not (Test-SameContent $file.FullName $target)) {
                [void]$differences.Add("content differs: $($file.Relative)")
            }
        }
        foreach ($item in (Get-ChildItem -LiteralPath $DestinationFull -Recurse -File -Force | Sort-Object -Property FullName)) {
            $relative = (Get-RelativePath $destinationPrefix $item.FullName) -replace '\\', '/'
            if (-not $kept.ContainsKey($relative.ToLowerInvariant())) {
                [void]$differences.Add("stale: $relative")
            }
        }
    }

    if ($differences.Count -gt 0) {
        Write-Host "[sync_web][FAIL] 目标目录与 app/web 不一致（$($differences.Count) 处）:" -ForegroundColor Red
        $shown = 0
        foreach ($difference in $differences) {
            Write-Host "  - $difference"
            $shown++
            if ($shown -ge 50) {
                Write-Host "  ...（其余 $($differences.Count - $shown) 处省略）"
                break
            }
        }
        exit 1
    }
    Write-Step "源目录   : $SourceFull"
    Write-Step "目标目录 : $DestinationFull"
    Write-Step "排除项   : $ExcludedDescription（跳过 $SkippedCount 个，其中 *.md $SkippedMdCount 个、package.json $SkippedPackageJsonCount 个、*.tmp $SkippedTmpCount 个）"
    Write-Step "校验通过: 目标目录已是最新（$($Files.Count) 个文件）: $DestinationFull"
    exit 0
}

# ---------------------------------------------------------------- 同步
New-Item -ItemType Directory -Force -Path $DestinationFull | Out-Null
$copied = 0
$unchanged = 0
$vanished = 0
$kept = @{}
foreach ($file in $Files) {
    $kept[$file.Relative.ToLowerInvariant()] = $true
    $target = Join-DestinationPath $DestinationFull $file.Relative
    $targetDirectory = Split-Path -Parent $target
    if (-not (Test-Path -LiteralPath $targetDirectory -PathType Container)) {
        New-Item -ItemType Directory -Force -Path $targetDirectory | Out-Null
    }
    if ((Test-Path -LiteralPath $target -PathType Leaf) -and (Test-SameContent $file.FullName $target)) {
        $unchanged++
        continue
    }
    try {
        Copy-Item -LiteralPath $file.FullName -Destination $target -Force
        $copied++
    }
    catch {
        # 并发编辑（web-ui 保存/删除源文件）时源可能消失：跳过并在汇总里报告，下一次 verify 会兜底。
        if (-not (Test-Path -LiteralPath $file.FullName -PathType Leaf)) {
            Write-Host "[sync_web][WARN] 源文件在同步过程中消失，已跳过: $($file.Relative)" -ForegroundColor Yellow
            $vanished++
        }
        else {
            throw
        }
    }
}

# 清理目标目录中的陈旧文件（源已删除/被排除）
$removed = 0
$destinationPrefix = $DestinationFull + [System.IO.Path]::DirectorySeparatorChar
foreach ($item in (Get-ChildItem -LiteralPath $DestinationFull -Recurse -File -Force)) {
    $relative = (Get-RelativePath $destinationPrefix $item.FullName) -replace '\\', '/'
    if (-not $kept.ContainsKey($relative.ToLowerInvariant())) {
        Remove-Item -LiteralPath $item.FullName -Force
        $removed++
    }
}

# 清理因删除文件而变空的目录（不动目标根）
$emptyDirectories = Get-ChildItem -LiteralPath $DestinationFull -Recurse -Directory -Force |
    Sort-Object -Property { $_.FullName.Length } -Descending
foreach ($directory in $emptyDirectories) {
    if ((Get-ChildItem -LiteralPath $directory.FullName -Force | Measure-Object).Count -eq 0) {
        Remove-Item -LiteralPath $directory.FullName -Force
    }
}

$destinationIndex = Join-Path $DestinationFull 'index.html'
if (-not (Test-Path -LiteralPath $destinationIndex -PathType Leaf)) {
    Stop-WithError "同步后缺少目标入口文件: $destinationIndex"
}

Write-Step "源目录   : $SourceFull"
Write-Step "目标目录 : $DestinationFull"
Write-Step "排除项   : $ExcludedDescription（跳过 $SkippedCount 个，其中 *.md $SkippedMdCount 个、package.json $SkippedPackageJsonCount 个、*.tmp $SkippedTmpCount 个）"
Write-Step "同步完成 : 共 $($Files.Count) 个文件（复制 $copied，未变 $unchanged，清理陈旧 $removed，同步中消失 $vanished）"
Write-Step "OK: $destinationIndex"
exit 0
