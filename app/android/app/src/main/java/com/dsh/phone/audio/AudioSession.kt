package com.dsh.phone.audio

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import com.dsh.phone.log.LogStore
import com.dsh.phone.net.WssClient
import com.dsh.phone.protocol.AudioFrame
import com.dsh.phone.protocol.Protocol
import com.dsh.phone.service.MicService
import com.dsh.phone.util.JsonObj
import java.util.ArrayDeque
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicBoolean

/**
 * One PTT voice turn, from microphone to WSS (ARCHITECTURE §4, PRD §7.4).
 *
 * Lifecycle: [start] sends `audio.start`, starts capture immediately (PTT must
 * feel instant), switches the Bluetooth route and buffers frames until the
 * server answers `audio.ready`; [stop] sends `audio.stop` and releases the
 * route. All protocol `seq` values are owned by [WssClient]; this class only
 * numbers the binary frames (aseq from 1, wrapping at 65535).
 */
class AudioSession(
    context: Context,
    private val wss: WssClient,
    private val btRouter: BtRouter,
    private val emitEventJson: (String) -> Unit,
) {

    private enum class State {
        IDLE, STARTING, RECORDING, STOPPING
    }

    private val appContext: Context = context.applicationContext
    private val handler = Handler(Looper.getMainLooper())
    private val controlExecutor = Executors.newSingleThreadExecutor { runnable ->
        Thread(runnable, "dsh-audio-ctrl").apply { isDaemon = true }
    }
    private val recorder = AudioRecorder()
    private val frameLock = Any()
    private val pendingFrames = ArrayDeque<ByteArray>()

    @Volatile
    private var state = State.IDLE
    private var sessionId: String? = null
    private var sampleRate = Protocol.AUDIO_SAMPLE_RATE
    private var lang = "zh"
    private var mode = "ptt"
    private var startedAtMs = 0L
    private var audioReady = false
    private var aseq = 0
    private var lastAseq = 0
    private var lastLevelAt = 0L
    private var maxDurationRunnable: Runnable? = null
    private var ackTimeoutRunnable: Runnable? = null
    private val recorderFailureReported = AtomicBoolean(false)

    val isRecording: Boolean
        get() = state == State.STARTING || state == State.RECORDING

    /* ------------------------------------------------------------------ public */

    fun start(sampleRate: Int, lang: String, mode: String, sessionId: String?) {
        handler.post { startInternal(sampleRate, lang, mode, sessionId) }
    }

    fun stop() {
        handler.post { stopInternal(true) }
    }

    fun cancel() {
        handler.post { stopInternal(false) }
    }

    /** WssClient callback: the server accepted the stream; flush buffered frames. */
    fun onTransportAudioReady() {
        handler.post { flushBuffered() }
    }

    /** WssClient callback: the server rejected/aborted the stream. */
    fun onTransportAudioError() {
        handler.post {
            if (isRecording) stopInternal(false)
        }
    }

    /** WssClient callback: the connection dropped while recording. */
    fun onTransportLost(reason: String) {
        handler.post {
            if (isRecording) {
                emitAudioError("connection_lost", reason, true)
                stopInternal(false)
            }
        }
    }

    fun release() {
        handler.post { stopInternal(false) }
        try {
            controlExecutor.shutdownNow()
        } catch (ignored: Exception) {
            // Already shut down.
        }
    }

    /* ------------------------------------------------------------------- start */

    private fun startInternal(sampleRateIn: Int, langIn: String, modeIn: String, sessionIdIn: String?) {
        when (state) {
            State.STARTING, State.RECORDING -> {
                emitAudioError("audio_busy", "an audio stream is already active", false)
                return
            }
            State.STOPPING -> {
                handler.postDelayed(
                    { startInternal(sampleRateIn, langIn, modeIn, sessionIdIn) },
                    RETRY_START_DELAY_MS,
                )
                return
            }
            State.IDLE -> Unit
        }
        if (!wss.isOnline()) {
            emitAudioError("offline", "network is not online", true)
            return
        }
        if (!hasRecordPermission()) {
            emitAudioError("permission_denied", "microphone permission is not granted", false)
            return
        }
        state = State.STARTING
        sessionId = sessionIdIn
        this.sampleRate = if (sampleRateIn in 8000..48000) sampleRateIn else Protocol.AUDIO_SAMPLE_RATE
        this.lang = langIn.ifBlank { "zh" }
        this.mode = if (modeIn == "continuous") "continuous" else "ptt"
        startedAtMs = SystemClock.elapsedRealtime()
        lastLevelAt = 0L
        synchronized(frameLock) {
            pendingFrames.clear()
            audioReady = false
            aseq = 0
            lastAseq = 0
        }
        recorderFailureReported.set(false)
        try {
            MicService.startRecording(appContext)
        } catch (e: Exception) {
            LogStore.warn("audio.service_start_failed")
        }
        emitAudioState("starting")
        val startSent = wss.sendAudioStart(this.sampleRate, this.lang, this.mode, sessionId)
        if (!startSent) {
            state = State.IDLE
            try {
                MicService.stopRecording(appContext)
            } catch (ignored: Exception) {
                // Best effort.
            }
            emitAudioState("idle")
            return
        }
        val started = recorder.start(recorderListener)
        if (!started) {
            // G1: the posted onError callback runs after this function returned the
            // state to IDLE, so it cannot surface the failure any more. Report the
            // synchronous start failure here, before tearing down.
            reportRecorderFailure("failed to start the audio recorder", false)
            wss.sendAudioStop(0)
            state = State.IDLE
            try {
                MicService.stopRecording(appContext)
            } catch (ignored: Exception) {
                // Best effort.
            }
            emitAudioState("idle")
            return
        }
        scheduleMaxDuration()
        scheduleAckTimeout()
        scheduleRoute()
    }

    private fun scheduleRoute() {
        btRouter.requestRoute(object : BtRouter.Callback {
            override fun onRoute(info: BtRouter.RouteInfo) {
                if (!isRecording) return
                emitAudioRoute(info.route, info.deviceName)
                if (info.route != BtRouter.ROUTE_PHONE) restartRecorderForRoute()
            }

            override fun onFailed(message: String) {
                if (!isRecording) return
                // FR-504: fall back to the phone microphone and tell the UI.
                emitAudioRoute(BtRouter.ROUTE_PHONE, "")
                emitAudioError("route_failed", message, true)
            }
        })
    }

    /** Rebuild the audio record so capture follows the new input device. */
    private fun restartRecorderForRoute() {
        controlExecutor.execute {
            if (!isRecording) return@execute
            try {
                recorder.stop()
            } catch (ignored: Exception) {
                // Continue with a fresh recorder.
            }
            try {
                Thread.sleep(ROUTE_SETTLE_MS)
            } catch (ignored: InterruptedException) {
                Thread.currentThread().interrupt()
                return@execute
            }
            if (!isRecording) return@execute
            val restarted = recorder.start(recorderListener)
            if (!restarted) {
                // G2: a failed rebuild after a route change used to leave the
                // stream "recording" with no frames and no event. Report it and
                // stop the stream instead of stalling silently.
                handler.post {
                    if (state == State.STARTING || state == State.RECORDING) {
                        reportRecorderFailure(
                            "failed to restart the audio recorder after a route change",
                            true,
                        )
                        stopInternal(false)
                    }
                }
            }
        }
    }

    /* -------------------------------------------------------------------- stop */

    private fun stopInternal(notifyStopped: Boolean) {
        if (state == State.IDLE) {
            if (notifyStopped) {
                emitEventJson(JsonObj().put("type", "audio.stopped").put("aseq", lastAseq).toJsonText())
            }
            return
        }
        if (state == State.STOPPING) return
        state = State.STOPPING
        cancelMaxDuration()
        cancelAckTimeout()
        val finalAseq = synchronized(frameLock) {
            audioReady = false
            pendingFrames.clear()
            lastAseq
        }
        recorder.stop()
        btRouter.releaseRoute()
        wss.sendAudioStop(finalAseq)
        try {
            MicService.stopRecording(appContext)
        } catch (ignored: Exception) {
            // Best effort.
        }
        if (notifyStopped) {
            emitEventJson(JsonObj().put("type", "audio.stopped").put("aseq", finalAseq).toJsonText())
        }
        emitAudioState("idle")
        state = State.IDLE
    }

    /* ------------------------------------------------------------------ frames */

    private val recorderListener = object : AudioRecorder.Listener {
        override fun onFrame(pcm: ByteArray) {
            if (!isRecording) return
            val timestampMs = (SystemClock.elapsedRealtime() - startedAtMs).coerceIn(0L, 0xFFFFFFFFL)
            synchronized(frameLock) {
                if (aseq >= AudioFrame.MAX_ASEQ) aseq = 0
                aseq += 1
                lastAseq = aseq
                val frame = AudioFrame.encode(aseq, timestampMs, pcm)
                if (!audioReady) {
                    if (pendingFrames.size >= MAX_BUFFERED_FRAMES) pendingFrames.removeFirst()
                    pendingFrames.addLast(frame)
                } else {
                    wss.sendBinary(frame)
                }
            }
        }

        override fun onLevel(level: Float) {
            if (!isRecording) return
            val now = SystemClock.elapsedRealtime()
            if (now - lastLevelAt < LEVEL_INTERVAL_MS) return
            lastLevelAt = now
            val normalized = level.coerceIn(0f, 1f)
            handler.post {
                if (isRecording) {
                    emitEventJson(
                        JsonObj().put("type", "audio.level").putRaw("level", formatLevel(normalized)).toJsonText(),
                    )
                }
            }
        }

        override fun onError(message: String) {
            handler.post {
                if (state == State.STARTING || state == State.RECORDING) {
                    reportRecorderFailure(message, false)
                    stopInternal(false)
                }
            }
        }
    }

    /**
     * G1/G2: report a recorder failure exactly once per stream, keeping one
     * structured log line for on-device diagnosis (the Web UI sees audio.error).
     */
    private fun reportRecorderFailure(message: String, retryable: Boolean) {
        if (!recorderFailureReported.compareAndSet(false, true)) return
        LogStore.error("audio.recorder_failed", "message" to message)
        emitAudioError("recorder_failed", message, retryable)
    }

    private fun flushBuffered() {
        cancelAckTimeout()
        if (state == State.STARTING) {
            state = State.RECORDING
            emitAudioState("recording")
        }
        if (state != State.RECORDING) return
        synchronized(frameLock) {
            while (pendingFrames.isNotEmpty()) {
                wss.sendBinary(pendingFrames.removeFirst())
            }
            audioReady = true
        }
    }

    /* ------------------------------------------------------------------ timers */

    private fun scheduleMaxDuration() {
        cancelMaxDuration()
        val runnable = Runnable {
            if (state == State.STARTING || state == State.RECORDING) {
                emitAudioError("max_duration", "audio stream reached the 120 second limit", false)
                stopInternal(true)
            }
        }
        maxDurationRunnable = runnable
        handler.postDelayed(runnable, Protocol.AUDIO_MAX_STREAM_MS.toLong())
    }

    private fun cancelMaxDuration() {
        val runnable = maxDurationRunnable
        maxDurationRunnable = null
        if (runnable != null) handler.removeCallbacks(runnable)
    }

    /**
     * G6: do not buffer silently for 150 frames (3 s) when the server never
     * acknowledges audio.start. Lead decision (m00516): 3 s ack timeout.
     */
    private fun scheduleAckTimeout() {
        cancelAckTimeout()
        val runnable = Runnable {
            if ((state == State.STARTING || state == State.RECORDING) && !audioReady) {
                LogStore.warn("audio.ack_timeout", "timeoutMs" to ACK_TIMEOUT_MS)
                emitAudioError(
                    "audio_timeout",
                    "server did not answer audio.ready within " + (ACK_TIMEOUT_MS / 1000) + "s",
                    true,
                )
                stopInternal(true)
            }
        }
        ackTimeoutRunnable = runnable
        handler.postDelayed(runnable, ACK_TIMEOUT_MS)
    }

    private fun cancelAckTimeout() {
        val runnable = ackTimeoutRunnable
        ackTimeoutRunnable = null
        if (runnable != null) handler.removeCallbacks(runnable)
    }

    /* ------------------------------------------------------------------ events */

    private fun emitAudioState(stateName: String) {
        emitEventJson(JsonObj().put("type", "audio.state").put("state", stateName).toJsonText())
    }

    private fun emitAudioRoute(route: String, deviceName: String) {
        emitEventJson(
            JsonObj()
                .put("type", "audio.route")
                .put("route", route)
                .put("deviceName", deviceName)
                .toJsonText(),
        )
    }

    private fun emitAudioError(code: String, message: String, retryable: Boolean) {
        emitEventJson(
            JsonObj()
                .put("type", "audio.error")
                .put("code", code)
                .put("message", message)
                .put("retryable", retryable)
                .toJsonText(),
        )
    }

    private fun hasRecordPermission(): Boolean {
        return appContext.checkSelfPermission(Manifest.permission.RECORD_AUDIO) ==
            PackageManager.PERMISSION_GRANTED
    }

    private fun formatLevel(level: Float): String {
        return (Math.round(level * 1000f) / 1000f).toString()
    }

    private companion object {
        const val MAX_BUFFERED_FRAMES = 150
        const val ACK_TIMEOUT_MS = 3_000L
        const val LEVEL_INTERVAL_MS = 100L
        const val ROUTE_SETTLE_MS = 150L
        const val RETRY_START_DELAY_MS = 100L
    }
}
