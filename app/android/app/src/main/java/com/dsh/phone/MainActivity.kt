package com.dsh.phone

import android.Manifest
import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.content.res.Configuration
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.MediaStore
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.webkit.ConsoleMessage
import android.webkit.PermissionRequest
import android.webkit.ValueCallback
import android.webkit.WebChromeClient
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.TextView
import com.dsh.phone.bridge.DshBridge
import com.dsh.phone.log.LogStore
import com.dsh.phone.storage.ImageFileProvider
import java.io.ByteArrayInputStream
import java.io.IOException
import java.io.InputStream
import java.util.Collections

/**
 * Single-activity WebView shell.
 *
 * The UI is plain HTML/JS under `assets/web` (synced from `app/web` by CI) and
 * is served through a virtual `https://appassets.local/` origin so that
 * modules, IndexedDB and service-worker-free secure-context APIs behave the same
 * way they do in a browser.
 */
class MainActivity : Activity(), AppCore.Host {

    private lateinit var webView: WebView
    private lateinit var fallbackView: View
    private lateinit var bridge: DshBridge
    private var firstPageFinished = true
    private var lastNightMode = false
    private var pendingCaptureUri: Uri? = null
    private var fileChooserCallback: ValueCallback<Array<Uri>>? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        lastNightMode = isNightMode()

