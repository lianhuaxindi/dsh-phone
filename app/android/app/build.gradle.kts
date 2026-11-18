plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

// CI 注入版本（ci/version.json）：gradle ... -Pdsphone.versionName=0.2.0 -Pdsphone.versionCode=7
// v1.1 默认版本（D-033）：versionName 1.1.0 / versionCode 10100，仍可被 -P 覆盖。
val dsphoneVersionName: String = (findProperty("dsphone.versionName") as String?) ?: "1.1.0"
val dsphoneVersionCode: Int = ((findProperty("dsphone.versionCode") as String?) ?: "10100").toIntOrNull() ?: 10100

android {
    namespace = "com.dsh.phone"
    compileSdk = 35
    // 显式钉住 Build Tools：AGP 8.7.x 的默认修订号是 34.0.0，而本仓库基线只安装
    // build-tools;35.0.0（本机 .tools 与 CI sdkmanager 步骤一致）。不钉住会在没有
    // 34.0.0 的干净机器上以 "Failed to find Build Tools revision 34.0.0" 失败。
    buildToolsVersion = "35.0.0"

    defaultConfig {
        applicationId = "com.dsh.phone"
        minSdk = 29
        targetSdk = 35
        versionCode = if (dsphoneVersionCode >= 1) dsphoneVersionCode else 1
        versionName = dsphoneVersionName
    }

    buildFeatures {
        buildConfig = true
    }

    buildTypes {
        debug {
            // 调试包不混淆，方便 CI 产物直接排查问题（D-004：debug APK 侧载）。
            isMinifyEnabled = false
            isShrinkResources = false
        }
        release {
            // V1 不发布 release 到商店；先保持不混淆，规则文件已备好。
            isMinifyEnabled = false
            isShrinkResources = false
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro"
            )
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }

    testOptions {
        unitTests.isIncludeAndroidResources = false
    }

    lint {
        abortOnError = false
        checkReleaseBuilds = false
    }
}

dependencies {
    // 唯一网络运行时依赖：WSS 客户端（java.net.http 在 Android 上不可用，只能 OkHttp）。
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    // v1.1 扫码配对：ZXing core 纯本地解码（~0.5MB，无 GMS、无联网），D-030。
    implementation("com.google.zxing:core:3.5.3")
    // JVM 单元测试（帧头、鉴权、退避、协议 JSON、脱敏等纯逻辑）。
    testImplementation("junit:junit:4.13.2")
}
