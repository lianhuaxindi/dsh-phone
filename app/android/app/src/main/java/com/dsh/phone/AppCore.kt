package com.dsh.phone

import android.Manifest
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.content.Context
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import com.dsh.phone.audio.AudioSession
import com.dsh.phone.audio.BtRouter
import com.dsh.phone.log.LogStore
import com.dsh.phone.media.ImagePicker
import com.dsh.phone.net.WssClient
import com.dsh.phone.protocol.Pairing
import com.dsh.phone.service.MicService
import com.dsh.phone.storage.SettingsStore
import com.dsh.phone.storage.TokenStore
import com.dsh.phone.util.Json
import com.dsh.phone.util.JsonObj
import java.util.concurrent.Executors

/**
 * Process-wide wiring between the JsBridge, the WSS client and the audio stack.
 *
 * Owns nothing protocol-specific: `seq` lives in [WssClient], frame numbering in
 * [AudioSession], and the WebView only ever sees JSON strings through the event
 * sink installed by the bridge.
 */
object AppCore {

    const val SOURCE_CAMERA = "camera"
    const val SOURCE_GALLERY = "gallery"

    /**
     * UI callbacks implemented by [MainActivity]. Keeping this interface here (and
     * not in the Activity) lets [DshBridge] start a scan or an image pick without
     * holding an Activity reference, and keeps protocol rules in this singleton.
     */
    interface Host {
        fun launchQrScanner()
        fun launchImagePicker(source: String): Boolean
    }

    private var appContext: Context? = null
    private var settingsStore: SettingsStore? = null
    private var tokenStore: TokenStore? = null
    private var wss: WssClient? = null
    private var audio: AudioSession? = null
    private var btRouter: BtRouter? = null
    private var eventSink: ((String) -> Unit)? = null
    private var initialized = false
    private var host: Host? = null
    private var pendingScan = false
    private var pendingPick: String? = null
    private val imageExecutor = Executors.newSingleThreadExecutor { runnable ->
        Thread(runnable, "dsh-image-pick").apply { isDaemon = true }
    }

    private val callbacks = object : WssClient.Callbacks {
        override fun emitEvent(json: String) {
            emit(json)
        }

        override fun onAudioReady() {
            audio?.onTransportAudioReady()
        }

        override fun onAudioError() {
            audio?.onTransportAudioError()
        }

        override fun onConnectionLost(reason: String) {
            audio?.onTransportLost(reason)
        }

        override fun onOnline() {
            LogStore.info("net.online")
        }
    }

    fun init(context: Context) {
        if (initialized) return
        val ctx = context.applicationContext
        appContext = ctx
        LogStore.init(ctx)
        val settings = SettingsStore(ctx)
        val tokens = TokenStore(ctx)
        val router = BtRouter(ctx, settings)
        val client = WssClient(tokens, settings, BuildConfig.VERSION_NAME, callbacks)
        settingsStore = settings
        tokenStore = tokens
        btRouter = router
        wss = client
        audio = AudioSession(ctx, client, router) { json -> emit(json) }
        initialized = true
        LogStore.info("app.core_init", "version" to BuildConfig.VERSION_NAME)
    }

    /** The bridge installs a sink that posts JSON into the WebView. */
    fun setEventSink(sink: ((String) -> Unit)?) {
        eventSink = sink
    }

    /** [MainActivity] registers itself on create and clears itself when finishing. */
    fun setHost(host: Host?) {
        this.host = host
        if (host == null) {
            pendingScan = false
            pendingPick = null
        }
    }

    fun emit(json: String) {
        val sink = eventSink
        if (sink == null) {
            LogStore.debug("event.dropped")
            return
        }
        try {
            sink(json)
        } catch (e: Exception) {
            LogStore.warn("event.sink_failed")
        }
    }

    fun deviceId(): String = settingsStore?.deviceId() ?: "dev-unknown"

    /* ------------------------------------------------------------------ network */

