# dsh-phone Android shell (`app/android`)

Kotlin + WebView shell for the DSH Phone app. It implements the frozen
`dsphone.v1` WebSocket sub-protocol (see `docs/PROTOCOL.md`, DECISIONS D-015),
the JsBridge
contract from `docs/ARCHITECTURE.md` §3.2, PTT audio capture, Bluetooth routing,
foreground-service keep-alive, Keystore token storage and redacted rolling logs.

The UI itself lives in `app/web/` and is **not** committed here; CI copies it
into `app/src/main/assets/web/` before building.

## Toolchain

| Item | Value |
| --- | --- |
| Gradle | 8.9 (`gradle/wrapper/gradle-wrapper.properties`; the wrapper JAR is **not** committed, see below) |
| Android Gradle Plugin | 8.7.3 |
| Kotlin | 2.0.21 |
| JDK | 17 (Temurin in CI) |
| compileSdk / targetSdk | 35 |
| minSdk | 29 (Android 10, DECISIONS D-006) |
| Java/Kotlin target | 17 |

Runtime dependencies: `com.squareup.okhttp3:okhttp:4.12.0` (WSS) and
`com.google.zxing:core:3.5.3` (local QR decoding for `scanPairing`, D-030;
~0.5 MB, no Play Services, no network).
Unit-test dependency: `junit:junit:4.13.2`.

### Why OkHttp instead of `java.net.http`

`java.net.http.WebSocket` does not exist on Android, so the only realistic
HTTP/WebSocket stack is OkHttp. `androidx.security-crypto` is deliberately
avoided (alpha, requires AndroidX); `TokenStore` uses
`AndroidKeyStore` + `AES/GCM/NoPadding` directly so the app has **zero AndroidX
dependencies**. UI is built from platform `Activity`/`WebView` APIs and framework
themes in `res/values[-night]`.

## Building

Two paths exist; both produce the same APK.

**Local (works on this machine, already used to produce `app-debug.apk`):** the
repo carries a self-contained toolchain under `.tools/android/` (gitignored JDK 17
+ Android SDK 35 + Gradle 8.9). Full recipe, timings and troubleshooting:
**[BUILD-LOCAL.md](BUILD-LOCAL.md)** (D-027). Short version:

```powershell
$t = '<repo>\.tools\android'
$env:JAVA_HOME = "$t\jdk"; $env:ANDROID_HOME = "$t\sdk"; $env:ANDROID_SDK_ROOT = "$t\sdk"
& "$t\gradle-8.9\bin\gradle.bat" -p app\android assembleDebug --no-daemon --console=plain `
  -Pdsphone.versionName=1.1.0 -Pdsphone.versionCode=10100
# → app\android\app\build\outputs\apk\debug\app-debug.apk
```

**CI (`.github/workflows/ci.yml`, ubuntu + Temurin JDK 17 + Android SDK):** note
that GitHub Actions is currently blocked by an account-level billing lock
(D-026), so CI cannot run until the account's Billing page is fixed.

CI must provision these SDK components:

```
platforms;android-35
build-tools;35.0.0
platform-tools
```

Because `gradle/wrapper/gradle-wrapper.jar` is intentionally not committed, CI
generates the wrapper first (it must not be committed back):

```bash
cd app/android
gradle wrapper --gradle-version 8.9 --distribution-type bin --no-daemon
./gradlew assembleDebug --no-daemon --console=plain --stacktrace \
  -Pdsphone.versionName=1.1.0 -Pdsphone.versionCode=10100
```

The build reads `-Pdsphone.versionName` / `-Pdsphone.versionCode` from
`app/build.gradle.kts`; defaults are `1.1.0` / `10100` (D-033).

A normal local build (with JDK 17 + Android SDK + `local.properties` containing
`sdk.dir=...`) is:

```bash
./gradlew assembleDebug          # APK at app/build/outputs/apk/debug/app-debug.apk
./gradlew testDebugUnitTest      # JVM unit tests (not run by CI v1)
```

`local.properties`, `*.jks`/`*.keystore`, `keystore.properties` and `*.apk` are
git-ignored; debug builds sign with the AGP default debug keystore.

### Web assets

`app/android/app/src/main/assets/web/` is generated and git-ignored. Before
building, sync the Web UI:

```powershell
pwsh scripts/sync_web.ps1          # Windows
```
```bash
bash scripts/sync_web.sh           # CI / Linux
```

The WebView serves these assets through a virtual origin
`https://appassets.local/` (secure context for ES modules and IndexedDB), with
explicit MIME types: `.js` → `text/javascript`, `.json` → `application/json`,
`.css` → `text/css`. If the assets are missing the app shows a localized
fallback page instead of a blank screen.

### v1.1 additions (D-029 / D-030 / D-031 / D-033 / D-036)

