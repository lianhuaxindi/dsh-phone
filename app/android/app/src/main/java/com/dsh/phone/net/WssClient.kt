package com.dsh.phone.net

import com.dsh.phone.log.LogStore
import com.dsh.phone.protocol.Pairing
import com.dsh.phone.protocol.Protocol
import com.dsh.phone.storage.SettingsStore
import com.dsh.phone.storage.TokenStore
import com.dsh.phone.util.Hex
import com.dsh.phone.util.JsonObj
import okhttp3.ConnectionSpec
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okio.ByteString
import org.json.JSONArray
import org.json.JSONObject
import java.security.MessageDigest
import java.security.SecureRandom
import java.security.cert.CertificateException
import java.security.cert.X509Certificate
import java.util.ArrayDeque
import java.util.LinkedHashMap
import java.util.Random
import java.util.concurrent.Executors
import java.util.concurrent.ScheduledExecutorService
import java.util.concurrent.ScheduledFuture
import java.util.concurrent.TimeUnit
import javax.net.ssl.SSLContext
import javax.net.ssl.X509TrustManager

/**
 * WSS client for the dsphone.v1 protocol (PROTOCOL.md v1, DECISIONS D-015).
 *
 * Owns the whole connection state machine and is the single C→S `seq` authority:
 *
 * - TLS 1.2+ with SHA-256 certificate-fingerprint pinning (Bridge ships a
 *   self-signed certificate; the fingerprint comes from the pairing URI).
 * - Subprotocol `dsphone.v1` (RFC 6455 token characters only), text frames =
 *   JSON, binary frames = PCM audio.
 * - heartbeat ping every 15 s, reconnect when the server stays silent for 45 s,
 *   backoff 1/2/4/8/15 s with ±20% jitter.
 * - hello → (pair) → auth → resync handshake; business frames are forwarded to
 *   the WebView as `proto.message` events, handshake frames are consumed here.
 * - Request correlation: the Web may attach a local `_reqId` to a message; the
 *   bridge forwards the matching response with an extra `_reqTo` field.
 */