    fun connect(
        host: String,
        port: Int,
        fingerprint: String,
        deviceId: String,
        hosts: List<String> = emptyList(),
    ) {
        val effectiveDeviceId = deviceId.ifBlank { deviceId() }
        wss?.connect(host, port, fingerprint, effectiveDeviceId, hosts)
    }

    fun disconnect() {
        wss?.disconnect()
    }

    fun reconnect() {
        wss?.reconnect()
    }

    fun pair(code: String, deviceName: String) {
        wss?.pair(code, deviceName)
    }

    fun auth() {
        wss?.authNow()
    }

    fun send(json: String): Boolean {
        val client = wss ?: return false
        if (!client.isOnline()) {
            LogStore.warn("bridge.send_offline")
        }
        return client.sendUserJson(json)
    }

    fun isOnline(): Boolean = wss?.isOnline() == true

    /** Reconnect automatically after a restart when a paired session exists. */
    fun autoConnectIfPaired() {
        val client = wss ?: return
        val session = tokenStore?.loadSession() ?: return
        if (session.host.isBlank() || session.port !in 1..65535) return
        if (!Pairing.isValidFingerprint(session.fingerprint)) return
        val current = client.currentState
        if (current != WssClient.State.IDLE &&
            current != WssClient.State.CLOSED &&
            current != WssClient.State.ERROR
        ) {
            return
        }
        LogStore.info("net.auto_connect", "host" to session.host, "port" to session.port)
        // D-031: prefer the last address that worked, then the paired host, then
        // everything remembered from earlier pairing URIs.
        val known = settingsStore?.knownHosts().orEmpty()
        val last = settingsStore?.lastHost()?.takeIf { it.isNotBlank() }
        val candidates = (listOfNotNull(last) + known)
            .filter { it.isNotBlank() && it != session.host }
            .distinct()
        client.connect(session.host, session.port, session.fingerprint, deviceId(), candidates)
    }

    /* -------------------------------------------------------------------- audio */

    fun startAudio(sampleRate: Int, lang: String, mode: String, sessionId: String?) {
        audio?.start(sampleRate, lang, mode, sessionId)
    }

    fun stopAudio() {
        audio?.stop()
    }

    fun cancelAudio() {
        audio?.cancel()
    }

    fun routeInfo() {
        val info = btRouter?.routeInfo()
        emit(
            JsonObj()
                .put("type", "audio.route")
                .put("route", info?.route ?: BtRouter.ROUTE_PHONE)
                .put("deviceName", info?.deviceName ?: "")
                .toJsonText(),
        )
    }

    fun preferDevice(address: String?) {
        val settings = settingsStore ?: return
        val normalized = address?.takeIf { it.isNotBlank() }
        settings.setPreferredBtAddress(normalized)
        emit(JsonObj().put("type", "settings.audioRoute").put("address", normalized).toJsonText())
    }

    /* ------------------------------------------------------- pairing scan */

    /**
     * `scanPairing` (ARCHITECTURE §3.2 / D-030). Runs the Camera2 + ZXing scanner
     * Activity, validates the QR payload as a pairing URI and reports through
     * `scanPairing.result`. Manual entry stays available in the Web UI, so every
     * failure is a typed reason rather than a dead end.
     */
    fun requestScanPairing() {
        val target = host
        if (target == null) {
            emitScanFailure(SCAN_UNAVAILABLE)
            return
        }
        if (pendingScan) {
            emitScanFailure(SCAN_BUSY)
            return
        }
        pendingScan = true
        try {
            target.launchQrScanner()
        } catch (e: Exception) {
            pendingScan = false
            LogStore.warn("scan.launch_failed")
            emitScanFailure(SCAN_FAILED)
        }
    }

