from io import BytesIO

from PIL import Image, ImageOps, UnidentifiedImageError

from edu_domain.contracts import MAX_IMAGE_PIXELS, MAX_PAGE_BYTES, MAX_UPLOAD_BYTES


class ImageError(ValueError):
    pass


def normalize_page(data: bytes) -> bytes:
    if not data or len(data) > MAX_UPLOAD_BYTES:
        raise ImageError("每张图片必须小于 8 MB。")
    try:
        with Image.open(BytesIO(data)) as image:
            if image.format not in ("JPEG", "PNG") or image.n_frames != 1:
                raise ImageError("请上传单帧 JPG 或 PNG 图片。")
            if min(image.size) < 120 or image.width * image.height > MAX_IMAGE_PIXELS:
                raise ImageError("图片尺寸不合适，请保存清晰的题图后重试。")
            frame = ImageOps.exif_transpose(image)
            frame.thumbnail((1800, 1800), Image.Resampling.LANCZOS)
            rgba = frame.convert("RGBA")
            clean = Image.new("RGB", rgba.size, "white")
            clean.paste(rgba, mask=rgba.getchannel("A"))
            for quality in (89, 78, 67):
                output = BytesIO()
                clean.save(output, "JPEG", quality=quality, optimize=True)
                if output.tell() <= MAX_PAGE_BYTES:
                    return output.getvalue()
    except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError) as exc:
        raise ImageError("图片无法读取，请重拍或另存为 JPG 或 PNG。") from exc
    raise ImageError("图片细节过多，请裁掉不相关区域后重新上传。")
