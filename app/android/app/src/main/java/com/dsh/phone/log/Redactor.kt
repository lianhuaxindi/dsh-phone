package com.dsh.phone.log

/**
 * Log redaction (ARCHITECTURE §5): tokens, HMACs, nonces, pairing codes and any
 * message/audio payload must never appear in exported logs.
 *
 * Field-name based masking keeps this dependency-free and unit-testable. Call it
 * on every line written by [LogStore] and again on the exported text, so that a
 * future log statement cannot leak by accident.
 */
object Redactor {

    const val REDACTED = "<redacted>"

    private val SENSITIVE_KEYS = listOf(
        "token",
        "deviceToken",
        "hmac",
        "nonce",
        "authorization",
        "password",
        "secret",
        "code",
        "text",
        "body",
        "content",
        "pcm",
        "payload",
        "audio",
        "json",
        "data",
    )

    /** Mask values of sensitive JSON field names in [json]; other fields stay intact. */
    fun redactJson(json: String): String {
        var out = json
        for (key in SENSITIVE_KEYS) {
            val pattern = "\"" + Regex.escape(key) + "\"\\s*:\\s*(\"(?:[^\"\\\\]|\\\\.)*\"|[^,}\\]]+)"
            out = Regex(pattern).replace(out) { "\"$key\":\"$REDACTED\"" }
        }
        return out
    }

    /** Redact a free-form log chunk (journal lines, stack traces, export tail). */
    fun redactText(text: String): String = redactJson(text)
}