    fun onScanResult(uri: String?, reason: String?) {
        if (!pendingScan) {
            LogStore.debug("scan.unexpected_result")
            return
        }
        pendingScan = false
        val failure = reason?.takeIf { it.isNotBlank() }
        if (failure != null) {
            emitScanFailure(failure)
            return
        }
        val raw = uri?.trim()
        if (raw.isNullOrEmpty()) {
            emitScanFailure(SCAN_DECODE_FAILED)
            return
        }
        val info = try {
            Pairing.parseUri(raw)
        } catch (e: Exception) {
            LogStore.warn("scan.bad_uri", "message" to (e.message ?: "invalid"))
            emitScanFailure(SCAN_DECODE_FAILED)
            return
        }
        if (!Pairing.isValidFingerprint(info.fingerprint)) {
            LogStore.warn("scan.bad_fingerprint")
            emitScanFailure(SCAN_DECODE_FAILED)
            return
        }
        val event = JsonObj()
            .put("type", "scanPairing.result")
            .put("ok", true)
            .put("uri", raw)
            .put("host", info.host)
            .put("port", info.port)
            .put("fp", info.fingerprint)
        if (!info.code.isNullOrBlank()) event.put("code", info.code)
        if (!info.serverId.isNullOrBlank()) event.put("sid", info.serverId)
        if (info.hosts.isNotEmpty()) event.putRaw("hosts", jsonStringArray(info.hosts))
        if (!info.name.isNullOrBlank()) event.put("name", info.name)
        LogStore.info("scan.paired_uri", "host" to info.host, "hosts" to info.hosts.size)
        emit(event.toJsonText())
    }

    private fun emitScanFailure(reason: String) {
        emit(
            JsonObj()
                .put("type", "scanPairing.result")
                .put("ok", false)
                .put("reason", reason)
                .toJsonText(),
        )
    }

    /* ------------------------------------------------------------ image pick */

    /**
     * `pickImage` (ARCHITECTURE §3.2 / D-029). The Activity returns a content URI;
     * decoding, downscaling (long edge ≤1600) and JPEG q≈0.8 re-encoding happen
     * on a background thread here so the WebView never blocks and the base64
     * payload is already within the chat.send.images limits.
     */
    fun requestPickImage(source: String) {
        val target = host
        if (target == null) {
            emitPickFailure(PICK_FAILED)
            return
        }
        val normalized = source.trim().lowercase()
        if (normalized != SOURCE_CAMERA && normalized != SOURCE_GALLERY) {
            emitPickFailure(PICK_UNSUPPORTED_SOURCE)
            return
        }
        if (pendingPick != null) {
            emitPickFailure(PICK_BUSY)
            return
        }
        pendingPick = normalized
        val launched = try {
            target.launchImagePicker(normalized)
        } catch (e: Exception) {
            LogStore.warn("image.launch_failed")
            false
        }
        if (!launched) {
            pendingPick = null
            emitPickFailure(PICK_FAILED)
        }
    }

    fun onImagePicked(uri: Uri?) {
        val pending = pendingPick ?: return
        pendingPick = null
        val ctx = appContext
        if (uri == null || ctx == null) {
            emitPickFailure(PICK_CANCELLED)
            return
        }
        imageExecutor.execute {
            try {
                val picked = ImagePicker.load(ctx, uri)
                val event = JsonObj()
                    .put("type", "image.picked")
                    .put("data", picked.base64)
                    .put("mimeType", picked.mimeType)
                    .put("name", picked.name)
                    .put("width", picked.width)
                    .put("height", picked.height)
                LogStore.info(
                    "image.picked",
                    "source" to pending,
                    "bytes" to picked.base64.length,
                    "width" to picked.width,
                    "height" to picked.height,
                )
                emit(event.toJsonText())
            } catch (e: ImagePicker.PickException) {
                LogStore.warn("image.pick_failed", "reason" to e.reason)
                emitPickFailure(e.reason)
            } catch (e: Exception) {
                LogStore.warn("image.pick_failed")
                emitPickFailure(PICK_FAILED)
            }
        }
    }

    fun onImagePickCancelled() {
        if (pendingPick == null) return
        pendingPick = null
        emitPickFailure(PICK_CANCELLED)
    }

    fun onImagePickFailed(reason: String) {
        if (pendingPick == null) return
        pendingPick = null
        emitPickFailure(reason)
    }

