package com.dsh.phone

import android.Manifest
import android.app.Activity
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.ImageFormat
import android.hardware.camera2.CameraCaptureSession
import android.hardware.camera2.CameraCharacteristics
import android.hardware.camera2.CameraDevice
import android.hardware.camera2.CameraManager
import android.hardware.camera2.CaptureRequest
import android.hardware.camera2.params.MeteringRectangle
import android.media.ImageReader
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.HandlerThread
import android.util.Size
import android.view.Gravity
import android.view.MotionEvent
import android.view.Surface
import android.view.TextureView
import android.view.ViewGroup
import android.widget.Button
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.TextView
import com.dsh.phone.log.LogStore
import com.dsh.phone.scan.QrDecoder
import com.dsh.phone.scan.YuvLuma
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger

/**
 * 全屏二维码扫描（`scanPairing`，PROTOCOL §5.1 / D-030 / D-036）。
 *
 * 识别策略（D-036）：
 *  - 优先取较大的 YUV 尺寸（最高 1920×1080）→ 二维码在画面里占更多像素，屏幕上的码更容易命中；
 *  - 每 3 帧解一次，避免解码拖慢预览；
 *  - 解码走 [QrDecoder]（整帧 → 反色 → 中心 62% 裁剪，Hybrid/GlobalHistogram 两种二值化）；
 *  - 触摸画面 = 点按对焦；「2×」= 数字变焦；「补光」= 手电；「相册」= 从图片里解码（绕开相机问题）。
 *
 * 画面上实时显示「帧数 / 解码次数 / 失败次数」，便于区分「取不到画面」和「取到但认不出」。
 *
 * 结果：`RESULT_OK` + [EXTRA_URI]；`RESULT_CANCELED` + [EXTRA_REASON]。
 */
class ScanActivity : Activity() {

    private lateinit var textureView: TextureView
    private lateinit var statusView: TextView
    private lateinit var zoomButton: Button
    private lateinit var torchButton: Button

    private var cameraDevice: CameraDevice? = null
    private var captureSession: CameraCaptureSession? = null
    private var imageReader: ImageReader? = null
    private var previewSurface: Surface? = null
    private var requestBuilder: CaptureRequest.Builder? = null
    private var hasFlash = false
    private var maxZoom = 1.0f

    private var backgroundThread: HandlerThread? = null
    private var backgroundHandler: Handler? = null

    private val decoding = AtomicBoolean(false)
    private val framesSeen = AtomicInteger(0)
    private val decodeTries = AtomicInteger(0)
    private val decodeFails = AtomicInteger(0)

    private var finished = false
    private var surfaceReady = false
    private var zoomRatio = 1.0f
    private var torchOn = false
    private var lastStatusAt = 0L

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        buildUi()
        if (checkSelfPermission(Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(arrayOf(Manifest.permission.CAMERA), REQUEST_CAMERA_PERMISSION)
        }
    }

    private fun buildUi() {
        val root = FrameLayout(this)

        textureView = TextureView(this)
        root.addView(
            textureView,
            FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.MATCH_PARENT,
            ),
        )
        textureView.surfaceTextureListener = object : TextureView.SurfaceTextureListener {
            override fun onSurfaceTextureAvailable(surface: android.graphics.SurfaceTexture, width: Int, height: Int) {
                surfaceReady = true
                openCameraIfPossible()
            }

            override fun onSurfaceTextureSizeChanged(surface: android.graphics.SurfaceTexture, width: Int, height: Int) = Unit

            override fun onSurfaceTextureDestroyed(surface: android.graphics.SurfaceTexture): Boolean {
                surfaceReady = false
                releaseCamera()
                return true
            }

            override fun onSurfaceTextureUpdated(surface: android.graphics.SurfaceTexture) = Unit
        }
        textureView.setOnTouchListener { _, event ->
            if (event.action == MotionEvent.ACTION_UP) {
                focusAt(event.x, event.y)
            }
            true
        }

