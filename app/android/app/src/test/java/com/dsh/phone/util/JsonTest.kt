package com.dsh.phone.util

import org.junit.Assert.assertEquals
import org.junit.Test

/** JSON escaping and deterministic ordered object building for the wire format. */
class JsonTest {

    @Test
    fun escapeHandlesJsonControlCharacters() {
        assertEquals("a\\\"b", Json.escape("a\"b"))
        assertEquals("a\\\\b", Json.escape("a\\b"))
        assertEquals("a\\nb", Json.escape("a\nb"))
        assertEquals("a\\rb", Json.escape("a\rb"))
        assertEquals("a\\tb", Json.escape("a\tb"))
        assertEquals("a\\bb", Json.escape("a\bb"))
        assertEquals("a\\fb", Json.escape("a\u000Cb"))
        assertEquals("\\u0001", Json.escape("\u0001"))
        assertEquals("\\u001f", Json.escape("\u001f"))
    }

    @Test
    fun escapeLeavesUnicodeAlone() {
        assertEquals("中文", Json.escape("中文"))
        assertEquals("é", Json.escape("é"))
    }

    @Test
    fun quoteWrapsEscapedText() {
        assertEquals("\"hi\"", Json.quote("hi"))
        assertEquals("\"a\\\"b\"", Json.quote("a\"b"))
        assertEquals("\"\"", Json.quote(""))
    }

    @Test
    fun jsonObjPreservesInsertionOrder() {
        val json = JsonObj()
            .put("type", "hello")
            .put("seq", 1)
            .toJsonText()
        assertEquals("{\"type\":\"hello\",\"seq\":1}", json)
        assertEquals(2, JsonObj().put("a", 1).put("b", 2).size())
    }

    @Test
    fun jsonObjNullHandling() {
        val value: String? = null
        val json = JsonObj()
            .put("type", "pair.request")
            .put("deviceId", value)
            .putNull("pubKey")
            .toJsonText()
        assertEquals("{\"type\":\"pair.request\",\"deviceId\":null,\"pubKey\":null}", json)
    }

    @Test
    fun jsonObjSupportsNumbersBooleansNestedAndRaw() {
        val nested = JsonObj().put("a", 1).put("ok", true)
        val json = JsonObj()
            .put("long", 4_294_967_295L)
            .put("int", 42)
            .put("bool", false)
            .putObj("nested", nested)
            .putRaw("array", "[1,2,3]")
            .toJsonText()
        assertEquals(
            "{\"long\":4294967295,\"int\":42,\"bool\":false," +
                "\"nested\":{\"a\":1,\"ok\":true},\"array\":[1,2,3]}",
            json,
        )
    }

    @Test
    fun escapeEmitsLowercaseUnicodeEscapesForControls() {
        assertEquals("\"a\\u0000b\"", Json.quote("a\u0000b"))
        assertEquals("\"\\u000b\"", Json.quote("\u000B"))
    }
}
