package com.dsh.phone.protocol

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertThrows
import org.junit.Test

/**
 * PROTOCOL §6.1 pairing URI: `dsphone://pair?v=1&host=…&port=…&fp=…&code=…&sid=…`.
 */
class PairingTest {

    private val fingerprint =
        "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff"

    private fun pairingUri(
        host: String = "192.168.1.10",
        port: Int = 8765,
        fp: String = fingerprint,
        extra: String = "&code=123456&sid=srv-1a2b",
    ): String = "dsphone://pair?v=1&host=$host&port=$port&fp=$fp$extra"

    @Test
    fun parseValidPairingUri() {
        val info = Pairing.parseUri(pairingUri())
        assertEquals("192.168.1.10", info.host)
        assertEquals(8765, info.port)
        assertEquals(fingerprint, info.fingerprint)
        assertEquals("123456", info.code)
        assertEquals("srv-1a2b", info.serverId)
        assertEquals(1, info.version)
    }

    @Test
    fun codeAndServerIdAreOptional() {
        val info = Pairing.parseUri(pairingUri(extra = ""))
        assertNull(info.code)
        assertNull(info.serverId)
    }

    @Test
    fun fingerprintIsNormalized() {
        val upper = fingerprint.uppercase()
        assertEquals(fingerprint, Pairing.normalizeFingerprint(upper))
        assertEquals(fingerprint, Pairing.normalizeFingerprint("sha256:$upper"))
        val coloned = fingerprint.chunked(2).joinToString(":")
        assertEquals(fingerprint, Pairing.normalizeFingerprint(coloned))
        assertEquals(fingerprint, Pairing.parseUri(pairingUri(fp = upper)).fingerprint)
    }

    @Test
    fun rejectsInvalidFingerprints() {
        assertThrows(IllegalArgumentException::class.java) {
            Pairing.normalizeFingerprint("deadbeef")
        }
        assertThrows(IllegalArgumentException::class.java) {
            Pairing.normalizeFingerprint(fingerprint.dropLast(1) + "z")
        }
        assertEquals(false, Pairing.isValidFingerprint(null))
        assertEquals(false, Pairing.isValidFingerprint("not-a-fingerprint"))
        assertEquals(true, Pairing.isValidFingerprint(fingerprint))
    }

    @Test
    fun rejectsInvalidHostPortAndVersion() {
        assertThrows(IllegalArgumentException::class.java) {
            Pairing.parseUri(pairingUri(host = "0.0.0.0"))
        }
        assertThrows(IllegalArgumentException::class.java) {
            Pairing.parseUri(pairingUri(port = 0))
        }
        assertThrows(IllegalArgumentException::class.java) {
            Pairing.parseUri(pairingUri(port = 70000))
        }
        assertThrows(IllegalArgumentException::class.java) {
            Pairing.parseUri(pairingUri().replace("v=1", "v=2"))
        }
    }

    @Test
    fun rejectsWrongSchemeAndTarget() {
        assertThrows(IllegalArgumentException::class.java) {
            Pairing.parseUri(pairingUri().replace("dsphone://pair", "https://pair"))
        }
        assertThrows(IllegalArgumentException::class.java) {
            Pairing.parseUri(pairingUri().replace("dsphone://pair", "dsphone://pairing"))
        }
        assertThrows(IllegalArgumentException::class.java) {
            Pairing.parseUri("not a uri at all")
        }
        assertThrows(IllegalArgumentException::class.java) {
            Pairing.parseUri("dsphone://pair")
        }
    }

    @Test
    fun rejectsMalformedPairingCode() {
        assertThrows(IllegalArgumentException::class.java) {
            Pairing.parseUri(pairingUri(extra = "&code=12345"))
        }
        assertThrows(IllegalArgumentException::class.java) {
            Pairing.parseUri(pairingUri(extra = "&code=12a456"))
        }
    }

    @Test
    fun isPairingCodeChecksSixDigits() {
        assertEquals(true, Pairing.isPairingCode("123456"))
        assertEquals(false, Pairing.isPairingCode("12345"))
        assertEquals(false, Pairing.isPairingCode("1234567"))
        assertEquals(false, Pairing.isPairingCode("12 456"))
    }

    /* -------------------------------------------------- v1.1 hosts / name */

    @Test
    fun parsesHostsAndNameFromV11Uri() {
        val info = Pairing.parseUri(
            pairingUri(
                extra = "&code=123456&sid=srv-1a2b" +
                    "&hosts=192.168.1.10,10.5.19.39,10.5.19.40&name=KQ-PC",
            ),
        )
        // The primary host is not repeated in the candidate list.
        assertEquals(listOf("10.5.19.39", "10.5.19.40"), info.hosts)
        assertEquals("KQ-PC", info.name)
    }

    @Test
    fun hostsAndNameAreOptional() {
        val info = Pairing.parseUri(pairingUri())
        assertEquals(emptyList<String>(), info.hosts)
        assertNull(info.name)
    }

    @Test
    fun parseHostsSkipsInvalidEntriesAndPrimary() {
        val hosts = Pairing.parseHosts(
            raw = "10.0.0.2, 0.0.0.0,,10.0.0.2,192.168.1.10,10.0.0.3:9,10.0.0.4",
            primary = "192.168.1.10",
        )
        assertEquals(listOf("10.0.0.2", "10.0.0.4"), hosts)
    }

    @Test
    fun parseHostsCapsTheCandidateList() {
        val raw = (2..40).joinToString(",") { "10.0.0.$it" }
        assertEquals(16, Pairing.parseHosts(raw).size)
    }

    @Test
    fun isCandidateHostFiltersUnusableAddresses() {
        assertEquals(true, Pairing.isCandidateHost("192.168.1.10"))
        assertEquals(false, Pairing.isCandidateHost(""))
        assertEquals(false, Pairing.isCandidateHost("0.0.0.0"))
        assertEquals(false, Pairing.isCandidateHost("::"))
        assertEquals(false, Pairing.isCandidateHost("http://host"))
        assertEquals(false, Pairing.isCandidateHost("host name"))
    }
}
