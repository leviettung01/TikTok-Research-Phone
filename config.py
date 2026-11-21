"""Cấu hình độc lập cho TikTok Research & Phone Tool.

Tool tách biệt hoàn toàn, tự đóng gói sẵn adb.exe + scrcpy-server + ADBKeyboard.apk
ngay trong thư mục này để chạy độc lập không phụ thuộc tool cũ.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

APP_NAME = "TikTok Research & Phone Studio"
APP_VERSION = "2.0.0"

ROOT = Path(__file__).resolve().parent
WORKSPACE_DIR = ROOT.parent  # C:\Users\ADMIN\Desktop\Proxy
LEGACY_BASE_DIR = WORKSPACE_DIR / "Dcom BlueStack"

# Ưu tiên tuyệt đối thư mục tools/ đóng gói sẵn ngay trong folder mới này
_LOCAL_TOOLS = ROOT / "tools"
_FALLBACK_TOOLS = LEGACY_BASE_DIR / "TikTok Position Lab" / "resources" / "tools" / "scrcpy-win64-v4.1"
_DEFAULT_TOOLS = _LOCAL_TOOLS if (_LOCAL_TOOLS / "adb.exe").is_file() else _FALLBACK_TOOLS

TOOLS_DIR = Path(os.environ.get("TIKTOK_RESEARCH_TOOLS", str(_DEFAULT_TOOLS)))
ADB_PATH = Path(os.environ.get("TIKTOK_RESEARCH_ADB", str(TOOLS_DIR / "adb.exe")))
SCRCPY_SERVER = Path(os.environ.get("TIKTOK_RESEARCH_SCRCPY_SERVER", str(TOOLS_DIR / "scrcpy-server")))
SCRCPY_VERSION = os.environ.get("TIKTOK_RESEARCH_SCRCPY_VERSION", "4.1")

_LOCAL_TESS = TOOLS_DIR / "ocr" / "tesseract.exe"
_FALLBACK_TESS = _FALLBACK_TOOLS / "ocr" / "tesseract.exe"
TESSERACT_PATH = Path(
    os.environ.get(
        "TIKTOK_RESEARCH_TESSERACT",
        str(_LOCAL_TESS if _LOCAL_TESS.is_file() else _FALLBACK_TESS),
    )
)
TESSDATA_DIR = TESSERACT_PATH.parent / "tessdata"


def resolve_ffmpeg_path() -> Path | None:
    """Tìm ffmpeg.exe (nếu có) để chuẩn hóa codec video không tương thích."""
    candidates = [
        ROOT / "tools" / "ffmpeg" / "ffmpeg.exe",
        ROOT / "tools" / "ffmpeg.exe",
        LEGACY_BASE_DIR / "tools" / "ffmpeg" / "ffmpeg.exe",
    ]
    for c in candidates:
        if c.is_file():
            return c
    in_path = shutil.which("ffmpeg")
    return Path(in_path) if in_path else None


HOST = "127.0.0.1"
PORT = int(os.environ.get("TIKTOK_RESEARCH_PORT", "9098"))

RUNTIME_DIR = Path(os.environ.get("TIKTOK_RESEARCH_RUNTIME", str(ROOT / "runtime")))
DATA_DIR = RUNTIME_DIR / "data"
WORK_DIR = RUNTIME_DIR / "work"
WEB_DIR = ROOT / "web"

STORE_DEVICES = DATA_DIR / "devices.json"
STORE_SETTINGS = DATA_DIR / "settings.json"
STORE_RESEARCH = DATA_DIR / "tiktok-product-research.json"
STORE_WIRELESS = DATA_DIR / "wireless-hosts.json"
PRODUCT_IMAGES_DIR = DATA_DIR / "product_images"
HOT_PRODUCTS_DIR = DATA_DIR / "hot_products"

# Đường dẫn tham chiếu tùy chọn nếu người dùng muốn nhập kho SP từ bản cũ hoặc thử Cách 2 Browser
LEGACY_RESEARCH_JSON = LEGACY_BASE_DIR / "tiktok_web" / "runtime" / "data" / "tiktok-product-research.json"
LEGACY_PRODUCT_IMAGES_DIR = LEGACY_BASE_DIR / "tiktok_web" / "runtime" / "data" / "product_images"

_LEGACY_SHOP_PROFILE = LEGACY_BASE_DIR / "TiktokShop" / "profiles" / "tiktok_chrome_profile"
_DEFAULT_CHROME_PROFILE = _LEGACY_SHOP_PROFILE if _LEGACY_SHOP_PROFILE.exists() else (RUNTIME_DIR / "chrome_profile")
TIKTOK_SHOP_PROFILE_DIR = Path(
    os.environ.get("TIKTOK_SHOP_PROFILE_DIR", str(_DEFAULT_CHROME_PROFILE))
)
CHROME_DEBUG_PORT = int(os.environ.get("TIKTOK_CHROME_DEBUG_PORT", "9222"))

TIKTOK_PACKAGES = ("com.zhiliaoapp.musically", "com.ss.android.ugc.trill")
TIKTOK_MAIN_ACTIVITY_CLASS = "com.ss.android.ugc.aweme.main.MainActivity"

VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"}

BLUESTACKS_CONF_PATHS = (
    r"C:\ProgramData\BlueStacks_nxt\bluestacks.conf",
    r"C:\ProgramData\BlueStacks\bluestacks.conf",
)
HD_PLAYER_PATHS = (
    r"C:\Program Files\BlueStacks_nxt\HD-Player.exe",
    r"C:\Program Files (x86)\BlueStacks_nxt\HD-Player.exe",
)


def ensure_runtime_dirs() -> None:
    for directory in (DATA_DIR, WORK_DIR, PRODUCT_IMAGES_DIR, HOT_PRODUCTS_DIR):
        directory.mkdir(parents=True, exist_ok=True)
