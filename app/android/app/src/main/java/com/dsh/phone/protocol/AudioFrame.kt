package com.dsh.phone.protocol

/**
 * Audio frame header (PROTOCOL §8):
 *
 * ```
 * offset 0     1 byte   frame type: 0x01 = PCM16LE (0x02 = Opus reserved)
 * offset 1     2 bytes  aseq, u16 little-endian, starts at 1, wraps 65535 -> 1
 * offset 3     4 bytes  tsMs, u32 little-endian, capture time relative to audio.start
 * offset 7     640 bytes PCM16LE mono, 16 kHz, 20 ms
 * ```
 *
 * Pure Kotlin so the exact byte layout is covered by JVM unit tests.
 */
object AudioFrame {

    const val HEADER_SIZE = 7
    const val PCM16_FRAME_BYTES = 640
    const val FRAME_MS = 20
    const val TYPE_PCM16: Int = 0x01
    const val TYPE_OPUS_RESERVED: Int = 0x02

    const val MAX_ASEQ = 0xFFFF

    data class Header(val type: Int, val aseq: Int, val tsMs: Long)

    /** aseq counter: 1..65535, wraps 65535 -> 1 (never 0). */
    fun nextAseq(current: Int): Int {
        if (current < 1 || current >= MAX_ASEQ) return 1
        return current + 1
    }

    fun frameLength(pcmBytes: Int): Int = HEADER_SIZE + pcmBytes

    /** Encode header bytes only (without the PCM payload). */
    fun header(aseq: Int, tsMs: Long): ByteArray {
        require(aseq in 1..MAX_ASEQ) { "aseq out of range: $aseq" }
        require(tsMs >= 0L) { "tsMs must be non-negative: $tsMs" }
        val out = ByteArray(HEADER_SIZE)
        out[0] = TYPE_PCM16.toByte()
        out[1] = (aseq and 0xFF).toByte()
        out[2] = ((aseq ushr 8) and 0xFF).toByte()
        val ts = tsMs and 0xFFFFFFFFL
        out[3] = (ts and 0xFF).toByte()
        out[4] = ((ts ushr 8) and 0xFF).toByte()
        out[5] = ((ts ushr 16) and 0xFF).toByte()
        out[6] = ((ts ushr 24) and 0xFF).toByte()
        return out
    }

    /** Encode a complete binary frame: header + PCM16LE payload. */
    fun encode(aseq: Int, tsMs: Long, pcm: ByteArray): ByteArray {
        val head = header(aseq, tsMs)
        val out = ByteArray(HEADER_SIZE + pcm.size)
        head.copyInto(out, 0)
        pcm.copyInto(out, HEADER_SIZE)
        return out
    }

    /** Decode a 7-byte header; returns null when the buffer is too short. */
    fun decodeHeader(data: ByteArray, offset: Int = 0): Header? {
        if (offset < 0 || data.size - offset < HEADER_SIZE) return null
        val type = data[offset].toInt() and 0xFF
        val aseq = (data[offset + 1].toInt() and 0xFF) or
            ((data[offset + 2].toInt() and 0xFF) shl 8)
        val tsMs =
            (data[offset + 3].toLong() and 0xFFL) or
                ((data[offset + 4].toLong() and 0xFFL) shl 8) or
                ((data[offset + 5].toLong() and 0xFFL) shl 16) or
                ((data[offset + 6].toLong() and 0xFFL) shl 24)
        return Header(type = type, aseq = aseq, tsMs = tsMs)
    }
}
