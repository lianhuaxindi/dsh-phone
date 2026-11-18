package com.dsh.phone.storage

import android.content.ContentProvider
import android.content.ContentValues
import android.content.Context
import android.database.Cursor
import android.net.Uri
import android.os.ParcelFileDescriptor
import java.io.File
import java.io.FileNotFoundException

/**
 * Minimal FileProvider replacement for camera captures (D-029).
 *
 * The project deliberately carries no AndroidX dependency, so instead of
 * `androidx.core.content.FileProvider` this provider exposes exactly one
 * directory (`cacheDir/images`) and only plain file names. The camera app
 * receives a temporary `content://com.dsh.phone.fileprovider/<name>` URI with
 * write permission granted by `MediaStore.ACTION_IMAGE_CAPTURE`; the captured
 * JPEG is then read back, scaled and re-encoded by [com.dsh.phone.media.ImagePicker].
 *
 * Security: exported=false in the manifest, path traversal rejected, and only
 * JPEG captures are produced here.
 */
class ImageFileProvider : ContentProvider() {

    override fun onCreate(): Boolean = true

    override fun getType(uri: Uri): String = MIME_JPEG

    override fun openFile(uri: Uri, mode: String): ParcelFileDescriptor? {
        val ctx = context ?: throw FileNotFoundException("no context")
        val name = uri.lastPathSegment ?: throw FileNotFoundException("missing file name")
        if (!isSafeName(name)) throw FileNotFoundException("invalid file name")
        val file = File(imagesDir(ctx), name)
        val flags = when {
            mode == "r" -> ParcelFileDescriptor.MODE_READ_ONLY
            mode.startsWith("w") || mode.startsWith("rw") || mode.startsWith("rwt") ->
                ParcelFileDescriptor.MODE_READ_WRITE or
                    ParcelFileDescriptor.MODE_CREATE or
                    ParcelFileDescriptor.MODE_TRUNCATE
            else -> throw FileNotFoundException("unsupported mode: $mode")
        }
        return ParcelFileDescriptor.open(file, flags)
    }

    override fun query(
        uri: Uri,
        projection: Array<out String>?,
        selection: String?,
        selectionArgs: Array<out String>?,
        sortOrder: String?,
    ): Cursor? = null

    override fun insert(uri: Uri, values: ContentValues?): Uri? = null

    override fun update(
        uri: Uri,
        values: ContentValues?,
        selection: String?,
        selectionArgs: Array<out String>?,
    ): Int = 0

    override fun delete(uri: Uri, selection: String?, selectionArgs: Array<out String>?): Int = 0

    companion object {
        const val AUTHORITY = "com.dsh.phone.fileprovider"
        const val MIME_JPEG = "image/jpeg"
        private const val IMAGES_DIR = "images"
        private const val MAX_AGE_MS = 24L * 60L * 60L * 1000L

        /** Directory used for temporary camera captures. */
        fun imagesDir(context: Context): File {
            val dir = File(context.applicationContext.cacheDir, IMAGES_DIR)
            if (!dir.exists()) dir.mkdirs()
            return dir
        }

        /** Fresh capture target file; caller builds the URI with [uriFor]. */
        fun newCaptureFile(context: Context): File {
            val dir = imagesDir(context)
            cleanupOldFiles(context)
            return File(dir, "capture-" + System.currentTimeMillis() + ".jpg")
        }

        fun uriFor(file: File): Uri = Uri.parse("content://$AUTHORITY/${file.name}")

        /** Best-effort cleanup of stale capture files (never throws). */
        fun cleanupOldFiles(context: Context) {
            try {
                val dir = imagesDir(context)
                val cutoff = System.currentTimeMillis() - MAX_AGE_MS
                dir.listFiles()?.forEach { file ->
                    if (file.isFile && file.lastModified() < cutoff) file.delete()
                }
            } catch (ignored: Exception) {
                // Cleanup is best effort.
            }
        }

        private fun isSafeName(name: String): Boolean {
            if (name.isEmpty() || name.length > 128) return false
            if (name.contains('/') || name.contains('\\') || name.contains("..")) return false
            return name.all { it.isLetterOrDigit() || it == '.' || it == '_' || it == '-' }
        }
    }
}
