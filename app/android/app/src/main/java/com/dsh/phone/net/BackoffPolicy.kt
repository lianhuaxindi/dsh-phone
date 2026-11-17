package com.dsh.phone.net

/**
 * Reconnect backoff (PROTOCOL §7): 1, 2, 4, 8, 15, 15, … seconds with ±20%
 * jitter; the cap is 15 s. Kept pure so the schedule is unit-tested.
 */
object BackoffPolicy {

    private val BASE_DELAYS_MS = longArrayOf(1_000L, 2_000L, 4_000L, 8_000L, 15_000L)

    const val MAX_DELAY_MS: Long = 15_000L
    const val MIN_FACTOR: Double = 0.8
    const val MAX_FACTOR: Double = 1.2

    /** Base delay for a zero-based attempt index; saturates at 15 s. */
    fun baseDelayMs(attempt: Int): Long {
        if (attempt <= 0) return BASE_DELAYS_MS[0]
        val idx = if (attempt >= BASE_DELAYS_MS.size) BASE_DELAYS_MS.size - 1 else attempt
        return BASE_DELAYS_MS[idx]
    }

    /** Base delay multiplied by a clamped jitter factor, in milliseconds. */
    fun delayMs(attempt: Int, factor: Double): Long {
        val clamped = factor.coerceIn(MIN_FACTOR, MAX_FACTOR)
        return (baseDelayMs(attempt) * clamped).toLong()
    }
}
