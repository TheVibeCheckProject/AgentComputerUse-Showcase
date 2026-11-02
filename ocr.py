"""The OCR pass: Windows' built-in OCR over a screenshot (about 0.6 s for a 1920x1080 screen).

Returns lines of text with their boxes in screen coordinates, so text the accessibility list misses (images,
pages drawn as one picture, custom-drawn apps) can still be read and clicked.
"""
import asyncio
from dataclasses import dataclass
from typing import List, Optional, Tuple

from PIL import Image

Rect = Tuple[int, int, int, int]      # left, top, right, bottom (screen pixels)


@dataclass
class OcrLine:
    text: str
    rect: Rect


def grab(rect: Optional[Rect] = None) -> Tuple[Image.Image, Tuple[int, int]]:
    """A screenshot of one area (or the main screen) and its top-left corner on screen."""
    import mss
    with mss.MSS() as s:
        if rect is None:
            m = s.monitors[1]
            area = {"left": m["left"], "top": m["top"], "width": m["width"], "height": m["height"]}
        else:
            area = {"left": rect[0], "top": rect[1], "width": max(1, rect[2] - rect[0]),
                    "height": max(1, rect[3] - rect[1])}
        shot = s.grab(area)
    return Image.frombytes("RGB", shot.size, shot.rgb), (area["left"], area["top"])


def read_image(img: Image.Image, origin: Tuple[int, int] = (0, 0)) -> List[OcrLine]:
    from winrt.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.storage.streams import DataWriter

    rgba = img.convert("RGBA")
    r, g, b, a = rgba.split()
    writer = DataWriter()
    writer.write_bytes(Image.merge("RGBA", (b, g, r, a)).tobytes())          # Windows wants BGRA
    bitmap = SoftwareBitmap.create_copy_from_buffer(writer.detach_buffer(), BitmapPixelFormat.BGRA8,
                                                    img.width, img.height)

    async def recognize():
        return await OcrEngine.try_create_from_user_profile_languages().recognize_async(bitmap)

    result = asyncio.run(recognize())
    ox, oy = origin
    lines = []
    for line in result.lines:
        boxes = [w.bounding_rect for w in line.words]
        if not boxes:
            continue
        left = min(bx.x for bx in boxes)
        top = min(bx.y for bx in boxes)
        right = max(bx.x + bx.width for bx in boxes)
        bottom = max(bx.y + bx.height for bx in boxes)
        lines.append(OcrLine(line.text, (int(left + ox), int(top + oy), int(right + ox), int(bottom + oy))))
    return lines


def read(rect: Optional[Rect] = None) -> List[OcrLine]:
    """OCR of one screen area (or the whole main screen)."""
    img, origin = grab(rect)
    return read_image(img, origin)
