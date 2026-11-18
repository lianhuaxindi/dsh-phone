package com.dsh.phone.storage

import android.content.Context
import java.util.UUID

/**
 * Non-secret device settings: stable deviceId, preferred audio route and the
 * last known Bridge addresses (D-031 multi-address fallback).
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

    /** Last Bridge address that completed a WebSocket handshake; tried first on reconnect. */
    fun lastHost(): String? =
        prefs.getString(KEY_LAST_HOST, null)?.takeIf { it.isNotBlank() }

    fun setLastHost(host: String?) {
        val editor = prefs.edit()
        if (host.isNullOrBlank()) {
            editor.remove(KEY_LAST_HOST)
        } else {
            editor.putString(KEY_LAST_HOST, host)
        }
        editor.apply()
    }

    /** Known candidate addresses for the paired Bridge (primary host first). */
    fun knownHosts(): List<String> {
        val raw = prefs.getString(KEY_KNOWN_HOSTS, null)
        if (raw.isNullOrEmpty()) return emptyList()
        return raw.split(',').map { it.trim() }.filter { it.isNotEmpty() }
    }

    fun setKnownHosts(hosts: List<String>) {
        val cleaned = hosts.map { it.trim() }.filter { it.isNotEmpty() }.distinct()
        val editor = prefs.edit()
        if (cleaned.isEmpty()) {
            editor.remove(KEY_KNOWN_HOSTS)
        } else {
            editor.putString(KEY_KNOWN_HOSTS, cleaned.joinToString(","))
        }
        editor.apply()
    }

    private companion object {
        const val PREFS = "dsh_settings"
        const val KEY_DEVICE_ID = "device_id"
        const val KEY_PREFERRED_BT = "preferred_bt_address"
        const val KEY_LAST_HOST = "last_host"
        const val KEY_KNOWN_HOSTS = "known_hosts"
    }
}
