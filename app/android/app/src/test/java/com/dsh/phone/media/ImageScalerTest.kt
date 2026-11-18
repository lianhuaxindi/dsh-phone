package com.dsh.phone.media

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertThrows
import org.junit.Test

/**
 * D-029: v1.1 image pipeline geometry. Long edge is capped at 1600 px, never
 * upscaled, and decoded with a power-of-two inSampleSize first.
 */
class ImageScalerTest {

    @Test
    fun smallImagesAreNotUpscaled() {
        assertArrayEquals(intArrayOf(800, 600), ImageScaler.targetSize(800, 600))
        assertArrayEquals(intArrayOf(1600, 1200), ImageScaler.targetSize(1600, 1200))
        assertEquals(1, ImageScaler.inSampleSize(800, 600))
    }

    @Test
    fun landscapeImagesAreScaledTo1600Wide() {
        assertArrayEquals(intArrayOf(1600, 1200), ImageScaler.targetSize(4000, 3000))
        assertArrayEquals(intArrayOf(1600, 900), ImageScaler.targetSize(1920, 1080))
    }

    @Test
    fun portraitImagesAreScaledTo1600Tall() {
        assertArrayEquals(intArrayOf(1200, 1600), ImageScaler.targetSize(3000, 4000))
    }

    @Test
    fun roundingNeverProducesZeroPixels() {
        assertArrayEquals(intArrayOf(1, 1600), ImageScaler.targetSize(1, 5000))
        // Exact longest edge at the limit stays untouched.
        assertArrayEquals(intArrayOf(1600, 1), ImageScaler.targetSize(1600, 1))
    }

    @Test
    fun inSampleSizeIsAPowerOfTwoNearTheTarget() {
        assertEquals(2, ImageScaler.inSampleSize(4000, 3000))
        assertEquals(4, ImageScaler.inSampleSize(8000, 6000))
        assertEquals(1, ImageScaler.inSampleSize(1600, 900))
        assertEquals(2, ImageScaler.inSampleSize(3200, 2400))
    }

    @Test
    fun invalidDimensionsAreRejected() {
        assertThrows(IllegalArgumentException::class.java) { ImageScaler.targetSize(0, 100) }
        assertThrows(IllegalArgumentException::class.java) { ImageScaler.inSampleSize(100, -1) }
        assertThrows(IllegalArgumentException::class.java) { ImageScaler.targetSize(100, 100, 0) }
    }

    @Test
    fun defaultPolicyMatchesDecisions() {
        assertEquals(1600, ImageScaler.MAX_LONG_EDGE)
        assertEquals(80, ImageScaler.JPEG_QUALITY)
    }
}
