package com.dsh.phone

import android.Manifest
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.content.Context
import android.content.pm.PackageManager
import android.os.Build
import com.dsh.phone.audio.AudioSession
import com.dsh.phone.audio.BtRouter
import com.dsh.phone.log.LogStore
import com.dsh.phone.net.WssClient
import com.dsh.phone.protocol.Pairing
import com.dsh.phone.service.MicService
import com.dsh.phone.storage.SettingsStore
import com.dsh.phone.storage.TokenStore
import com.dsh.phone.util.JsonObj

/**
 * Process-wide wiring between the JsBridge, the WSS client and the audio stack.
 *
 * Owns nothing protocol-specific: `seq` lives in [WssClient], frame numbering in
 * [AudioSession], and the WebView only ever sees JSON strings through the event
 * sink installed by the bridge.
 */
object AppCore {

    private var appContext: Context? = null
    private var settingsStore: SettingsStore? = null
    private var tokenStore: TokenStore? = null
    private var wss: WssClient? = null
    private var audio: AudioSession? = null
    private var btRouter: BtRouter? = null
    private var eventSink: ((String) -> Unit)? = null
    private var initialized = false

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

    fun connect(host: String, port: Int, fingerprint: String, deviceId: String) {
        val effectiveDeviceId = deviceId.ifBlank { deviceId() }
        wss?.connect(host, port, fingerprint, effectiveDeviceId)
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
            current != WssClient.State.FAILED
        ) {
            return
        }
        LogStore.info("net.auto_connect", "host" to session.host, "port" to session.port)
        client.connect(session.host, session.port, session.fingerprint, deviceId())
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
}
