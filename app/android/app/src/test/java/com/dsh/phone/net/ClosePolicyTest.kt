package com.dsh.phone.net

import org.junit.Assert.assertEquals
import org.junit.Test

/** PROTOCOL §11 WebSocket close-code handling policy. */
class ClosePolicyTest {

    @Test
    fun normalCloseKeepsIdle() {
        assertEquals(ClosePolicy.Action.NONE, ClosePolicy.actionFor(1000))
    }

    @Test
    fun authFailureClearsTokenAndRepairs() {
        assertEquals(
            ClosePolicy.Action.CLEAR_TOKEN_AND_PAIR,
            ClosePolicy.actionFor(ClosePolicy.AUTH_FAILED),
        )
        assertEquals(
            ClosePolicy.Action.CLEAR_TOKEN_AND_PAIR,
            ClosePolicy.actionFor(4401),
        )
    }

    @Test
    fun protocolProblemsAreFatal() {
        assertEquals(ClosePolicy.Action.FATAL, ClosePolicy.actionFor(4400))
        assertEquals(ClosePolicy.Action.FATAL, ClosePolicy.actionFor(4402))
        assertEquals(ClosePolicy.Action.FATAL, ClosePolicy.actionFor(4410))
    }

    @Test
    fun transientFailuresReconnect() {
        assertEquals(ClosePolicy.Action.RECONNECT, ClosePolicy.actionFor(1011))
        assertEquals(ClosePolicy.Action.RECONNECT, ClosePolicy.actionFor(4408))
    }

    @Test
    fun unknownCodesDefaultToReconnect() {
        assertEquals(ClosePolicy.Action.RECONNECT, ClosePolicy.actionFor(4999))
        assertEquals(ClosePolicy.Action.RECONNECT, ClosePolicy.actionFor(1006))
    }

    @Test
    fun closeCodeConstants() {
        assertEquals(1000, ClosePolicy.NORMAL)
        assertEquals(1011, ClosePolicy.SERVER_ERROR)
        assertEquals(4400, ClosePolicy.PROTOCOL_ERROR)
        assertEquals(4401, ClosePolicy.AUTH_FAILED)
        assertEquals(4402, ClosePolicy.VERSION_MISMATCH)
        assertEquals(4408, ClosePolicy.HEARTBEAT_TIMEOUT)
        assertEquals(4410, ClosePolicy.PAIR_LOCKED)
    }
}
