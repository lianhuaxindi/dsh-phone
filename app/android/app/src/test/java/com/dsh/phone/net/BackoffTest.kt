package com.dsh.phone.net

import org.junit.Assert.assertEquals
import org.junit.Test

/**
 * PROTOCOL §7 reconnect backoff: 1, 2, 4, 8, 15, 15, … s with ±20% jitter and a
 * 15 s base cap.
 */
class BackoffTest {

    @Test
    fun baseDelaysFollowProtocolSchedule() {
        assertEquals(1_000L, BackoffPolicy.baseDelayMs(0))
        assertEquals(2_000L, BackoffPolicy.baseDelayMs(1))
        assertEquals(4_000L, BackoffPolicy.baseDelayMs(2))
        assertEquals(8_000L, BackoffPolicy.baseDelayMs(3))
        assertEquals(15_000L, BackoffPolicy.baseDelayMs(4))
        assertEquals(15_000L, BackoffPolicy.baseDelayMs(5))
        assertEquals(15_000L, BackoffPolicy.baseDelayMs(99))
    }

    @Test
    fun negativeAttemptUsesFirstDelay() {
        assertEquals(1_000L, BackoffPolicy.baseDelayMs(-1))
    }

    @Test
    fun jitterIsAppliedWithinTwentyPercent() {
        assertEquals(800L, BackoffPolicy.delayMs(0, 0.8))
        assertEquals(1_000L, BackoffPolicy.delayMs(0, 1.0))
        assertEquals(1_200L, BackoffPolicy.delayMs(0, 1.2))
        assertEquals(4_000L, BackoffPolicy.delayMs(2, 1.0))
    }

    @Test
    fun jitterFactorIsClamped() {
        // 0.5 clamps to 0.8; 2.0 clamps to 1.2.
        assertEquals(12_000L, BackoffPolicy.delayMs(4, 0.5))
        assertEquals(18_000L, BackoffPolicy.delayMs(4, 2.0))
        assertEquals(800L, BackoffPolicy.delayMs(0, 0.1))
        assertEquals(1_200L, BackoffPolicy.delayMs(0, 10.0))
    }
}
