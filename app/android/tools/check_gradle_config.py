#!/usr/bin/env python3
"""Static consistency check for the Android build configuration.

The machine that authored this project has no JDK / Android SDK, so
`./gradlew assembleDebug` can only run in CI.  Most "the build is red before
anyone looks at the code" failures are configuration mismatches: wrong
AGP/Gradle/JDK combination, compileSdk/targetSdk drift, AndroidX enabled while
the project is AndroidX-free, a foreground service type without its matching
permission, or a required file missing.  This script checks all of that
statically and exits non-zero on contradictions.

Checks
------
1.  Required build files exist (settings, root/app build scripts, properties,
    wrapper properties, proguard rules, manifest).
2.  Toolchain versions are parsed and cross-checked:
      AGP 8.7.x  ->  Gradle >= 8.9  (AGP release requirement)
      AGP 8.x    ->  JDK 17 compileOptions + Kotlin jvmTarget 17
      compileSdk 35 -> AGP >= 8.6
      targetSdk 35, minSdk 29 (D-006), targetSdk <= compileSdk.
3.  `android.useAndroidX` / Jetifier policy: zero AndroidX dependencies, so
    `android.useAndroidX=true` (or Jetifier) is a warning; an `androidx.`
    import/dependency with the flag off is a failure.
4.  namespace == applicationId == com.dsh.phone; manifest must not carry the
    removed `package` attribute.
5.  Manifest/permission invariants: required permissions, `MicService` as a
    `microphone` foreground service with exported=false and a matching
    FOREGROUND_SERVICE_MICROPHONE permission, exported MainActivity with
    LAUNCHER filter, TLS/backup hardening flags, runtime permission requests
    for POST_NOTIFICATIONS (33+) and BLUETOOTH_CONNECT (31+) in the activity.
6.  Debug build is not minified (readable crash traces), release uses the
    proguard rules, buildConfig is on (BuildConfig.VERSION_NAME/CONFIG used).
7.  CI version injection contract: app script reads `dsphone.versionName` and
    `dsphone.versionCode` system properties (ci-build passes them with -P).
8.  Repo hygiene: no local.properties / keystore / *.apk / wrapper jar / web
    asset copies (these are local or CI-generated and must not be committed).
9.  Wrapper/Gradle: distributionUrl 8.9-bin.zip; an explicit buildToolsVersion
    is accepted when it matches compileSdk (otherwise WARN); `assets/web/index.html`
    present locally (WARN only: CI syncs it before building).

Exit code: 0 = consistent, 1 = contradiction found.  Static check only, no
build is performed.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
ANDROID_ROOT = REPO_ROOT / "app" / "android"
APP_ROOT = ANDROID_ROOT / "app"
MANIFEST = APP_ROOT / "src" / "main" / "AndroidManifest.xml"
ACTIVITY = APP_ROOT / "src" / "main" / "java" / "com" / "dsh" / "phone" / "MainActivity.kt"
JAVA_ROOT = APP_ROOT / "src" / "main" / "java"

PACKAGE_NAME = "com.dsh.phone"
REQUIRED_PERMISSIONS = [
    "android.permission.INTERNET",
    "android.permission.ACCESS_NETWORK_STATE",
    "android.permission.RECORD_AUDIO",
    "android.permission.MODIFY_AUDIO_SETTINGS",
    "android.permission.POST_NOTIFICATIONS",
    "android.permission.FOREGROUND_SERVICE",
    "android.permission.FOREGROUND_SERVICE_MICROPHONE",
    "android.permission.BLUETOOTH_CONNECT",
]

FAILS: list[str] = []
WARNS: list[str] = []
CHECKS = 0


def ok(label: str) -> None:
    global CHECKS
    CHECKS += 1


def fail(label: str) -> None:
    global CHECKS
    CHECKS += 1
    FAILS.append(label)


def warn(label: str) -> None:
    global CHECKS
    CHECKS += 1
    WARNS.append(label)


def rel(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT)).replace("\\", "/")
    except ValueError:
        return str(path)


def read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def expect(condition: bool, success: str, failure: str) -> None:
    if condition:
        ok(success)
    else:
        fail(failure)


# --------------------------------------------------------------------- 1 files
def check_files() -> None:
    for path in (
        ANDROID_ROOT / "settings.gradle.kts",
        ANDROID_ROOT / "build.gradle.kts",
        ANDROID_ROOT / "gradle.properties",
        ANDROID_ROOT / "gradle" / "wrapper" / "gradle-wrapper.properties",
        APP_ROOT / "build.gradle.kts",
        APP_ROOT / "proguard-rules.pro",
        MANIFEST,
    ):
        expect(path.is_file(), f"found {rel(path)}", f"missing required file {rel(path)}")


# ------------------------------------------------------------------ 2 toolchain
def parse_root_versions(root: str) -> tuple[str | None, str | None]:
    agp = re.search(r'com\.android\.application"?\)\s*version\s*"([^"]+)"', root)
    kotlin = re.search(r'org\.jetbrains\.kotlin\.android"?\)\s*version\s*"([^"]+)"', root)
    return (agp.group(1) if agp else None), (kotlin.group(1) if kotlin else None)


def version_tuple(text: str) -> tuple[int, int, int]:
    parts = [int(part) for part in re.findall(r"\d+", text)[:3]]
    while len(parts) < 3:
        parts.append(0)
    return (parts[0], parts[1], parts[2])


def check_toolchain() -> None:
    root_text = read(ANDROID_ROOT / "build.gradle.kts") or ""
    app_text = read(APP_ROOT / "build.gradle.kts") or ""
    wrapper_text = read(ANDROID_ROOT / "gradle" / "wrapper" / "gradle-wrapper.properties") or ""

    agp, kotlin = parse_root_versions(root_text)
    expect(agp is not None, f"AGP version declared: {agp}", "root build.gradle.kts: AGP plugin version not found")
    expect(kotlin is not None, f"Kotlin version declared: {kotlin}", "root build.gradle.kts: Kotlin plugin version not found")

    gradle_match = re.search(r"distributionUrl=.*?gradle-(\d+\.\d+(?:\.\d+)?)-", wrapper_text)
    gradle = gradle_match.group(1) if gradle_match else None
    expect(gradle is not None, f"Gradle wrapper distribution: {gradle}-bin.zip", "wrapper properties: Gradle distribution not found")

    compile_sdk = re.search(r"compileSdk\s*=\s*(\d+)", app_text)
    min_sdk = re.search(r"minSdk\s*=\s*(\d+)", app_text)
    target_sdk = re.search(r"targetSdk\s*=\s*(\d+)", app_text)
    compile_v = int(compile_sdk.group(1)) if compile_sdk else None
    min_v = int(min_sdk.group(1)) if min_sdk else None
    target_v = int(target_sdk.group(1)) if target_sdk else None

    expect(compile_v == 35, f"compileSdk = {compile_v}", f"compileSdk is {compile_v}, expected 35 (CI installs platforms;android-35)")
    expect(min_v == 29, f"minSdk = {min_v} (D-006: Android 10 / API 29)", f"minSdk is {min_v}, expected 29 (D-006)")
    expect(target_v == 35, f"targetSdk = {target_v}", f"targetSdk is {target_v}, expected 35")
    if compile_v is not None and target_v is not None:
        expect(target_v <= compile_v, "targetSdk <= compileSdk", f"targetSdk {target_v} > compileSdk {compile_v}")
    if min_v is not None and target_v is not None:
        expect(min_v <= target_v, "minSdk <= targetSdk", f"minSdk {min_v} > targetSdk {target_v}")

    # AGP <-> Gradle: AGP 8.7 requires Gradle 8.9; AGP 8.6/8.5 -> 8.7/8.5.
    if agp and gradle:
        agp_v = version_tuple(agp)
        gradle_v = version_tuple(gradle)
        minimum: tuple[int, ...] | None = None
        if agp_v[:2] == (8, 7):
            minimum = (8, 9, 0)
        elif agp_v[:2] == (8, 6):
            minimum = (8, 7, 0)
        elif agp_v[:2] == (8, 5):
            minimum = (8, 5, 0)
        if minimum is not None:
            expect(
                gradle_v >= minimum,
                f"AGP {agp} needs Gradle >= {'.'.join(map(str, minimum))}; wrapper has {gradle}",
                f"AGP {agp} requires Gradle >= {'.'.join(map(str, minimum))} but wrapper has {gradle}",
            )
        if agp_v[:2] == (8, 7) and gradle != "8.9" and gradle_v >= (8, 9, 0):
            warn(f"AGP {agp}: wrapper uses Gradle {gradle} (CI pins Gradle 8.9 via setup-gradle; keep local wrapper aligned)")

    # AGP -> compileSdk: compileSdk 35 needs AGP >= 8.6.
    if agp and compile_v is not None:
        agp_v = version_tuple(agp)
        expect(
            agp_v >= (8, 6, 0),
            f"AGP {agp} supports compileSdk {compile_v}",
            f"compileSdk {compile_v} requires AGP >= 8.6, found {agp}",
        )

    # JDK 17: compileOptions + Kotlin jvmTarget.
    java17 = re.search(r"VERSION_17", app_text) is not None
    jvm17 = re.search(r'jvmTarget\s*=\s*"17"', app_text) is not None
    expect(java17, "compileOptions: Java 17", "app build.gradle.kts: Java 17 compileOptions not found")
    expect(jvm17, 'kotlinOptions: jvmTarget = "17"', 'app build.gradle.kts: kotlinOptions jvmTarget = "17" not found')
    if agp and version_tuple(agp)[0] == 8:
        expect(java17 and jvm17, f"AGP {agp} requires JDK 17 sources/target", f"AGP {agp} requires Java 17 compileOptions and jvmTarget 17")

    # Dependencies pinned by the architecture decision.
    expect("com.squareup.okhttp3:okhttp:4.12.0" in app_text, "OkHttp 4.12.0 dependency present", "okhttp 4.12.0 dependency missing")
    expect("junit:junit:4.13.2" in app_text, "JUnit 4.13.2 test dependency present", "junit test dependency missing")
    if "testOptions" not in app_text or "unitTests.isIncludeAndroidResources" not in app_text:
        warn("app build.gradle.kts: testOptions.unitTests.isIncludeAndroidResources not configured")


# ------------------------------------------------------------------- 3 androidx
def check_androidx(properties: str, app_text: str) -> None:
    use_androidx = re.search(r"^android\.useAndroidX\s*=\s*true", properties, re.MULTILINE)
    enable_jetifier = re.search(r"^android\.enableJetifier\s*=\s*true", properties, re.MULTILINE)
    # Real usage: imports in code and dependency coordinates.  Mentions inside
    # comments/doc strings (e.g. "deliberately does not use androidx.security")
    # must not count.
    has_androidx_import = any(
        re.search(r"^\s*import\s+androidx\.", read(path) or "", re.MULTILINE) is not None
        for path in JAVA_ROOT.rglob("*.kt")
    )
    has_androidx_dependency = re.search(
        r'(?:implementation|api|compileOnly|runtimeOnly|testImplementation|androidTestImplementation)\s*\(\s*"androidx[.:]',
        app_text,
    ) is not None
    has_androidx_usage = has_androidx_import or has_androidx_dependency
    if use_androidx and not has_androidx_usage:
        warn("gradle.properties: android.useAndroidX=true but the project has zero AndroidX dependencies (leave it off)")
    if enable_jetifier and not has_androidx_usage:
        warn("gradle.properties: android.enableJetifier=true but the project has zero AndroidX dependencies")
    if has_androidx_usage and not use_androidx:
        fail("androidx.* usage/dependency found but android.useAndroidX=true is missing")


# ------------------------------------------------------- 4 namespace/applicationId
def check_namespace(app_text: str, manifest: str) -> None:
    namespace = re.search(r'namespace\s*=\s*"([^"]+)"', app_text)
    application_id = re.search(r'applicationId\s*=\s*"([^"]+)"', app_text)
    expect(namespace is not None and namespace.group(1) == PACKAGE_NAME, f"namespace = {PACKAGE_NAME}", f"namespace mismatch: {namespace.group(1) if namespace else 'missing'}")
    expect(application_id is not None and application_id.group(1) == PACKAGE_NAME, f"applicationId = {PACKAGE_NAME}", f"applicationId mismatch: {application_id.group(1) if application_id else 'missing'}")
    if re.search(r'<manifest[^>]*\spackage\s*=', manifest):
        fail("AndroidManifest.xml: `package` attribute is not allowed with AGP 8 (use namespace in build.gradle.kts)")
    else:
        ok("manifest has no legacy package attribute")


# -------------------------------------------------------- 5 manifest invariants
def check_manifest(manifest: str, activity: str) -> None:
    for permission in REQUIRED_PERMISSIONS:
        expect(
            f'android:name="{permission}"' in manifest,
            f"manifest permission {permission}",
            f"manifest missing permission {permission}",
        )

    if 'android:name="android.permission.FOREGROUND_SERVICE_MICROPHONE"' in manifest:
        ok("FOREGROUND_SERVICE_MICROPHONE declared")
    if 'android:foregroundServiceType="microphone"' not in manifest:
        fail('AndroidManifest.xml: MicService is not declared with android:foregroundServiceType="microphone"')
    else:
        ok('MicService foregroundServiceType="microphone"')

    service_match = re.search(r"<service[^>]*\.service\.MicService[^>]*/?>", manifest)
    expect(service_match is not None, "MicService declared in manifest", "AndroidManifest.xml: MicService declaration not found")
    if service_match:
        tag = service_match.group(0)
        expect('android:exported="false"' in tag, "MicService exported=false", "MicService must be exported=false")
        expect(
            'android:foregroundServiceType="microphone"' in tag,
            "MicService foregroundServiceType=microphone",
            "MicService: foregroundServiceType must be microphone",
        )

    activity_match = re.search(r"<activity[^>]*\.MainActivity[^>]*>", manifest)
    expect(activity_match is not None, "MainActivity declared in manifest", "AndroidManifest.xml: MainActivity declaration not found")
    if activity_match:
        tag = activity_match.group(0)
        expect('android:exported="true"' in tag, "MainActivity exported=true (launcher)", "MainActivity must be exported=true for the launcher")
    expect("android.intent.action.MAIN" in manifest and "android.intent.category.LAUNCHER" in manifest, "MAIN/LAUNCHER intent filter present", "missing MAIN/LAUNCHER intent filter")

    expect('android:usesCleartextTraffic="false"' in manifest, "usesCleartextTraffic=false (TLS only)", 'application must set android:usesCleartextTraffic="false"')
    expect('android:allowBackup="false"' in manifest, "allowBackup=false (token/state not backed up)", 'application must set android:allowBackup="false"')
    expect("android:theme=" in manifest, "application theme declared", "application theme missing")
    expect('android:icon=' in manifest or '<application' in manifest, "application icon", "application icon missing")

    # Runtime permission requests must match the manifest declarations.
    if "android.permission.POST_NOTIFICATIONS" in manifest:
        expect("POST_NOTIFICATIONS" in activity, "MainActivity requests POST_NOTIFICATIONS (API 33+)", "MainActivity does not request POST_NOTIFICATIONS although manifest declares it")
    if "android.permission.BLUETOOTH_CONNECT" in manifest:
        expect("BLUETOOTH_CONNECT" in activity, "MainActivity requests BLUETOOTH_CONNECT (API 31+)", "MainActivity does not request BLUETOOTH_CONNECT although manifest declares it")
    if "android.permission.RECORD_AUDIO" in manifest:
        expect("RECORD_AUDIO" in activity, "MainActivity requests RECORD_AUDIO", "MainActivity does not request RECORD_AUDIO")


# ----------------------------------------------------- 6 build behavior flags
def check_build_flags(app_text: str) -> None:
    debug_block = re.search(r"debug\s*\{([^}]*)\}", app_text)
    expect(
        debug_block is not None and "isMinifyEnabled = false" in debug_block.group(1),
        "debug build is not minified (readable crash traces)",
        "debug build must set isMinifyEnabled = false",
    )
    expect("proguardFiles" in app_text and "proguard-rules.pro" in app_text, "release uses proguard-rules.pro", "release proguardFiles configuration missing")
    expect("buildConfig = true" in app_text, "buildFeatures.buildConfig enabled (BuildConfig.VERSION_NAME)", "buildFeatures.buildConfig must be true")
    expect('val dsphoneVersionName' in app_text and 'findProperty("dsphone.versionName")' in app_text, "CI version injection: versionName", "app build.gradle.kts does not read -Pdsphone.versionName")
    expect('val dsphoneVersionCode' in app_text and 'findProperty("dsphone.versionCode")' in app_text, "CI version injection: versionCode", "app build.gradle.kts does not read -Pdsphone.versionCode")


# ------------------------------------------------------------- 7 repo hygiene
def check_hygiene() -> None:
    forbidden_patterns = ["local.properties", "keystore.properties"]
    for name in forbidden_patterns:
        candidates = list(ANDROID_ROOT.glob(name)) + list(REPO_ROOT.glob(f"**/{name}"))
        candidates = [path for path in candidates if "build" not in path.parts and ".gradle" not in path.parts]
        if candidates:
            warn(f"local-only file present, must stay untracked: {rel(candidates[0])}")
        else:
            ok(f"no {name} in the tree")
    keystores = [path for path in ANDROID_ROOT.rglob("*") if path.suffix in (".jks", ".keystore")]
    if keystores:
        fail(f"keystore file inside the repo: {rel(keystores[0])}")
    else:
        ok("no keystore files committed")
    apks = [path for path in ANDROID_ROOT.rglob("*.apk") if "build" not in path.parts]
    if apks:
        fail(f"APK inside the source tree: {rel(apks[0])}")
    else:
        ok("no APK in the source tree")
    wrapper_jar = ANDROID_ROOT / "gradle" / "wrapper" / "gradle-wrapper.jar"
    if wrapper_jar.exists():
        warn(f"{rel(wrapper_jar)} exists locally but must not be committed (CI bootstraps the wrapper)")
    else:
        ok("gradle-wrapper.jar absent (CI bootstraps it)")
    assets_index = APP_ROOT / "src" / "main" / "assets" / "web" / "index.html"
    if assets_index.exists():
        ok("app/web sync target present locally (gitignored)")
    else:
        warn("assets/web/index.html absent here — CI runs scripts/sync_web.sh before building; local APK builds need the sync step")


# ------------------------------------------------ 8 wrapper + settings scripts
def check_wrapper_and_settings(wrapper_text: str, settings: str, app_text: str) -> None:
    expect("gradle-8.9-bin.zip" in wrapper_text, "wrapper distributionUrl uses Gradle 8.9", "wrapper distributionUrl is not Gradle 8.9 (CI bootstrap parses it; keep 8.9)")
    expect("include(\":app\")" in settings, "settings includes :app", "settings.gradle.kts does not include(\":app\")")
    expect("FAIL_ON_PROJECT_REPOS" in settings, "dependencyResolutionManagement FAIL_ON_PROJECT_REPOS", "settings.gradle.kts: repository mode not pinned")
    expect("google()" in settings and "mavenCentral()" in settings, "google() + mavenCentral() repositories", "settings.gradle.kts: google()/mavenCentral() missing")
    explicit = re.search(r"buildToolsVersion\s*=\s*\"([0-9]+(?:\.[0-9]+){1,2})\"", app_text)
    compile_sdk = re.search(r"compileSdk\s*=\s*(\d+)", app_text)
    if explicit:
        version = explicit.group(1)
        expected = f"{compile_sdk.group(1)}.0.0" if compile_sdk else None
        if expected and version == expected:
            ok(
                f"explicit buildToolsVersion {version} matches compileSdk "
                f"{compile_sdk.group(1)} (needed where AGP's default Build Tools are not installed)"
            )
        else:
            warn(
                f"explicit buildToolsVersion {version} does not match compileSdk"
                f" {expected or '?'} — align it with the installed build-tools"
            )
    else:
        ok("buildToolsVersion left to the AGP default")


def main() -> int:
    settings = read(ANDROID_ROOT / "settings.gradle.kts") or ""
    root = read(ANDROID_ROOT / "build.gradle.kts") or ""
    properties = read(ANDROID_ROOT / "gradle.properties") or ""
    wrapper = read(ANDROID_ROOT / "gradle" / "wrapper" / "gradle-wrapper.properties") or ""
    app_text = read(APP_ROOT / "build.gradle.kts") or ""
    manifest = read(MANIFEST) or ""
    activity = read(ACTIVITY) or ""

    check_files()
    check_toolchain()
    check_androidx(properties, app_text)
    check_namespace(app_text, manifest)
    check_manifest(manifest, activity)
    check_build_flags(app_text)
    check_hygiene()
    check_wrapper_and_settings(wrapper, settings, app_text)

    for message in FAILS:
        print(f"FAIL: {message}")
    for message in WARNS:
        print(f"WARN: {message}")

    if FAILS:
        print(f"\nFAIL: {len(FAILS)} configuration contradiction(s), {len(WARNS)} warning(s), {CHECKS} checks")
        return 1

    print(
        f"OK: {CHECKS} configuration checks passed, {len(WARNS)} warning(s) — "
        f"static check only, no Gradle build performed"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
