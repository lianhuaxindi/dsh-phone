package com.dsh.phone.protocol

import org.junit.Assert.assertEquals
import org.junit.Test

/**
 * Outbound protocol builders must produce byte-exact JSON: field names, order
 * and value types are part of the frozen v1 wire format.
 */
class ProtocolTest {

    @Test
    fun helloMatchesProtocolExample() {
        assertEquals(
            "{\"type\":\"hello\",\"seq\":1,\"v\":1,\"deviceId\":\"dev-1\"," +
                "\"app\":\"1.0.0\",\"os\":\"android\",\"ts\":1710000000}",
            Protocol.hello(
                seq = 1,
                deviceId = "dev-1",
                appVersion = "1.0.0",
                tsSec = 1_710_000_000L,
            ),
        )
    }

    @Test
    fun pairRequestUsesNullPubKey() {
        assertEquals(
            "{\"type\":\"pair.request\",\"seq\":2,\"deviceId\":\"dev-1\"," +
                "\"code\":\"123456\",\"deviceName\":\"Pixel 8\",\"pubKey\":null}",
            Protocol.pairRequest(
                seq = 2,
                deviceId = "dev-1",
                code = "123456",
                deviceName = "Pixel 8",
            ),
        )
    }

    @Test
    fun pingAndResync() {
        assertEquals(
            "{\"type\":\"ping\",\"seq\":5,\"ts\":1710000000}",
            Protocol.ping(seq = 5, tsSec = 1_710_000_000L),
        )
        assertEquals(
            "{\"type\":\"resync\",\"seq\":6,\"lastSeq\":42}",
            Protocol.resync(seq = 6, lastSeq = 42),
        )
    }

    @Test
    fun audioStartWithAndWithoutSession() {
        assertEquals(
            "{\"type\":\"audio.start\",\"seq\":11,\"sampleRate\":16000," +
                "\"channels\":1,\"bitDepth\":16,\"lang\":\"zh\",\"mode\":\"ptt\"," +
                "\"sessionId\":\"s-1\"}",
            Protocol.audioStart(
                seq = 11,
                sampleRate = Protocol.AUDIO_SAMPLE_RATE,
                channels = Protocol.AUDIO_CHANNELS,
                bitDepth = Protocol.AUDIO_BIT_DEPTH,
                lang = "zh",
                mode = "ptt",
                sessionId = "s-1",
            ),
        )
        assertEquals(
            "{\"type\":\"audio.start\",\"seq\":11,\"sampleRate\":16000," +
                "\"channels\":1,\"bitDepth\":16,\"lang\":\"zh\",\"mode\":\"ptt\"}",
            Protocol.audioStart(
                seq = 11,
                sampleRate = 16000,
                channels = 1,
                bitDepth = 16,
                lang = "zh",
                mode = "ptt",
                sessionId = null,
            ),
        )
    }

    @Test
    fun audioStopCarriesLastAseq() {
        assertEquals(
            "{\"type\":\"audio.stop\",\"seq\":12,\"aseq\":200}",
            Protocol.audioStop(seq = 12, aseq = 200),
        )
    }

    @Test
    fun frozenConstants() {
        assertEquals(1, Protocol.VERSION)
        assertEquals("dsphone.v1", Protocol.SUBPROTOCOL)
        // RFC 6455 subprotocol tokens cannot contain '/'; D-015 fixed the old name.
        assertEquals(false, Protocol.SUBPROTOCOL.contains('/'))
        assertEquals(65_536, Protocol.MAX_JSON_BYTES)
        assertEquals(8_192, Protocol.MAX_BINARY_BYTES)
        assertEquals(32_000, Protocol.MAX_TEXT_CHARS)
        assertEquals(16_000, Protocol.AUDIO_SAMPLE_RATE)
        assertEquals(1, Protocol.AUDIO_CHANNELS)
        assertEquals(16, Protocol.AUDIO_BIT_DEPTH)
        assertEquals(20, Protocol.AUDIO_FRAME_MS)
        assertEquals(640, Protocol.AUDIO_FRAME_BYTES)
        assertEquals(120_000L, Protocol.AUDIO_MAX_STREAM_MS)
    }

    @Test
    fun stringValuesAreJsonEscaped() {
        val json = Protocol.pairRequest(
            seq = 1,
            deviceId = "dev-1",
            code = "123456",
            deviceName = "Pixel \"8\"\\",
        )
        assertEquals(true, json.contains("Pixel \\\"8\\\"\\\\"))
    }
}
