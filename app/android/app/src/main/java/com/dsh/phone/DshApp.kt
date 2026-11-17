package com.dsh.phone

import android.app.Application
import com.dsh.phone.log.LogStore

/**
 * Application entry point: initializes logging, storage and the network/audio
 * stack once per process.
 */
class DshApp : Application() {

    override fun onCreate() {
        super.onCreate()
        AppCore.init(this)
        LogStore.info("app.start", "version" to BuildConfig.VERSION_NAME)
    }
}
