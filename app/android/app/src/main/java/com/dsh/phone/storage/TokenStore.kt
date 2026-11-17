package com.dsh.phone.storage

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import com.dsh.phone.log.LogStore
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/**
 * Device token storage (ARCHITECTURE §3.4).
 *
 * The token is encrypted with AES/GCM using a non-exportable key held by the
 * Android Keystore; only the ciphertext and non-secret session metadata are
 * persisted in SharedPreferences. Plaintext never touches disk or logs.
 *
 * The class deliberately does not use androidx.security-crypto (alpha) so that
 * the APK keeps zero androidx dependencies.
 */
class TokenStore(context: Context) {

    data class Session(
        val serverId: String,
        val host: String,
        val port: Int,
        val fingerprint: String,
        val tokenBase64: String,
    )

    private val prefs = context.applicationContext.getSharedPreferences(PREFS, Context.MODE_PRIVATE)

    fun saveSession(
        serverId: String,
        host: String,
        port: Int,
        fingerprint: String,
        tokenBase64: String,
    ) {
        val encrypted = encrypt(tokenBase64)
        if (encrypted == null) {
            LogStore.error("token.save_failed")
            return
        }
        prefs.edit()
            .putString(KEY_TOKEN, encrypted)
            .putString(KEY_SERVER_ID, serverId)
            .putString(KEY_HOST, host)
            .putInt(KEY_PORT, port)
            .putString(KEY_FP, fingerprint)
            .apply()
    }

    fun loadSession(): Session? {
        val encrypted = prefs.getString(KEY_TOKEN, null) ?: return null
        val token = decrypt(encrypted) ?: run {
            clearSession()
            return null
        }
        val host = prefs.getString(KEY_HOST, null) ?: return null
        val port = prefs.getInt(KEY_PORT, 0)
        if (port <= 0 || port > 65535) return null
        return Session(
            serverId = prefs.getString(KEY_SERVER_ID, "").orEmpty(),
            host = host,
            port = port,
            fingerprint = prefs.getString(KEY_FP, "").orEmpty(),
            tokenBase64 = token,
        )
    }

    fun clearSession() {
        prefs.edit().clear().apply()
    }

    fun hasSession(): Boolean = loadSession() != null

    private fun encrypt(plaintext: String): String? = try {
        val key = getOrCreateKey() ?: return null
        val cipher = Cipher.getInstance(TRANSFORMATION)
        cipher.init(Cipher.ENCRYPT_MODE, key)
        val iv = cipher.iv
        val ciphertext = cipher.doFinal(plaintext.toByteArray(Charsets.UTF_8))
        Base64.encodeToString(iv, Base64.NO_WRAP) + ":" + Base64.encodeToString(ciphertext, Base64.NO_WRAP)
    } catch (e: Exception) {
        LogStore.error("token.encrypt_failed")
        null
    }

    private fun decrypt(stored: String): String? = try {
        val idx = stored.indexOf(':')
        if (idx <= 0 || idx == stored.length - 1) null else {
            val iv = Base64.decode(stored.substring(0, idx), Base64.NO_WRAP)
            val ciphertext = Base64.decode(stored.substring(idx + 1), Base64.NO_WRAP)
            val key = getOrCreateKey() ?: return null
            val cipher = Cipher.getInstance(TRANSFORMATION)
            cipher.init(Cipher.DECRYPT_MODE, key, GCMParameterSpec(128, iv))
            String(cipher.doFinal(ciphertext), Charsets.UTF_8)
        }
    } catch (e: Exception) {
        // Keystore key invalidated (factory reset / lock screen change / ROM bug).
        LogStore.warn("token.decrypt_failed")
        null
    }

    private fun getOrCreateKey(): SecretKey? = try {
        val keyStore = KeyStore.getInstance(KEYSTORE_PROVIDER).apply { load(null) }
        val entry = keyStore.getEntry(KEY_ALIAS, null) as? KeyStore.SecretKeyEntry
        entry?.secretKey ?: generateKey()
    } catch (e: Exception) {
        LogStore.error("token.keystore_failed")
        null
    }

    private fun generateKey(): SecretKey {
        val generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, KEYSTORE_PROVIDER)
        val spec = KeyGenParameterSpec.Builder(
            KEY_ALIAS,
            KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT,
        )
            .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
            .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
            .setRandomizedEncryptionRequired(true)
            .setKeySize(256)
            .build()
        generator.init(spec)
        return generator.generateKey()
    }

    private companion object {
        const val PREFS = "dsh_token_store"
        const val KEY_TOKEN = "token_ct"
        const val KEY_SERVER_ID = "server_id"
        const val KEY_HOST = "host"
        const val KEY_PORT = "port"
        const val KEY_FP = "fingerprint"
        const val KEYSTORE_PROVIDER = "AndroidKeyStore"
        const val KEY_ALIAS = "com.dsh.phone.token.v1"
        const val TRANSFORMATION = "AES/GCM/NoPadding"
    }
}