- **QR scanning (`scanPairing`).** `ScanActivity` uses Camera2 + ZXing core
  (`BarcodeFormat.QR_CODE`, `MultiFormatReader`) to decode locally — no Play
  Services, no network. `CAMERA` is a runtime permission and the manifest
  declares `uses-feature android.hardware.camera required="false"`, so
  camera-less devices still install and fall back to manual pairing. Success →
  `{"type":"scanPairing.result","ok":true,"uri":...,"host":...,"port":...,"fp":...,"code":...,"sid":...,"hosts":[...],"name":...}`;
  failure → `{"ok":false,"reason":"cancelled|permission_denied|no_camera|decode_failed"}`.
  - **D-036 hardening:** the decode core lives in the pure-JVM
    `scan/QrDecoder.kt` (whole frame HybridBinarizer → GlobalHistogramBinarizer
    → inverted → center 62% crop) with `scan/YuvLuma.kt` handling
    `rowStride`/`pixelStride`; `ScanActivity` captures up to 1920×1080, decodes
    every 3rd frame, and adds tap-to-focus, a `2×` digital-zoom button, a torch
    button and a **"pick from gallery"** fallback (`ACTION_GET_CONTENT` → bitmap
    luminance → same decoder). A live status line shows
    `frames / decode tries / misses`, so "no frames" (camera busy) is
    distinguishable from "frames but no QR". Scan `bridge/state/qr.png`, not the
    terminal's block-character QR art.
- **Image pick (`pickImage`).** `{"op":"pickImage","source":"camera"|"gallery"}`.
  Gallery = `ACTION_GET_CONTENT` (`image/*`, `CATEGORY_OPENABLE`); camera =
  `ACTION_IMAGE_CAPTURE` with `EXTRA_OUTPUT` pointing at a private
  `content://com.dsh.phone.fileprovider/cache/images/...` URI (custom
  `ImageFileProvider`; zero AndroidX). `media/ImagePicker` decodes twice
  (`inJustDecodeBounds` → `inSampleSize` → exact `ImageScaler` scale), caps the
  long edge at **1600 px**, re-encodes **JPEG q80** (`Base64.NO_WRAP`, quality
  ladder to stay ≤2 MiB) and answers
  `image.picked{data,mimeType,name,width,height}` or
  `image.pickFailed{reason}` (`permission_denied|cancelled|failed|decode_failed|too_large`).
  `<input type=file>` from the Web UI is served by `onShowFileChooser`.
- **Multi-address connect (D-031).** The pairing URI may carry `hosts=`
  (comma-separated candidates, same port) and `name=`. Android tries `host`,
  then each `hosts` entry with a **3 s timeout per address**
  (`AddressCandidates` + `WssClient` candidate cycle), keeps the first success,
  and persists the winner as `last_host` / `known_hosts` (preferred on
  auto-connect). When every address fails native emits
  `net.state{"state":"error","reason":"all_addresses_failed","message":...,
  "tried":[{"host","port","elapsedMs","error"}]}` **plus**
  `error{scope:"connect",code:5001,retryable:true}`. D-034 freezes the
  `net.state` wire enum as `idle|connecting|online|closed|error`; the extra
  fields are additive, so the Web UI renders the disconnected state and can
  still show which addresses were attempted.
- **v1.1 protocol fields** (`chat.send.images`, `session.meta`,
  `assistant.done.usage`, `media.get` / `media.data`) flow through
  `proto.message` untouched; image payloads are built by the Web UI and only
  travel inside `chat.send.images` (base64 never touches the bridge directly).
  Subprotocol stays `dsphone.v1`.
- **Version defaults:** `versionName 1.1.0`, `versionCode 10100` (D-033);
  `-Pdsphone.versionName/Code` still override.

## Source map

