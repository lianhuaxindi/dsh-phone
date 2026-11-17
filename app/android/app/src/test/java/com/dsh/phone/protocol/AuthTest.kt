package com.dsh.phone.protocol

import com.dsh.phone.util.Hex
import java.security.MessageDigest
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * PROTOCOL §6.3 authentication test vector. These values are frozen in the
 * protocol document, so any change here is a protocol break.
 */
class AuthTest {

    private val tokenHex =
        "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff"
    private val tokenBase64 = "ABEiM0RVZneImaq7zN3u/wARIjNEVWZ3iJmqu8zd7v8="
    private val deviceId = "dev-abc123"
    private val tsSec = 1_710_000_000L
    private val nonceBase64 = "bm9uY2UxMjM0NTY3OA=="
    private val expectedCanonical = "DSPHONE1\ndev-abc123\n1710000000\nbm9uY2UxMjM0NTY3OA=="
    private val expectedHmacBase64 = "gYfqlKpD9PLEml5FzwSMmtd93hlDbF/JrA7lGcDBSjo="
    private val expectedHmacHex =
        "8187ea94aa43f4f2c49a5e45cf048c9ad77dde19436c5fc9ac0ee519c0c14a3a"
    private val expectedCanonicalSha256 =
        "4c1b758da310551fca45aeb91ed7cdb302fe09627d50421e0cd0aac59597c6e1"

    @Test
    fun tokenTestVectorDecodesTo32Bytes() {
        val token = Hex.decodeOrNull(tokenHex)
        assertEquals(32, token!!.size)
        assertEquals(tokenBase64, Protocol.encodeBase64(token))
        assertArrayEquals(token, Protocol.decodeBase64(tokenBase64))
    }

    @Test
    fun canonicalStringHasNoTrailingNewline() {
        assertEquals(expectedCanonical, Protocol.authCanonical(deviceId, tsSec, nonceBase64))
        assertTrue(!expectedCanonical.endsWith("\n"))
    }

    @Test
    fun canonicalSha256MatchesProtocolVector() {
        val digest = MessageDigest.getInstance("SHA-256")
            .digest(expectedCanonical.toByteArray(Charsets.UTF_8))
        assertEquals(expectedCanonicalSha256, Hex.encode(digest))
    }

    @Test
    fun hmacMatchesProtocolVector() {
        val token = Hex.decodeOrNull(tokenHex)!!
        assertEquals(expectedHmacBase64, Protocol.hmacBase64(token, expectedCanonical))
        assertEquals(expectedHmacHex, Protocol.hmacHex(token, expectedCanonical))
    }

    @Test
    fun hmacChangesWhenAnyFieldChanges() {
        val token = Hex.decodeOrNull(tokenHex)!!
        val other = Protocol.authCanonical("dev-other", tsSec, nonceBase64)
        assertNotEquals(
            Protocol.hmacBase64(token, expectedCanonical),
            Protocol.hmacBase64(token, other),
        )
    }

    @Test
    fun randomNonceIs16BytesAndUnique() {
        val first = Protocol.randomNonceBase64()
        val second = Protocol.randomNonceBase64()
        assertEquals(16, Protocol.decodeBase64(first).size)
        assertEquals(16, Protocol.decodeBase64(second).size)
        assertNotEquals(first, second)
    }

    @Test
    fun authBuilderEmitsExpectedFields() {
        assertEquals(
            "{\"type\":\"auth\",\"seq\":3,\"deviceId\":\"dev-abc123\"," +
                "\"ts\":1710000000,\"nonce\":\"bm9uY2UxMjM0NTY3OA==\"," +
                "\"hmac\":\"gYfqlKpD9PLEml5FzwSMmtd93hlDbF/JrA7lGcDBSjo=\",\"v\":1}",
            Protocol.auth(3, deviceId, tsSec, nonceBase64, expectedHmacBase64),
        )
    }
}
