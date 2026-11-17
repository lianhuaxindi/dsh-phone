# dsh-phone Android shell ProGuard 规则
# 当前 debug/release 都未开启 minify，本文件作为 V1.1 开启混淆时的基线。

# OkHttp / Okio 通过反射访问平台类，保留其警告抑制即可（其自带 consumer rules 大部分场景够用）。
-dontwarn okhttp3.**
-dontwarn okio.**
-dontwarn org.conscrypt.**
-dontwarn org.bouncycastle.**
-dontwarn org.openjsse.**

# 被测纯逻辑对象由 JUnit 直接调用，保留名字便于堆栈排查。
-keepnames class com.dsh.phone.net.Protocol { *; }
-keepnames class com.dsh.phone.audio.AudioFrame { *; }
-keepnames class com.dsh.phone.util.JsonObj { *; }
