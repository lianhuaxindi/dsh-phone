package com.dsh.phone.util

/**
 * JSON string escaping used by the outbound protocol builders.
 *
 * Pure Kotlin (no Android / org.json dependency) so that the exact wire bytes
 * can be asserted from JVM unit tests. Incoming JSON on the device is parsed
 * with org.json; outbound frames are built here to keep the output ordering
 * deterministic (useful for tests and logs).
 */
object Json {

    private val HEX = charArrayOf(
        '0', '1', '2', '3', '4', '5', '6', '7',
        '8', '9', 'a', 'b', 'c', 'd', 'e', 'f',
    )

    /** Escape [value] into a JSON string literal body (without quotes). */
    fun escape(value: String): String {
        val sb = StringBuilder(value.length + 8)
        var i = 0
        while (i < value.length) {
            val ch = value[i]
            when (ch) {
                '"' -> sb.append("\\\"")
                '\\' -> sb.append("\\\\")
                '\b' -> sb.append("\\b")
                '\u000C' -> sb.append("\\f")
                '\n' -> sb.append("\\n")
                '\r' -> sb.append("\\r")
                '\t' -> sb.append("\\t")
                else -> {
                    if (ch < ' ') {
                        val code = ch.code
                        sb.append("\\u")
                        sb.append(HEX[(code shr 12) and 0xF])
                        sb.append(HEX[(code shr 8) and 0xF])
                        sb.append(HEX[(code shr 4) and 0xF])
                        sb.append(HEX[code and 0xF])
                    } else {
                        sb.append(ch)
                    }
                }
            }
            i++
        }
        return sb.toString()
    }

    /** Quote [value] as a complete JSON string literal, including quotes. */
    fun quote(value: String): String = "\"" + escape(value) + "\""
}

/**
 * Minimal ordered JSON object builder.
 *
 * Values are stored pre-encoded so nested objects can be embedded without
 * reflection or a parser. Field order follows insertion order.
 */
class JsonObj {

    private val entries = ArrayList<Pair<String, String>>(8)

    private fun raw(name: String, encoded: String): JsonObj {
        entries.add(name to encoded)
        return this
    }

    fun put(name: String, value: String?): JsonObj =
        raw(name, if (value == null) "null" else Json.quote(value))

    fun put(name: String, value: Int): JsonObj = raw(name, value.toString())

    fun put(name: String, value: Long): JsonObj = raw(name, value.toString())

    fun put(name: String, value: Boolean): JsonObj = raw(name, value.toString())

    fun putNull(name: String): JsonObj = raw(name, "null")

    fun putObj(name: String, value: JsonObj): JsonObj = raw(name, value.toJsonText())

    /** Embed an already-encoded JSON value (object/array/number) verbatim. */
    fun putRaw(name: String, rawJson: String): JsonObj = raw(name, rawJson)

    fun size(): Int = entries.size

    fun toJsonText(): String {
        val sb = StringBuilder(64 + entries.size * 16)
        sb.append('{')
        var first = true
        for (entry in entries) {
            if (!first) sb.append(',')
            first = false
            sb.append(Json.quote(entry.first))
            sb.append(':')
            sb.append(entry.second)
        }
        sb.append('}')
        return sb.toString()
    }

    override fun toString(): String = toJsonText()
}
