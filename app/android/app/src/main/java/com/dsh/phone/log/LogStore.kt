package com.dsh.phone.log

import android.content.Context
import android.util.Log
import com.dsh.phone.util.JsonObj
import java.io.File
import java.io.FileOutputStream
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.TimeZone

/**
 * Structured, redacted, rolling diagnostic log (ARCHITECTURE §5).
 *
 * - One JSON line per event: `ts, level, event, …fields`.
 * - Files roll daily (`dsh-YYYYMMDD.jsonl`), kept for 7 days, capped per file.
 * - Every line passes through [Redactor] before it is written and the exported
 *   text is redacted again, so tokens/audio/message bodies never leak.
 */
object LogStore {

    private const val DIR_NAME = "logs"
    private const val TAG = "DshPhone"
    private const val RETAIN_DAYS = 7
    private const val MAX_EXPORT_CHARS = 48 * 1024
    private const val MAX_FILE_BYTES = 2 * 1024 * 1024L
    private const val MAX_FIELD_CHARS = 512

    data class ExportResult(val text: String, val truncated: Boolean)

    private val lock = Any()
    private var dir: File? = null
    private val dayFormat = SimpleDateFormat("yyyyMMdd", Locale.US)
    private val isoFormat = SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss.SSSZ", Locale.US)
    private var lastPrunedDay: String = ""

    fun init(context: Context) {
        synchronized(lock) {
            if (dir != null) return
            val base = File(context.applicationContext.filesDir, DIR_NAME)
            if (!base.exists()) base.mkdirs()
            dir = base
            pruneLocked()
        }
    }

    fun debug(event: String, vararg fields: Pair<String, Any?>) = write("debug", event, fields)

    fun info(event: String, vararg fields: Pair<String, Any?>) = write("info", event, fields)

    fun warn(event: String, vararg fields: Pair<String, Any?>) = write("warn", event, fields)

    fun error(event: String, vararg fields: Pair<String, Any?>) = write("error", event, fields)

    /** Export the retained log text, redacted and truncated from the tail. */
    fun export(): ExportResult {
        synchronized(lock) {
            val base = dir ?: return ExportResult("", false)
            val files = base.listFiles { f -> f.isFile && f.name.startsWith("dsh-") }
                ?.sortedBy { it.name }
                ?: emptyList()
            val sb = StringBuilder()
            for (file in files) {
                try {
                    if (sb.length + file.length() > MAX_EXPORT_CHARS * 2L) continue
                    sb.append(file.readText())
                } catch (e: Exception) {
                    // Skip unreadable file, keep exporting the rest.
                }
            }
            val redacted = Redactor.redactText(sb.toString())
            val truncated = redacted.length > MAX_EXPORT_CHARS
            val text = if (truncated) redacted.substring(redacted.length - MAX_EXPORT_CHARS) else redacted
            return ExportResult(text, truncated)
        }
    }

    private fun write(level: String, event: String, fields: Array<out Pair<String, Any?>>) {
        val now = System.currentTimeMillis()
        val obj = JsonObj()
            .put("ts", isoFormat.format(Date(now)))
            .put("level", level)
            .put("event", event)
        for (field in fields) {
            putField(obj, field.first, field.second)
        }
        val line = Redactor.redactJson(obj.toJsonText())
        Log.println(priorityFor(level), TAG, line)
        synchronized(lock) {
            appendLocked(now, line)
        }
    }

    private fun putField(obj: JsonObj, name: String, value: Any?) {
        when (value) {
            null -> obj.putNull(name)
            is Int -> obj.put(name, value)
            is Long -> obj.put(name, value)
            is Boolean -> obj.put(name, value)
            is Float -> putDouble(obj, name, value.toDouble())
            is Double -> putDouble(obj, name, value)
            else -> {
                val text = value.toString()
                obj.put(name, if (text.length > MAX_FIELD_CHARS) text.substring(0, MAX_FIELD_CHARS) else text)
            }
        }
    }

    private fun putDouble(obj: JsonObj, name: String, value: Double) {
        if (value.isNaN() || value.isInfinite()) {
            obj.putNull(name)
        } else {
            obj.putRaw(name, String.format(Locale.US, "%.3f", value))
        }
    }

    private fun appendLocked(now: Long, line: String) {
        val base = dir ?: return
        val day = dayFormat.format(Date(now))
        if (day != lastPrunedDay) {
            lastPrunedDay = day
            pruneLocked()
        }
        val file = File(base, "dsh-$day.jsonl")
        if (file.length() > MAX_FILE_BYTES) return
        try {
            FileOutputStream(file, true).use { stream ->
                stream.write(line.toByteArray(Charsets.UTF_8))
                stream.write('\n'.code)
            }
        } catch (e: Exception) {
            // Logging must never crash the app.
        }
    }

    private fun pruneLocked() {
        val base = dir ?: return
        val cutoff = System.currentTimeMillis() - RETAIN_DAYS * 24L * 60L * 60L * 1000L
        val files = base.listFiles() ?: return
        for (file in files) {
            if (!file.isFile) continue
            if (file.lastModified() < cutoff) {
                try {
                    file.delete()
                } catch (e: Exception) {
                    // Best effort.
                }
            }
        }
    }

    private fun priorityFor(level: String): Int = when (level) {
        "debug" -> Log.DEBUG
        "warn" -> Log.WARN
        "error" -> Log.ERROR
        else -> Log.INFO
    }
}
