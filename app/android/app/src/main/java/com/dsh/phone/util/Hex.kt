package com.dsh.phone.util

/** Lowercase hex helpers (certificate fingerprints, HMAC test vectors). */
object Hex {

    private val DIGITS = charArrayOf(
        '0', '1', '2', '3', '4', '5', '6', '7',
        '8', '9', 'a', 'b', 'c', 'd', 'e', 'f',
    )

    fun encode(bytes: ByteArray): String {
        val sb = StringBuilder(bytes.size * 2)
        for (b in bytes) {
            val v = b.toInt() and 0xFF
            sb.append(DIGITS[(v ushr 4) and 0xF])
            sb.append(DIGITS[v and 0xF])
        }
        return sb.toString()
    }

    fun decodeOrNull(text: String?): ByteArray? {
        if (text == null) return null
        if (text.length % 2 != 0) return null
        val out = ByteArray(text.length / 2)
        var i = 0
        while (i < text.length) {
            val hi = digitValue(text[i])
            val lo = digitValue(text[i + 1])
            if (hi < 0 || lo < 0) return null
            out[i / 2] = (((hi shl 4) or lo) and 0xFF).toByte()
            i += 2
        }
        return out
    }

    private fun digitValue(ch: Char): Int = when (ch) {
        in '0'..'9' -> ch - '0'
        in 'a'..'f' -> ch - 'a' + 10
        in 'A'..'F' -> ch - 'A' + 10
        else -> -1
    }
}
