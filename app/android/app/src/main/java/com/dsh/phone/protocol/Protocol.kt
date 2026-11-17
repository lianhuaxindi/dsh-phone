package com.dsh.phone.protocol

import com.dsh.phone.util.Hex
import com.dsh.phone.util.JsonObj
import java.security.SecureRandom
import java.util.Base64
import javax.crypto.Mac
import javax.crypto.spec.SecretKeySpec

/**
 * Protocol v1 wire builders (PROTOCOL.md, frozen).
 *
 * All outbound JSON is constructed here so that field names/order are fixed and
 * unit-testable. Sequence numbers are injected by [com.dsh.phone.net.WssClient],
 * the single C→S seq authority; these builders require the caller to pass a seq.
 */
object Protocol {

    const val VERSION = 1

    // RFC 6455 subprotocol tokens cannot contain '/', so the agreed v1 name is
    // `dsphone.v1` (DECISIONS D-015). The old draft `dsphone/1` is invalid and
    // would be rejected by compliant WebSocket clients/servers.
    const val SUBPROTOCOL = "dsphone.v1"
    const val APP_VERSION_FALLBACK = "1.0.0"

    const val MAX_TEXT_CHARS = 32000
    const val MAX_JSON_BYTES = 65536
    const val MAX_BINARY_BYTES = 8192

    const val AUDIO_SAMPLE_RATE = 16000
    const val AUDIO_CHANNELS = 1
    const val AUDIO_BIT_DEPTH = 16
    const val AUDIO_FRAME_MS = 20
    const val AUDIO_FRAME_BYTES = 640
    const val AUDIO_MAX_STREAM_MS = 120_000L

    /** HMAC canonical string for `auth` (PROTOCOL §6.2): no trailing newline. */
    fun authCanonical(deviceId: String, tsSec: Long, nonceBase64: String): String =
        "DSPHONE1\n$deviceId\n$tsSec\n$nonceBase64"

    fun randomNonceBase64(): String {
        val bytes = ByteArray(16)
        SecureRandom().nextBytes(bytes)
        return Base64.getEncoder().encodeToString(bytes)
    }

    fun hmacBase64(token: ByteArray, canonical: String): String {
        val mac = Mac.getInstance("HmacSHA256")
        mac.init(SecretKeySpec(token, "HmacSHA256"))
        return Base64.getEncoder().encodeToString(mac.doFinal(canonical.toByteArray(Charsets.UTF_8)))
    }

    fun hmacHex(token: ByteArray, canonical: String): String {
        val mac = Mac.getInstance("HmacSHA256")
        mac.init(SecretKeySpec(token, "HmacSHA256"))
        return Hex.encode(mac.doFinal(canonical.toByteArray(Charsets.UTF_8)))
    }

    fun decodeBase64(text: String): ByteArray = Base64.getDecoder().decode(text)

    fun encodeBase64(bytes: ByteArray): String = Base64.getEncoder().encodeToString(bytes)

    fun hello(seq: Int, deviceId: String, appVersion: String, tsSec: Long): String =
        JsonObj()
            .put("type", "hello")
            .put("seq", seq)
            .put("v", VERSION)
            .put("deviceId", deviceId)
            .put("app", appVersion)
            .put("os", "android")
            .put("ts", tsSec)
            .toJsonText()

    fun pairRequest(seq: Int, deviceId: String, code: String, deviceName: String): String =
        JsonObj()
            .put("type", "pair.request")
            .put("seq", seq)
            .put("deviceId", deviceId)
            .put("code", code)
            .put("deviceName", deviceName)
            .putNull("pubKey")
            .toJsonText()

    fun auth(
        seq: Int,
        deviceId: String,
        tsSec: Long,
        nonceBase64: String,
        hmacBase64: String,
    ): String =
        JsonObj()
            .put("type", "auth")
            .put("seq", seq)
            .put("deviceId", deviceId)
            .put("ts", tsSec)
            .put("nonce", nonceBase64)
            .put("hmac", hmacBase64)
            .put("v", VERSION)
            .toJsonText()

    fun ping(seq: Int, tsSec: Long): String =
        JsonObj()
            .put("type", "ping")
            .put("seq", seq)
            .put("ts", tsSec)
            .toJsonText()

    fun resync(seq: Int, lastSeq: Int): String =
        JsonObj()
            .put("type", "resync")
            .put("seq", seq)
            .put("lastSeq", lastSeq)
            .toJsonText()

    fun audioStart(
        seq: Int,
        sampleRate: Int,
        channels: Int,
        bitDepth: Int,
        lang: String,
        mode: String,
        sessionId: String?,
    ): String {
        val obj = JsonObj()
            .put("type", "audio.start")
            .put("seq", seq)
            .put("sampleRate", sampleRate)
            .put("channels", channels)
            .put("bitDepth", bitDepth)
            .put("lang", lang)
            .put("mode", mode)
        if (sessionId != null) obj.put("sessionId", sessionId)
        return obj.toJsonText()
    }

    fun audioStop(seq: Int, aseq: Int): String =
        JsonObj()
            .put("type", "audio.stop")
            .put("seq", seq)
            .put("aseq", aseq)
            .toJsonText()
}
