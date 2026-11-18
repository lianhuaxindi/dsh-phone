package com.dsh.phone.scan

import com.google.zxing.BarcodeFormat
import com.google.zxing.qrcode.QRCodeWriter
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Test
import java.nio.ByteBuffer

/**
 * 扫码解码核心的纯 JVM 测试（D-036）。用 ZXing 自己生成二维码，再走与真机相同的
 * Y 平面 → 灰度 → 解码路径，覆盖：
 *  - 正常亮底黑码；
 *  - 深色背景（测试反色 fallback）；
 *  - Camera2 常见的 rowStride 对齐填充（宽度不是 16 的倍数时）；
 *  - 二维码偏小、只占画面中央时（测试中心裁剪 fallback）；
 *  - 噪点图必须返回 null。
 */
class QrDecoderTest {

    private val uri =
        "dsphone://pair?v=1&host=10.15.187.127&port=8765" +
            "&fp=e2a832d683743b295276fd85a1b8d7c8fc9179acb9f989de8ec3d4110a2e071b" +
            "&code=384158&sid=srv-e2a832d68374&hosts=10.15.187.127&name=KQ"

    private fun grayOf(size: Int, invert: Boolean = false): ByteArray {
        val matrix = QRCodeWriter().encode(uri, BarcodeFormat.QR_CODE, size, size)
        val gray = ByteArray(size * size)
        for (y in 0 until size) {
            for (x in 0 until size) {
                val dark = matrix.get(x, y)
                gray[y * size + x] = if (dark != invert) 0 else 255.toByte()
            }
        }
        return gray
    }

    @Test
    fun decodesCleanQrCode() {
        val size = 400
        assertEquals(uri, QrDecoder.decodeGray(grayOf(size), size, size))
    }

    @Test
    fun decodesInvertedQrCode() {
        val size = 400
        val inverted = grayOf(size, invert = true)
        assertNotNull("深色背景二维码应通过反色 fallback 识别", QrDecoder.decodeGray(inverted, size, size))
    }

    @Test
    fun decodesQrCodeInsideLargerCanvas() {
        // 900×900 画面，二维码只占中间 300×300（模拟手机离屏幕较远）
        val canvas = 900
        val qr = 300
        val small = grayOf(qr)
        val gray = ByteArray(canvas * canvas) { 255.toByte() }
        val offset = (canvas - qr) / 2
        for (y in 0 until qr) {
            System.arraycopy(small, y * qr, gray, (offset + y) * canvas + offset, qr)
        }
        assertNotNull(QrDecoder.decodeGray(gray, canvas, canvas))
    }

    @Test
    fun decodesFromCamera2PaddedYPlane() {
        // Camera2 的 Y 平面常见 rowStride = 宽度向上对齐后的值（如 400 → 416）
        val width = 400
        val height = 400
        val rowStride = 416
        val matrix = QRCodeWriter().encode(uri, BarcodeFormat.QR_CODE, width, height)
        val plane = ByteArray(rowStride * height) { 0x5A.toByte() } // 填充区放噪声
        for (y in 0 until height) {
            for (x in 0 until width) {
                plane[y * rowStride + x] = if (matrix.get(x, y)) 0 else 255.toByte()
            }
        }
        val gray = YuvLuma.toGray(width, height, rowStride, 1, ByteBuffer.wrap(plane))
        assertEquals(uri, QrDecoder.decodeGray(gray, width, height))
    }

    @Test
    fun returnsNullForNoise() {
        val size = 240
        val noise = ByteArray(size * size) { index ->
            // 确定性的伪随机噪声，避免 flaky
            ((index * 1103515245 + 12345) ushr 16).toByte()
        }
        assertNull(QrDecoder.decodeGray(noise, size, size))
    }

    @Test
    fun cropCenterKeepsMiddleRegion() {
        val width = 400
        val height = 200
        val gray = ByteArray(width * height) { 7.toByte() }
        val cropped = QrDecoder.cropCenter(gray, width, height, 0.5)!!
        assertEquals(200, cropped.second)
        assertEquals(100, cropped.third)
        assertEquals(200 * 100, cropped.first.size)
    }
}