```
app/src/main/java/com/dsh/phone/
├── DshApp.kt                 Application: AppCore.init + boot log
├── MainActivity.kt           WebView shell, appassets.local handler, permissions, AppCore.Host
├── ScanActivity.kt           Camera2 + ZXing QR scanner for scanPairing (D-030)
├── AppCore.kt                wiring: stores, WssClient, AudioSession, event sink
├── bridge/DshBridge.kt       @JavascriptInterface `window.DshNative.post(json)`
├── net/
│   ├── WssClient.kt          OkHttp WSS, cert pinning, handshake, seq, reconnect, multi-address
│   ├── AddressCandidates.kt  host → hosts[] order, 3 s per-address timeout (D-031)
│   ├── BackoffPolicy.kt      PROTOCOL §7 backoff (1/2/4/8/15 s ±20%)
│   └── ClosePolicy.kt        close-code → reconnect / re-pair / fatal
├── protocol/
│   ├── Protocol.kt           frozen v1 JSON builders + HMAC (no Android deps)
│   ├── Pairing.kt            `dsphone://pair` URI parsing/validation incl. hosts/name
│   └── AudioFrame.kt         7-byte header + 640-byte PCM16LE frames
├── audio/
│   ├── AudioRecorder.kt      AudioRecord VOICE_COMMUNICATION 16k/16-bit/mono
│   ├── BtRouter.kt           setCommunicationDevice (31+) / SCO (29-30)
│   └── AudioSession.kt       PTT lifecycle, pre-roll buffer, route + 120 s cap
├── media/
│   ├── ImageScaler.kt        ≤1600 px long edge + JPEG q80 constants (pure Kotlin)
│   └── ImagePicker.kt        decode/scale/JPEG/base64 pipeline for pickImage (D-029)
├── service/MicService.kt     foreground service (type microphone)
├── storage/                  TokenStore (Keystore AES/GCM), SettingsStore,
│                             ImageFileProvider (camera output ContentProvider)
├── log/                      LogStore (7-day JSONL), Redactor
└── util/                     Json / JsonObj (pure Kotlin), Hex
```

`app/src/test/java/com/dsh/phone/` contains JVM unit tests for the pure logic:
`AudioFrameTest`, `AuthTest` (PROTOCOL §6.3 vector), `BackoffTest`,
`PairingTest` (incl. v1.1 hosts/name), `AddressCandidatesTest`,
`ImageScalerTest`, `ClosePolicyTest`, `JsonTest`, `RedactorTest`,
`ProtocolTest` — 10 classes / 71 tests.

`tools/verify_protocol.py` re-implements the same pure protocol rules in Python
and checks the frozen vectors without any Android toolchain
(`python tools/verify_protocol.py`). It is a cross-check, not a build.

## Behaviour notes

- **Sequence authority.** Native owns all client→server `seq` numbers
  (hello = 1 on every new connection; reconnect resets the counter). Web code
  sends `{"op":"send","json":"..."}` without `seq`; native injects it. Web may
  attach a local `_reqId` that is stripped before sending; if a server message
  has a matching `replyTo`, native adds `_reqTo` to the forwarded
  `proto.message`.
- **Resync.** After `auth.ok` native automatically sends `resync{lastSeq}`
  when it has seen server messages before; Web must not send `resync`.
- **Auth.** Native authenticates automatically after `pair.ok`. The `auth`
  bridge op is idempotent (already online → `auth.result{ok:true,already:true}`).
- **Reconnect.** Native reconnects on its own (backoff + heartbeat watchdog 45 s
  client-side / server 60 s). The `reconnect` op forces an immediate attempt.
  `restartBridge` does not exist: a phone cannot restart the desktop Bridge.
- **Scanning.** v1.1 implements `scanPairing` for real: `ScanActivity`
  (Camera2 + ZXing core) decodes a QR locally and the native side parses the
  `dsphone://pair` URI (fingerprint/code validated) before emitting
  `scanPairing.result`. Manual host/port/fp/code entry remains as fallback
  (D-030). The old `{"supported":false,"reason":"m3"}` stub is gone.
- **Audio.** 16 kHz / 16-bit / mono, 20 ms = 640 B, header
  `[0x01][aseq u16LE][tsMs u32LE]`, `aseq` from 1 wrapping 65535→1, no binary
  frames before `audio.ready` (frames captured while the route is warming up are
  buffered, ≤150 frames). Recording starts on press, is capped at 120 s, and
  runs inside the `microphone` foreground service so screen-off use works.
- **Bluetooth.** API 31+ `setCommunicationDevice`, preferring LE Audio
  (`TYPE_BLE_HEADSET`, API 33+) then HFP (`TYPE_BLUETOOTH_SCO`); API 29-30
  `startBluetoothSco`/`stopBluetoothSco`. Route switch has an 8 s timeout and
  falls back to the phone mic with `audio.error{code:"route_failed"}` (FR-504).
  Releasing the route does not stop music playback.
- **Security.** The self-signed Bridge certificate is pinned by SHA-256 of the
  DER certificate (`fp` from the pairing URI); hostname verification is replaced
  by the pin. `usesCleartextTraffic=false`. Token is AES/GCM encrypted with a
  non-exportable AndroidKeyStore key, never logged and never exposed to the Web
  UI (`getToken` only reports whether a token exists).
- **Logs.** JSONL per day under `filesDir/logs`, 7-day retention, 2 MiB per
  file, export capped at 48 KiB from the tail, and every line passes
  `Redactor` (tokens, HMACs, nonces, pairing codes, text/audio bodies masked).

## Not verified on a device (runtime risk)

