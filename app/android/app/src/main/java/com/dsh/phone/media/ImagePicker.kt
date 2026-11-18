package com.dsh.phone.media

import android.content.Context
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.net.Uri
import android.provider.OpenableColumns
import android.util.Base64
import com.dsh.phone.log.LogStore
import com.dsh.phone.storage.ImageFileProvider
import java.io.ByteArrayOutputStream
import java.io.FileNotFoundException

/**
 * Native image pipeline for v1.1 `pickImage` (D-029, PROTOCOL §9).
 *
 * Whatever the source (gallery content URI or a camera `EXTRA_OUTPUT` capture
 * file), the image is decoded with a power-of-two sample, scaled to a long edge
 * of at most [ImageScaler.MAX_LONG_EDGE] px, re-encoded as JPEG quality
 * [ImageScaler.JPEG_QUALITY] and base64 encoded (`NO_WRAP`). Only the encoded
 * payload ever reaches the WebView, which then puts it into
 * `chat.send.images[].data`; base64 never goes to the Bridge by itself.
 *
 * The function blocks and allocates bitmaps; call it on a worker thread.
 */
object ImagePicker {

    /** Hard ceiling accepted by the protocol for one decoded image (PROTOCOL §9). */
    const val MAX_IMAGE_BYTES = 2 * 1024 * 1024

    data class PickedImage(
        val base64: String,
        val mimeType: String,
        val name: String,
        val width: Int,
        val height: Int,
    )

    /** Failure carrying a `image.pickFailed { reason }` value. */
    class PickException(val reason: String, message: String) : Exception(message)

    /**
     * @throws PickException with one of `permission_denied|cancelled|failed|decode_failed|too_large`
     */
    fun load(context: Context, uri: Uri): PickedImage {
        val resolver = context.contentResolver
        val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
        try {
            resolver.openInputStream(uri)?.use { BitmapFactory.decodeStream(it, null, bounds) }
                ?: throw PickException("failed", "cannot open image stream")
        } catch (e: SecurityException) {
            throw PickException("permission_denied", "no read permission for the picked image")
        } catch (e: FileNotFoundException) {
            throw PickException("failed", "picked image no longer exists")
        } catch (e: Exception) {
            throw PickException("decode_failed", "cannot read image bounds: ${e.message}")
        }
        val sourceWidth = bounds.outWidth
        val sourceHeight = bounds.outHeight
        if (sourceWidth <= 0 || sourceHeight <= 0) {
            throw PickException("decode_failed", "image has no decodable pixels")
        }
        if (sourceWidth.toLong() * sourceHeight.toLong() > MAX_SOURCE_PIXELS) {
            throw PickException("too_large", "image is too large: ${sourceWidth}x$sourceHeight")
        }

        val options = BitmapFactory.Options().apply {
            inSampleSize = ImageScaler.inSampleSize(sourceWidth, sourceHeight)
            inPreferredConfig = Bitmap.Config.ARGB_8888
        }
        val decoded = try {
            resolver.openInputStream(uri)?.use { BitmapFactory.decodeStream(it, null, options) }
        } catch (e: SecurityException) {
            throw PickException("permission_denied", "no read permission for the picked image")
        } catch (e: OutOfMemoryError) {
            throw PickException("failed", "out of memory while decoding the image")
        }
        if (decoded == null) throw PickException("decode_failed", "cannot decode the image")

        try {
            val target = ImageScaler.targetSize(decoded.width, decoded.height)
            val scaled = if (target[0] == decoded.width && target[1] == decoded.height) {
                decoded
            } else {
                Bitmap.createScaledBitmap(decoded, target[0], target[1], true)
            }
            val outWidth = scaled.width
            val outHeight = scaled.height
            try {
                val encoded = encodeUnderLimit(scaled)
                return PickedImage(
                    base64 = Base64.encodeToString(encoded, Base64.NO_WRAP),
                    mimeType = ImageFileProvider.MIME_JPEG,
                    name = displayName(context, uri),
                    width = outWidth,
                    height = outHeight,
                )
            } finally {
                if (scaled !== decoded) scaled.recycle()
            }
        } finally {
            decoded.recycle()
        }
    }

    /** JPEG quality ladder: stay under the 2 MiB protocol limit when possible. */
    private fun encodeUnderLimit(bitmap: Bitmap): ByteArray {
        var quality = ImageScaler.JPEG_QUALITY
        while (quality >= 40) {
            val out = ByteArrayOutputStream(256 * 1024)
            bitmap.compress(Bitmap.CompressFormat.JPEG, quality, out)
            val bytes = out.toByteArray()
            if (bytes.size <= MAX_IMAGE_BYTES) return bytes
            quality -= 10
        }
        throw PickException("too_large", "image stays above 2 MiB even at low JPEG quality")
    }

    private fun displayName(context: Context, uri: Uri): String {
        var name: String? = null
        try {
            context.contentResolver.query(uri, arrayOf(OpenableColumns.DISPLAY_NAME), null, null, null)
                ?.use { cursor ->
                    val index = cursor.getColumnIndex(OpenableColumns.DISPLAY_NAME)
                    if (index >= 0 && cursor.moveToFirst()) name = cursor.getString(index)
                }
        } catch (e: Exception) {
            LogStore.warn("image.name_query_failed")
        }
        val fallback = uri.lastPathSegment?.substringAfterLast('/').orEmpty()
        val result = (name ?: fallback).ifBlank { "image.jpg" }
        return if (result.length > 120) result.take(120) else result
    }

    private const val MAX_SOURCE_PIXELS = 12_000L * 12_000L
}