        statusView = TextView(this).apply {
            text = getString(R.string.scan_hint)
            textSize = 14f
            setTextColor(0xFFFFFFFF.toInt())
            setShadowLayer(4f, 0f, 2f, 0xFF000000.toInt())
            gravity = Gravity.CENTER
            setPadding(dp(16), dp(8), dp(16), dp(8))
        }
        root.addView(
            statusView,
            FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT,
                Gravity.TOP or Gravity.CENTER_HORIZONTAL,
            ).apply { topMargin = dp(32) },
        )

        val row = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER
        }
        val gallery = Button(this).apply {
            text = getString(R.string.scan_gallery)
            setOnClickListener { pickGalleryImage() }
        }
        zoomButton = Button(this).apply {
            text = getString(R.string.scan_zoom)
            setOnClickListener { toggleZoom() }
        }
        torchButton = Button(this).apply {
            text = getString(R.string.scan_torch)
            setOnClickListener { toggleTorch() }
        }
        val cancel = Button(this).apply {
            text = getString(R.string.scan_cancel)
            setOnClickListener { finishFailure(REASON_CANCELLED) }
        }
        row.addView(gallery)
        row.addView(zoomButton)
        row.addView(torchButton)
        row.addView(cancel)

        root.addView(
            row,
            FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT,
                ViewGroup.LayoutParams.WRAP_CONTENT,
                Gravity.BOTTOM or Gravity.CENTER_HORIZONTAL,
            ).apply { bottomMargin = dp(24) },
        )

        setContentView(root)
    }

    private fun dp(value: Int): Int = (value * resources.displayMetrics.density).toInt()

    private fun setStatus(message: String) {
        runOnUiThread {
            if (!finished) statusView.text = message
        }
    }

    /* ----------------------------------------------------------------- camera */

    private fun openCameraIfPossible() {
        if (finished || !surfaceReady || cameraDevice != null) return
        if (checkSelfPermission(Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED) return
        startBackgroundThread()
        val handler = backgroundHandler ?: return
        val manager = getSystemService(Context.CAMERA_SERVICE) as? CameraManager
        if (manager == null) {
            finishFailure(REASON_NO_CAMERA)
            return
        }
        val cameraId = chooseCamera(manager)
        if (cameraId == null) {
            finishFailure(REASON_NO_CAMERA)
            return
        }
        val characteristics = try {
            manager.getCameraCharacteristics(cameraId)
        } catch (e: Exception) {
            LogStore.warn("scan.characteristics_failed")
            finishFailure(REASON_NO_CAMERA)
            return
        }
        val map = characteristics.get(CameraCharacteristics.SCALER_STREAM_CONFIGURATION_MAP)
        val previewSize = choosePreviewSize(map)
        if (previewSize == null) {
            finishFailure(REASON_NO_CAMERA)
            return
        }
        hasFlash = characteristics.get(CameraCharacteristics.FLASH_INFO_AVAILABLE) == true
        maxZoom = characteristics.get(CameraCharacteristics.SCALER_AVAILABLE_MAX_DIGITAL_ZOOM) ?: 1.0f
        if (!hasFlash) torchButton.visibility = ViewGroup.GONE
        if (maxZoom < 1.5f) zoomButton.visibility = ViewGroup.GONE
        try {
            val surfaceTexture = textureView.surfaceTexture
            if (surfaceTexture == null) {
                finishFailure(REASON_NO_CAMERA)
                return
            }
            surfaceTexture.setDefaultBufferSize(previewSize.width, previewSize.height)
            val surface = Surface(surfaceTexture)
            previewSurface = surface
            val reader = ImageReader.newInstance(
                previewSize.width,
                previewSize.height,
                ImageFormat.YUV_420_888,
                2,
            )
            reader.setOnImageAvailableListener({ source -> handleImageAvailable(source) }, handler)
            imageReader = reader
            LogStore.info("scan.capture_size", "size" to "${previewSize.width}x${previewSize.height}")
            characteristicsRef = characteristics
            openCamera(manager, cameraId, surface, reader.surface, handler)
        } catch (e: Exception) {
            LogStore.warn("scan.setup_failed", "message" to (e.message ?: e.javaClass.simpleName))
            finishFailure(REASON_NO_CAMERA)
        }
    }

    private fun chooseCamera(manager: CameraManager): String? {
        val ids = try {
            manager.cameraIdList
        } catch (e: Exception) {
            return null
        }
        var fallback: String? = null
        for (id in ids) {
            if (fallback == null) fallback = id
            try {
                val facing = manager.getCameraCharacteristics(id)
                    .get(CameraCharacteristics.LENS_FACING)
                if (facing == CameraCharacteristics.LENS_FACING_BACK) return id
            } catch (e: Exception) {
                // Try the next camera.
            }
        }
        return fallback
    }

    /**
     * 取「不超过 1920×1080 的最大 YUV 尺寸」：二维码在屏幕上是细纹理，分辨率越高越容易识别；
     * 1280 上限在旧实现里被证实对远处小二维码偏紧（D-036）。
     */
    private fun choosePreviewSize(map: android.hardware.camera2.params.StreamConfigurationMap?): Size? {
        val sizes = map?.getOutputSizes(ImageFormat.YUV_420_888) ?: return null
        if (sizes.isEmpty()) return null
        val maxPixels = 1920 * 1080
        var best: Size? = null
        for (size in sizes) {
            if (size.width * size.height > maxPixels) continue
            if (size.width < 640 || size.height < 480) continue
            if (best == null || size.width * size.height > best.width * best.height) best = size
        }
        return best ?: sizes.minByOrNull { it.width * it.height }
    }

    @Suppress("MissingPermission")
    private fun openCamera(
        manager: CameraManager,
        cameraId: String,
        surface: Surface,
        readerSurface: Surface,
        handler: Handler,
    ) {
        try {
            manager.openCamera(
                cameraId,
                object : CameraDevice.StateCallback() {
                    override fun onOpened(device: CameraDevice) {
                        cameraDevice = device
                        createSession(device, surface, readerSurface, handler)
                    }

                    override fun onDisconnected(device: CameraDevice) {
                        try {
                            device.close()
                        } catch (ignored: Exception) {
                            // Already gone.
                        }
                        finishFailure(REASON_NO_CAMERA)
                    }

                    override fun onError(device: CameraDevice, error: Int) {
                        LogStore.warn("scan.camera_error", "error" to error)
                        try {
                            device.close()
                        } catch (ignored: Exception) {
                            // Already gone.
                        }
                        finishFailure(REASON_NO_CAMERA)
                    }
                },
                handler,
            )
        } catch (e: SecurityException) {
            finishFailure(REASON_PERMISSION_DENIED)
        } catch (e: Exception) {
            LogStore.warn("scan.open_failed", "message" to (e.message ?: e.javaClass.simpleName))
            finishFailure(REASON_NO_CAMERA)
        }
    }

    @Suppress("DEPRECATION")
    private fun createSession(
        device: CameraDevice,
        surface: Surface,
        readerSurface: Surface,
        handler: Handler,
    ) {
        try {
            device.createCaptureSession(
                listOf(surface, readerSurface),
                object : CameraCaptureSession.StateCallback() {
                    override fun onConfigured(session: CameraCaptureSession) {
                        captureSession = session
                        try {
                            val builder = device.createCaptureRequest(CameraDevice.TEMPLATE_PREVIEW)
                            builder.addTarget(surface)
                            builder.addTarget(readerSurface)
                            requestBuilder = builder
                            applyRepeatingRequest()
                            scheduleNoFrameHint()
                        } catch (e: Exception) {
                            LogStore.warn("scan.preview_failed")
                            finishFailure(REASON_NO_CAMERA)
                        }
                    }

                    override fun onConfigureFailed(session: CameraCaptureSession) {
                        LogStore.warn("scan.session_failed")
                        finishFailure(REASON_NO_CAMERA)
                    }
                },
                handler,
            )
        } catch (e: Exception) {
            LogStore.warn("scan.create_session_failed")
            finishFailure(REASON_NO_CAMERA)
        }
    }

    private fun applyRepeatingRequest() {
        val session = captureSession ?: return
        val builder = requestBuilder ?: return
        val handler = backgroundHandler ?: return
        try {
            val afModes = cameraCharacteristics()?.get(CameraCharacteristics.CONTROL_AF_AVAILABLE_MODES)
            if (afModes != null && afModes.contains(CaptureRequest.CONTROL_AF_MODE_CONTINUOUS_PICTURE)) {
                builder.set(CaptureRequest.CONTROL_AF_MODE, CaptureRequest.CONTROL_AF_MODE_CONTINUOUS_PICTURE)
            }
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R && zoomRatio > 1.001f) {
                builder.set(CaptureRequest.CONTROL_ZOOM_RATIO, zoomRatio)
            }
            builder.set(
                CaptureRequest.FLASH_MODE,
                if (torchOn && hasFlash) CaptureRequest.FLASH_MODE_TORCH else CaptureRequest.FLASH_MODE_OFF,
            )
            session.setRepeatingRequest(builder.build(), null, handler)
        } catch (e: Exception) {
            LogStore.warn("scan.repeating_failed", "message" to (e.message ?: e.javaClass.simpleName))
        }
    }

    private var characteristicsRef: CameraCharacteristics? = null

    private fun cameraCharacteristics(): CameraCharacteristics? = characteristicsRef

    private fun focusAt(x: Float, y: Float) {
        val session = captureSession ?: return
        val builder = requestBuilder ?: return
        val handler = backgroundHandler ?: return
        val chars = characteristicsRef ?: return
        val afModes = chars.get(CameraCharacteristics.CONTROL_AF_AVAILABLE_MODES) ?: return
        if (!afModes.contains(CaptureRequest.CONTROL_AF_MODE_AUTO) &&
            !afModes.contains(CaptureRequest.CONTROL_AF_MODE_CONTINUOUS_PICTURE)
        ) {
            return
        }
        val view = textureView
        if (view.width <= 0 || view.height <= 0) return
        val active = chars.get(CameraCharacteristics.SENSOR_INFO_ACTIVE_ARRAY_SIZE) ?: return
        val nx = (x / view.width).coerceIn(0f, 1f)
        val ny = (y / view.height).coerceIn(0f, 1f)
        val half = 120
        val cx = (nx * active.width()).toInt()
        val cy = (ny * active.height()).toInt()
        val left = (cx - half).coerceIn(0, maxOf(0, active.width() - 1))
        val top = (cy - half).coerceIn(0, maxOf(0, active.height() - 1))
        val right = (cx + half).coerceIn(left + 1, active.width())
        val bottom = (cy + half).coerceIn(top + 1, active.height())
        try {
            val afBuilder = cameraDevice!!.createCaptureRequest(CameraDevice.TEMPLATE_PREVIEW)
            afBuilder.addTarget(previewSurface!!)
            imageReader?.surface?.let { afBuilder.addTarget(it) }
            afBuilder.set(CaptureRequest.CONTROL_AF_MODE, CaptureRequest.CONTROL_AF_MODE_AUTO)
            afBuilder.set(CaptureRequest.CONTROL_AF_REGIONS, arrayOf(MeteringRectangle(left, top, right - left, bottom - top, 1000)))
            afBuilder.set(CaptureRequest.CONTROL_AE_REGIONS, arrayOf(MeteringRectangle(left, top, right - left, bottom - top, 1000)))
            afBuilder.set(CaptureRequest.CONTROL_AF_TRIGGER, CaptureRequest.CONTROL_AF_TRIGGER_START)
            session.capture(afBuilder.build(), null, handler)
            afBuilder.set(CaptureRequest.CONTROL_AF_TRIGGER, CaptureRequest.CONTROL_AF_TRIGGER_IDLE)
            setStatus(getString(R.string.scan_focusing))
        } catch (e: Exception) {
            LogStore.debug("scan.focus_failed")
        }
    }

    private fun toggleZoom() {
        zoomRatio = if (zoomRatio > 1.001f) 1.0f else minOf(2.0f, maxOf(1.0f, maxZoom))
        zoomButton.text = if (zoomRatio > 1.001f) getString(R.string.scan_zoom_on) else getString(R.string.scan_zoom)
        applyRepeatingRequest()
    }

    private fun toggleTorch() {
        torchOn = !torchOn
        applyRepeatingRequest()
    }

    private fun scheduleNoFrameHint() {
        val handler = backgroundHandler ?: return
        handler.postDelayed({
            if (!finished && framesSeen.get() == 0) {
                setStatus(getString(R.string.scan_no_frames))
                LogStore.warn("scan.no_frames")
            }
        }, 3000)
    }

    /* ----------------------------------------------------------------- decode */

    private fun handleImageAvailable(reader: ImageReader) {
        val image = try {
            reader.acquireLatestImage()
        } catch (e: Exception) {
            null
        } ?: return
        try {
            if (finished) return
            val count = framesSeen.incrementAndGet()
            if (count % 3 != 0) return
            if (!decoding.compareAndSet(false, true)) return
            try {
                decode(image)
                publishProgress()
            } finally {
                decoding.set(false)
            }
        } finally {
            try {
                image.close()
            } catch (ignored: Exception) {
                // Already closed.
            }
        }
    }

    private fun publishProgress() {
        if (finished) return
        val now = System.currentTimeMillis()
        if (now - lastStatusAt < 400) return
        lastStatusAt = now
        val frames = framesSeen.get()
        val tries = decodeTries.get()
        val fails = decodeFails.get()
        if (frames > 0 && tries > 0) {
            setStatus(getString(R.string.scan_status_running, frames, tries, fails))
        }
    }

    private fun decode(image: android.media.Image) {
        val width = image.width
        val height = image.height
        if (width <= 0 || height <= 0) return
        val plane = image.planes.getOrNull(0) ?: return
        val gray = try {
            YuvLuma.toGray(width, height, plane.rowStride, plane.pixelStride, plane.buffer)
        } catch (e: Exception) {
            LogStore.warn("scan.luminance_failed", "message" to (e.message ?: e.javaClass.simpleName))
            decodeFails.incrementAndGet()
            return
        }
        decodeTries.incrementAndGet()
        val text = QrDecoder.decodeGray(gray, width, height)
        if (text != null) {
            LogStore.info("scan.decoded", "len" to text.length)
            runOnUiThread { finishSuccess(text) }
        } else {
            decodeFails.incrementAndGet()
            if (decodeTries.get() == 1) {
                setStatus(getString(R.string.scan_status_first_miss))
            }
        }
    }

    /* ----------------------------------------------------------------- gallery */

    private fun pickGalleryImage() {
        try {
            val intent = Intent(Intent.ACTION_GET_CONTENT)
            intent.type = "image/*"
            intent.addCategory(Intent.CATEGORY_OPENABLE)
            startActivityForResult(intent, REQUEST_GALLERY)
        } catch (e: Exception) {
            LogStore.warn("scan.gallery_intent_failed")
            setStatus(getString(R.string.scan_gallery_unavailable))
        }
    }

    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode != REQUEST_GALLERY) return
        val uri = data?.data
        if (resultCode != RESULT_OK || uri == null) return
        setStatus(getString(R.string.scan_gallery_decoding))
        Thread {
            val text = try {
                decodeImageUri(uri)
            } catch (e: Exception) {
                LogStore.warn("scan.gallery_failed", "message" to (e.message ?: e.javaClass.simpleName))
                null
            }
            runOnUiThread {
                if (finished) return@runOnUiThread
                if (text != null) {
                    finishSuccess(text)
                } else {
                    setStatus(getString(R.string.scan_gallery_no_qr))
                }
            }
        }.start()
    }

    private fun decodeImageUri(uri: Uri): String? {
        val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
        contentResolver.openInputStream(uri)?.use { BitmapFactory.decodeStream(it, null, bounds) }
        if (bounds.outWidth <= 0 || bounds.outHeight <= 0) return null
        var sample = 1
        val maxEdge = 2000
        while (maxOf(bounds.outWidth, bounds.outHeight) / (sample * 2) >= maxEdge) sample *= 2
        val options = BitmapFactory.Options().apply {
            inSampleSize = sample
            inPreferredConfig = Bitmap.Config.ARGB_8888
        }
        var bitmap: Bitmap? = contentResolver.openInputStream(uri)?.use {
            BitmapFactory.decodeStream(it, null, options)
        }
        val source = bitmap ?: return null
        val w = source.width
        val h = source.height
        val pixels = IntArray(w * h)
        source.getPixels(pixels, 0, w, 0, 0, w, h)
        source.recycle()
        bitmap = null
        val gray = ByteArray(w * h)
        for (i in pixels.indices) {
            val p = pixels[i]
            val r = (p shr 16) and 0xff
            val g = (p shr 8) and 0xff
            val b = p and 0xff
            gray[i] = ((r * 299 + g * 587 + b * 114) / 1000).toByte()
        }
        return QrDecoder.decodeGray(gray, w, h)
    }

    /* --------------------------------------------------------------- lifecycle */

    override fun onResume() {
        super.onResume()
        if (textureView.isAvailable) {
            surfaceReady = true
            openCameraIfPossible()
        }
    }

    override fun onPause() {
        releaseCamera()
        stopBackgroundThread()
        super.onPause()
    }

    override fun onDestroy() {
        releaseCamera()
        stopBackgroundThread()
        super.onDestroy()
    }

    @Deprecated("Deprecated in Java")
    override fun onBackPressed() {
        finishFailure(REASON_CANCELLED)
    }

    private fun releaseCamera() {
        try {
            captureSession?.close()
        } catch (ignored: Exception) {
            // Already closed.
        }
        captureSession = null
        requestBuilder = null
        try {
            cameraDevice?.close()
        } catch (ignored: Exception) {
            // Already closed.
        }
        cameraDevice = null
        try {
            imageReader?.close()
        } catch (ignored: Exception) {
            // Already closed.
        }
        imageReader = null
        try {
            previewSurface?.release()
        } catch (ignored: Exception) {
            // Already released.
        }
        previewSurface = null
        characteristicsRef = null
    }

    private fun startBackgroundThread() {
        if (backgroundThread != null) return
        val thread = HandlerThread("dsh-scan")
        thread.start()
        backgroundThread = thread
        backgroundHandler = Handler(thread.looper)
    }

    private fun stopBackgroundThread() {
        val thread = backgroundThread ?: return
        backgroundThread = null
        backgroundHandler = null
        try {
            thread.quitSafely()
            thread.join(500)
        } catch (ignored: InterruptedException) {
            Thread.currentThread().interrupt()
        }
    }

    private fun finishSuccess(uri: String) {
        if (finished) return
        finished = true
        setResult(RESULT_OK, Intent().putExtra(EXTRA_URI, uri))
        finish()
    }

    private fun finishFailure(reason: String) {
        if (finished) return
        finished = true
        setResult(RESULT_CANCELED, Intent().putExtra(EXTRA_REASON, reason))
        finish()
    }

    override fun onRequestPermissionsResult(
        requestCode: Int,
        permissions: Array<out String>,
        grantResults: IntArray,
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode != REQUEST_CAMERA_PERMISSION) return
        val granted = permissions.indices.any {
            permissions[it] == Manifest.permission.CAMERA &&
                grantResults.getOrNull(it) == PackageManager.PERMISSION_GRANTED
        }
        if (granted) {
            surfaceReady = textureView.isAvailable
            openCameraIfPossible()
        } else {
            LogStore.warn("scan.permission_denied")
            finishFailure(REASON_PERMISSION_DENIED)
        }
    }

    companion object {
        const val EXTRA_URI = "com.dsh.phone.extra.SCAN_URI"
        const val EXTRA_REASON = "com.dsh.phone.extra.SCAN_REASON"

        const val REASON_CANCELLED = "cancelled"
        const val REASON_PERMISSION_DENIED = "permission_denied"
        const val REASON_NO_CAMERA = "no_camera"
        const val REASON_DECODE_FAILED = "decode_failed"

        private const val REQUEST_CAMERA_PERMISSION = 2001
        private const val REQUEST_GALLERY = 2002
    }
}
