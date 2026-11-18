# Local build & verification (Windows, no preinstalled JDK / Android SDK)

This document records how the debug APK was built and verified on a Windows
machine that had **no JDK, Android SDK, adb or Gradle**, using only downloads
performed with Python (the machine's curl / .NET HTTP stacks are broken for
HTTPS downloads). It also documents the sandbox-workarounds that were needed
here, the measured results, and known warnings.

Verified on 2026-11-18, Windows 11 x64, PowerShell 5.1 (`pwsh` is not on PATH),
project path contains non-ASCII characters: `D:\code\apps\手机端\`.

> This file is a deliverable. It is safe to commit. Tool downloads under
> `.tools\` are **not** committed (`.tools/` is gitignored).

## 0. v1.1 rebuild (2026-11-18, current) — QR scan / pickImage / multi-address

The toolchain in this file did **not** change for v1.1 (D-033); only one runtime
dependency was added. The numbers below supersede the v1.0 baseline recorded in
sections 5–7 (which are kept as history).

| Item | v1.1 value |
|---|---|
| New dependency | `com.google.zxing:core:3.5.3` (local QR decode, D-030) |
| Default version | `versionName 1.1.0` / `versionCode 10100` (D-033; `-P` still overrides) |
| Build command | same as §4 but `-Pdsphone.versionName=1.1.0 -Pdsphone.versionCode=10100` |
| Build result | Final rebuild after web-ui v1.1 sync: `BUILD SUCCESSFUL in 27s` (35 actionable tasks: 5 executed, 30 up-to-date; `mergeDebugAssets` + `packageDebug` re-ran); first v1.1 build was `1m 17s` |
| APK | `app\android\app\build\outputs\apk\debug\app-debug.apk` — **4,578,710 bytes** (final, with synced v1.1 Web assets) |
| APK SHA-256 | `A45C164E55331ACF03697D7106DEC6C60DD37361AEE0E4C263DB14E726704601` |
| badging | `package: name='com.dsh.phone' versionCode='10100' versionName='1.1.0'`, minSdk 29 / targetSdk 35 / compileSdk 35 |
| Packaging | 49 ZIP entries, 9 `classes*.dex`, 26 `assets/web/*`; `scanPairing`/`pickImage` code and `ScanActivity` + `storage.ImageFileProvider` (authority `com.dsh.phone.fileprovider`) are packaged; `sync_web.ps1 -VerifyOnly` exits 0 after the build |
| Unit tests | **10 classes / 71 tests, `OK (71 tests)`** in 0.414 s (direct JUnitCore, §7.2) |
| Static checks | `verify_protocol.py` → `OK: 101 protocol checks`; `check_contract.py` (after web-ui v1.1 landed) → `OK: 17 web ops / 17 native ops (docs 17), 17 web events / 17 native events, 11 documented return events (0 warning(s))`; `check_kotlin_static.py` → `OK: 25 Kotlin files scanned, 0 failures, 0 warnings`; `check_gradle_config.py` → `OK: 67 configuration checks, 0 warning(s)` |
| Build warnings | Final incremental build: `0` Kotlin warnings (Kotlin compile was up-to-date); the first v1.1 build showed 6 `MainActivity.kt` deprecations + the 6 `BtRouter.kt` SCO deprecations from the v1.0 baseline — see §9 |

Device verification still required (nothing below ran on hardware):

1. `ScanActivity`: Camera2 preview + YUV_420_888 `ImageReader` on different OEM
   cameras, CAMERA permission deny/allow, QR decode latency, rotation/background
   lifecycle, camera-in-use by another app.
2. `pickImage`: camera `ACTION_IMAGE_CAPTURE` + `EXTRA_OUTPUT` grant through the
   custom `ImageFileProvider` on OEM ROMs, gallery `ACTION_GET_CONTENT`, EXIF
   rotation of camera photos, 1600 px / JPEG q80 / ≤2 MiB output on real photos,
   `onShowFileChooser` round-trip from the Web UI.
3. Multi-address connect: 3 s per-address timeout on slow Wi-Fi/hotspot, `hosts=`
   fallback order, `last_host`/`known_hosts` persistence, the
   `net.state{state:"error",reason:"all_addresses_failed","tried":[...]}` shape
   (D-034) consumed by the v1.1 Web UI.
4. Web UI v1.1 is now present (web-ui task-12 landed): it parses
   `scanPairing.result{ok,...}`, `image.picked`, `connect{hosts}` and maps
   `net.state{state:"error"}` per D-034. `check_contract.py` is 0 FAIL / 0 WARN.

## 1. Installed toolchain (location + version)

Everything is installed under `D:\code\apps\手机端\.tools\android\`
(short name `$tools` in the commands below):

| Component | Version | Location |
|---|---|---|
| JDK (Temurin / Adoptium) | 17.0.21+9 | `$tools\jdk` |
| Android cmdline-tools | 12.0 (build 11076708) | `$tools\cmdline-tools\latest` |
| Gradle | 8.9 (unpacked, no wrapper jar) | `$tools\gradle-8.9` |
| Android platform-tools | 37.0.1 | `$tools\sdk\platform-tools` |
| Android platform | `platforms;android-35` rev 2 | `$tools\sdk\platforms\android-35` |
| Android build-tools | `build-tools;35.0.0` | `$tools\sdk\build-tools\35.0.0` |
| Gradle user home | - | `$tools\gradle-home` |
| AGP Android user home (debug keystore, etc.) | - | `$tools\android-user-home` |
| Build logs | - | `$tools\logs\` |

Downloaded archives (Python urllib, size-verified after download):

| Archive | Bytes | URL |
|---|---|---|
| `jdk17.zip` | 191,117,380 | `https://api.adoptium.net/v3/binary/latest/17/ga/windows/x64/jdk/hotspot/normal/eclipse?project=jdk` |
| `commandlinetools-win-11076708_latest.zip` | 153,583,359 | `https://dl.google.com/android/repository/commandlinetools-win-11076708_latest.zip` |
| `gradle-8.9-bin.zip` | 136,114,148 | `https://services.gradle.org/distributions/gradle-8.9-bin.zip` |

Helper scripts used here (not part of the app):

- `$tools\download.py` — stdlib urllib downloader (`name url expected_bytes`),
  writes `.part` then renames, checks size. Returns non-zero on size mismatch.
- `$tools\unpack.py` — idempotent extractor (`jdk17.zip` -> `jdk/`,
  cmdline-tools zip -> `cmdline-tools/latest`, `gradle-8.9-bin.zip` ->
  `gradle-8.9/`).

## 2. First-time install (clean machine recipe)

```powershell
$tools = 'D:\code\apps\手机端\.tools\android'
New-Item -ItemType Directory -Force $tools\downloads | Out-Null

# 1) download with Python (curl / Invoke-WebRequest are unreliable on this host)
$py = 'D:\anaconda3\python.exe'
& $py "$tools\download.py" jdk17.zip 'https://api.adoptium.net/v3/binary/latest/17/ga/windows/x64/jdk/hotspot/normal/eclipse?project=jdk' 191117380
& $py "$tools\download.py" commandlinetools-win-11076708_latest.zip 'https://dl.google.com/android/repository/commandlinetools-win-11076708_latest.zip' 153583359
& $py "$tools\download.py" gradle-8.9-bin.zip 'https://services.gradle.org/distributions/gradle-8.9-bin.zip' 136114148

# 2) unpack (idempotent)
& $py "$tools\unpack.py"

# 3) sdkmanager needs a writable SDK cache + user home inside the workspace
$env:JAVA_HOME          = "$tools\jdk"
$env:ANDROID_HOME       = "$tools\sdk"
$env:ANDROID_SDK_ROOT   = $env:ANDROID_HOME
$env:ANDROID_USER_HOME  = "$tools\android-user-home"
$env:JAVA_TOOL_OPTIONS  = "-Duser.home=$tools\user-home"
$env:PATH               = "$env:JAVA_HOME\bin;$env:PATH"
New-Item -ItemType Directory -Force $env:ANDROID_USER_HOME | Out-Null

# 4) accept licenses, then install the packages required by this project
('y' + "`n") * 200 | & "$tools\cmdline-tools\latest\bin\sdkmanager.bat" --sdk_root="$tools\sdk" --licenses
& "$tools\cmdline-tools\latest\bin\sdkmanager.bat" --sdk_root="$tools\sdk" `
    "platform-tools" "platforms;android-35" "build-tools;35.0.0"

# 5) Gradle also needs its own writable user home (the default one is blocked)
$env:GRADLE_USER_HOME = "$tools\gradle-home"
& "$tools\gradle-8.9\bin\gradle.bat" --version
```

## 3. Per-shell environment for build commands

Every new shell must set these before invoking Gradle (Gradle must **not** see
`JAVA_TOOL_OPTIONS`; that variable is only needed while running `sdkmanager`,
otherwise Gradle prints a "Picked up JAVA_TOOL_OPTIONS" notice):

```powershell
$tools = 'D:\code\apps\手机端\.tools\android'
$env:JAVA_HOME         = "$tools\jdk"
$env:ANDROID_HOME      = "$tools\sdk"
$env:ANDROID_SDK_ROOT  = $env:ANDROID_HOME
$env:ANDROID_USER_HOME = "$tools\android-user-home"   # set ONLY this one (see troubleshooting)
$env:GRADLE_USER_HOME  = "$tools\gradle-home"
$env:PATH              = "$env:JAVA_HOME\bin;$env:PATH"
Remove-Item Env:JAVA_TOOL_OPTIONS -ErrorAction SilentlyContinue
```

Notes:

- `local.properties` is **not** used and must not be committed. The SDK path is
  taken from `ANDROID_HOME`.
- The repository deliberately does **not** contain `gradlew` /
  `gradle/wrapper/gradle-wrapper.jar`; CI generates the wrapper on the fly.
  Locally we call the unpacked Gradle 8.9 directly.

## 4. Sync Web UI assets, then build

The UI is served from the synced asset tree (the repo does not commit the copy):

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File 'D:\code\apps\手机端\scripts\sync_web.ps1'
```

(Direct `& scripts\sync_web.ps1` may be blocked by the PowerShell execution
policy. `sync_web.ps1` is expected to report e.g. `26 files / copied 0 / unchanged 26`.)

Build (pass Gradle arguments as an **array**; putting them inline in a
harness/redirection pipeline mangles `-P...` values):

```powershell
$argList = @(
  '-p', 'D:\code\apps\手机端\app\android',
  'assembleDebug',
  '--no-daemon', '--console=plain', '--stacktrace', '--no-watch-fs',
  '-Pdsphone.versionName=1.0.0',
  '-Pdsphone.versionCode=10000'
)
& "$tools\gradle-8.9\bin\gradle.bat" @argList 2>&1 | Out-File "$tools\logs\assemble-debug.log" -Encoding utf8
```

`--no-watch-fs` is required in this sandbox (the JVM file watcher cannot open
the current thread and fails with `error = 5`).

## 5. Measured build result

- First successful build: **`BUILD SUCCESSFUL in 54s`** (35 actionable tasks:
  12 executed, 23 up-to-date), exit code 0.
- Incremental `assembleDebug` re-run: **`BUILD SUCCESSFUL in 21s`** (35
  actionable tasks, 35 up-to-date, exit 0; log
  `$tools\logs\assemble-debug-incremental.log`) — the artifact is reproducible
  and the APK was not repackaged.
- Version props were verified in the manifest: `versionCode=10000`,
  `versionName=1.0.0` (v1.0 baseline). The **v1.1 rebuild** produced
  `versionCode=10100` / `versionName=1.1.0` (final rebuild after the web-ui v1.1
  sync: 27 s incremental — see §0).

## 6. APK and its verification

Artifact:

- Path: `D:\code\apps\手机端\app\android\app\build\outputs\apk\debug\app-debug.apk`
- Size: **3,867,632 bytes**
- SHA-256: `72C34DBA629475D421B517FD8976942DAC764A91985111DAC0FDA97E718CA408`

> This is the v1.0 baseline (APK `1.0.0/10000`). The **final v1.1 APK**
> (built after web-ui v1.1 assets were synced) is **4,578,710 bytes**,
> SHA-256
> `A45C164E55331ACF03697D7106DEC6C60DD37361AEE0E4C263DB14E726704601`,
> badging `versionName='1.1.0' versionCode='10100'` — see §0.

Verify identity / SDK levels / permissions:

```powershell
& "$tools\sdk\build-tools\35.0.0\aapt2.exe" dump badging `
    'D:\code\apps\手机端\app\android\app\build\outputs\apk\debug\app-debug.apk'
```

Observed (2026-11-18):

```
package: name='com.dsh.phone' versionCode='10000' versionName='1.0.0'
  platformBuildVersionName='15' platformBuildVersionCode='35' compileSdkVersion='35'
sdkVersion:'29'  targetSdkVersion:'35'
uses-permission: INTERNET, ACCESS_NETWORK_STATE, RECORD_AUDIO,
  MODIFY_AUDIO_SETTINGS, POST_NOTIFICATIONS, FOREGROUND_SERVICE,
  FOREGROUND_SERVICE_MICROPHONE, CAMERA, BLUETOOTH (maxSdkVersion='30'),
  BLUETOOTH_ADMIN (maxSdkVersion='30'), BLUETOOTH_CONNECT
```

Content check:

- `assets/web/*` present (26 files: `index.html`, 3 CSS, 22 JS) — the synced
  Web UI is packaged.
- 9 `classes*.dex` files; 49 ZIP entries total.

## 7. Unit tests

### 7.1 Gradle task in this sandbox

`testDebugUnitTest` from the project path **fails in this environment** with:

```
Could not write standard input to Gradle Test Executor 1.
java.io.IOException: 管道已在本地关闭。
java.lang.ClassNotFoundException: worker.org.gradle.process.internal.worker.GradleWorkerMain
```

This is a harness restriction (the sandbox forbids the pipes Gradle uses to
fork its test worker), combined with non-ASCII paths appearing in the worker
`@argfile`. It is **not** a code/test problem. On CI (Ubuntu, ASCII paths) the
normal Gradle task runs fine.

### 7.2 Workaround A (quick): direct JUnit 4 runner on the compiled test classes

The compiled classes are already in `app\build\tmp\kotlin-classes\{debug,debugUnitTest}`;
run them with the JDK directly (no Gradle worker, no pipes):

```powershell
$tools='D:\code\apps\手机端\.tools\android'; $java="$tools\jdk\bin\java.exe"
$cache="$tools\gradle-home\caches\modules-2\files-2.1"
$junit=(Get-ChildItem $cache -Recurse -Filter 'junit-4.13.2.jar' -File|Select -First 1).FullName
$ham=(Get-ChildItem $cache -Recurse -Filter 'hamcrest-core-1.3.jar' -File|Select -First 1).FullName
$kotlin=(Get-ChildItem $cache -Recurse -Filter 'kotlin-stdlib-*.jar' -File|Select -First 1).FullName
$build='D:\code\apps\手机端\app\android\app\build'
$cp="$build\tmp\kotlin-classes\debug;$build\tmp\kotlin-classes\debugUnitTest;$junit;$ham;$kotlin"
& $java -cp $cp org.junit.runner.JUnitCore com.dsh.phone.log.RedactorTest com.dsh.phone.net.BackoffTest com.dsh.phone.net.ClosePolicyTest com.dsh.phone.net.AddressCandidatesTest com.dsh.phone.media.ImageScalerTest com.dsh.phone.protocol.AudioFrameTest com.dsh.phone.protocol.AuthTest com.dsh.phone.protocol.PairingTest com.dsh.phone.protocol.ProtocolTest com.dsh.phone.util.JsonTest
```

Result: `OK (53 tests)` (8 test classes, ~0.3 s) for v1.0; the **v1.1 run is
`OK (71 tests)`** (10 test classes, 0.414 s) — the two extra classes are
`AddressCandidatesTest` and `ImageScalerTest`, and `PairingTest` gained 5 v1.1
cases (hosts/name). Compile the test classes first if needed:
`gradle :app:compileDebugUnitTestKotlin --no-daemon --console=plain --no-watch-fs`.

### 7.3 Workaround B (official Gradle test report): build/test from an ASCII temp copy

Copy the project and Gradle home to an ASCII-only temp path, then run the real
Gradle test task there. This produced a genuine JUnit XML report:

```powershell
$tmp = Join-Path $env:TEMP 'dsh-build'      # $env:TEMP must be an ASCII path
$tools = 'D:\code\apps\手机端\.tools\android'
robocopy 'D:\code\apps\手机端\app\android' "$tmp\android" /E `
    /XD "$tools\..\..\app\android\app\build" 'D:\code\apps\手机端\app\android\app\build' 'D:\code\apps\手机端\app\android\build' 'D:\code\apps\手机端\app\android\.gradle'
robocopy 'D:\code\apps\手机端\.tools\android\gradle-home' "$tmp\gradle-home" /E
robocopy 'D:\code\apps\手机端\app\web' "$tmp\android\app\src\main\assets\web" /E

$env:JAVA_HOME         = "$tools\jdk"
$env:ANDROID_HOME      = "$tools\sdk"
$env:ANDROID_SDK_ROOT  = $env:ANDROID_HOME
$env:ANDROID_USER_HOME = "$tmp\android-user-home"
$env:GRADLE_USER_HOME  = "$tmp\gradle-home"
$env:PATH              = "$env:JAVA_HOME\bin;$env:PATH"
Remove-Item Env:JAVA_TOOL_OPTIONS -ErrorAction SilentlyContinue

& "$tools\gradle-8.9\bin\gradle.bat" -p "$tmp\android" testDebugUnitTest `
    --no-daemon --console=plain --stacktrace --no-watch-fs
```

Observed: `BUILD SUCCESSFUL in 45s`, exit 0, and JUnit results in
`$tmp\android\app\build\test-results\testDebugUnitTest\*.xml`:

| Test class | Tests | Failures |
|---|---|---|
| `com.dsh.phone.log.RedactorTest` | 6 | 0 |
| `com.dsh.phone.net.BackoffTest` | 4 | 0 |
| `com.dsh.phone.net.ClosePolicyTest` | 6 | 0 |
| `com.dsh.phone.protocol.AudioFrameTest` | 8 | 0 |
| `com.dsh.phone.protocol.AuthTest` | 7 | 0 |
| `com.dsh.phone.protocol.PairingTest` | 8 | 0 |
| `com.dsh.phone.protocol.ProtocolTest` | 7 | 0 |
| `com.dsh.phone.util.JsonTest` | 7 | 0 |
| **Total** | **53** | **0** |

CI keeps using `./gradlew testDebugUnitTest` (see section 10).

## 8. Non-ASCII project path (important for this workspace)

The project path `D:\code\apps\手机端\` is non-ASCII. Two project settings make
AGP/Gradle tolerate it:

1. `app/android/gradle.properties`:
   `android.overridePathCheck=true` — AGP's opt-in to continue despite
   non-ASCII paths (it logs `WARNING: The option setting
   'android.overridePathCheck=true' is experimental.`).
2. `app/android/app/build.gradle.kts`:
   `buildToolsVersion = "35.0.0"` — AGP 8.7.x otherwise defaults to Build Tools
   `34.0.0`, which is not installed on a machine that only installs the
   compileSdk-matching build-tools.

Residual effect: `aapt2` prints a non-fatal noise line with mojibake
(`aapt2: Failed to stat file 'D:\code\apps\<mojibake>\...\android.jar'`) while
processing, but the build succeeds and the APK is correct. This warning is
expected on this host and harmless.

## 9. Known warnings in the build log

The v1.1 build was incremental: only `MainActivity.kt` recompiled, so its 6
deprecations appeared (line numbers now `90`, `466`, `467`, `470`, `472`, `475`).
A clean build shows the same 6 plus the 6 `BtRouter.kt` SCO deprecations listed
below — no new warning category was introduced by v1.1.

Kotlin deprecations (no action for M2; kept for M3 cleanup):

- `MainActivity.kt`: `WebSettings.databaseEnabled` (`~:83`), `statusBarColor`
  (`~:341`), `navigationBarColor` (`~:342`), `SYSTEM_UI_FLAG_LIGHT_STATUS_BAR`
  (`~:345`), `SYSTEM_UI_FLAG_LIGHT_NAVIGATION_BAR` (`~:347`),
  `systemUiVisibility` (`~:350`) — target SDK 35 prefers
  `WindowInsetsController`.
- `BtRouter.kt`: `isBluetoothScoOn` (`~:51/175/197`), `stopBluetoothSco`
  (`~:72/232`), `startBluetoothSco` (`~:185`) — legacy SCO path intentionally
  kept for API 29–30.

Build noise (expected, non-blocking):

- `Unable to initialize metrics, ensure C:\Users\15653\.android is writable ...`
  (AGP analytics cannot write the blocked user profile).
- `WARNING: The option setting 'android.overridePathCheck=true' is experimental.`
- `aapt2: Failed to stat file '...<mojibake>...\android.jar'` (see section 8).

## 10. CI reference

CI (`.github/workflows/ci.yml`, Ubuntu + Temurin JDK 17 + Gradle 8.9) does
**not** need any of the sandbox workarounds above:

- It generates the Gradle wrapper on the fly if `gradlew` / the wrapper jar are
  absent (they are intentionally not committed).
- It installs `platform-tools`, `platforms;android-35`, `build-tools;35.0.0`
  via `sdkmanager`.
- It runs the repository's static checks
  (`app/android/tools/verify_protocol.py`, `check_contract.py`,
  `check_kotlin_static.py`, `check_gradle_config.py`) and
  `./gradlew assembleDebug testDebugUnitTest`.

Local commands above are for reproducing the APK without CI only.

## 11. Troubleshooting (symptoms actually seen here)

| Symptom | Root cause | Fix |
|---|---|---|
| `sdkmanager` prints `Failed to download any source lists!`, `IO exception while downloading manifest`, `Failed to find package 'platform-tools'` | sdkmanager cannot write its cache under `C:\Users\<user>\.android` (blocked/absent user profile) | set `ANDROID_USER_HOME=$tools\android-user-home` and `JAVA_TOOL_OPTIONS=-Duser.home=$tools\user-home`, create the dirs, re-run `--licenses` then install |
| `Could not initialize native services. > Failed to load native library 'native-platform.dll'` | Gradle user home unwritable (`C:\Users\<user>\.gradle`) | set `GRADLE_USER_HOME=$tools\gradle-home` |
| `Task '.versionName=1.0.0' not found in root project 'dsh-phone'` | `-P...` args split by the shell/redirection pipeline | pass the whole command as an array (`$argList`) and splat with `@argList`; avoid `*>` redirection |
| `net.rubygrapefruit.platform.NativeException: Couldn't open current thread, error = 5` | sandbox blocks the file watcher | add `--no-watch-fs` |
| `Failed to find Build Tools revision 34.0.0` | AGP 8.7.x default build-tools | pin `buildToolsVersion = "35.0.0"` |
| `AndroidLocationsException: Several environment variables ... different paths to the Android Preferences folder` | `ANDROID_USER_HOME` + `ANDROID_PREFS_ROOT` + `ANDROID_SDK_HOME` disagree | set only `ANDROID_USER_HOME` |
| `:app:validateSigningDebug` — `AccessDeniedException: ...\.android\debug.keystore.lock` | debug keystore location unwritable | same fix: writable `ANDROID_USER_HOME` |
| Kotlin error `'onResponse' overrides nothing` in `WssClient.kt` | `OkHttp` `WebSocketListener` has no `onResponse`; the handshake response is delivered to `onOpen` | validate `Sec-WebSocket-Protocol` inside `onOpen(webSocket, response)` |
| `testDebugUnitTest` — `ClassNotFoundException: GradleWorkerMain` / `管道已在本地关闭` | sandbox forbids forked-worker pipes (+ non-ASCII argfile paths) | run tests via direct JUnit (7.2) or from an ASCII temp copy (7.3) |
| `& scripts\sync_web.ps1` blocked | PowerShell execution policy | `powershell -NoProfile -ExecutionPolicy Bypass -File ...` |

## 12. Files touched for this local build

Only files inside `app/android/`; nothing was committed by the shell task
(Lead commits).

- `app/android/app/build.gradle.kts` — added explicit
  `buildToolsVersion = "35.0.0"` (clean machines lack AGP's default 34.0.0).
- `app/android/gradle.properties` — added `android.overridePathCheck=true`
  (non-ASCII workspace path).
- `app/android/app/src/main/java/com/dsh/phone/net/WssClient.kt` — subprotocol
  echo validation moved from a non-existent `onResponse` override into
  `onOpen(webSocket, response)`; on mismatch: log, close with
  `PROTOCOL_ERROR`, emit `transport` error, `error` state (D-034 wire value),
  `return` without opening the session.
- `app/android/BUILD-LOCAL.md` — this document.

### v1.1 additions (D-029/D-030/D-031/D-033), also inside `app/android/` only

- New: `app/src/main/java/com/dsh/phone/ScanActivity.kt` (Camera2 + ZXing QR
  scanner), `media/ImageScaler.kt`, `media/ImagePicker.kt`,
  `storage/ImageFileProvider.kt`, `net/AddressCandidates.kt`;
  tests `net/AddressCandidatesTest.kt`, `media/ImageScalerTest.kt`.
- Modified: `protocol/Pairing.kt` (hosts/name + isCandidateHost/parseHosts),
  `storage/SettingsStore.kt` (last_host/known_hosts), `net/WssClient.kt`
  (multi-address candidate cycle, 3 s per address), `AppCore.kt`
  (Host + scanPairing/pickImage request flow), `bridge/DshBridge.kt`
  (`pickImage`, real `scanPairing`, connect `hosts`), `MainActivity.kt`
  (`AppCore.Host`, `onShowFileChooser`, camera capture + gallery intents),
  `AndroidManifest.xml` (`.ScanActivity` + `.storage.ImageFileProvider`),
  `app/build.gradle.kts` (zxing core 3.5.3, default 1.1.0/10100),
  `res/values*/strings.xml` (scan_hint/scan_cancel),
  `tools/check_contract.py` (Web-pending FAIL → WARN),
  `tools/check_kotlin_static.py` (camera/provider/pickImage/multi-address checks),
  `tools/verify_protocol.py` (hosts/name + images/session.meta/usage/media),
  `tools/check_gradle_config.py` (1.1.0/10100 + zxing), `README.md` (v1.1 section).
- Build output (final, with synced v1.1 Web assets): APK **4,578,710 B**,
  SHA-256
  `A45C164E55331ACF03697D7106DEC6C60DD37361AEE0E4C263DB14E726704601`,
  badging `1.1.0/10100`, 71 unit tests green, 4 static scripts exit 0
  (`check_contract` now 0 WARN after web-ui v1.1 landed) — see §0.
