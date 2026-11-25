from io import BytesIO
from threading import Lock

from PIL import Image, ImageOps, UnidentifiedImageError

from edu_domain.contracts import (
    MAX_IMAGE_PIXELS,
    MAX_PAGE_BYTES,
    MAX_SOURCE_IMAGE_PIXELS,
    MAX_UPLOAD_BYTES,
)

_PAGE_SIZE = (1800, 1800)
_OVERSIZED_IMAGE_LOCK = Lock()


class ImageError(ValueError):
    pass


def normalize_page(data: bytes) -> bytes:
    if not data or len(data) > MAX_UPLOAD_BYTES:
        raise ImageError("每张图片必须小于 8 MB。")
    try:
        with Image.open(BytesIO(data)) as image:
            if image.format not in ("JPEG", "PNG") or getattr(image, "n_frames", 1) != 1:
                raise ImageError("请上传单帧 JPG 或 PNG 图片。")
            if min(image.size) < 120:
                raise ImageError(
                    f"图片尺寸过小（{image.width}×{image.height}），宽高均需至少 120 像素，请重拍清晰题图。"
                )
            if image.width * image.height > MAX_SOURCE_IMAGE_PIXELS:
                raise ImageError(
                    f"图片分辨率过高（{image.width}×{image.height}），"
                    "超过 6400 万像素安全上限，请先缩小后重试。"
                )
            if image.width * image.height > MAX_IMAGE_PIXELS:
                # Bound concurrent large decodes, and shrink before EXIF makes a full-size copy.
                with _OVERSIZED_IMAGE_LOCK:
                    if image.format == "JPEG":
                        image.draft("RGB", _PAGE_SIZE)
                    image.thumbnail(_PAGE_SIZE, Image.Resampling.LANCZOS)
            frame = ImageOps.exif_transpose(image)
            frame.thumbnail(_PAGE_SIZE, Image.Resampling.LANCZOS)
            rgba = frame.convert("RGBA")
            clean = Image.new("RGB", rgba.size, "white")
            clean.paste(rgba, mask=rgba.getchannel("A"))
            for quality in (89, 78, 67):
                output = BytesIO()
                clean.save(output, "JPEG", quality=quality, optimize=True)
                if output.tell() <= MAX_PAGE_BYTES:
                    return output.getvalue()
    except Image.DecompressionBombError as exc:
        raise ImageError("图片分辨率超过安全上限，请先缩小后重试。") from exc
    except (UnidentifiedImageError, OSError, SyntaxError) as exc:
        raise ImageError("图片无法读取，请重拍或另存为 JPG 或 PNG。") from exc
    raise ImageError("图片细节过多，请裁掉不相关区域后重新上传。")
