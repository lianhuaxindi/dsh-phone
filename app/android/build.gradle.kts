// Root build script: 只声明插件版本，不 apply。
// 版本矩阵：AGP 8.7.3 ⇔ Gradle 8.9+，Kotlin 2.0.21，JDK 17，compileSdk 35。
plugins {
    id("com.android.application") version "8.7.3" apply false
    id("org.jetbrains.kotlin.android") version "2.0.21" apply false
}
