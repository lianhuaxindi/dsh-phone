package com.dsh.phone.scan

import java.nio.ByteBuffer

/**
 * Camera2 `ImageFormat.YUV_420_888` 的 Y 平面 → 灰度数组（纯 JVM，可直接单测）。
 *
 * Android 的 Y 平面不保证 `rowStride == width`（对齐填充很常见），`pixelStride` 理论
 * 上恒为 1，但两份情况都按 stride 读，避免出现「画面是对的但解码器看到斜图」的坑。
 */
object YuvLuma {

    /**
     * @param buffer `image.planes[0].buffer`（用绝对下标读，不改 position）
     * @return 长度 `width * height` 的灰度数组（row-major）
     */
    fun toGray(width: Int, height: Int, rowStride: Int, pixelStride: Int, buffer: ByteBuffer): ByteArray {
        require(width > 0 && height > 0) { "invalid size ${width}x$height" }
        require(rowStride > 0 && pixelStride > 0) { "invalid stride row=$rowStride pixel=$pixelStride" }
        val out = ByteArray(width * height)
        if (pixelStride == 1 && rowStride == width) {
            val dup = buffer.duplicate()
            dup.position(0)
            val n = minOf(dup.remaining(), out.size)
            dup.get(out, 0, n)
            return out
        }
        val limit = buffer.limit()
        for (y in 0 until height) {
            val rowStart = y * rowStride
            if (rowStart >= limit) break
            val dst = y * width
            for (x in 0 until width) {
                val index = rowStart + x * pixelStride
                if (index >= limit) break
                out[dst + x] = buffer.get(index)
            }
        }
        return out
    }
}
