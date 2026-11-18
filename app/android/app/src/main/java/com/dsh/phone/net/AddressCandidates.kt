package com.dsh.phone.net

import com.dsh.phone.protocol.Pairing

/**
 * Address ordering for multi-address connections (D-031, PROTOCOL §5.1).
 *
 * A pairing URI may carry `host` plus an optional `hosts` candidate list. The
 * client tries the last successfully used address first (so a roaming phone or
 * a hot spot keeps working), then the URI's primary `host`, then the remaining
 * candidates in URI order. Duplicates and unusable entries are removed.
 *
 * This object is pure Kotlin so the ordering rules are covered by JVM tests;
 * the actual socket fallback lives in WssClient.
 */
object AddressCandidates {

    const val ATTEMPT_TIMEOUT_MS = 3_000L

    /**
     * Build the ordered candidate host list.
     *
     * @param lastSuccess most recently successful host (may be null/blank).
     * @param primary URI `host` field.
     * @param extra URI `hosts` candidates in the order they appeared.
     */
    fun order(lastSuccess: String?, primary: String, extra: List<String>): List<String> {
        val result = LinkedHashSet<String>()
        addIfUsable(result, lastSuccess)
        addIfUsable(result, primary)
        for (candidate in extra) {
            addIfUsable(result, candidate)
        }
        return result.toList()
    }

    private fun addIfUsable(result: LinkedHashSet<String>, host: String?) {
        if (host == null) return
        val value = host.trim()
        if (!Pairing.isCandidateHost(value)) return
        result.add(value)
    }
}
