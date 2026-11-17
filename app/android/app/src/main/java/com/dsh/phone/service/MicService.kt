package com.dsh.phone.service

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import com.dsh.phone.MainActivity
import com.dsh.phone.R
import com.dsh.phone.log.LogStore

/**
 * Foreground service of type `microphone` that keeps audio capture and the WSS
 * connection alive while the app is in the background (PRD FR-408/§15: five
 * minutes of locked-screen usability).
 *
 * The service is started on PTT down and stays up for [KEEP_ALIVE_MS] after the
 * activity goes to the background, then stops itself unless a recording is
 * still active.
 */
class MicService : Service() {

    private val handler = Handler(Looper.getMainLooper())
    private var keepAliveScheduled = false
    private var stopped = false

    private val keepAliveRunnable = Runnable {
        keepAliveScheduled = false
        if (!recordingActive) {
            stopForegroundAndSelf()
        } else {
            scheduleKeepAlive()
        }
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        instance = this
        stopped = false
        createNotificationChannels()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_START, ACTION_KEEPALIVE, null -> {
                cancelKeepAlive()
                startForegroundCompat()
                if (intent?.action == ACTION_START) {
                    refreshNotification()
                }
            }
            ACTION_STOP -> stopForegroundAndSelf()
            else -> startForegroundCompat()
        }
        return Service.START_NOT_STICKY
    }

    override fun onDestroy() {
        stopped = true
        cancelKeepAlive()
        instance = null
        LogStore.info("service.destroyed")
        super.onDestroy()
    }

    /* --------------------------------------------------------------- foreground */

    private fun startForegroundCompat() {
        val notification = buildNotification()
        try {
            startForeground(
                NOTIFICATION_ID,
                notification,
                ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE,
            )
        } catch (e: Exception) {
            LogStore.warn("service.foreground_failed")
        }
    }

    private fun stopForegroundAndSelf() {
        if (stopped) return
        stopped = true
        cancelKeepAlive()
        try {
            stopForeground(Service.STOP_FOREGROUND_REMOVE)
        } catch (ignored: Exception) {
            // Already stopped.
        }
        stopSelf()
    }

    private fun createNotificationChannels() {
        val manager = getSystemService(Context.NOTIFICATION_SERVICE) as? NotificationManager ?: return
        try {
            val microphone = NotificationChannel(
                CHANNEL_MIC,
                getString(R.string.notification_channel_mic),
                NotificationManager.IMPORTANCE_LOW,
            )
            microphone.description = getString(R.string.notification_channel_mic_desc)
            microphone.setShowBadge(false)
            val alerts = NotificationChannel(
                CHANNEL_ALERTS,
                getString(R.string.notification_channel_alerts),
                NotificationManager.IMPORTANCE_HIGH,
            )
            alerts.description = getString(R.string.notification_channel_alerts_desc)
            manager.createNotificationChannels(listOf(microphone, alerts))
        } catch (e: Exception) {
            LogStore.warn("service.channel_failed")
        }
    }

    private fun buildNotification(): Notification {
        val launchIntent = Intent(this, MainActivity::class.java).apply {
            flags = Intent.FLAG_ACTIVITY_SINGLE_TOP or Intent.FLAG_ACTIVITY_CLEAR_TOP
        }
        var pendingIntentFlags = PendingIntent.FLAG_UPDATE_CURRENT
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
            pendingIntentFlags = pendingIntentFlags or PendingIntent.FLAG_IMMUTABLE
        }
        val contentIntent = PendingIntent.getActivity(this, 0, launchIntent, pendingIntentFlags)
        val text = if (recordingActive) {
            R.string.notification_mic_text_recording
        } else {
            R.string.notification_mic_text_ready
        }
        val builder = Notification.Builder(this, CHANNEL_MIC)
            .setSmallIcon(R.drawable.ic_notification)
            .setContentTitle(getString(R.string.notification_mic_title))
            .setContentText(getString(text))
            .setContentIntent(contentIntent)
            .setOngoing(true)
            .setShowWhen(false)
            .setCategory(Notification.CATEGORY_SERVICE)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            builder.setForegroundServiceBehavior(Notification.FOREGROUND_SERVICE_IMMEDIATE)
        }
        return builder.build()
    }

    private fun refreshNotification() {
        if (stopped) return
        try {
            val manager = getSystemService(Context.NOTIFICATION_SERVICE) as? NotificationManager ?: return
            manager.notify(NOTIFICATION_ID, buildNotification())
        } catch (e: Exception) {
            LogStore.warn("service.notify_failed")
        }
    }

    /* --------------------------------------------------------------- keep-alive */

    private fun scheduleKeepAlive() {
        if (keepAliveScheduled || stopped) return
        keepAliveScheduled = true
        handler.postDelayed(keepAliveRunnable, KEEP_ALIVE_MS)
    }

    private fun cancelKeepAlive() {
        if (!keepAliveScheduled) return
        keepAliveScheduled = false
        handler.removeCallbacks(keepAliveRunnable)
    }

    companion object {
        const val ACTION_START = "com.dsh.phone.action.START_AUDIO"
        const val ACTION_STOP = "com.dsh.phone.action.STOP_AUDIO"
        const val ACTION_KEEPALIVE = "com.dsh.phone.action.KEEPALIVE"

        private const val CHANNEL_MIC = "dsh_mic"
        private const val CHANNEL_ALERTS = "dsh_alerts"
        private const val NOTIFICATION_ID = 1001
        private const val KEEP_ALIVE_MS = 5 * 60 * 1000L

        @Volatile
        private var recordingActive = false

        @Volatile
        private var instance: MicService? = null

        /** PTT down: make sure the foreground service is running. */
        fun startRecording(context: Context) {
            recordingActive = true
            val intent = Intent(context, MicService::class.java).setAction(ACTION_START)
            try {
                context.startForegroundService(intent)
            } catch (e: Exception) {
                LogStore.warn("service.start_failed")
            }
            instance?.refreshNotification()
        }

        /** PTT up: keep the service (network) alive, just update the notification. */
        fun stopRecording(context: Context) {
            recordingActive = false
            instance?.refreshNotification()
        }

        /** Activity resumed: cancel the pending self-stop. */
        fun onActivityStart(context: Context) {
            instance?.cancelKeepAlive()
        }

        /** Activity left the foreground: allow five minutes before self-stop. */
        fun onActivityStop(context: Context) {
            instance?.scheduleKeepAlive()
        }

        fun isRecording(): Boolean = recordingActive
    }
}
