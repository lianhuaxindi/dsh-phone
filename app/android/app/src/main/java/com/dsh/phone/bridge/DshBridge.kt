package com.dsh.phone.bridge

import android.webkit.JavascriptInterface
import android.webkit.WebView
import com.dsh.phone.AppCore
import com.dsh.phone.log.LogStore
import com.dsh.phone.protocol.Protocol
import com.dsh.phone.util.Json
import com.dsh.phone.util.JsonObj
import org.json.JSONObject

/**
 * The WebView <-> native boundary described by ARCHITECTURE §3.2.
 *
 * The page calls `window.DshNative.post(jsonString)` with a flat JSON object
 * `{ "op": "...", ...fields }`. Native answers only through
 * `window.DshBridge.onEvent(jsonString)`; no synchronous return values cross the
 * bridge and audio never travels through JavaScript.
 *
 * All events are marshalled onto the WebView's UI thread with
 * `webView.post { evaluateJavascript(...) }`. Events that arrive before the
 * page is ready are buffered (bounded) and flushed from [onPageReady].
 */
class DshBridge(private val webView: WebView) {

    private val lock = Any()
    private val pendingEvents = ArrayDeque<String>()
    private var pageReady = false

    init {
        AppCore.setEventSink { json -> postEvent(json) }
    }

    fun onPageReady() {
        val queued: List<String>
        synchronized(lock) {
            pageReady = true
            queued = pendingEvents.toList()
            pendingEvents.clear()
        }
        for (json in queued) {
            deliver(json)
        }
        LogStore.info("bridge.page_ready", "queued" to queued.size)
    }

    @JavascriptInterface
    fun post(message: String) {
        try {
            val obj = JSONObject(message)
            when (val op = obj.optString("op", "")) {
                "connect" -> AppCore.connect(
                    host = obj.optString("host", ""),
                    port = obj.optInt("port", 0),
                    fingerprint = obj.optString("fp", ""),
                    deviceId = obj.optString("deviceId", ""),
                    hosts = readHosts(obj),
                )

                "disconnect" -> AppCore.disconnect()
                "reconnect" -> AppCore.reconnect()
                "pair" -> AppCore.pair(
                    code = obj.optString("code", ""),
                    deviceName = obj.optString("deviceName", ""),
                )

                "auth" -> AppCore.auth()
                "send" -> handleSend(obj)
                "startAudio" -> AppCore.startAudio(
                    sampleRate = obj.optInt("sampleRate", Protocol.AUDIO_SAMPLE_RATE),
                    lang = obj.optString("lang", "zh"),
                    mode = obj.optString("mode", "ptt"),
                    sessionId = obj.optString("sessionId", "").takeIf { it.isNotBlank() },
                )

                "stopAudio" -> AppCore.stopAudio()
                "cancelAudio" -> AppCore.cancelAudio()
                "getToken" -> AppCore.getToken()
                "setToken" -> AppCore.setToken(
                    token = obj.optString("token", "").takeIf { it.isNotBlank() },
                    host = obj.optString("host", "").takeIf { it.isNotBlank() },
                    port = obj.optInt("port", 0).takeIf { it > 0 },
                    serverId = obj.optString("serverId", "").takeIf { it.isNotBlank() },
                )

                "routeInfo" -> AppCore.routeInfo()
                "preferDevice" -> AppCore.preferDevice(
                    obj.optString("address", "").takeIf { it.isNotBlank() },
                )

                "notify" -> AppCore.notify(
                    title = obj.optString("title", ""),
                    text = obj.optString("text", ""),
                )

                "exportLogs" -> AppCore.exportLogs()
                "scanPairing" -> AppCore.requestScanPairing()
                "pickImage" -> AppCore.requestPickImage(
                    obj.optString("source", "").ifBlank { AppCore.SOURCE_GALLERY },
                )

                else -> emitBridgeError("unsupported op: $op")
            }
        } catch (e: Exception) {
            LogStore.warn("bridge.bad_request")
            emitBridgeError("bad request")
        }
    }

    private fun handleSend(obj: JSONObject) {
        val raw = obj.optString("json", "")
        if (raw.isBlank()) {
            emitBridgeError("send.json is required")
            return
        }
        val ok = AppCore.send(raw)
        if (!ok) {
            LogStore.warn("bridge.send_rejected")
        }
    }

    /**
     * `connect` may carry the v1.1 `hosts` candidate list from the pairing URI
     * (D-031). Validation of individual addresses stays in Pairing/WssClient.
     */
    private fun readHosts(obj: JSONObject): List<String> {
        val array = obj.optJSONArray("hosts") ?: return emptyList()
        val hosts = ArrayList<String>(array.length())
        for (index in 0 until array.length()) {
            if (hosts.size >= MAX_HOST_CANDIDATES) break
            val value = array.optString(index, "").trim()
            if (value.isNotEmpty() && !hosts.contains(value)) {
                hosts.add(value)
            }
        }
        return hosts
    }

    private fun emitBridgeError(message: String) {
        emit(
            JsonObj()
                .put("type", "error")
                .put("scope", "bridge")
                .put("code", 5002)
                .put("message", message)
                .put("retryable", false)
                .toJsonText(),
        )
    }

    private fun emit(json: String) {
        postEvent(json)
    }

    private fun postEvent(json: String) {
        val queued: Boolean
        synchronized(lock) {
            if (!pageReady) {
                if (pendingEvents.size >= MAX_PENDING_EVENTS) {
                    pendingEvents.removeFirst()
                }
                pendingEvents.addLast(json)
                queued = true
            } else {
                queued = false
            }
        }
        if (!queued) {
            deliver(json)
        }
    }

    private fun deliver(json: String) {
        val literal = Json.quote(json)
        webView.post {
            try {
                webView.evaluateJavascript(
                    "window.DshBridge && window.DshBridge.onEvent($literal);",
                    null,
                )
            } catch (e: Exception) {
                LogStore.warn("bridge.event_failed")
            }
        }
    }

    private companion object {
        const val MAX_PENDING_EVENTS = 200
        const val MAX_HOST_CANDIDATES = 16
    }
}
