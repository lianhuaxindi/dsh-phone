package com.dsh.phone.net

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * D-031 / PROTOCOL §5.1: candidate address ordering is
 * last-success -> URI host -> URI hosts, with invalid entries dropped.
 */
class AddressCandidatesTest {

    @Test
    fun primaryFirstWhenNoHistoryExists() {
        assertEquals(
            listOf("192.168.1.10", "10.5.19.39"),
            AddressCandidates.order(null, "192.168.1.10", listOf("10.5.19.39")),
        )
    }

    @Test
    fun lastSuccessIsTriedBeforeThePrimaryHost() {
        assertEquals(
            listOf("10.5.19.39", "192.168.1.10", "10.5.19.40"),
            AddressCandidates.order(
                lastSuccess = "10.5.19.39",
                primary = "192.168.1.10",
                extra = listOf("10.5.19.40"),
            ),
        )
    }

    @Test
    fun duplicatesAreRemovedInOrder() {
        assertEquals(
            listOf("192.168.1.10", "10.5.19.39"),
            AddressCandidates.order(
                lastSuccess = "192.168.1.10",
                primary = "192.168.1.10",
                extra = listOf("192.168.1.10", "10.5.19.39", "10.5.19.39"),
            ),
        )
    }

    @Test
    fun unusableAddressesAreDropped() {
        assertEquals(
            listOf("10.5.19.39"),
            AddressCandidates.order(
                lastSuccess = "0.0.0.0",
                primary = "",
                extra = listOf("", "http://host", "0.0.0.0", "  ", "10.5.19.39"),
            ),
        )
    }

    @Test
    fun blankInputsProduceAnEmptyListWhenNothingIsUsable() {
        assertTrue(AddressCandidates.order("   ", "", emptyList()).isEmpty())
    }

    @Test
    fun attemptTimeoutMatchesProtocolThreeSeconds() {
        assertEquals(3_000L, AddressCandidates.ATTEMPT_TIMEOUT_MS)
    }
}
