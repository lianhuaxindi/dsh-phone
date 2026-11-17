package com.dsh.phone.protocol

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertThrows
import org.junit.Test

/**
 * PROTOCOL §8 binary audio framing: 7-byte header, little-endian aseq / tsMs,
 * 640-byte PCM16LE payload and the 65535 -> 1 aseq wrap.
 */
class AudioFrameTest {

    @Test
    fun firstFrameHeaderMatchesProtocolExample() {
        // PROTOCOL §8: first frame is `01 01 00 00 00 00 00`.
        assertArrayEquals(
            byteArrayOf(0x01, 0x01, 0x00, 0x00, 0x00, 0x00, 0x00),
            AudioFrame.header(aseq = 1, tsMs = 0L),
        )
    }

    @Test
    fun headerEncodesAseqAndTimestampLittleEndian() {
        assertArrayEquals(
            byteArrayOf(0x01, 0x34, 0x12, 0x04, 0x03, 0x02, 0x01),
            AudioFrame.header(aseq = 0x1234, tsMs = 0x01020304L),
        )
    }

    @Test
    fun tsMsIsTruncatedToU32() {
        assertArrayEquals(
            byteArrayOf(0x01, 0x01, 0x00, 0x01, 0x00, 0x00, 0x00),
            AudioFrame.header(aseq = 1, tsMs = 0x1_0000_0001L),
        )
    }

    @Test
    fun headerRejectsOutOfRangeValues() {
        assertThrows(IllegalArgumentException::class.java) { AudioFrame.header(0, 0L) }
        assertThrows(IllegalArgumentException::class.java) { AudioFrame.header(65536, 0L) }
        assertThrows(IllegalArgumentException::class.java) { AudioFrame.header(1, -1L) }
    }

    @Test
    fun encodeAppendsPcmAfterHeader() {
        val pcm = ByteArray(AudioFrame.PCM16_FRAME_BYTES) { (it % 251).toByte() }
        val frame = AudioFrame.encode(aseq = 7, tsMs = 100L, pcm = pcm)
        assertEquals(647, frame.size)
        assertEquals(647, AudioFrame.frameLength(pcm.size))
        assertEquals(0x01, frame[0].toInt() and 0xFF)
        assertEquals(7, frame[1].toInt() and 0xFF)
        assertEquals(pcm[0], frame[AudioFrame.HEADER_SIZE])
        assertEquals(pcm[pcm.lastIndex], frame[frame.lastIndex])
    }

    @Test
    fun decodeHeaderRoundTrips() {
        val frame = AudioFrame.encode(aseq = 42, tsMs = 123_456L, pcm = ByteArray(640))
        assertEquals(
            AudioFrame.Header(type = 1, aseq = 42, tsMs = 123_456L),
            AudioFrame.decodeHeader(frame),
        )
    }

    @Test
    fun decodeHeaderSupportsOffsetAndShortBuffers() {
        val frame = AudioFrame.encode(aseq = 65535, tsMs = 4_294_967_295L, pcm = ByteArray(640))
        val padded = ByteArray(frame.size + 3)
        frame.copyInto(padded, 3)
        assertEquals(
            AudioFrame.Header(type = 1, aseq = 65535, tsMs = 4_294_967_295L),
            AudioFrame.decodeHeader(padded, 3),
        )
        assertNull(AudioFrame.decodeHeader(ByteArray(6)))
        assertNull(AudioFrame.decodeHeader(ByteArray(7), 1))
    }

    @Test
    fun aseqWrapsFrom65535To1() {
        assertEquals(2, AudioFrame.nextAseq(1))
        assertEquals(65535, AudioFrame.nextAseq(65534))
        assertEquals(1, AudioFrame.nextAseq(65535))
        assertEquals(1, AudioFrame.nextAseq(0))
    }
}