The module **compiles and packages locally** (D-027): the **final v1.1 APK**
(includes the web-ui v1.1 assets synced by `scripts/sync_web.ps1`) was built
with `assembleDebug` after a 27 s incremental rebuild, `app-debug.apk` is
**4,578,710 B** (`com.dsh.phone` **1.1.0 / 10100**, minSdk 29 / targetSdk 35;
SHA-256 `A45C164E55331ACF03697D7106DEC6C60DD37361AEE0E4C263DB14E726704601`), and
all **10 JVM test classes / 71 tests** pass (see BUILD-LOCAL.md §7 for the two
ways to run them when the sandbox blocks Gradle's forked test worker). What is
still unverified is **device behaviour** — nothing below has run on real
hardware yet, and no `adb` exists on this workstation.

Highest-risk items, in order:

1. Kotlin/AGP compilation (API level annotations, deprecations, overload
   resolution) — first fix should be expected in CI.
2. OkHttp + custom `TrustManager` pinning path and `Sec-WebSocket-Protocol`
   negotiation against the Python Bridge.
3. **Camera2 + ZXing scan path (new in v1.1):** preview/`ImageReader` formats on
   different OEM cameras, QR decode latency and battery, camera permission
   denial flow, `ScanActivity` lifecycle (rotation/background).
4. **Image picking (new in v1.1):** `ACTION_IMAGE_CAPTURE` `EXTRA_OUTPUT` grants
   on OEM ROMs, custom `ImageFileProvider` behaviour, EXIF rotation of camera
   photos, 2 MiB/6 MiB limits on real photos, `onShowFileChooser` from the Web UI.
5. **Multi-address connect (new in v1.1):** 3 s per-address timeout on slow Wi-Fi,
   `last_host` persistence, failure diagnostics JSON shape consumed by the Web UI.
6. `AudioRecord`/`VOICE_COMMUNICATION` behaviour and AEC availability per device.
7. `setCommunicationDevice` / SCO route switching incl. 8 s timeout and
   restoring the phone route without breaking music.
8. Foreground-service restrictions on OEM ROMs.
9. AndroidKeyStore AES/GCM (StrongBox-backed keys not required; fallback keystore
   may need the device to be unlocked after reboot).
10. WebView virtual-origin handling (relative module imports, IndexedDB).

## Known OEM differences

- Xiaomi/MIUI, Huawei/EMUI, OPPO/ColorOS, vivo, Samsung: aggressive process
  cleaners can kill the microphone foreground service; test with battery
  optimization disabled for the app.
- Some ROMs throw on `startForeground` with `FOREGROUND_SERVICE_TYPE_MICROPHONE`
  unless `RECORD_AUDIO` was granted first — the app requests permissions at
  launch and re-checks before recording.
- Bluetooth: SCO cold start takes 1.7–3.5 s (PROTOCOL/PRD §9.5) and some devices
  report `communicationDevice` before audio actually routes; the 8 s poll loop
  covers this, and the Web UI should show a "connecting headset…" state.
- LE Audio availability varies; when `TYPE_BLE_HEADSET` is rejected the code
  falls back to HFP automatically.
- WebView versions differ; the app requires a reasonably recent Android System
  WebView for ES modules and `Intl`.

## On-device smoke checklist (PRD §15)

Matrix: Android 12 / 14 / 15 devices, an HFP headset, an LE Audio headset,
networks = home Wi-Fi, phone hotspot, Tailscale, and offline.

- [ ] Fresh install → pair via typed host/port/fingerprint/code ≤ 3 min;
      QR scanning is M3.
- [ ] Process restart keeps the pairing (token in Keystore, no re-pair).
- [ ] Revoked token / `4401` → token cleared, app returns to pairing; no loop.
- [ ] Typed chat streams partials to final (Web UI); reconnect does not
      duplicate a `chat.send` (clientId idempotency).
- [ ] PTT: press → preheating indicator → `asr.partial` → release → final
      text → auto-send → streamed reply.
- [ ] Bluetooth: HFP headset press routes to headset (≤8 s), release restores
      phone audio, music playback is unaffected; LE Audio preferred when
      available, HFP fallback otherwise.
- [ ] Route failure (headset off) falls back to the phone mic with a visible
      error and recording still works.
- [ ] Network drop > 5 s → auto reconnect with backoff, resync, no duplicated
      messages; server-side heartbeat timeout also reconnects.
- [ ] Background/screen-off: recording continues ≥5 min in the foreground
      service and the notification shows the correct state.
- [ ] Logout/revoke from the Bridge side disconnects immediately.
- [ ] Diagnostics page exports redacted logs; grep the export for `token`,
      `hmac`, `nonce`, PCM bytes — none may appear.
- [ ] Dark/light theme follows system settings including status/navigation bars.
- [ ] Memory ≤ 250 MB, APK ≤ 25 MB, touch targets ≥ 48 dp.

## M3 on-device acceptance runbook (audio timing & failure paths)

Written from a static review of `AudioRecorder.kt`, `AudioSession.kt`,
`BtRouter.kt`, `MicService.kt` and `WssClient.kt` before any M3 hardware was
available. Use it as the per-device script during M3 hardening: every item is
checkable on a real phone and names the native event(s) and log line(s) that
prove it. A matrix column is only green when **both** the UI behaviour and the
log evidence match; "it seemed to work" is not acceptance.

### 0. Preparation and evidence capture

- Install the CI debug APK (`app-debug.apk` from the `android-build` job).
- Logs are structured JSON lines on logcat tag `DshPhone` (one line per event:
  `ts`, `level`, `event`, and redacted fields):
  `adb logcat -s DshPhone:V`
- The same lines roll into `files/logs/dsh-YYYYMMDD.jsonl` (7 days):
  `adb shell run-as com.dsh.phone ls files/logs`
  `adb exec-out run-as com.dsh.phone cat files/logs/dsh-$(date +%Y%m%d).jsonl`
- WebView console (native path debugging): `chrome://inspect` on the host while
  the device is attached, open the app's WebView. The bridge in the page is
  `window.DshNative.post` / `window.DshBridge.onEvent`. Alternatively use the
  in-app diagnostics page, which renders the same events.
- Finish every column with **E1** below (redacted export) before reporting.

### 1. Event and log reference (native side)

| Event | Emitted by | Meaning / expected fields |
|---|---|---|
| `audio.state` | `AudioSession.kt:329-331` | `starting` right after PTT down, `recording` on server `audio.ready`, `idle` after stop/failure |
| `audio.route` | `AudioSession.kt:333-341`, `AppCore.kt:162-171` | `route` = `phone` / `hfp` / `le` + `deviceName` |
| `audio.level` | `AudioSession.kt:268-281` | 0..1 float, throttled to 100 ms while recording |
| `audio.ready` | `WssClient.kt:702-713` | `asrMode`, `lang`, `codecs` from the server |
| `audio.error` | `AudioSession.kt:343-352` (local), `WssClient.kt:715-723/984-994` (server) | local codes are strings: `offline`, `permission_denied`, `audio_busy`, `recorder_failed`, `route_failed`, `max_duration`, `connection_lost`; server codes are numbers (`2001` busy, `2002` ASR unavailable) |
| `audio.stopped` | `AudioSession.kt:221/242` | `aseq` of the last frame sent |
| `net.state` | `WssClient.kt` (setState) | `idle` / `connecting` / `pairing` / `authenticating` / `online` / `reconnecting` / `error` / `closed` (D-034: failures use `error`; on a multi-address failure the event also carries `reason`/`message`/`tried`) |
| `error` | `WssClient.kt:972-982` | `scope` + numeric protocol code (tls/protocol/transport/connect/pair) |

Relevant `DshPhone` log lines: `app.start`, `ws.connect`, `ws.reconnect_scheduled`,
`ws.closed`, `ws.failure`, `ws.heartbeat_timeout`, `ws.connect_timeout`,
`ws.send_failed`, `ws.send_no_socket`, `ws.subprotocol_mismatch`,
`audio.record_create_failed`, `audio.start_failed`, `audio.service_start_failed`,
`bt.route_failed`, `service.foreground_failed`, `service.start_failed`,
`service.channel_failed`, `service.notify_failed`, `service.destroyed`,
`web.error`, `web.asset_missing`.

### A. Capture start and recovery (`AudioRecorder` / `AudioSession`)

- [ ] **A1 mic permission denied.** Revoke it
      (`adb shell pm revoke com.dsh.phone android.permission.RECORD_AUDIO`),
      PTT down. Expect `audio.error{code:"permission_denied",retryable:false}`
      and no `audio.start` on the wire; pressing again after granting works.
- [ ] **A2 microphone busy / recorder init failure.** Start another recorder app
      that holds the mic, then PTT. Expect `audio.error{code:"recorder_failed"}`
      (G1, fixed) while `audio.state` goes `starting` -> `idle`,
      `audio.stop{aseq:0}` sent, microphone FGS notification returns to the
      "ready" text, and no crash. The DshPhone log must show exactly one
      `audio.recorder_failed` error line for the attempt.
- [ ] **A3 read failure mid-stream.** During a held PTT, remove the headset /
      revoke mic access, or use an OEM ROM that resets the audio HAL. Expect
      `audio.error{code:"recorder_failed"}` followed by `audio.stopped` and
      `audio.state idle` within a few seconds (`MAX_READ_ERRORS = 5`, silently
      retried 4 times). No ANR, next PTT recovers. A single
      `audio.read_failed` warn line with an `errors` count must appear in
      `DshPhone` (G7, fixed).
- [ ] **A4 rapid PTT cycling.** Press/release 20 times quickly (including
      sub-200 ms taps). Expect no crash/ANR, no interleaved `audio.state`
      (`starting` -> `recording` -> `idle` only), monotonic `aseq` per stream,
      and the FGS notification never stuck on "recording".
- [ ] **A5 120 s cap.** Hold PTT (or run continuous mode) for >120 s. Expect
      `audio.error{code:"max_duration"}` and a clean stop just before 120 s;
      server receives `audio.stop` with the last `aseq`.

### B. Bluetooth routing (`BtRouter`)

- [ ] **B1 HFP headset (all devices).** With an HFP headset connected and music
      playing, PTT down. Expect preheating UI, then
      `audio.route{route:"hfp",deviceName:"<headset>"}` within 8 s
      (SCO cold start can take 1.7-3.5 s); `audio.state` reaches `recording`
      with speech; release restores phone audio and `audio.stopped` arrives.
      Music playback must continue uninterrupted (never stops/changes volume).
- [ ] **B2 LE Audio (Android 13+ / API 33).** Repeat with an LE Audio headset.
      Expect `audio.route{route:"le",...}` when the headset is listed as
      `TYPE_BLE_HEADSET`; if the system exposes only HFP, expect `hfp` (also
      acceptable). Record which type the ROM reports
      (`adb shell dumpsys audio | grep -i ble`).
- [ ] **B3 headset off / unreachable.** PTT down with the headset off or far
      away. Expect within ~8 s: `audio.route{route:"phone"}` +
      `audio.error{code:"route_failed",retryable:true}` (log `bt.route_failed`),
      and recording continues on the phone mic (speech still transcribes).
- [ ] **B4 BLUETOOTH_CONNECT denied (API 31+).** Revoke
      `android.permission.BLUETOOTH_CONNECT`, PTT. Expect
      `route_failed` with the "bluetooth permission is not granted" message,
      phone-mic fallback, no crash; grant and retry routes correctly.
- [ ] **B5 quick tap before route settles.** Tap PTT < 1 s with a headset
      connected. Expect no stuck `audio.route`/notification; `releaseRoute()`
      clears the communication device; verify
      `window.DshNative.post({op:'routeInfo'})` returns `phone` afterwards and
      the next PTT still routes to the headset.
- [ ] **B6 route switch during an active stream.** Hold PTT for >10 s while the
      headset connects. `AudioSession.restartRecorderForRoute()` rebuilds the
      recorder on `dsh-audio-ctrl` (150 ms settle). Expect continuous speech,
      no `audio.error`, `aseq` continues monotonically, and the server's final
      transcript contains the whole utterance. A failed rebuild now emits
      `audio.error{code:"recorder_failed"}` and stops the stream instead of
      stalling silently (G2, fixed).

### C. Foreground service and activity lifecycle (`MicService`)

- [ ] **C1 background keep-alive (5 min).** Start a PTT, release, press Home and
      lock the screen. Notification stays in the "ready" text for 5 min, then
      the service self-stops (log `service.destroyed`) while reconnecting WSS
      state is preserved per protocol. Returning to the app re-arms everything.
- [ ] **C2 recording with the screen off.** In continuous mode (P1) record with
      the screen locked for >=2 min. Expect mic frames to keep flowing
      (`audio.level` events, server `asr.partial`), log has no
      `service.foreground_failed`.
- [ ] **C3 service killed while recording.** During a held PTT run
      `adb shell am stopservice com.dsh.phone/.service.MicService`. Expect log
      `service.destroyed`, no crash, and the recording either stops or hits the
      120 s cap; the next PTT must restart the foreground service and record
      again. If the stream keeps running with no notification, that is gap
      **G4** (known).
- [ ] **C4 Back/finish while recording.** During a held PTT press Back/goto
      launcher so `MainActivity.isFinishing` is true. Expect `audio.stopped` /
      `audio.state idle` immediately, capture to stop within a few hundred ms
      (no orphan notification, no capture running to the 120 s cap) and the log
      line `app.finish_stop_audio` (G5, fixed). The next launch must record
      normally again.
- [ ] **C5 notification contract.** Notification shows the mic icon, the
      recording/ready text, is ongoing, and tapping it opens the app without
      recreating the WebView (singleTask). On Android 13+ it only appears when
      POST_NOTIFICATIONS is granted (C0: deny it once and confirm the FGS still
      records while permission-denied is surfaced to the Web UI).

### D. WSS / ASR interplay (`WssClient` / `AudioSession`)

- [ ] **D1 `audio.ready` latency / never arrives.** Throttle the Bridge or put a
      breakpoint at the server: expect `audio.state` `starting` while frames
      are buffered (max 150 = 3 s, then the oldest are dropped) until
      `audio.ready` flushes in order. If `audio.ready` does not arrive within
      3 s, expect `audio.error{code:"audio_timeout",retryable:true}`, the log
      line `audio.ack_timeout`, then `audio.stopped` + `idle` (G6, fixed).
      Speech captured while waiting may still be lost server-side; record how
      much.
- [ ] **D2 server `audio.error` (2001 busy / 2002 ASR unavailable).** Trigger
      with a second concurrent stream and with ASR disabled. Expect
      `audio.error{code:2002|2001,retryable:...}` from `WssClient`, then
      `audio.state idle` (note: `onTransportAudioError` calls
      `stopInternal(false)`, so no `audio.stopped` event in this path).
- [ ] **D3 network drop mid-PTT (two distinct paths, D-023).**
      **(a) Normal loss** (airplane mode, Wi-Fi drop, Bridge process killed):
      OkHttp `onFailure` fires immediately. Expect
      `audio.error{code:"connection_lost",retryable:true}`, stream stop,
      `net.state reconnecting`, log `ws.failure` followed by backoff
      1/2/4/8/15 s ±20 % (`ws.reconnect_scheduled`), then auth +
      `resync{lastSeq}` (`ws.resync_sent`) and no duplicated `chat.send`.
      Acceptance per PRD §15 counts **network-restored -> `net.state online`
      within <= 5 s** (measure it).
      **(b) Black hole** (cable pulled / NAT drops the flow without a RST):
      there is no immediate error. OkHttp's WebSocket `pingInterval(15 s)`
      pings; without a pong within two ping periods it closes the socket ->
      `onFailure` -> reconnect. Expected detection 30-45 s; the app-layer 45 s
      watchdog (`ws.heartbeat_timeout`) is only a fallback and must not be the
      normal reconnect path. Record the measured detection window separately;
      do not judge case (b) against the 5 s PRD target, and report it for the
      M3 decision (D-023 allows lowering `pingInterval` to 10 s).
- [ ] **D4 heartbeat timeout path.** Let the server stop responding (block the
      port) while online. After ~45 s of silence expect `ws.heartbeat_timeout`,
      a reconnect cycle, and recovery. No infinite `connecting` loop
      (`ws.connect_timeout` after 15 s).
- [ ] **D5 aseq continuity.** After B6 (route rebuild) and after D3 (reconnect),
      inspect the server-side frames: `aseq` restarts at 1 only on a new
      `audio.start`; within one stream it is monotonic with no duplicates and
      no gaps except the route/buffer window.

### E. Log and export evidence

- [ ] **E1 redacted export.** Diagnostics -> export logs; the shown text must
      not contain `token`, `deviceToken`, `hmac`, `nonce`, message bodies or PCM
      (grep the export, and grep `files/logs/*.jsonl` on the device). Then
      `adb logcat -d -s DshPhone:V` must also be clean of the same secrets.
- [ ] **E2 structural log sanity.** Every line is valid JSON with `ts`, `level`,
      `event`; noise events (`ws.binary_in`) are debug level only; no field is
      longer than 512 chars; the export is truncated cleanly (`truncated` in
      `logs.exported`).

### Known gaps: fix status (static review, updated m00516)

| ID | Location | Risk found | Fix landed / status |
|---|---|---|---|
| G1 | `AudioSession.kt` start path | synchronous `AudioRecorder.start()` failure reported `onError` via `handler.post` after the state returned to `IDLE`, so no `audio.error` reached the UI | **Fixed** — `reportRecorderFailure()` emits `audio.error{code:"recorder_failed"}` + `LogStore.error("audio.recorder_failed")` once per stream; covered by **A2** |
| G2 | `AudioSession.kt` route rebuild | `restartRecorderForRoute()` ignored the `recorder.start()` result: a failed rebuild left the stream "recording" with no frames and no event | **Fixed** — failed rebuild reports `recorder_failed`, stops the stream; covered by **B6** |
| G3 | `BtRouter.kt` device pick | one communication device is picked (LE preferred); if `setCommunicationDevice()` returns false there is no HFP retry | **Accepted risk for V1 (D-024, 真机待验)** — see below; exercised by **B2/B3** |
| G4 | `MicService.kt` onDestroy | `onDestroy` leaves `recordingActive = true` and never tells `AudioSession`; a system-killed FGS can leave capture/notification inconsistent | **Accepted risk for V1 (D-024, 真机待验)** — see below; exercised by **C3** |
| G5 | `MainActivity.kt` finish | finishing the activity detached the event sink but did not stop audio: capture could continue to the 120 s cap with an orphan notification | **Fixed** — `isFinishing` calls `AppCore.stopAudio()` + logs `app.finish_stop_audio`; covered by **C4** |
| G6 | `AudioSession.kt` audio.ready wait | no timer for a missing `audio.ready`: frames buffered 3 s then dropped, UI learned only via `audio.state` | **Fixed** — 3 s ack timeout emits `audio.error{code:"audio_timeout"}` + logs `audio.ack_timeout`, then stops; covered by **D1** |
| G7 | `AudioRecorder.kt` read loop | read-failure path emitted `audio.error` but wrote no `DshPhone` log line | **Fixed** — `LogStore.warn("audio.read_failed", "errors" to n)` once per stream; covered by **A3** |

### Accepted risks for V1 (D-024)

These two gaps are known, bounded, and deliberately not fixed in this shell
change: they need real hardware to validate a fix, and both degrade to a
recoverable state. `docs/DECISIONS.md` D-024 records the acceptance and points
at this section.

- **G3: no LE-to-HFP retry in `BtRouter`.** `BtRouter.kt` picks one
  communication device in order (preferred address -> LE `TYPE_BLE_HEADSET` on
  API 33+ -> `TYPE_BLUETOOTH_SCO`). If `setCommunicationDevice()` returns false
  for the LE device, the turn falls back to the phone microphone with
  `audio.error{code:"route_failed"}` instead of retrying the HFP device once.
  Impact: on ROMs that advertise a stale LE communication route, one PTT may
  use the phone mic even though an HFP headset is connected; the utterance is
  still captured, never lost. M3 detection: B2/B3 plus
  `adb shell dumpsys audio | grep -i ble`; if reproduced, capture the
  `bt.route_failed` log line and the device type. Planned fix (post-M3): retry
  the HFP device once when the LE switch fails, then fall back to phone.
- **G4: `MicService.onDestroy` does not reconcile capture state.**
  `MicService.kt` `onDestroy` clears `instance` but leaves `recordingActive`
  true and does not notify `AudioSession`, so a system-killed FGS can leave a
  stale "recording" flag/notification until the next turn. Impact: cosmetic
  notification/state inconsistency at worst; no runaway capture is possible
  beyond the existing 120 s cap and the activity-finish stop (G5). M3
  detection: C3 (`am stopservice` while recording) and a natural OEM kill if
  observed; recovery is force-stop/relaunch. Planned fix (post-M3): on
  destroy, clear the flag and stop the active stream through `AppCore`.

### M3 sign-off gate (subset of the above)

- [ ] A1, A2, A3, A4, A5 pass on the three-device matrix (Android 12/14/15).
- [ ] B1, B3, B5, B6 pass with HFP; B2 records LE vs HFP behaviour.
- [ ] C1, C2, C4, C5 pass; C3 records the current accepted-risk G4 behaviour.
- [ ] D1, D3 (both cases), D4, D5 pass with measured timings in the M3 report.
- [ ] E1, E2 pass; export attached to the M3 report.
- [ ] G1/G2/G5/G6/G7 re-verified by the static checks and by the scenarios
      above; G3/G4 reproduced (or not) and the D-024 acceptance re-confirmed
      by the Lead.

## Deterministic self-checks (no build required — run before pushing)

A local build is possible now (BUILD-LOCAL.md / D-027), but these four Python
checks (`tools/`, standard library only, no network) stay as the fast gate: they
catch contract/constant drift that a successful compile would not. Run them all
before pushing:

```powershell
# 1. Protocol constants + test vectors (PROTOCOL v1 sections 5-8, 11)
python app/android/tools/verify_protocol.py

# 2. JsBridge contract: Web NativeTransport <-> Android <-> ARCHITECTURE 3.2
python app/android/tools/check_contract.py

# 3. Kotlin static risk scan (APIs, bridge surface, FGS type, API guards, secrets, logging)
python app/android/tools/check_kotlin_static.py

# 4. Gradle / AGP / Manifest / JDK consistency and repo hygiene
python app/android/tools/check_gradle_config.py
```

Expected results on a clean checkout (Windows, PYTHONIOENCODING=utf-8):

- `verify_protocol.py` -> `OK: 69 protocol checks passed (pure logic cross-check, no build performed)`
- `check_contract.py` -> `OK: 16 web ops / 16 native ops (docs 15), 16 web events / 15 native events, 11 documented return events (3 warning(s))`.
  The three warnings are known docs drift (owner: docs, not this module):
  `pair.scanned` vs the `scanPairing.result` event actually emitted by both
  native and Web; the `disconnect` op used by Web but absent from the
  ARCHITECTURE 3.2 table; and the legacy `scan.result` Web alias.
- `check_kotlin_static.py` -> `OK: 20 Kotlin files scanned, 0 failures, 0 warnings`.
  It catches what would otherwise only fail in CI or on the first device:
  `java.net.http`/AndroidX usage, Java statics used without qualification
  (`START_NOT_STICKY` etc. are not inherited in Kotlin), a missing
  `@JavascriptInterface`/`DshNative`/`DshBridge.onEvent` surface,
  `startForeground` without `FOREGROUND_SERVICE_TYPE_MICROPHONE`, `AudioRecord`
  without a `STATE_INITIALIZED` check, API-31/33 calls without
  `Build.VERSION.SDK_INT` guards, cleartext `http://`/`ws://` literals,
  sensitive fields in log calls, and plaintext token storage.
- `check_gradle_config.py` -> `OK: 63 configuration checks passed, 0 warning(s)`.
  It pins AGP 8.7.3 <-> Gradle 8.9 <-> JDK 17 <-> compileSdk/targetSdk 35 <->
  minSdk 29, verifies `namespace`/`applicationId`, zero-AndroidX policy,
  manifest permissions vs `foregroundServiceType="microphone"`, exported
  activity/service flags, debug non-minified, `BuildConfig`, the CI
  `-Pdsphone.versionName/versionCode` injection contract, wrapper distribution,
  and that no `local.properties` / keystore / `*.apk` / `gradle-wrapper.jar` is
  present.

Limitations: these are static checks. They cannot replace the Kotlin compiler,
resource merging, R8, the OkHttp TLS/pinning handshake, `AudioRecord` DSP
behaviour, Bluetooth routing, Keystore, or the WebView virtual origin. CI
(workflow in `.github/workflows/ci.yml`) remains the authority for the build;
the scripts are cheap tripwires so a red CI run is the exception, not the
debug loop.

## CI additions still needed

The current v1 workflow builds only `assembleDebug`. To exercise this module
fully, CI should additionally run `./gradlew testDebugUnitTest` (JVM tests) after
the build, and ideally `python3 app/android/tools/verify_protocol.py`. Required
SDK components are the three entries listed above; no NDK, no emulator.
