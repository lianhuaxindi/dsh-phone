package com.dsh.phone

import android.Manifest
import android.app.Activity
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.ImageFormat
import android.hardware.camera2.CameraCaptureSession
import android.hardware.camera2.CameraCharacteristics
import android.hardware.camera2.CameraDevice
import android.hardware.camera2.CameraManager
import android.hardware.camera2.CaptureRequest
import android.media.Image
import android.media.ImageReader
import android.os.Bundle
import android.os.Handler
import android.os.HandlerThread
import android.util.Size
import android.view.Gravity
import android.view.Surface
import android.view.TextureView
import android.view.ViewGroup
import android.widget.Button
import android.widget.FrameLayout
import android.widget.TextView
import com.dsh.phone.log.LogStore
import com.google.zxing.BarcodeFormat
import com.google.zxing.BinaryBitmap
import com.google.zxing.DecodeHintType
import com.google.zxing.MultiFormatReader
import com.google.zxing.NotFoundException
import com.google.zxing.PlanarYUVLuminanceSource
import com.google.zxing.common.HybridBinarizer
import java.util.concurrent.atomic.AtomicBoolean

/**
 * Full-screen QR scanner used by the `scanPairing` bridge op (D-030, PROTOCOL §5.1).
 *
 * Camera2 (no Google Play Services, no network) + ZXing `core` running locally.
 * Only QR codes are decoded; the payload is expected to be a `dsphone://pair?...`
 * URI which the caller ([MainActivity] -> [com.dsh.phone.AppCore]) validates with
 * [com.dsh.phone.protocol.Pairing.parseUri].
 *
 * Results are returned through the activity result:
 *  - `RESULT_OK`   + [EXTRA_URI] = raw QR text (the pairing URI)
 *  - `RESULT_CANCELED` + [EXTRA_REASON] = cancelled|permission_denied|no_camera|decode_failed
 *
 * Manually typing host/port/code remains available in the Web UI, so a denial,
 * missing camera or undecodable code is never a dead end.
 */
class ScanActivity : Activity() {

    private lateinit var textureView: TextureView
    private lateinit var hintView: TextView

    private var cameraDevice: CameraDevice? = null
    private var captureSession: CameraCaptureSession? = null
    private var imageReader: ImageReader? = null
    private var previewSurface: Surface? = null

    private var backgroundThread: HandlerThread? = null
    private var backgroundHandler: Handler? = null

    private val decoding = AtomicBoolean(false)
    private var frameCounter = 0
    private var finished = false
    private var surfaceReady = false

    private val multiReader = MultiFormatReader().apply {
        val hints = HashMap<DecodeHintType, Any>()
        hints[DecodeHintType.POSSIBLE_FORMATS] = listOf(BarcodeFormat.QR_CODE)
        hints[DecodeHintType.TRY_HARDER] = true
        setHints(hints)
    }

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

        hintView = TextView(this).apply {
            text = getString(R.string.scan_hint)
            textSize = 14f
            setTextColor(0xFFFFFFFF.toInt())
            setShadowLayer(4f, 0f, 2f, 0xFF000000.toInt())
            gravity = Gravity.CENTER
        }
        root.addView(
            hintView,
            FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT,
                Gravity.TOP or Gravity.CENTER_HORIZONTAL,
            ).apply { topMargin = dp(32) },
        )

        val cancel = Button(this).apply {
            text = getString(R.string.scan_cancel)
            setOnClickListener { finishFailure(REASON_CANCELLED) }
        }
        root.addView(
            cancel,
            FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT,
                ViewGroup.LayoutParams.WRAP_CONTENT,
                Gravity.BOTTOM or Gravity.CENTER_HORIZONTAL,
            ).apply { bottomMargin = dp(32) },
        )

        setContentView(root)
    }

    private fun dp(value: Int): Int = (value * resources.displayMetrics.density).toInt()

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
            openCamera(manager, cameraId, surface, reader.surface, characteristics, handler)
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

    private fun choosePreviewSize(map: android.hardware.camera2.params.StreamConfigurationMap?): Size? {
        val sizes = map?.getOutputSizes(ImageFormat.YUV_420_888) ?: return null
        if (sizes.isEmpty()) return null
        val maxEdge = 1280
        var best: Size? = null
        for (size in sizes) {
            if (size.width > maxEdge || size.height > maxEdge) continue
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
        characteristics: CameraCharacteristics,
        handler: Handler,
    ) {
        try {
            manager.openCamera(
                cameraId,
                object : CameraDevice.StateCallback() {
                    override fun onOpened(device: CameraDevice) {
                        cameraDevice = device
                        createSession(device, surface, readerSurface, characteristics, handler)
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
        characteristics: CameraCharacteristics,
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
                            val afModes = characteristics.get(CameraCharacteristics.CONTROL_AF_AVAILABLE_MODES)
                            if (afModes != null && afModes.contains(CaptureRequest.CONTROL_AF_MODE_CONTINUOUS_PICTURE)) {
                                builder.set(
                                    CaptureRequest.CONTROL_AF_MODE,
                                    CaptureRequest.CONTROL_AF_MODE_CONTINUOUS_PICTURE,
                                )
                            }
                            session.setRepeatingRequest(builder.build(), null, handler)
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

    /* ----------------------------------------------------------------- decode */

    private fun handleImageAvailable(reader: ImageReader) {
        val image = try {
            reader.acquireLatestImage()
        } catch (e: Exception) {
            null
        } ?: return
        try {
            if (finished) return
            frameCounter += 1
            if (frameCounter % 2 != 0) return
            if (!decoding.compareAndSet(false, true)) return
            try {
                decode(image)
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

    private fun decode(image: Image) {
        val width = image.width
        val height = image.height
        if (width <= 0 || height <= 0) return
        val luminance = toLuminance(image, width, height) ?: return
        val source = PlanarYUVLuminanceSource(luminance, width, height, 0, 0, width, height, false)
        val bitmap = BinaryBitmap(HybridBinarizer(source))
        val result = try {
            multiReader.decodeWithState(bitmap)
        } catch (e: NotFoundException) {
            null
        } catch (e: Exception) {
            LogStore.warn("scan.decode_failed", "message" to (e.message ?: e.javaClass.simpleName))
            null
        } finally {
            multiReader.reset()
        }
        if (result != null) {
            LogStore.info("scan.decoded", "format" to result.barcodeFormat.name)
            val text = result.text ?: return
            runOnUiThread { finishSuccess(text) }
        }
    }

    private fun toLuminance(image: Image, width: Int, height: Int): ByteArray? {
        val plane = image.planes.getOrNull(0) ?: return null
        return try {
            val buffer = plane.buffer
            val rowStride = plane.rowStride
            val pixelStride = plane.pixelStride
            val limit = buffer.limit()
            val data = ByteArray(width * height)
            if (pixelStride == 1 && rowStride == width) {
                buffer.rewind()
                val n = minOf(buffer.remaining(), data.size)
                buffer.get(data, 0, n)
                data
            } else {
                for (y in 0 until height) {
                    val rowStart = y * rowStride
                    if (rowStart >= limit) break
                    val dst = y * width
                    for (x in 0 until width) {
                        val index = rowStart + x * pixelStride
                        if (index >= limit) break
                        data[dst + x] = buffer.get(index)
                    }
                }
                data
            }
        } catch (e: Exception) {
            LogStore.warn("scan.luminance_failed")
            null
        }
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
    }
}
