"""OCR dự phòng bằng tesseract đi kèm Position Lab (chỉ có eng.traineddata).

Dùng để đọc @username khi UI dump không trả về text (một số bản TikTok vẽ bằng canvas).
"""
from __future__ import annotations

import io
import subprocess

import config
from core.adb import CREATE_NO_WINDOW


def available() -> bool:
    return config.TESSERACT_PATH.is_file()


def words(png_bytes: bytes, region=None, psm: int = 11, timeout: float = 20) -> list[dict]:
    """Trả về [{text, conf, left, top, width, height}] theo toạ độ ảnh gốc."""
    if not available() or not png_bytes:
        return []
    from PIL import Image

    image = Image.open(io.BytesIO(png_bytes)).convert("L")
    offset_x = offset_y = 0
    if region:
        w, h = image.size
        x0, y0, x1, y1 = region
        box = (int(x0 * w), int(y0 * h), int(x1 * w), int(y1 * h))
        image = image.crop(box)
        offset_x, offset_y = box[0], box[1]
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    cmd = [str(config.TESSERACT_PATH), "stdin", "stdout", "--psm", str(psm), "--tessdata-dir",
           str(config.TESSDATA_DIR), "-l", "eng", "tsv"]
    try:
        proc = subprocess.run(cmd, input=buffer.getvalue(), capture_output=True, timeout=timeout,
                              creationflags=CREATE_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        return []
    result = []
    for line in proc.stdout.decode("utf-8", errors="replace").splitlines()[1:]:
        parts = line.split("\t")
        if len(parts) < 12 or not parts[11].strip():
            continue
        try:
            conf = float(parts[10])
            left, top, width, height = (int(parts[i]) for i in (6, 7, 8, 9))
        except ValueError:
            continue
        result.append({"text": parts[11].strip(), "conf": conf, "left": left + offset_x, "top": top + offset_y,
                       "width": width, "height": height})
    return result
