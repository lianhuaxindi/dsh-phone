package com.dsh.phone.net

/**
 * WebSocket close-code policy (PROTOCOL §11). Decides whether the client should
 * reconnect, go back to pairing, or stop retrying.
 */
object ClosePolicy {

    const val NORMAL = 1000
    const val SERVER_ERROR = 1011
    const val PROTOCOL_ERROR = 4400
    const val AUTH_FAILED = 4401
    const val VERSION_MISMATCH = 4402
    const val HEARTBEAT_TIMEOUT = 4408
    const val PAIR_LOCKED = 4410

    enum class Action {
        /** Close was intentional (normal) — stay idle. */
        NONE,

        /** Transient failure — retry with backoff. */
        RECONNECT,

        /** Token rejected — clear it and ask the user to pair again. */
        CLEAR_TOKEN_AND_PAIR,

        /** Fatal protocol/config problem — stop and let the user act. */
        FATAL,
    }

    fun actionFor(code: Int): Action = when (code) {
        NORMAL -> Action.NONE
        AUTH_FAILED -> Action.CLEAR_TOKEN_AND_PAIR
        VERSION_MISMATCH, PROTOCOL_ERROR, PAIR_LOCKED -> Action.FATAL
        SERVER_ERROR, HEARTBEAT_TIMEOUT -> Action.RECONNECT
        else -> Action.RECONNECT
    }
}
