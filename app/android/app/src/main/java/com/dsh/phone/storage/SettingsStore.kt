package com.dsh.phone.storage

import android.content.Context
import java.util.UUID

/**
 * Non-secret device settings: stable deviceId and the preferred audio route.
 * Sensitive values (device token) live in [TokenStore] instead.
 */
class SettingsStore(context: Context) {

    private val prefs = context.applicationContext.getSharedPreferences(PREFS, Context.MODE_PRIVATE)

    /** Stable per-install device id, created on first use. */
    fun deviceId(): String {
        val existing = prefs.getString(KEY_DEVICE_ID, null)
        if (!existing.isNullOrEmpty()) return existing
        val created = "dev-" + UUID.randomUUID().toString().replace("-", "").lowercase()
        prefs.edit().putString(KEY_DEVICE_ID, created).apply()
        return created
    }

    /** Preferred Bluetooth device hardware address; null = follow the system. */
    fun preferredBtAddress(): String? =
        prefs.getString(KEY_PREFERRED_BT, null)?.takeIf { it.isNotEmpty() }

    fun setPreferredBtAddress(address: String?) {
        val editor = prefs.edit()
        if (address.isNullOrBlank()) {
            editor.remove(KEY_PREFERRED_BT)
        } else {
            editor.putString(KEY_PREFERRED_BT, address)
        }
        editor.apply()
    }

    private companion object {
        const val PREFS = "dsh_settings"
        const val KEY_DEVICE_ID = "device_id"
        const val KEY_PREFERRED_BT = "preferred_bt_address"
    }
}
