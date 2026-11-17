package com.dsh.phone.audio

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.media.AudioDeviceInfo
import android.media.AudioManager
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import com.dsh.phone.log.LogStore
import com.dsh.phone.storage.SettingsStore

/**
 * Routes the microphone to a Bluetooth headset while PTT is held
 * (PRD FR-501…FR-506, ARCHITECTURE §3 audio).
 *
 * - API 31+: `setCommunicationDevice()`; LE Audio (`TYPE_BLE_HEADSET`) is
 *   preferred over HFP (`TYPE_BLUETOOTH_SCO`) on API 33+.
 * - API 29/30: `startBluetoothSco()` + `MODE_IN_COMMUNICATION`.
 * - Routing is given 8 s; on timeout everything falls back to the phone mic.
 * - Releasing the route must not disturb media playback: on API 31+ only the
 *   communication device is cleared, never the global audio mode.
 */
class BtRouter(context: Context, private val settings: SettingsStore) {

    interface Callback {
        fun onRoute(info: RouteInfo)
        fun onFailed(message: String)
    }

    data class RouteInfo(val route: String, val deviceName: String, val address: String?)

    private val appContext: Context = context.applicationContext
    private val audioManager: AudioManager? =
        appContext.getSystemService(Context.AUDIO_SERVICE) as? AudioManager
    private val handler = Handler(Looper.getMainLooper())

    private var pendingCallback: Callback? = null
    private var pendingRoute: RouteInfo? = null
    private var pollRunnable: Runnable? = null

