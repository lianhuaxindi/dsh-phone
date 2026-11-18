package com.dsh.phone.scan

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.fail
import org.junit.Test
import java.nio.ByteBuffer

/** Y 平面 → 灰度数组的纯 JVM 测试（D-036）。 */
class YuvLumaTest {

    @Test
    fun copiesWhenStridesMatch() {
        val width = 8
        val height = 4
        val plane = ByteArray(width * height) { it.toByte() }
        val gray = YuvLuma.toGray(width, height, width, 1, ByteBuffer.wrap(plane))
        assertArrayEquals(plane, gray)
    }

    @Test
    fun dropsRowPadding() {
        val width = 5
        val height = 3
        val rowStride = 8
        val plane = ByteArray(rowStride * height) { 0x7F.toByte() }
        for (y in 0 until height) {
            for (x in 0 until width) {
                plane[y * rowStride + x] = (y * width + x).toByte()
            }
        }
        val gray = YuvLuma.toGray(width, height, rowStride, 1, ByteBuffer.wrap(plane))
        assertEquals(width * height, gray.size)
        for (y in 0 until height) {
            for (x in 0 until width) {
                assertEquals("y=$y x=$x", (y * width + x).toByte(), gray[y * width + x])
            }
        }
    }

    @Test
    fun honorsPixelStride() {
        val width = 4
        val height = 2
        val pixelStride = 2
        val rowStride = width * pixelStride
        val plane = ByteArray(rowStride * height) { 0 }
        for (y in 0 until height) {
            for (x in 0 until width) {
                plane[y * rowStride + x * pixelStride] = (100 + y * width + x).toByte()
            }
        }
        val gray = YuvLuma.toGray(width, height, rowStride, pixelStride, ByteBuffer.wrap(plane))
        for (y in 0 until height) {
            for (x in 0 until width) {
                assertEquals((100 + y * width + x).toByte(), gray[y * width + x])
            }
        }
    }

    @Test
    fun rejectsInvalidArguments() {
        try {
            YuvLuma.toGray(0, 4, 4, 1, ByteBuffer.allocate(16))
            fail("width=0 should throw")
        } catch (expected: IllegalArgumentException) {
            // ok
        }
        try {
            YuvLuma.toGray(4, 4, 0, 1, ByteBuffer.allocate(16))
            fail("rowStride=0 should throw")
        } catch (expected: IllegalArgumentException) {
            // ok
        }
    }
}
