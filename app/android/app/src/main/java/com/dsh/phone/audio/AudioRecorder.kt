package com.dsh.phone.audio

import android.annotation.SuppressLint
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import android.media.audiofx.AcousticEchoCanceler
import android.media.audiofx.NoiseSuppressor
import com.dsh.phone.log.LogStore
import com.dsh.phone.protocol.AudioFrame
import java.util.concurrent.atomic.AtomicBoolean

/**
 * Captures 16 kHz / 16-bit / mono PCM in fixed 20 ms (640 byte) frames using
 * `AudioRecord` with `VOICE_COMMUNICATION` (PROTOCOL.md v1, PRD FR-407).
 *
 * A dedicated high-priority thread does exact-size reads so every emitted frame
 * is exactly [AudioFrame.PCM16_FRAME_BYTES] bytes; the caller (AudioSession)
 * prefixes the 7-byte protocol header and ships it over WSS.
 */
class AudioRecorder(private val sampleRate: Int = SAMPLE_RATE) {

    interface Listener {
        fun onFrame(pcm: ByteArray)
        fun onLevel(level: Float)
        fun onError(message: String)
    }

    private val running = AtomicBoolean(false)
    private var record: AudioRecord? = null
    private var thread: Thread? = null
    private var echoCanceler: AcousticEchoCanceler? = null
    private var noiseSuppressor: NoiseSuppressor? = null

    val isRecording: Boolean
        get() = running.get()

    @SuppressLint("MissingPermission")
    fun start(listener: Listener): Boolean {
        if (running.get()) return true
        val minBuffer = try {
            AudioRecord.getMinBufferSize(sampleRate, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
        } catch (e: Exception) {
            -1
        }
        if (minBuffer <= 0) {
            listener.onError("microphone buffer is unavailable")
            return false
        }
        val bufferSize = maxOf(minBuffer, AudioFrame.PCM16_FRAME_BYTES * 4)
        val recorder = try {
            AudioRecord.Builder()
                .setAudioSource(MediaRecorder.AudioSource.VOICE_COMMUNICATION)
                .setAudioFormat(
                    AudioFormat.Builder()
                        .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                        .setSampleRate(sampleRate)
                        .setChannelMask(AudioFormat.CHANNEL_IN_MONO)
                        .build(),
                )
                .setBufferSizeInBytes(bufferSize)
                .build()
        } catch (e: Exception) {
            LogStore.warn("audio.record_create_failed")
            listener.onError("failed to create the audio recorder")
            return false
        }
        if (recorder.state != AudioRecord.STATE_INITIALIZED) {
            try {
                recorder.release()
            } catch (ignored: Exception) {
                // Nothing else to do.
            }
            listener.onError("audio recorder is not initialized")
            return false
        }
        enableEffects(recorder)
        record = recorder
        running.set(true)
        val worker = Thread { readLoop(recorder, listener) }
        worker.name = "dsh-audio"
        thread = worker
        val started = try {
            recorder.startRecording()
            true
        } catch (e: Exception) {
            LogStore.warn("audio.start_failed")
            false
        }
        if (!started) {
            running.set(false)
            releaseResources()
            listener.onError("failed to start recording")
            return false
        }
        worker.start()
        return true
    }

    fun stop() {
        if (!running.getAndSet(false)) {
            releaseResources()
            return
        }
        try {
            record?.stop()
        } catch (ignored: Exception) {
            // Already stopped.
        }
        val worker = thread
        thread = null
        if (worker != null && Thread.currentThread() !== worker) {
            try {
                worker.join(500)
            } catch (ignored: InterruptedException) {
                Thread.currentThread().interrupt()
            }
        }
        releaseResources()
    }

    private fun readLoop(recorder: AudioRecord, listener: Listener) {
        try {
            android.os.Process.setThreadPriority(android.os.Process.THREAD_PRIORITY_AUDIO)
        } catch (ignored: Exception) {
            // Non-fatal: continue with the default priority.
        }
        val chunk = ByteArray(AudioFrame.PCM16_FRAME_BYTES)
        var offset = 0
        var readErrors = 0
        while (running.get()) {
            val remaining = AudioFrame.PCM16_FRAME_BYTES - offset
            val read = try {
                recorder.read(chunk, offset, remaining)
            } catch (e: Exception) {
                -1
            }
            if (read < 0) {
                readErrors += 1
                if (readErrors >= MAX_READ_ERRORS) {
                    running.set(false)
                    // G7: keep one structured line per stream so on-device
                    // diagnosis does not depend on the Web UI only.
                    LogStore.warn("audio.read_failed", "errors" to readErrors)
                    listener.onError("audio read failed")
                    break
                }
                continue
            }
            if (read == 0) continue
            offset += read
            if (offset >= AudioFrame.PCM16_FRAME_BYTES) {
                val frame = ByteArray(AudioFrame.PCM16_FRAME_BYTES)
                System.arraycopy(chunk, 0, frame, 0, AudioFrame.PCM16_FRAME_BYTES)
                offset = 0
                try {
                    listener.onFrame(frame)
                } catch (e: Exception) {
                    LogStore.warn("audio.frame_callback_failed")
                }
                try {
                    listener.onLevel(levelOf(frame))
                } catch (e: Exception) {
                    LogStore.warn("audio.level_callback_failed")
                }
            }
        }
        try {
            if (recorder.recordingState == AudioRecord.RECORDSTATE_RECORDING) recorder.stop()
        } catch (ignored: Exception) {
            // Already stopped.
        }
    }

    private fun enableEffects(recorder: AudioRecord) {
        try {
            if (AcousticEchoCanceler.isAvailable()) {
                echoCanceler = AcousticEchoCanceler.create(recorder.audioSessionId)
                echoCanceler?.enabled = true
            }
        } catch (ignored: Exception) {
            // Effects are best effort on vendor ROMs.
        }
        try {
            if (NoiseSuppressor.isAvailable()) {
                noiseSuppressor = NoiseSuppressor.create(recorder.audioSessionId)
                noiseSuppressor?.enabled = true
            }
        } catch (ignored: Exception) {
            // Effects are best effort on vendor ROMs.
        }
    }

    private fun releaseResources() {
        try {
            echoCanceler?.release()
        } catch (ignored: Exception) {
            // Ignore effect release failures.
        }
        try {
            noiseSuppressor?.release()
        } catch (ignored: Exception) {
            // Ignore effect release failures.
        }
        echoCanceler = null
        noiseSuppressor = null
        val recorder = record
        record = null
        if (recorder != null) {
            try {
                recorder.release()
            } catch (ignored: Exception) {
                // Already released.
            }
        }
    }

    private fun levelOf(pcm: ByteArray): Float {
        var sum = 0.0
        var i = 0
        while (i + 1 < pcm.size) {
            val sample = ((pcm[i].toInt() and 0xFF) or (pcm[i + 1].toInt() shl 8)).toShort().toInt()
            sum += sample.toDouble() * sample.toDouble()
            i += 2
        }
        val samples = (pcm.size / 2).coerceAtLeast(1)
        val rms = kotlin.math.sqrt(sum / samples)
        return (rms / 32768.0).toFloat().coerceIn(0f, 1f)
    }

    private companion object {
        const val SAMPLE_RATE = 16000
        const val MAX_READ_ERRORS = 5
    }
}