    /** Current input route, for the `routeInfo` bridge op. */
    fun routeInfo(): RouteInfo {
        val am = audioManager ?: return PHONE_ROUTE
        return try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                routeForDevice(am.communicationDevice) ?: PHONE_ROUTE
            } else {
                if (am.isBluetoothScoOn) RouteInfo(ROUTE_HFP, "", null) else PHONE_ROUTE
            }
        } catch (e: Exception) {
            PHONE_ROUTE
        }
    }

    /** Asynchronously switch to the preferred Bluetooth communication device. */
    fun requestRoute(callback: Callback) {
        handler.post { beginRoute(callback) }
    }

    /** Release the Bluetooth route after PTT is released. */
    fun releaseRoute() {
        handler.post {
            clearPending()
            val am = audioManager ?: return@post
            try {
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                    am.clearCommunicationDevice()
                } else {
                    am.stopBluetoothSco()
                    am.isBluetoothScoOn = false
                    am.mode = AudioManager.MODE_NORMAL
                }
            } catch (ignored: Exception) {
                // Best effort: never crash on vendor ROMs.
            }
        }
    }

    /* ------------------------------------------------------------------ modern */

    private fun beginRoute(callback: Callback) {
        clearPending()
        val am = audioManager
        if (am == null) {
            callback.onFailed("audio service is unavailable")
            return
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            beginRouteModern(am, callback)
        } else {
            beginRouteLegacy(am, callback)
        }
    }

    private fun beginRouteModern(am: AudioManager, callback: Callback) {
        if (!hasBluetoothPermission()) {
            callback.onFailed("bluetooth permission is not granted")
            return
        }
        val device = try {
            pickBluetoothDevice(am)
        } catch (e: SecurityException) {
            null
        }
        if (device == null) {
            callback.onFailed("no bluetooth headset is available")
            return
        }
        val route = routeForDevice(device) ?: RouteInfo(ROUTE_HFP, productName(device), addressOf(device))
        val switched = try {
            am.setCommunicationDevice(device)
        } catch (e: Exception) {
            false
        }
        if (!switched) {
            callback.onFailed("failed to switch to the bluetooth headset")
            return
        }
        pendingCallback = callback
        pendingRoute = route
        val deadline = SystemClock.elapsedRealtime() + ROUTE_TIMEOUT_MS
        val poll = object : Runnable {
            override fun run() {
                val current = try {
                    am.communicationDevice
                } catch (e: Exception) {
                    null
                }
                if (current != null && current.id == device.id) {
                    finishSuccess(route)
                    return
                }
                if (SystemClock.elapsedRealtime() >= deadline) {
                    finishFailure(am, "bluetooth routing timed out after 8 seconds")
                    return
                }
                handler.postDelayed(this, POLL_INTERVAL_MS)
            }
        }
        pollRunnable = poll
        handler.postDelayed(poll, POLL_INTERVAL_MS)
    }

    private fun pickBluetoothDevice(am: AudioManager): AudioDeviceInfo? {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.S) return null
        val devices = am.availableCommunicationDevices
        if (devices.isEmpty()) return null
        val bluetooth = devices.filter { isBluetoothDevice(it) }
        if (bluetooth.isEmpty()) return null
        val preferred = settings.preferredBtAddress()
        if (preferred != null) {
            val match = bluetooth.firstOrNull { addressOf(it).equals(preferred, ignoreCase = true) }
            if (match != null) return match
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            val leAudio = bluetooth.firstOrNull { it.type == AudioDeviceInfo.TYPE_BLE_HEADSET }
            if (leAudio != null) return leAudio
        }
        return bluetooth.firstOrNull { it.type == AudioDeviceInfo.TYPE_BLUETOOTH_SCO } ?: bluetooth[0]
    }

    private fun isBluetoothDevice(device: AudioDeviceInfo): Boolean {
        if (device.type == AudioDeviceInfo.TYPE_BLUETOOTH_SCO) return true
        return Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
            device.type == AudioDeviceInfo.TYPE_BLE_HEADSET
    }

    /* ------------------------------------------------------------------ legacy */

    private fun beginRouteLegacy(am: AudioManager, callback: Callback) {
        val scoAvailable = try {
            am.isBluetoothScoAvailableOffCall || am.isBluetoothScoOn
        } catch (e: Exception) {
            false
        }
        if (!scoAvailable) {
            callback.onFailed("no bluetooth headset is available")
            return
        }
        try {
            am.mode = AudioManager.MODE_IN_COMMUNICATION
            am.startBluetoothSco()
            am.isBluetoothScoOn = true
        } catch (e: Exception) {
            callback.onFailed("failed to start bluetooth SCO")
            return
        }
        pendingCallback = callback
        pendingRoute = RouteInfo(ROUTE_HFP, "", null)
        val deadline = SystemClock.elapsedRealtime() + ROUTE_TIMEOUT_MS
        val poll = object : Runnable {
            override fun run() {
                val scoOn = try {
                    am.isBluetoothScoOn
                } catch (e: Exception) {
                    false
                }
                if (scoOn) {
                    finishSuccess(pendingRoute ?: RouteInfo(ROUTE_HFP, "", null))
                    return
                }
                if (SystemClock.elapsedRealtime() >= deadline) {
                    finishFailure(am, "bluetooth SCO timed out after 8 seconds")
                    return
                }
                handler.postDelayed(this, POLL_INTERVAL_MS)
            }
        }
        pollRunnable = poll
        handler.postDelayed(poll, POLL_INTERVAL_MS)
    }

    /* ----------------------------------------------------------------- helpers */

    private fun finishSuccess(route: RouteInfo) {
        val callback = pendingCallback
        clearPending()
        callback?.onRoute(route)
    }

    private fun finishFailure(am: AudioManager?, message: String) {
        val callback = pendingCallback
        clearPending()
        try {
            if (am != null) {
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                    am.clearCommunicationDevice()
                } else {
                    am.stopBluetoothSco()
                    am.isBluetoothScoOn = false
                    am.mode = AudioManager.MODE_NORMAL
                }
            }
        } catch (ignored: Exception) {
            // Best effort.
        }
        LogStore.warn("bt.route_failed")
        callback?.onFailed(message)
    }

    private fun clearPending() {
        val poll = pollRunnable
        pollRunnable = null
        pendingCallback = null
        pendingRoute = null
        if (poll != null) handler.removeCallbacks(poll)
    }

    private fun routeForDevice(device: AudioDeviceInfo?): RouteInfo? {
        if (device == null) return null
        if (device.type == AudioDeviceInfo.TYPE_BLUETOOTH_SCO) {
            return RouteInfo(ROUTE_HFP, productName(device), addressOf(device))
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
            device.type == AudioDeviceInfo.TYPE_BLE_HEADSET
        ) {
            return RouteInfo(ROUTE_LE, productName(device), addressOf(device))
        }
        return null
    }

    private fun productName(device: AudioDeviceInfo): String {
        val name = try {
            device.productName?.toString()
        } catch (e: Exception) {
            null
        }
        return name ?: ""
    }

    private fun addressOf(device: AudioDeviceInfo): String? {
        return try {
            device.address?.takeIf { it.isNotEmpty() }
        } catch (e: Exception) {
            null
        }
    }

    private fun hasBluetoothPermission(): Boolean {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.S) return true
        return appContext.checkSelfPermission(Manifest.permission.BLUETOOTH_CONNECT) ==
            PackageManager.PERMISSION_GRANTED
    }

    companion object {
        const val ROUTE_PHONE = "phone"
        const val ROUTE_HFP = "hfp"
        const val ROUTE_LE = "le"

        private val PHONE_ROUTE = RouteInfo(ROUTE_PHONE, "", null)

        private const val ROUTE_TIMEOUT_MS = 8_000L
        private const val POLL_INTERVAL_MS = 200L
    }
}
