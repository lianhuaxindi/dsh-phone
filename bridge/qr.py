"""终端二维码渲染（配对用）。

优先使用 ``qrcode`` 库；缺失时降级为纯文本 URI + 警告，不崩溃。
终端渲染用半块字符（``▀▄█``）把两行模块压成一行；在 TTY 上额外用
ANSI 前景/背景色保证深色主题下也有正确对比度（黑模块/白底）。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, TextIO

# 加载优先级：先使用环境已安装的 qrcode，缺失时才回退到仓库内 vendored 副本
# （bridge/vendor/qrcode，来自 PyPI wheel qrcode-8.2-py3-none-any，见
# bridge/vendor/README.md）。vendored 副本只在本会话沙箱无法 pip 安装时兜底。
_VENDOR_DIR = Path(__file__).resolve().parent / "vendor"

try:  # pragma: no cover - 依赖环境
    import qrcode  # type: ignore[import-not-found]
    from qrcode.constants import ERROR_CORRECT_M  # type: ignore[import-not-found]

    QRCODE_AVAILABLE = True
except Exception:  # pragma: no cover - 缺依赖时尝试 vendor
    if _VENDOR_DIR.is_dir() and str(_VENDOR_DIR) not in sys.path:
        sys.path.insert(0, str(_VENDOR_DIR))
    try:
        import qrcode  # type: ignore[import-not-found,no-redef]
        from qrcode.constants import ERROR_CORRECT_M  # type: ignore[import-not-found,no-redef]

        QRCODE_AVAILABLE = True
    except Exception:
        qrcode = None  # type: ignore[assignment]
        ERROR_CORRECT_M = 13
        QRCODE_AVAILABLE = False


class QrError(RuntimeError):
    """二维码生成失败。"""


# 半块字符渲染：一个字符编码上下两个模块
_BOTH = "█"
_TOP = "▀"
_BOTTOM = "▄"
_NONE = " "
_BLOCK_FOR = {
    (True, True): _BOTH,
    (True, False): _TOP,
    (False, True): _BOTTOM,
    (False, False): _NONE,
}

# ANSI：上模块用前景色，下模块用背景色；黑=30/40，白=97/107
_FG_DARK = "\x1b[30m"
_FG_LIGHT = "\x1b[97m"
_BG_DARK = "\x1b[40m"
_BG_LIGHT = "\x1b[107m"
_RESET = "\x1b[0m"


def is_available() -> bool:
    """``qrcode`` 依赖是否可用。"""
    return QRCODE_AVAILABLE


def qr_matrix(data: str, *, border: int = 2) -> list[list[bool]]:
    """返回二维码模块矩阵（``True`` = 深色模块）。"""
    if not QRCODE_AVAILABLE:
        raise QrError("qrcode 库未安装（pip install qrcode）")
    if not data:
        raise QrError("二维码内容不能为空")
    try:
        qr = qrcode.QRCode(
            version=None,
            error_correction=ERROR_CORRECT_M,
            box_size=1,
            border=border,
        )
        qr.add_data(data)
        qr.make(fit=True)
        return [[bool(cell) for cell in row] for row in qr.get_matrix()]
    except Exception as exc:  # pragma: no cover - qrcode 内部错误
        raise QrError(f"二维码生成失败: {exc}") from exc


def render_half_blocks(matrix: list[list[bool]], *, invert: bool = False) -> str:
    """把模块矩阵渲染成半块字符文本（每字符 2 行模块）。"""
    if not matrix:
        return ""
    height = len(matrix)
    width = len(matrix[0])
    lines: list[str] = []
    for y in range(0, height, 2):
        top_row = matrix[y]
        bottom_row = matrix[y + 1] if y + 1 < height else [False] * width
        chars: list[str] = []
        for x in range(width):
            top = top_row[x]
            bottom = bottom_row[x]
            if invert:
                top, bottom = not top, not bottom
            chars.append(_BLOCK_FOR[(top, bottom)])
        lines.append("".join(chars))
    return "\n".join(lines)


def render_ansi(matrix: list[list[bool]]) -> str:
    """带 ANSI 黑/白配色的半块字符渲染（深色终端也能扫）。"""
    if not matrix:
        return ""
    height = len(matrix)
    width = len(matrix[0])
    lines: list[str] = []
    for y in range(0, height, 2):
        top_row = matrix[y]
        bottom_row = matrix[y + 1] if y + 1 < height else [False] * width
        chars: list[str] = []
        prev: tuple[bool, bool] | None = None
        for x in range(width):
            top = top_row[x]
            bottom = bottom_row[x]
            if (top, bottom) != prev:
                chars.append((_FG_DARK if top else _FG_LIGHT) + (_BG_DARK if bottom else _BG_LIGHT))
                prev = (top, bottom)
            chars.append(_BOTH)
        chars.append(_RESET)
        lines.append("".join(chars))
    return "\n".join(lines)


def render_ascii(matrix: list[list[bool]], *, invert: bool = False) -> str:
    """纯 ASCII 渲染（每模块 2 个字符，兼容无法输出 ``▀`` 的终端编码）。"""
    if not matrix:
        return ""
    lines: list[str] = []
    for row in matrix:
        chars: list[str] = []
        for cell in row:
            dark = not cell if invert else cell
            chars.append("##" if dark else "  ")
        lines.append("".join(chars))
    return "\n".join(lines)


def qr_text(data: str, *, invert: bool = False, ascii_only: bool = False) -> str:
    """渲染为纯文本二维码（供日志/测试/重定向输出使用）。"""
    matrix = qr_matrix(data)
    if ascii_only:
        return render_ascii(matrix, invert=invert)
    return render_half_blocks(matrix, invert=invert)


def _can_encode(stream: TextIO, text: str) -> bool:
    """判断输出流编码能否表示 ``text``（未知编码时假定可以）。"""
    encoding = getattr(stream, "encoding", None)
    if not encoding:
        return True
    try:
        text.encode(encoding)
        return True
    except (UnicodeEncodeError, LookupError):
        return False


def _stream_is_tty(stream: TextIO) -> bool:
    try:
        return bool(stream.isatty())
    except Exception:  # pragma: no cover
        return False


def print_qr(
    data: str,
    *,
    file: TextIO | None = None,
    ansi: bool | None = None,
    invert: bool = False,
    indent: str = "",
) -> bool:
    """打印二维码到终端。返回是否真正打印了二维码图形。

    ``ansi=None`` 时按输出流是否为 TTY 自动决定是否上色。
    输出流编码不支持半块字符（如 Windows GBK 控制台）时自动降级为 ASCII 渲染。
    """
    stream = file or sys.stdout
    if not QRCODE_AVAILABLE:
        try:
            print("[qr] 未安装 qrcode 库，无法打印二维码；请手动输入配对 URI：", file=stream)
            print(f"[qr] {data}", file=stream)
            print("[qr] 安装：pip install qrcode", file=stream)
        except UnicodeEncodeError:  # pragma: no cover - 极端终端编码
            print(f"[qr] qrcode is not installed; pairing URI: {data}", file=stream)
        return False
    matrix = qr_matrix(data)
    use_ansi = _stream_is_tty(stream) if ansi is None else ansi
    if use_ansi and not _can_encode(stream, "▀▄█"):
        use_ansi = False
    if use_ansi:
        text = render_ansi(matrix)
    elif _can_encode(stream, "▀▄█"):
        text = render_half_blocks(matrix, invert=invert)
    else:
        text = render_ascii(matrix, invert=invert)
    if indent:
        text = "\n".join(indent + line for line in text.splitlines())
    try:
        print(text, file=stream)
    except UnicodeEncodeError:  # pragma: no cover - 防御性兜底
        print(render_ascii(matrix, invert=invert), file=stream)
    return True


def save_png(
    data: str,
    path: str,
    *,
    box_size: int = 8,
    border: int = 2,
) -> str | None:
    """可选：把二维码保存为 PNG（需要 qrcode[pil] 与 Pillow）。"""
    if not QRCODE_AVAILABLE:
        return None
    try:  # pragma: no cover - 可选功能
        from qrcode.image.pil import PilImage  # type: ignore[import-not-found]

        qr = qrcode.QRCode(
            error_correction=ERROR_CORRECT_M, box_size=box_size, border=border
        )
        qr.add_data(data)
        qr.make(fit=True)
        image: Any = qr.make_image(image_factory=PilImage)
        image.save(path)
        return path
    except Exception:  # pragma: no cover - PIL 缺失或保存失败
        return None


def save_qr_png(
    data: str,
    path: str | Path,
    *,
    min_size: int = 512,
    border: int = 4,
    max_box_size: int = 160,
) -> str | None:
    """把二维码保存为 PNG，并保证每边 ≥ ``min_size`` 像素（PROTOCOL §5.1）。

    返回 PNG 的绝对路径；``qrcode``/Pillow 缺失或保存失败时返回 ``None``。
    """
    if not QRCODE_AVAILABLE:
        return None
    try:  # pragma: no cover - 可选功能
        from qrcode.image.pil import PilImage  # type: ignore[import-not-found]

        target = Path(path).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        probe = qrcode.QRCode(
            error_correction=ERROR_CORRECT_M, box_size=1, border=border
        )
        probe.add_data(data)
        probe.make(fit=True)
        modules = int(probe.modules_count) + 2 * border
        box_size = max(12, -(-min_size // max(modules, 1)))
        box_size = min(box_size, max_box_size)
        qr = qrcode.QRCode(
            error_correction=ERROR_CORRECT_M, box_size=box_size, border=border
        )
        qr.add_data(data)
        qr.make(fit=True)
        image: Any = qr.make_image(image_factory=PilImage)
        image.save(target)
        return str(target.resolve())
    except Exception:  # pragma: no cover - PIL 缺失或保存失败
        return None
