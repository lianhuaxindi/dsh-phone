package com.dsh.phone.log

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * ARCHITECTURE §5: exported logs must never contain tokens, HMACs, nonces,
 * pairing codes or message/audio bodies.
 */
class RedactorTest {

    @Test
    fun masksSensitiveFieldValues() {
        val redacted = Redactor.redactJson(
            "{\"type\":\"auth\",\"hmac\":\"sig\",\"nonce\":\"n1\",\"deviceId\":\"dev-1\"}",
        )
        assertEquals(
            "{\"type\":\"auth\",\"hmac\":\"<redacted>\",\"nonce\":\"<redacted>\"," +
                "\"deviceId\":\"dev-1\"}",
            redacted,
        )
    }

    @Test
    fun masksTokenDeviceTokenAndPairingCode() {
        val redacted = Redactor.redactJson(
            "{\"token\":\"abc\",\"deviceToken\":\"def\",\"code\":123456,\"host\":\"10.0.0.2\"}",
        )
        assertFalse(redacted.contains("\"abc\""))
        assertFalse(redacted.contains("\"def\""))
        assertFalse(redacted.contains("123456"))
        assertTrue(redacted.contains("\"host\":\"10.0.0.2\""))
        assertTrue(redacted.contains(Redactor.REDACTED))
    }

    @Test
    fun masksMessageBodiesAndAudioPayloads() {
        val redacted = Redactor.redactJson(
            "{\"text\":\"hello world\",\"pcm\":\"AAAA\",\"payload\":\"{}\",\"json\":\"x\"}",
        )
        assertFalse(redacted.contains("hello world"))
        assertFalse(redacted.contains("AAAA"))
        assertFalse(redacted.contains("\"{}\""))
        assertTrue(redacted.contains("\"text\":\"" + Redactor.REDACTED + "\""))
    }

    @Test
    fun keepsNonSensitiveFields() {
        val input = "{\"event\":\"net.state\",\"state\":\"online\",\"latencyMs\":120}"
        assertEquals(input, Redactor.redactJson(input))
    }

    @Test
    fun handlesEscapedQuotesInsideSensitiveValues() {
        val redacted = Redactor.redactJson("{\"text\":\"a\\\"b\",\"type\":\"chat\"}")
        assertEquals("{\"text\":\"<redacted>\",\"type\":\"chat\"}", redacted)
    }

    @Test
    fun redactTextDelegatesToJsonRedaction() {
        assertEquals(
            "{\"deviceToken\":\"<redacted>\"}",
            Redactor.redactText("{\"deviceToken\":\"secret\"}"),
        )
    }
}
