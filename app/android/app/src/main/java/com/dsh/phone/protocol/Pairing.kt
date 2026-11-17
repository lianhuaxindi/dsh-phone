package com.dsh.phone.protocol

import java.net.URI
import java.net.URLDecoder

/**
 * Pairing URI parsing (PROTOCOL §6.1):
 *
 * ```
 * dsphone://pair?v=1&host=192.168.1.10&port=8765&fp=<64 hex>&code=123456&sid=srv-1a2b
 * ```
 *
 * `code` and `sid` are optional for the URI itself (the code may be typed by the
 * user); `host`, `port` and `fp` are required. `host` must not be 0.0.0.0.
 */
object Pairing {

    const val SCHEME = "dsphone"
    const val PROTOCOL_VERSION = 1
    private const val MAX_PORT = 65535

    data class PairInfo(
        val host: String,
        val port: Int,
        val fingerprint: String,
        val code: String?,
        val serverId: String?,
        val version: Int,
    )

    fun normalizeFingerprint(raw: String): String {
        var fp = raw.trim().lowercase()
        if (fp.startsWith("sha256/")) fp = fp.substring("sha256/".length)
        if (fp.startsWith("sha256:")) fp = fp.substring("sha256:".length)
        fp = fp.replace(":", "").replace(" ", "")
        if (fp.length != 64) {
            throw IllegalArgumentException("fingerprint must be 64 hex chars, got ${fp.length}")
        }
        for (ch in fp) {
            val ok = (ch in '0'..'9') || (ch in 'a'..'f')
            if (!ok) throw IllegalArgumentException("fingerprint must be lowercase hex")
        }
        return fp
    }

    fun isValidFingerprint(raw: String?): Boolean {
        if (raw == null) return false
        return try {
            normalizeFingerprint(raw)
            true
        } catch (e: IllegalArgumentException) {
            false
        }
    }

    /** Parse and validate a pairing URI. @throws IllegalArgumentException on invalid input. */
    fun parseUri(uriText: String): PairInfo {
        val uri = try {
            URI(uriText.trim())
        } catch (e: Exception) {
            throw IllegalArgumentException("invalid pairing URI: ${e.message}")
        }
        if (!SCHEME.equals(uri.scheme, ignoreCase = true)) {
            throw IllegalArgumentException("unsupported scheme: ${uri.scheme}")
        }
        if (!"pair".equals(uri.host ?: uri.authority, ignoreCase = true)) {
            throw IllegalArgumentException("unsupported pairing target: ${uri.host ?: uri.authority}")
        }

        val params = HashMap<String, String>()
        val query = uri.rawQuery
        if (query == null || query.isEmpty()) {
            throw IllegalArgumentException("pairing URI has no parameters")
        }
        for (part in query.split('&')) {
            if (part.isEmpty()) continue
            val idx = part.indexOf('=')
            if (idx <= 0) continue
            val key = decode(part.substring(0, idx))
            val value = decode(part.substring(idx + 1))
            params[key] = value
        }

        val version = params["v"]?.toIntOrNull()
        if (version != PROTOCOL_VERSION) {
            throw IllegalArgumentException("unsupported pairing version: ${params["v"]}")
        }
        val host = params["host"].orEmpty().trim()
        if (host.isEmpty()) throw IllegalArgumentException("pairing URI is missing host")
        if (host == "0.0.0.0") throw IllegalArgumentException("host must not be 0.0.0.0")
        val port = params["port"]?.toIntOrNull()
        if (port == null || port < 1 || port > MAX_PORT) {
            throw IllegalArgumentException("invalid port: ${params["port"]}")
        }
        val fingerprint = normalizeFingerprint(params["fp"].orEmpty())
        val code = params["code"]?.trim()?.takeIf { it.isNotEmpty() }
        if (code != null && !isPairingCode(code)) {
            throw IllegalArgumentException("pairing code must be 6 digits")
        }
        val serverId = params["sid"]?.trim()?.takeIf { it.isNotEmpty() }
        return PairInfo(
            host = host,
            port = port,
            fingerprint = fingerprint,
            code = code,
            serverId = serverId,
            version = version,
        )
    }

    fun isPairingCode(code: String): Boolean {
        if (code.length != 6) return false
        for (ch in code) {
            if (ch !in '0'..'9') return false
        }
        return true
    }

    private fun decode(value: String): String = try {
        URLDecoder.decode(value, "UTF-8")
    } catch (e: Exception) {
        value
    }
}