        val root = FrameLayout(this)
        webView = WebView(this)
        configureWebView()
        root.addView(
            webView,
            FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.MATCH_PARENT,
            ),
        )
        fallbackView = createFallbackView()
        root.addView(
            fallbackView,
            FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.MATCH_PARENT,
            ),
        )
        setContentView(root)

        bridge = DshBridge(webView)
        webView.addJavascriptInterface(bridge, "DshNative")
        AppCore.setHost(this)

        applySystemBars()
        requestStartPermissions()
        webView.loadUrl(START_URL)
    }

    private fun configureWebView() {
        val settings = webView.settings
        settings.javaScriptEnabled = true
        settings.domStorageEnabled = true
        settings.databaseEnabled = true
        settings.allowFileAccess = false
        settings.allowContentAccess = false
        settings.javaScriptCanOpenWindowsAutomatically = false
        settings.mediaPlaybackRequiresUserGesture = false
        settings.setSupportMultipleWindows(false)
        webView.setBackgroundColor(getColor(R.color.window_background))
        if (BuildConfig.DEBUG) {
            WebView.setWebContentsDebuggingEnabled(true)
        }

        webView.webViewClient = object : WebViewClient() {
            override fun shouldInterceptRequest(
                view: WebView?,
                request: WebResourceRequest?,
            ): WebResourceResponse? {
                val url = request?.url ?: return null
                if (url.scheme == "https" && url.host == APPASSETS_HOST) {
                    return serveAsset(url.path ?: "/")
                }
                return null
            }

            override fun shouldOverrideUrlLoading(
                view: WebView?,
                request: WebResourceRequest?,
            ): Boolean {
                val url = request?.url ?: return true
                // Keep the WebView on the virtual origin; block everything else.
                return !(url.scheme == "https" && url.host == APPASSETS_HOST)
            }

            override fun onPageFinished(view: WebView?, url: String?) {
                super.onPageFinished(view, url)
                fallbackView.visibility = View.GONE
                webView.visibility = View.VISIBLE
                bridge.onPageReady()
                if (firstPageFinished) {
                    firstPageFinished = false
                    AppCore.autoConnectIfPaired()
                }
            }

            override fun onReceivedError(
                view: WebView?,
                request: WebResourceRequest?,
                error: WebResourceError?,
            ) {
                super.onReceivedError(view, request, error)
                if (request?.isForMainFrame == true) {
                    LogStore.warn("web.error", "code" to error?.errorCode)
                    showFallback()
                }
            }
        }

        webView.webChromeClient = object : WebChromeClient() {
            override fun onPermissionRequest(request: PermissionRequest?) {
                if (request == null) return
                val audioRequests = request.resources
                    .filter { it == PermissionRequest.RESOURCE_AUDIO_CAPTURE }
                    .toTypedArray()
                val granted = checkSelfPermission(Manifest.permission.RECORD_AUDIO) ==
                    PackageManager.PERMISSION_GRANTED
                if (granted && audioRequests.isNotEmpty()) {
                    request.grant(audioRequests)
                } else {
                    request.deny()
                }
            }

            override fun onShowFileChooser(
                view: WebView?,
                filePathCallback: ValueCallback<Array<Uri>>?,
                fileChooserParams: WebChromeClient.FileChooserParams?,
            ): Boolean {
                val callback = filePathCallback ?: return false
                // Only one pending chooser at a time; drop a previous unanswered one.
                fileChooserCallback?.onReceiveValue(null)
                fileChooserCallback = callback
                val intent = Intent(Intent.ACTION_GET_CONTENT).apply {
                    addCategory(Intent.CATEGORY_OPENABLE)
                    // The UI only offers images; keep the Web form fallback narrow.
                    type = "image/*"
                }
                return try {
                    startActivityForResult(intent, REQUEST_FILE_CHOOSER)
                    true
                } catch (e: Exception) {
                    fileChooserCallback = null
                    LogStore.warn("web.file_chooser_failed")
                    false
                }
            }

            override fun onConsoleMessage(consoleMessage: ConsoleMessage?): Boolean {
                if (BuildConfig.DEBUG && consoleMessage != null) {
                    LogStore.debug(
                        "web.console",
                        "level" to consoleMessage.messageLevel().name,
                        "message" to consoleMessage.message(),
                    )
                }
                return true
            }
        }
    }

    /* ------------------------------------------------------- native host ops */

    override fun launchQrScanner() {
        startActivityForResult(Intent(this, ScanActivity::class.java), REQUEST_SCAN)
    }

    override fun launchImagePicker(source: String): Boolean {
        val intent = when (source) {
            AppCore.SOURCE_CAMERA -> buildCaptureIntent() ?: return false
            else -> Intent(Intent.ACTION_GET_CONTENT).apply {
                addCategory(Intent.CATEGORY_OPENABLE)
                type = "image/*"
            }
        }
        val requestCode = if (source == AppCore.SOURCE_CAMERA) {
            REQUEST_IMAGE_CAPTURE
        } else {
            REQUEST_IMAGE_GALLERY
        }
        return try {
            startActivityForResult(intent, requestCode)
            true
        } catch (e: Exception) {
            LogStore.warn("image.launch_failed")
            false
        }
    }

    /**
     * Camera capture writes into a private cache file exposed through
     * [ImageFileProvider] (D-029): no `WRITE_EXTERNAL_STORAGE`, no legacy paths.
     */
    private fun buildCaptureIntent(): Intent? {
        return try {
            val file = ImageFileProvider.newCaptureFile(this) ?: return null
            val uri = ImageFileProvider.uriFor(file)
            pendingCaptureUri = uri
            Intent(MediaStore.ACTION_IMAGE_CAPTURE).apply {
                putExtra(MediaStore.EXTRA_OUTPUT, uri)
                addFlags(Intent.FLAG_GRANT_WRITE_URI_PERMISSION or Intent.FLAG_GRANT_READ_URI_PERMISSION)
            }
        } catch (e: Exception) {
            LogStore.warn("image.capture_intent_failed")
            null
        }
    }

    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        when (requestCode) {
            REQUEST_SCAN -> {
                if (resultCode == RESULT_OK) {
                    AppCore.onScanResult(data?.getStringExtra(ScanActivity.EXTRA_URI), null)
                } else {
                    AppCore.onScanResult(
                        null,
                        data?.getStringExtra(ScanActivity.EXTRA_REASON)
                            ?: ScanActivity.REASON_CANCELLED,
                    )
                }
            }

            REQUEST_IMAGE_GALLERY -> {
                if (resultCode == RESULT_OK) {
                    AppCore.onImagePicked(data?.data)
                } else {
                    AppCore.onImagePickCancelled()
                }
            }

            REQUEST_IMAGE_CAPTURE -> {
                val uri = pendingCaptureUri
                pendingCaptureUri = null
                if (resultCode == RESULT_OK && uri != null) {
                    AppCore.onImagePicked(uri)
                } else {
                    AppCore.onImagePickCancelled()
                }
            }

            REQUEST_FILE_CHOOSER -> {
                val callback = fileChooserCallback
                fileChooserCallback = null
                callback?.onReceiveValue(
                    WebChromeClient.FileChooserParams.parseResult(resultCode, data),
                )
            }
        }
    }

    /* --------------------------------------------------------------- assets */

    private fun serveAsset(path: String): WebResourceResponse {
        val clean = path.trim().trimStart('/')
        val relative = if (clean.isEmpty()) DEFAULT_DOCUMENT else clean
        val segments = relative.split('/')
        if (segments.any { it.isEmpty() || it == ".." || it == "." }) {
            return notFound()
        }
        return try {
            val bytes = assets.open("$ASSET_ROOT/$relative").use { it.readBytes() }
            WebResourceResponse(
                mimeTypeFor(relative),
                charsetFor(relative),
                ByteArrayInputStream(bytes),
            )
        } catch (e: IOException) {
            LogStore.warn("web.asset_missing", "path" to relative)
            notFound()
        }
    }

    private fun notFound(): WebResourceResponse = WebResourceResponse(
        "text/plain",
        "utf-8",
        404,
        "Not Found",
        Collections.emptyMap<String, String>(),
        ByteArrayInputStream(ByteArray(0)),
    )

    private fun mimeTypeFor(path: String): String {
        val name = path.substringAfterLast('/').lowercase()
        val extension = if (name.contains('.')) name.substringAfterLast('.') else ""
        return when (extension) {
            "html", "htm" -> "text/html"
            "js", "mjs" -> "text/javascript"
            "json" -> "application/json"
            "css" -> "text/css"
            "svg" -> "image/svg+xml"
            "png" -> "image/png"
            "jpg", "jpeg" -> "image/jpeg"
            "gif" -> "image/gif"
            "webp" -> "image/webp"
            "ico" -> "image/x-icon"
            "woff" -> "font/woff"
            "woff2" -> "font/woff2"
            "ttf" -> "font/ttf"
            "otf" -> "font/otf"
            "map" -> "application/json"
            "txt" -> "text/plain"
            "wasm" -> "application/wasm"
            else -> "application/octet-stream"
        }
    }

    private fun charsetFor(path: String): String? {
        return when (mimeTypeFor(path)) {
            "text/html", "text/javascript", "text/css", "application/json",
            "image/svg+xml", "text/plain" -> "utf-8"
            else -> null
        }
    }

    private fun createFallbackView(): View {
        val layout = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER
            visibility = View.GONE
            setBackgroundColor(getColor(R.color.window_background))
            val pad = (24 * resources.displayMetrics.density).toInt()
            setPadding(pad, pad, pad, pad)
        }
        val textColor = if (isNightMode()) 0xFFE6E6E6.toInt() else 0xFF1A1A1A.toInt()
        val title = TextView(this).apply {
            text = getString(R.string.fallback_title)
            textSize = 20f
            setTextColor(textColor)
        }
        val body = TextView(this).apply {
            text = getString(R.string.fallback_body)
            textSize = 14f
            setTextColor(textColor)
            setPadding(0, (16 * resources.displayMetrics.density).toInt(), 0, 0)
        }
        val hint = TextView(this).apply {
            text = getString(R.string.fallback_hint)
            textSize = 12f
            setTextColor(textColor)
            setPadding(0, (12 * resources.displayMetrics.density).toInt(), 0, 0)
        }
        layout.addView(title)
        layout.addView(body)
        layout.addView(hint)
        return layout
    }

    private fun showFallback() {
        webView.visibility = View.INVISIBLE
        fallbackView.visibility = View.VISIBLE
    }

    /* ------------------------------------------------------------ lifecycle */

    override fun onStart() {
        super.onStart()
        AppCore.onActivityStart(this)
    }

    override fun onStop() {
        super.onStop()
        AppCore.onActivityStop(this)
    }

    override fun onDestroy() {
        if (isFinishing) {
            // G5: never leave capture running behind a destroyed WebView. This is
            // the Activity's own signal (`isFinishing`); MicService cannot see it,
            // so the stop goes through the process-wide AppCore singleton.
            LogStore.info("app.finish_stop_audio")
            try {
                AppCore.stopAudio()
            } catch (e: Exception) {
                LogStore.warn("app.finish_stop_failed")
            }
            AppCore.setHost(null)
            val chooser = fileChooserCallback
            fileChooserCallback = null
            chooser?.onReceiveValue(null)
            AppCore.setEventSink(null)
        }
        try {
            webView.stopLoading()
            webView.loadUrl("about:blank")
            webView.destroy()
        } catch (e: Exception) {
            LogStore.warn("web.destroy_failed")
        }
        super.onDestroy()
    }

    override fun onConfigurationChanged(newConfig: Configuration) {
        super.onConfigurationChanged(newConfig)
        applySystemBars()
        val night = isNightMode()
        if (night != lastNightMode) {
            lastNightMode = night
            webView.reload()
        }
    }

    @Deprecated("Deprecated in Java")
    override fun onBackPressed() {
        if (!::webView.isInitialized || !webView.canGoBack() && firstPageFinished) {
            // Fall through to the JS hook even on the first page: it decides
            // whether the Web UI consumed the back press.
        }
        try {
            webView.evaluateJavascript(BACK_PROBE) { value ->
                if (value != "true") {
                    finishOrBack()
                }
            }
        } catch (e: Exception) {
            finishOrBack()
        }
    }

    private fun finishOrBack() {
        if (webView.canGoBack()) {
            webView.goBack()
        } else {
            finish()
        }
    }

    private fun applySystemBars() {
        val night = isNightMode()
        window.statusBarColor = getColor(R.color.status_bar)
        window.navigationBarColor = getColor(R.color.navigation_bar)
        var flags = 0
        if (!night) {
            flags = flags or View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                flags = flags or View.SYSTEM_UI_FLAG_LIGHT_NAVIGATION_BAR
            }
        }
        window.decorView.systemUiVisibility = flags
    }

    private fun isNightMode(): Boolean =
        (resources.configuration.uiMode and Configuration.UI_MODE_NIGHT_MASK) ==
            Configuration.UI_MODE_NIGHT_YES

    private fun requestStartPermissions() {
        val wanted = mutableListOf(Manifest.permission.RECORD_AUDIO)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            wanted.add(Manifest.permission.POST_NOTIFICATIONS)
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            wanted.add(Manifest.permission.BLUETOOTH_CONNECT)
        }
        val missing = wanted.filter {
            checkSelfPermission(it) != PackageManager.PERMISSION_GRANTED
        }
        if (missing.isNotEmpty()) {
            try {
                requestPermissions(missing.toTypedArray(), REQUEST_START_PERMISSIONS)
            } catch (e: Exception) {
                LogStore.warn("permission.request_failed")
            }
        }
    }

    override fun onRequestPermissionsResult(
        requestCode: Int,
        permissions: Array<out String>,
        grantResults: IntArray,
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == REQUEST_START_PERMISSIONS) {
            val denied = permissions.filterIndexed { index, _ ->
                grantResults.getOrNull(index) != PackageManager.PERMISSION_GRANTED
            }
            if (denied.isNotEmpty()) {
                LogStore.warn("permission.denied", "count" to denied.size)
            }
        }
    }

    private companion object {
        const val APPASSETS_HOST = "appassets.local"
        const val START_URL = "https://appassets.local/index.html"
        const val DEFAULT_DOCUMENT = "index.html"
        const val ASSET_ROOT = "web"
        const val REQUEST_START_PERMISSIONS = 1003
        const val REQUEST_SCAN = 1010
        const val REQUEST_IMAGE_GALLERY = 1011
        const val REQUEST_IMAGE_CAPTURE = 1012
        const val REQUEST_FILE_CHOOSER = 1013

        const val BACK_PROBE =
            "(function(){try{return (window.DshBridge && " +
                "typeof window.DshBridge.onBack === 'function') ? " +
                "!!window.DshBridge.onBack() : false;}catch(e){return false;}})()"
    }
}