class WssClient(
    private val tokenStore: TokenStore,
    private val settings: SettingsStore,
    private val appVersion: String,
    private val callbacks: Callbacks,
) {

    interface Callbacks {
        /** Emit one JSON event to `window.DshBridge.onEvent`. */
        fun emitEvent(json: String)

        /** Server accepted the audio stream (audio.ready) — flush buffered frames. */
        fun onAudioReady()

        /** The audio stream failed/closed — stop capture and notify the UI. */
        fun onAudioError()

        /** Connection is gone (intentional or not) — stop capture if running. */
        fun onConnectionLost(reason: String)

        /** Connection is authenticated and online. */
        fun onOnline()
    }

    enum class State(val wire: String) {
        IDLE("idle"),
        CONNECTING("connecting"),
        PAIRING("pairing"),
        AUTHENTICATING("authenticating"),
        RECONNECTING("reconnecting"),
        ONLINE("online"),
        FAILED("failed"),
        CLOSED("closed"),
    }

    data class ConnectionConfig(
        val host: String,
        val port: Int,
        val fingerprint: String,
        val deviceId: String,
    )

    private val lock = Any()
    private val random = Random()
    private val scheduler: ScheduledExecutorService = Executors.newSingleThreadScheduledExecutor { runnable ->
        Thread(runnable, "dsh-net").apply { isDaemon = true }
    }

    @Volatile
    private var released = false
    private var state: State = State.IDLE
    private var config: ConnectionConfig? = null
    private var socket: WebSocket? = null
    private var httpClient: OkHttpClient? = null
    private var httpClientKey: String = ""
    private var generation = 0
    private var manualClose = true
    private var reconnectAttempt = 0
    private var clientSeq = 0
    private var lastServerSeq = 0
    private var lastServerRecvAt = 0L
    private var connectStartedAt = 0L
    private var serverId: String? = null
    private var sessionTokenBase64: String? = null
    private var authSent = false
    private var resyncSent = false
    private var audioStartSeq = 0
    private var pendingPair: Pair<String, String>? = null
    private val pendingOutbound = ArrayDeque<String>()
    private val reqIdBySeq = LinkedHashMap<Int, String>()
    private var reconnectTask: ScheduledFuture<*>? = null
    private var heartbeatTask: ScheduledFuture<*>? = null
    private var watchdogTask: ScheduledFuture<*>? = null

    val currentState: State
        get() = synchronized(lock) { state }

    fun isOnline(): Boolean = synchronized(lock) { state == State.ONLINE }

    /* ------------------------------------------------------------ lifecycle */

    fun connect(host: String, port: Int, fingerprint: String, deviceId: String) {
        val normalized = try {
            Pairing.normalizeFingerprint(fingerprint)
        } catch (e: IllegalArgumentException) {
            emitError("tls", 1001, "invalid certificate fingerprint", false)
            setState(State.FAILED)
            return
        }
        if (host.isBlank() || port !in 1..65535) {
            emitError("connect", 5001, "invalid host or port", false)
            setState(State.FAILED)
            return
        }
        synchronized(lock) {
            if (released) return
            config = ConnectionConfig(host.trim(), port, normalized, deviceId)
            manualClose = false
            reconnectAttempt = 0
            lastServerSeq = 0
            sessionTokenBase64 = null
            pendingOutbound.clear()
            reqIdBySeq.clear()
            cancelReconnectLocked()
            closeSocketLocked()
            beginConnectLocked()
        }
    }

    fun disconnect() {
        synchronized(lock) {
            if (released) return
            manualClose = true
            reconnectAttempt = 0
            cancelReconnectLocked()
            closeSocketLocked()
            pendingOutbound.clear()
            setState(State.CLOSED)
            callbacks.onConnectionLost("manual")
        }
    }

    /** Diagnostics: drop the current socket and reconnect with backoff. */
    fun reconnect() {
        synchronized(lock) {
            if (released) return
            if (config == null) {
                emitError("connect", 5001, "no server configured", false)
                return
            }
            manualClose = false
            reconnectAttempt = 0
            cancelReconnectLocked()
            closeSocketLocked()
            setState(State.RECONNECTING)
            scheduleReconnectLocked()
        }
    }

    fun release() {
        synchronized(lock) {
            released = true
            manualClose = true
            cancelReconnectLocked()
            closeSocketLocked()
        }
        try {
            scheduler.shutdownNow()
        } catch (ignored: Exception) {
            // Already shut down.
        }
    }

    /* -------------------------------------------------------------- pairing */

    fun pair(code: String, deviceName: String) {
        synchronized(lock) {
            if (released) return
            if (!Pairing.isPairingCode(code)) {
                emitError("pair", 1002, "pairing code must be 6 digits", false)
                return
            }
            when (state) {
                State.CONNECTING, State.RECONNECTING -> pendingPair = code to deviceName
                State.PAIRING, State.AUTHENTICATING -> {
                    pendingPair = null
                    sendPairRequestLocked(code, deviceName)
                }
                else -> emitError("pair", 1002, "not connected; connect first", false)
            }
        }
    }

    fun authNow() {
        synchronized(lock) {
            if (released) return
            when (state) {
                State.ONLINE -> {
                    val event = JsonObj().put("type", "auth.result").put("ok", true).put("already", true)
                    if (serverId != null) event.put("serverId", serverId)
                    callbacks.emitEvent(event.toJsonText())
                }
                State.CONNECTING, State.RECONNECTING -> {
                    // hello.ok will start authentication automatically.
                }
                State.AUTHENTICATING -> sendAuthLocked()
                State.PAIRING -> emitError("auth", 1001, "pairing required", false)
                else -> emitError("auth", 1001, "not connected", false)
            }
        }
    }

    /* -------------------------------------------------------------- outbound */

    /** Forward one Web-originated protocol message, injecting our seq counter. */
    fun sendUserJson(raw: String): Boolean {
        synchronized(lock) {
            if (released) return false
            val obj = try {
                JSONObject(raw)
            } catch (e: Exception) {
                emitError("protocol", 5002, "invalid JSON from web", false)
                return false
            }
            val type = obj.optString("type", "")
            if (type.isEmpty()) {
                emitError("protocol", 5002, "protocol message without type", false)
                return false
            }
            if (HANDSHAKE_TYPES.contains(type)) {
                LogStore.warn("ws.drop_handshake", "type" to type)
                return false
            }
            if (state != State.ONLINE) {
                return if (isConnectingState()) {
                    if (pendingOutbound.size < MAX_PENDING_OUTBOUND) {
                        pendingOutbound.addLast(raw)
                        true
                    } else {
                        emitError("protocol", 4001, "outbound queue full", true)
                        false
                    }
                } else {
                    emitError("protocol", 5001, "not online", true)
                    false
                }
            }
            return sendUserJsonLocked(obj)
        }
    }

    fun sendAudioStart(sampleRate: Int, lang: String, mode: String, sessionId: String?): Boolean {
        synchronized(lock) {
            if (state != State.ONLINE) {
                emitAudioErrorLocked("offline", "network is not online", true)
                return false
            }
            val seq = nextSeqLocked()
            audioStartSeq = seq
            val frame = Protocol.audioStart(
                seq,
                sampleRate,
                Protocol.AUDIO_CHANNELS,
                Protocol.AUDIO_BIT_DEPTH,
                lang,
                mode,
                sessionId,
            )
            return sendTextLocked(frame)
        }
    }

    fun sendAudioStop(lastAseq: Int) {
        synchronized(lock) {
            if (state != State.ONLINE && state != State.AUTHENTICATING) return
            sendTextLocked(Protocol.audioStop(nextSeqLocked(), lastAseq.coerceAtLeast(0)))
            audioStartSeq = 0
        }
    }

    fun sendBinary(payload: ByteArray): Boolean {
        synchronized(lock) {
            if (state != State.ONLINE) return false
            val ws = socket ?: return false
            if (payload.size > Protocol.MAX_BINARY_BYTES) {
                emitAudioErrorLocked("frame_too_large", "audio frame exceeds the protocol limit", false)
                return false
            }
            return try {
                ws.send(ByteString.of(*payload))
            } catch (e: Exception) {
                false
            }
        }
    }

    /* --------------------------------------------------------------- connect */

    private fun beginConnectLocked() {
        val cfg = config ?: return
        if (released || manualClose) return
        generation += 1
        val gen = generation
        clientSeq = 0
        authSent = false
        resyncSent = false
        audioStartSeq = 0
        reqIdBySeq.clear()
        connectStartedAt = System.currentTimeMillis()
        lastServerRecvAt = connectStartedAt
        setState(State.CONNECTING)
        val client = obtainHttpClientLocked(cfg)
        if (client == null) {
            scheduleReconnectLocked()
            return
        }
        val request = try {
            buildRequest(cfg)
        } catch (e: Exception) {
            emitError("connect", 5001, "invalid server address", false)
            manualClose = true
            setState(State.FAILED)
            return
        }
        LogStore.info("ws.connect", "host" to cfg.host, "port" to cfg.port, "attempt" to reconnectAttempt)
        socket = client.newWebSocket(request, listenerFor(gen))
        startTimersLocked()
    }

    private fun obtainHttpClientLocked(cfg: ConnectionConfig): OkHttpClient? {
        val key = cfg.host + ":" + cfg.port + ":" + cfg.fingerprint
        val existing = httpClient
        if (existing != null && httpClientKey == key) return existing
        return try {
            val trustManager = PinnedTrustManager(cfg.fingerprint)
            val sslContext = SSLContext.getInstance("TLS")
            sslContext.init(null, arrayOf<X509TrustManager>(trustManager), SecureRandom())
            val client = OkHttpClient.Builder()
                .sslSocketFactory(sslContext.socketFactory, trustManager)
                // The fingerprint is the identity: Bridge uses a self-signed cert,
                // so hostname verification is replaced by certificate pinning.
                .hostnameVerifier { _, _ -> true }
                .connectionSpecs(listOf(ConnectionSpec.MODERN_TLS))
                .connectTimeout(CONNECT_TIMEOUT_MS, TimeUnit.MILLISECONDS)
                .readTimeout(0, TimeUnit.MILLISECONDS)
                .writeTimeout(20, TimeUnit.SECONDS)
                // D-023: WebSocket-level ping/pong detects black-hole networks
                // (TCP connection alive, no RST) in roughly 30-45 s. The app-layer
                // 15 s heartbeat and 45 s watchdog stay as protocol/business
                // fallbacks and are not the normal reconnect path.
                .pingInterval(15, TimeUnit.SECONDS)
                .retryOnConnectionFailure(true)
                .build()
            httpClient = client
            httpClientKey = key
            client
        } catch (e: Exception) {
            LogStore.error("tls.init_failed")
            null
        }
    }

    private fun buildRequest(cfg: ConnectionConfig): Request {
        val hostPart = if (cfg.host.contains(':') && !cfg.host.startsWith("[")) {
            "[" + cfg.host + "]"
        } else {
            cfg.host
        }
        return Request.Builder()
            .url("wss://$hostPart:${cfg.port}/ws")
            .header("Sec-WebSocket-Protocol", Protocol.SUBPROTOCOL)
            .build()
    }

    private fun listenerFor(gen: Int): WebSocketListener = object : WebSocketListener() {
        /**
         * OkHttp does not negotiate subprotocols by itself: the request header is
         * set manually in [buildRequest], and the server echo has to be validated
         * here (the handshake response is exposed by [onOpen]). A mismatch means
         * the server is not speaking `dsphone.v1`, so the socket is closed and the
         * client fails instead of sending v1 frames.
         */
        override fun onOpen(webSocket: WebSocket, response: Response) {
            val agreed = response.header("Sec-WebSocket-Protocol")
            if (agreed != Protocol.SUBPROTOCOL) {
                LogStore.warn("ws.subprotocol_mismatch", "agreed" to (agreed ?: "none"))
                try {
                    webSocket.close(ClosePolicy.PROTOCOL_ERROR, "subprotocol mismatch")
                } catch (ignored: Exception) {
                    // Socket is already unusable.
                }
                synchronized(lock) {
                    if (!released) {
                        manualClose = true
                        emitError(
                            "transport",
                            5001,
                            "server did not accept subprotocol " + Protocol.SUBPROTOCOL,
                            false,
                        )
                        setState(State.FAILED)
                    }
                }
                return
            }
            synchronized(lock) { onSocketOpenLocked(gen, webSocket) }
        }

        override fun onMessage(webSocket: WebSocket, text: String) {
            synchronized(lock) { onTextLocked(gen, text) }
        }

        override fun onMessage(webSocket: WebSocket, bytes: ByteString) {
            synchronized(lock) { onBinaryLocked(gen, bytes.size) }
        }

        override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
            try {
                webSocket.close(ClosePolicy.NORMAL, null)
            } catch (ignored: Exception) {
                // Already closing.
            }
        }

        override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
            synchronized(lock) { onSocketClosedLocked(gen, code, reason) }
        }

        override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
            synchronized(lock) { onSocketFailureLocked(gen, t) }
        }
    }

    /* ------------------------------------------------------------ callbacks */

    private fun onSocketOpenLocked(gen: Int, webSocket: WebSocket) {
        if (released || manualClose || gen != generation) {
            try {
                webSocket.close(ClosePolicy.NORMAL, null)
            } catch (ignored: Exception) {
                // Stale socket.
            }
            return
        }
        socket = webSocket
        lastServerRecvAt = System.currentTimeMillis()
        connectStartedAt = lastServerRecvAt
        sendHelloLocked()
    }

    private fun onSocketClosedLocked(gen: Int, code: Int, reason: String) {
        if (gen != generation) return
        generation += 1
        socket = null
        stopTimersLocked()
        cancelReconnectLocked()
        if (manualClose) {
            setState(State.CLOSED)
            return
        }
        LogStore.warn("ws.closed", "code" to code)
        when (ClosePolicy.actionFor(code)) {
            ClosePolicy.Action.NONE -> {
                setState(State.CLOSED)
                callbacks.onConnectionLost("closed")
            }
            ClosePolicy.Action.RECONNECT -> {
                callbacks.onConnectionLost("closed:$code")
                scheduleReconnectLocked()
            }
            ClosePolicy.Action.CLEAR_TOKEN_AND_PAIR -> {
                callbacks.onConnectionLost("auth_failed")
                sessionTokenBase64 = null
                tokenStore.clearSession()
                manualClose = true
                emitAuthFailureLocked(1001, "authentication failed; pair again", true)
            }
            ClosePolicy.Action.FATAL -> {
                callbacks.onConnectionLost("fatal:$code")
                manualClose = true
                emitError("transport", 5001, "connection closed with code $code", false)
                setState(State.FAILED)
            }
        }
    }

    private fun onSocketFailureLocked(gen: Int, t: Throwable) {
        if (gen != generation) return
        generation += 1
        socket = null
        stopTimersLocked()
        cancelReconnectLocked()
        val message = t.message ?: t.javaClass.simpleName
        LogStore.warn("ws.failure", "message" to message)
        if (manualClose) {
            setState(State.CLOSED)
            return
        }
        callbacks.onConnectionLost("failure")
        if (message.contains("fingerprint", ignoreCase = true)) {
            manualClose = true
            emitError("tls", 1001, "certificate fingerprint mismatch; re-pair with the correct fingerprint", false)
            setState(State.FAILED)
            return
        }
        scheduleReconnectLocked()
    }

    private fun onBinaryLocked(gen: Int, size: Int) {
        if (gen != generation) return
        lastServerRecvAt = System.currentTimeMillis()
        // PROTOCOL v1 is PCM C→S only; S→C binary frames are reserved.
        LogStore.debug("ws.binary_in", "bytes" to size)
    }

    private fun onTextLocked(gen: Int, text: String) {
        if (released || gen != generation) return
        lastServerRecvAt = System.currentTimeMillis()
        val obj = try {
            JSONObject(text)
        } catch (e: Exception) {
            LogStore.warn("ws.bad_json")
            return
        }
        val type = obj.optString("type", "")
        val seq = if (obj.has("seq")) obj.optInt("seq", 0) else 0
        if (seq > lastServerSeq) lastServerSeq = seq
        when (type) {
            "hello.ok" -> handleHelloOkLocked(obj)
            "pair.ok" -> handlePairOkLocked(obj)
            "pair.error" -> handlePairErrorLocked(obj)
            "auth.ok" -> handleAuthOkLocked(obj)
            "auth.error" -> handleAuthErrorLocked(obj)
            "pong" -> Unit
            "audio.ready" -> handleAudioReadyLocked(obj)
            "audio.error" -> handleAudioErrorLocked(obj)
            else -> forwardBusinessLocked(type, obj)
        }
    }

    /* --------------------------------------------------------------- handshake */

    private fun handleHelloOkLocked(obj: JSONObject) {
        serverId = optStringOrNull(obj, "serverId")
        val needPair = obj.optBoolean("needPair", false)
        if (needPair || currentTokenLocked() == null) {
            setState(State.PAIRING)
            val pair = pendingPair
            pendingPair = null
            if (pair != null) sendPairRequestLocked(pair.first, pair.second)
        } else {
            setState(State.AUTHENTICATING)
            sendAuthLocked()
        }
    }

    private fun handlePairOkLocked(obj: JSONObject) {
        val token = obj.optString("deviceToken", "")
        if (token.isEmpty()) {
            emitError("pair", 1002, "server did not return a device token", false)
            return
        }
        sessionTokenBase64 = token
        val sid = optStringOrNull(obj, "serverId") ?: serverId ?: ""
        serverId = sid
        val cfg = config
        if (cfg != null) {
            val fingerprint = if (Pairing.isValidFingerprint(obj.optString("serverFingerprint", ""))) {
                Pairing.normalizeFingerprint(obj.optString("serverFingerprint", ""))
            } else {
                cfg.fingerprint
            }
            tokenStore.saveSession(sid, cfg.host, cfg.port, fingerprint, token)
        }
        val event = JsonObj().put("type", "pair.result").put("ok", true)
        if (sid.isNotEmpty()) event.put("serverId", sid)
        callbacks.emitEvent(event.toJsonText())
        LogStore.info("pair.ok", "serverId" to sid)
        setState(State.AUTHENTICATING)
        sendAuthLocked()
    }

    private fun handlePairErrorLocked(obj: JSONObject) {
        val event = JsonObj()
            .put("type", "pair.result")
            .put("ok", false)
            .put("code", obj.optInt("code", 1002))
            .put("message", obj.optString("message", ""))
        if (obj.has("attemptsLeft")) event.put("attemptsLeft", obj.optInt("attemptsLeft", 0))
        if (obj.has("retryable")) event.put("retryable", obj.optBoolean("retryable", false))
        callbacks.emitEvent(event.toJsonText())
        setState(State.PAIRING)
    }

    private fun handleAuthOkLocked(obj: JSONObject) {
        serverId = optStringOrNull(obj, "serverId") ?: serverId
        val sessionsRaw = obj.optJSONArray("sessions")?.toString() ?: "[]"
        val event = JsonObj().put("type", "auth.result").put("ok", true)
        if (serverId != null) event.put("serverId", serverId)
        event.putRaw("sessions", sessionsRaw)
        callbacks.emitEvent(event.toJsonText())
        reconnectAttempt = 0
        setState(State.ONLINE)
        if (!resyncSent && lastServerSeq > 0) {
            resyncSent = true
            sendTextLocked(Protocol.resync(nextSeqLocked(), lastServerSeq))
            LogStore.info("ws.resync_sent", "lastSeq" to lastServerSeq)
        }
        flushPendingLocked()
        callbacks.onOnline()
    }

    private fun handleAuthErrorLocked(obj: JSONObject) {
        val code = obj.optInt("code", 1001)
        val needPair = obj.optBoolean("needPair", false)
        callbacks.onConnectionLost("auth_error")
        sessionTokenBase64 = null
        tokenStore.clearSession()
        manualClose = true
        cancelReconnectLocked()
        emitAuthFailureLocked(code, obj.optString("message", ""), needPair)
        closeSocketLocked()
    }

    private fun emitAuthFailureLocked(code: Int, message: String, needPair: Boolean) {
        val event = JsonObj()
            .put("type", "auth.result")
            .put("ok", false)
            .put("code", code)
            .put("message", message)
            .put("needPair", needPair)
        callbacks.emitEvent(event.toJsonText())
        if (needPair) setState(State.PAIRING) else setState(State.FAILED)
    }

    private fun sendHelloLocked() {
        val cfg = config ?: return
        sendTextLocked(Protocol.hello(nextSeqLocked(), cfg.deviceId, appVersion, nowSec()))
    }

    private fun sendPairRequestLocked(code: String, deviceName: String) {
        val cfg = config ?: return
        sendTextLocked(Protocol.pairRequest(nextSeqLocked(), cfg.deviceId, code, deviceName))
    }

    private fun sendAuthLocked() {
        if (authSent) return
        val cfg = config ?: return
        val token = currentTokenLocked()
        if (token == null) {
            emitAuthFailureLocked(1001, "no device token; pair first", true)
            return
        }
        val tokenBytes = try {
            Protocol.decodeBase64(token)
        } catch (e: Exception) {
            tokenStore.clearSession()
            sessionTokenBase64 = null
            emitAuthFailureLocked(1001, "stored token is corrupt; pair again", true)
            return
        }
        val nonce = Protocol.randomNonceBase64()
        val ts = nowSec()
        val canonical = Protocol.authCanonical(cfg.deviceId, ts, nonce)
        val hmac = try {
            Protocol.hmacBase64(tokenBytes, canonical)
        } catch (e: Exception) {
            emitAuthFailureLocked(1001, "failed to sign authentication", true)
            return
        }
        authSent = true
        setState(State.AUTHENTICATING)
        sendTextLocked(Protocol.auth(nextSeqLocked(), cfg.deviceId, ts, nonce, hmac))
    }

    /* --------------------------------------------------------------- inbound */

    private fun handleAudioReadyLocked(obj: JSONObject) {
        val asrMode = obj.optString("asrMode", "")
        val lang = obj.optString("lang", "")
        val codecs = parseStringArray(obj.optJSONArray("codecs"))
        val event = JsonObj().put("type", "audio.ready")
        if (asrMode.isNotEmpty()) event.put("asrMode", asrMode)
        if (lang.isNotEmpty()) event.put("lang", lang)
        event.putRaw("codecs", JSONArray(codecs).toString())
        callbacks.emitEvent(event.toJsonText())
        audioStartSeq = 0
        callbacks.onAudioReady()
    }

    private fun handleAudioErrorLocked(obj: JSONObject) {
        emitAudioErrorLocked(
            obj.optInt("code", 5001),
            obj.optString("message", ""),
            obj.optBoolean("retryable", false),
        )
        audioStartSeq = 0
        callbacks.onAudioError()
    }

    private fun forwardBusinessLocked(type: String, obj: JSONObject) {
        val replyTo = if (obj.has("replyTo")) obj.optInt("replyTo", -1) else -1
        if (replyTo > 0) {
            val reqId = reqIdBySeq.remove(replyTo)
            if (reqId != null) {
                try {
                    obj.put("_reqTo", reqId)
                } catch (ignored: Exception) {
                    // Correlation is best effort.
                }
            }
        }
        if (type == "error" && audioStartSeq > 0 && replyTo == audioStartSeq) {
            emitAudioErrorLocked(
                obj.optInt("code", 5001),
                obj.optString("message", ""),
                obj.optBoolean("retryable", false),
            )
            audioStartSeq = 0
            callbacks.onAudioError()
            return
        }
        callbacks.emitEvent(
            JsonObj()
                .put("type", "proto.message")
                .putRaw("message", obj.toString())
                .toJsonText(),
        )
    }

    /* --------------------------------------------------------------- sending */

    private fun sendUserJsonLocked(obj: JSONObject): Boolean {
        var reqId: String? = null
        if (obj.has("_reqId")) {
            reqId = obj.optString("_reqId", "").takeIf { it.isNotEmpty() }
            obj.remove("_reqId")
        }
        val seq = nextSeqLocked()
        try {
            obj.put("seq", seq)
        } catch (e: Exception) {
            emitError("protocol", 5002, "failed to inject seq", false)
            return false
        }
        val text = obj.toString()
        if (text.toByteArray(Charsets.UTF_8).size > Protocol.MAX_JSON_BYTES) {
            emitError("protocol", 4400, "outbound message exceeds 64 KiB", false)
            return false
        }
        if (reqId != null) rememberReqIdLocked(seq, reqId)
        return sendTextLocked(text)
    }

    private fun sendTextLocked(text: String): Boolean {
        val ws = socket
        if (ws == null) {
            LogStore.warn("ws.send_no_socket")
            return false
        }
        return try {
            ws.send(text)
        } catch (e: Exception) {
            LogStore.warn("ws.send_failed")
            false
        }
    }

    private fun flushPendingLocked() {
        if (pendingOutbound.isEmpty()) return
        val queued = ArrayList<String>(pendingOutbound)
        pendingOutbound.clear()
        for (raw in queued) {
            try {
                sendUserJsonLocked(JSONObject(raw))
            } catch (ignored: Exception) {
                // Drop malformed queued message.
            }
        }
    }

    /* ---------------------------------------------------------------- timers */

    private fun startTimersLocked() {
        stopTimersLocked()
        heartbeatTask = try {
            scheduler.scheduleAtFixedRate(
                { synchronized(lock) { heartbeatTickLocked() } },
                HEARTBEAT_INTERVAL_MS,
                HEARTBEAT_INTERVAL_MS,
                TimeUnit.MILLISECONDS,
            )
        } catch (e: Exception) {
            null
        }
        watchdogTask = try {
            scheduler.scheduleAtFixedRate(
                { synchronized(lock) { watchdogTickLocked() } },
                WATCHDOG_INTERVAL_MS,
                WATCHDOG_INTERVAL_MS,
                TimeUnit.MILLISECONDS,
            )
        } catch (e: Exception) {
            null
        }
    }

    private fun stopTimersLocked() {
        heartbeatTask?.cancel(false)
        heartbeatTask = null
        watchdogTask?.cancel(false)
        watchdogTask = null
    }

    private fun heartbeatTickLocked() {
        if (released || manualClose || socket == null) return
        val current = state
        if (current == State.ONLINE || current == State.AUTHENTICATING || current == State.PAIRING) {
            sendTextLocked(Protocol.ping(nextSeqLocked(), nowSec()))
        }
    }

    private fun watchdogTickLocked() {
        if (released || manualClose) return
        val ws = socket ?: return
        val now = System.currentTimeMillis()
        if (now - lastServerRecvAt > SERVER_SILENCE_TIMEOUT_MS) {
            LogStore.warn("ws.heartbeat_timeout")
            socket = null
            stopTimersLocked()
            try {
                ws.cancel()
            } catch (ignored: Exception) {
                // Already gone.
            }
            callbacks.onConnectionLost("heartbeat_timeout")
            scheduleReconnectLocked()
            return
        }
        if (state == State.CONNECTING && now - connectStartedAt > CONNECT_TIMEOUT_MS) {
            LogStore.warn("ws.connect_timeout")
            socket = null
            stopTimersLocked()
            try {
                ws.cancel()
            } catch (ignored: Exception) {
                // Already gone.
            }
            scheduleReconnectLocked()
        }
    }

    /* ------------------------------------------------------------- reconnect */

    private fun scheduleReconnectLocked() {
        if (released || manualClose) return
        if (config == null) return
        cancelReconnectLocked()
        stopTimersLocked()
        val attempt = reconnectAttempt
        reconnectAttempt += 1
        val delay = BackoffPolicy.delayMs(attempt, 0.8 + 0.4 * random.nextDouble())
        setState(State.RECONNECTING)
        LogStore.info("ws.reconnect_scheduled", "attempt" to attempt, "delayMs" to delay)
        reconnectTask = try {
            scheduler.schedule(
                {
                    synchronized(lock) {
                        if (released || manualClose || config == null) return@synchronized
                        if (state != State.RECONNECTING) return@synchronized
                        beginConnectLocked()
                    }
                },
                delay,
                TimeUnit.MILLISECONDS,
            )
        } catch (e: Exception) {
            null
        }
    }

    private fun cancelReconnectLocked() {
        reconnectTask?.cancel(false)
        reconnectTask = null
    }

    private fun closeSocketLocked() {
        generation += 1
        stopTimersLocked()
        val ws = socket
        socket = null
        if (ws != null) {
            try {
                ws.close(ClosePolicy.NORMAL, null)
            } catch (ignored: Exception) {
                // Already closed.
            }
        }
    }

    /* ---------------------------------------------------------------- helpers */

    private fun isConnectingState(): Boolean = when (state) {
        State.CONNECTING, State.RECONNECTING, State.PAIRING, State.AUTHENTICATING -> true
        else -> false
    }

    private fun nextSeqLocked(): Int {
        clientSeq += 1
        return clientSeq
    }

    private fun nowSec(): Long = System.currentTimeMillis() / 1000L

    private fun currentTokenLocked(): String? = sessionTokenBase64 ?: tokenStore.loadSession()?.tokenBase64

    private fun rememberReqIdLocked(seq: Int, reqId: String) {
        reqIdBySeq[seq] = reqId
        while (reqIdBySeq.size > MAX_REQ_ID_ENTRIES) {
            val eldest = reqIdBySeq.keys.firstOrNull() ?: break
            reqIdBySeq.remove(eldest)
        }
    }

    private fun optStringOrNull(obj: JSONObject, name: String): String? {
        if (!obj.has(name)) return null
        val value = obj.optString(name, "")
        return if (value.isEmpty()) null else value
    }

    private fun parseStringArray(array: JSONArray?): List<String> {
        if (array == null) return emptyList()
        val result = ArrayList<String>(array.length())
        for (i in 0 until array.length()) {
            val value = array.optString(i, "")
            if (value.isNotEmpty()) result.add(value)
        }
        return result
    }

    private fun setState(newState: State) {
        state = newState
        val event = JsonObj().put("type", "net.state").put("state", newState.wire)
        if (serverId != null) event.put("serverId", serverId)
        callbacks.emitEvent(event.toJsonText())
    }

    private fun emitError(scope: String, code: Int, message: String, retryable: Boolean) {
        callbacks.emitEvent(
            JsonObj()
                .put("type", "error")
                .put("scope", scope)
                .put("code", code)
                .put("message", message)
                .put("retryable", retryable)
                .toJsonText(),
        )
    }

    private fun emitAudioErrorLocked(code: Any, message: String?, retryable: Boolean) {
        val event = JsonObj().put("type", "audio.error")
        when (code) {
            is Int -> event.put("code", code)
            is Long -> event.put("code", code)
            else -> event.put("code", code.toString())
        }
        event.put("message", message ?: "")
        event.put("retryable", retryable)
        callbacks.emitEvent(event.toJsonText())
    }

    private companion object {
        val HANDSHAKE_TYPES: Set<String> = setOf("hello", "pair.request", "auth", "ping", "resync")

        const val HEARTBEAT_INTERVAL_MS = 15_000L
        const val WATCHDOG_INTERVAL_MS = 5_000L
        const val SERVER_SILENCE_TIMEOUT_MS = 45_000L
        const val CONNECT_TIMEOUT_MS = 15_000L
        const val MAX_PENDING_OUTBOUND = 64
        const val MAX_REQ_ID_ENTRIES = 128
    }
}

/**
 * Verifies the server certificate against the SHA-256 fingerprint from the
 * pairing URI (TLS ≥ 1.2 is enforced by `ConnectionSpec.MODERN_TLS`).
 *
 * The Bridge uses a self-signed certificate, so a custom trust manager replaces
 * the system CA store; hostname verification is also replaced by pinning.
 */
private class PinnedTrustManager(private val expectedFingerprint: String) : X509TrustManager {

    override fun checkClientTrusted(chain: Array<X509Certificate>, authType: String) {
        throw CertificateException("client certificates are not supported")
    }

    override fun checkServerTrusted(chain: Array<X509Certificate>, authType: String) {
        if (chain.isEmpty()) throw CertificateException("empty certificate chain")
        val actual = Hex.encode(MessageDigest.getInstance("SHA-256").digest(chain[0].encoded))
        if (!actual.equals(expectedFingerprint, ignoreCase = true)) {
            throw CertificateException("certificate fingerprint mismatch")
        }
    }

    override fun getAcceptedIssuers(): Array<X509Certificate> = emptyArray()
}
