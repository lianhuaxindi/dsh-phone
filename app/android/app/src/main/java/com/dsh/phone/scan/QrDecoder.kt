package com.dsh.phone.scan

import com.google.zxing.BarcodeFormat
import com.google.zxing.BinaryBitmap
import com.google.zxing.DecodeHintType
import com.google.zxing.LuminanceSource
import com.google.zxing.MultiFormatReader
import com.google.zxing.NotFoundException
import com.google.zxing.common.GlobalHistogramBinarizer
import com.google.zxing.common.HybridBinarizer

/**
 * 纯 JVM 的二维码解码核心（D-036）——不依赖任何 Android API，方便单测。
 *
 * 依次尝试（每次都用新的 [MultiFormatReader]，避免 `reset()` 遗留状态）：
 *  1. 整帧 + HybridBinarizer（通常最快最准）；
 *  2. 整帧 + GlobalHistogramBinarizer（屏幕反光/低对比时更稳）；
 *  3. 整帧反色（部分深色背景二维码）；
 *  4. 中心 62% 裁剪后再走 1/2（手机离屏幕较远、二维码只占画面中间时更容易命中）。
 *
 * 不做 90° 旋转重试：QR 本身旋转不变，帧方向不影响检测。
 */
object QrDecoder {

    private val HINTS: Map<DecodeHintType, Any> = mapOf(
        DecodeHintType.POSSIBLE_FORMATS to listOf(BarcodeFormat.QR_CODE),
        DecodeHintType.TRY_HARDER to true,
        DecodeHintType.CHARACTER_SET to "UTF-8",
    )

    /** 把灰度数组包成 ZXing 的 [LuminanceSource]。 */
    class GraySource(private val data: ByteArray, w: Int, h: Int) : LuminanceSource(w, h) {
        override fun getRow(y: Int, row: ByteArray?): ByteArray {
            val w = width
            val out = if (row == null || row.size < w) ByteArray(w) else row
            System.arraycopy(data, y * w, out, 0, w)
            return out
        }

        override fun getMatrix(): ByteArray = data
    }

    /** @return 识别到的文本；识别不到返回 null（不抛异常）。 */
    fun decodeGray(data: ByteArray, width: Int, height: Int): String? {
        if (width <= 0 || height <= 0 || data.size < width * height) return null
        decodeWithBinarizers(GraySource(data, width, height))?.let { return it }
        cropCenter(data, width, height)?.let { (cropped, cw, ch) ->
            decodeWithBinarizers(GraySource(cropped, cw, ch))?.let { return it }
        }
        return null
    }

    private fun decodeWithBinarizers(source: LuminanceSource): String? {
        tryDecode(BinaryBitmap(HybridBinarizer(source)))?.let { return it }
        tryDecode(BinaryBitmap(GlobalHistogramBinarizer(source)))?.let { return it }
        tryDecode(BinaryBitmap(HybridBinarizer(source.invert())))?.let { return it }
        return null
    }

    private fun tryDecode(bitmap: BinaryBitmap): String? = try {
        val reader = MultiFormatReader()
        reader.setHints(HINTS)
        val result = reader.decode(bitmap)
        result.text?.takeIf { it.isNotEmpty() }
    } catch (e: NotFoundException) {
        null
    } catch (e: Exception) {
        null
    }

    /** 中心裁剪（默认 62%）；尺寸太小时返回 null。 */
    fun cropCenter(data: ByteArray, width: Int, height: Int, fraction: Double = 0.62): Triple<ByteArray, Int, Int>? {
        val cw = (width * fraction).toInt()
        val ch = (height * fraction).toInt()
        if (cw < 32 || ch < 32) return null
        val x0 = (width - cw) / 2
        val y0 = (height - ch) / 2
        val out = ByteArray(cw * ch)
        for (y in 0 until ch) {
            System.arraycopy(data, (y0 + y) * width + x0, out, y * cw, cw)
        }
        return Triple(out, cw, ch)
    }
}
