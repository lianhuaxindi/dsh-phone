package com.dsh.phone.media

/**
 * Pure geometric helpers for the v1.1 image pipeline (D-029).
 *
 * Images picked from the camera/gallery are re-encoded natively before they
 * cross the WebView: the long edge is capped at [MAX_LONG_EDGE] px and the
 * result is JPEG at quality [JPEG_QUALITY]. Keeping the arithmetic in a plain
 * Kotlin object makes the policy testable on the JVM (no Android classes).
 */
object ImageScaler {

    const val MAX_LONG_EDGE = 1600
    const val JPEG_QUALITY = 80

    /** Target output size in pixels, keeping aspect ratio and never upscaling. */
    fun targetSize(width: Int, height: Int, maxLongEdge: Int = MAX_LONG_EDGE): IntArray {
        require(width > 0 && height > 0) { "image dimensions must be positive" }
        require(maxLongEdge > 0) { "maxLongEdge must be positive" }
        val longEdge = if (width >= height) width else height
        if (longEdge <= maxLongEdge) return intArrayOf(width, height)
        val ratio = maxLongEdge.toDouble() / longEdge.toDouble()
        val targetWidth = Math.max(1, Math.round(width * ratio).toInt())
        val targetHeight = Math.max(1, Math.round(height * ratio).toInt())
        return intArrayOf(targetWidth, targetHeight)
    }

    /**
     * Power-of-two [android.graphics.BitmapFactory.Options.inSampleSize] that
     * decodes near (not below) the target size. Sampling by 2 keeps memory sane
     * for modern 12-50 MP phone cameras before the exact scale step.
     */
    fun inSampleSize(width: Int, height: Int, maxLongEdge: Int = MAX_LONG_EDGE): Int {
        require(width > 0 && height > 0) { "image dimensions must be positive" }
        require(maxLongEdge > 0) { "maxLongEdge must be positive" }
        val longEdge = if (width >= height) width else height
        if (longEdge <= maxLongEdge) return 1
        var sample = 1
        while (longEdge / (sample * 2) >= maxLongEdge) {
            sample *= 2
        }
        return sample
    }
}