    private fun emitPickFailure(reason: String) {
        emit(
            JsonObj()
                .put("type", "image.pickFailed")
                .put("reason", reason)
                .toJsonText(),
        )
    }

    private fun jsonStringArray(values: List<String>): String {
        val builder = StringBuilder("[")
        for ((index, value) in values.withIndex()) {
            if (index > 0) builder.append(',')
            builder.append(Json.quote(value))
        }
        builder.append(']')
        return builder.toString()
    }

    /* ---------------------------------------------------------------- token/logs */

    fun getToken() {
        val session = tokenStore?.loadSession()
        val event = JsonObj().put("type", "token").put("hasToken", session != null)
        if (session != null) {
            event.put("host", session.host)
            event.put("port", session.port)
            event.put("serverId", session.serverId)
        }
        emit(event.toJsonText())
    }

    fun setToken(token: String?, host: String?, port: Int?, serverId: String?) {
        val store = tokenStore ?: return
        if (token.isNullOrBlank()) {
            store.clearSession()
        } else {
            store.saveSession(
                serverId ?: "",
                host ?: "",
                port ?: 0,
                "",
                token,
            )
        }
        getToken()
    }

    fun exportLogs() {
        val result = LogStore.export()
        emit(
            JsonObj()
                .put("type", "logs.exported")
                .put("text", result.text)
                .put("truncated", result.truncated)
                .toJsonText(),
        )
    }

    /* -------------------------------------------------------------- notifications */

    fun notify(title: String, text: String) {
        val ctx = appContext ?: return
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
            ctx.checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) !=
            PackageManager.PERMISSION_GRANTED
        ) {
            LogStore.warn("notify.permission_denied")
            return
        }
        try {
            val manager = ctx.getSystemService(Context.NOTIFICATION_SERVICE) as? NotificationManager
                ?: return
            ensureAlertChannel(ctx, manager)
            val notification = Notification.Builder(ctx, CHANNEL_ALERTS)
                .setSmallIcon(R.drawable.ic_notification)
                .setContentTitle(title)
                .setContentText(text)
                .setAutoCancel(true)
                .build()
            manager.notify(ALERT_NOTIFICATION_ID, notification)
        } catch (e: Exception) {
            LogStore.warn("notify.failed")
        }
    }

    private fun ensureAlertChannel(ctx: Context, manager: NotificationManager) {
        try {
            val channel = NotificationChannel(
                CHANNEL_ALERTS,
                ctx.getString(R.string.notification_channel_alerts),
                NotificationManager.IMPORTANCE_HIGH,
            )
            channel.description = ctx.getString(R.string.notification_channel_alerts_desc)
            manager.createNotificationChannel(channel)
        } catch (e: Exception) {
            LogStore.warn("notify.channel_failed")
        }
    }

    /* ----------------------------------------------------------------- lifecycle */

    fun onActivityStart(context: Context) {
        MicService.onActivityStart(context)
    }

    fun onActivityStop(context: Context) {
        MicService.onActivityStop(context)
    }

    fun shutdown() {
        try {
            audio?.release()
        } catch (ignored: Exception) {
            // Best effort.
        }
        try {
            wss?.release()
        } catch (ignored: Exception) {
            // Best effort.
        }
        audio = null
        wss = null
        eventSink = null
        initialized = false
    }

    private const val CHANNEL_ALERTS = "dsh_alerts"
    private const val ALERT_NOTIFICATION_ID = 1002

    private const val SCAN_UNAVAILABLE = "scan_unavailable"
    private const val SCAN_BUSY = "scan_busy"
    private const val SCAN_FAILED = "scan_failed"
    private const val SCAN_DECODE_FAILED = "decode_failed"

    private const val PICK_BUSY = "pick_busy"
    private const val PICK_CANCELLED = "pick_cancelled"
    private const val PICK_FAILED = "pick_failed"
    private const val PICK_UNSUPPORTED_SOURCE = "unsupported_source"
}
