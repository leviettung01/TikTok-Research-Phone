"""TikTok Research & Phone Studio — Backend độc lập (HTTP + WebSocket localhost).

Tối ưu gọn nhẹ chỉ gồm 2 chức năng cốt lõi:
1. Kết nối & điều khiển điện thoại thật / giả lập (USB, Wi-Fi ADB, QR, mDNS, Scrcpy Realtime 60FPS)
   + Kéo thả video vào điện thoại -> Tự động nạp vào Thư viện TikTok, trích xuất ID sản phẩm,
     Copy vào Clipboard và Paste thẳng vào điện thoại!
2. Tab AI Research Sản Phẩm đầy đủ 100% tính năng (Tự bắt link từ điện thoại, tải ảnh & mô tả
   ưu tiên Cách 1 SPAI Auto tốc độ cao; nếu thử Cách 2 Browser bị lỗi vùng quốc gia thì tự về Cách 1,
   quản lý SP HOT, lọc theo ngày, xem Gallery chi tiết, Copy Full, Xuất Excel, Sao lưu/Khôi phục).
"""
from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
import json
import mimetypes
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

# Ép ADB dùng Google Openscreen mDNS engine trên toàn bộ ứng dụng
os.environ.setdefault("ADB_MDNS_OPENSCREEN", "1")
os.environ["ADB_MDNS_OPENSCREEN"] = "1"

sys.path.insert(0, str(Path(__file__).resolve().parent))

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import config  # noqa: E402
from core import bluestacks, file_picker, ocr  # noqa: E402
from core.adb import CREATE_NO_WINDOW, Adb, AdbError  # noqa: E402
from core.devices import DeviceManager, require_serial  # noqa: E402
from core.scrcpy_stream import StreamManager  # noqa: E402
from core.storage import MediaTransfer, StorageMonitor  # noqa: E402
from core.websocket import WsHub  # noqa: E402
from core.wireless import WirelessManager, usb_diagnose  # noqa: E402
from tiktok.research import RESEARCH_STEPS, ResearchStore, extract_product_id, resolve_tiktok_share  # noqa: E402

MAX_BODY = 4 * 1024 * 1024

_kernel32 = ctypes.windll.kernel32
_user32 = ctypes.windll.user32

_kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
_kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
_kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
_kernel32.GlobalLock.restype = wintypes.LPVOID
_kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
_kernel32.GlobalUnlock.restype = wintypes.BOOL
_user32.OpenClipboard.argtypes = [wintypes.HWND]
_user32.OpenClipboard.restype = wintypes.BOOL
_user32.EmptyClipboard.argtypes = []
_user32.EmptyClipboard.restype = wintypes.BOOL
_user32.GetClipboardData.argtypes = [wintypes.UINT]
_user32.GetClipboardData.restype = wintypes.HANDLE
_user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
_user32.SetClipboardData.restype = wintypes.HANDLE
_user32.CloseClipboard.argtypes = []
_user32.CloseClipboard.restype = wintypes.BOOL


def read_from_windows_clipboard() -> str:
    """Đọc văn bản Unicode trực tiếp từ Clipboard của Windows PC."""
    try:
        if not _user32.OpenClipboard(None):
            return ""
        try:
            h_mem = _user32.GetClipboardData(13)  # CF_UNICODETEXT = 13
            if not h_mem:
                return ""
            ptr = _kernel32.GlobalLock(h_mem)
            if not ptr:
                return ""
            try:
                return ctypes.wstring_at(ptr) or ""
            finally:
                _kernel32.GlobalUnlock(h_mem)
        finally:
            _user32.CloseClipboard()
    except Exception:
        return ""


def copy_to_windows_clipboard(text: str) -> bool:
    """Sao chép chuỗi văn bản trực tiếp vào Clipboard của hệ điều hành Windows."""
    if not text:
        return False
    try:
        if not _user32.OpenClipboard(None):
            return False
        try:
            _user32.EmptyClipboard()
            data = text.encode("utf-16-le") + b"\x00\x00"
            h_mem = _kernel32.GlobalAlloc(0x0042, len(data))
            if not h_mem:
                return False
            ptr = _kernel32.GlobalLock(h_mem)
            if not ptr:
                return False
            ctypes.memmove(ptr, data, len(data))
            _kernel32.GlobalUnlock(h_mem)
            return bool(_user32.SetClipboardData(13, h_mem))
        finally:
            _user32.CloseClipboard()
    except Exception:
        return False


def extract_video_product_id(filename: str) -> str:
    """Trích xuất ID sản phẩm từ tên file video.
    Hỗ trợ:
      - 1737248949145339531_v2.mp4 -> 1737248949145339531
      - 1737248949145339531_v2_nv1.mp4 -> 1737248949145339531
      - 1737248949145339531.mp4 -> 1737248949145339531
      - SP_1737248949145339531_v1.mp4 -> 1737248949145339531
    """
    if not filename:
        return ""
    stem = Path(filename).stem.strip()
    parts = re.split(r"[-_ ]+", stem)
    if parts and parts[0].isdigit() and len(parts[0]) >= 8:
        return parts[0]
    m = re.search(r"(?:^|[^0-9])([0-9]{12,25})(?:[^0-9]|$)", stem)
    if m:
        return m.group(1)
    for part in parts:
        if part.isdigit() and len(part) >= 8:
            return part
    return ""


class LogBuffer:
    def __init__(self, size: int = 500):
        self.items: deque = deque(maxlen=size)
        self.seq = 0
        self.lock = threading.Lock()
        self.hub: WsHub | None = None

    def __call__(self, message: str) -> None:
        with self.lock:
            self.seq += 1
            entry = {"id": self.seq, "at": time.time(), "message": str(message)}
            self.items.append(entry)
        try:
            print(time.strftime("%H:%M:%S"), message, flush=True)
        except (UnicodeEncodeError, OSError):
            pass
        if self.hub:
            self.hub.broadcast({"type": "log", "entry": entry})

    def info(self, message: str) -> None:
        self.__call__(message)

    def warn(self, message: str) -> None:
        self.__call__(message)

    def error(self, message: str) -> None:
        self.__call__(message)

    def since(self, after: int = 0) -> list[dict]:
        with self.lock:
            return [e for e in self.items if e["id"] > after]


def normalize_video_for_tiktok(src: Path, logger=print) -> Path | None:
    """Chuẩn hóa video sang chuẩn tương thích 100% với Android & TikTok (nếu có ffmpeg và cần thiết).
    Nếu video đã là H.264 YUV420P + AAC <= 48kHz .mp4 -> Giữ nguyên gốc 100% không xử lý lại.
    """
    ffmpeg_bin = config.resolve_ffmpeg_path()
    if not ffmpeg_bin or not ffmpeg_bin.is_file():
        return None

    try:
        probe = subprocess.run(
            [str(ffmpeg_bin), "-i", str(src)],
            capture_output=True, text=True, errors="replace", creationflags=CREATE_NO_WINDOW
        )
        info = probe.stderr or ""
    except Exception:
        return None

    video_line = ""
    audio_line = ""
    for line in info.splitlines():
        if "Stream #" in line and "Video:" in line:
            video_line = line
        elif "Stream #" in line and "Audio:" in line:
            audio_line = line

    if not video_line:
        return None

    needs_v = False
    v_reason = ""
    if any(k in video_line for k in ["4:4:4", "yuv444p", "yuv422p", "10le", "hevc", "av1", "vp9", "prores"]):
        needs_v = True
        v_reason = "Codec video (4:4:4/10-bit/HEVC/AV1) không tương thích điện thoại"
    elif "h264" not in video_line or "yuv420p" not in video_line:
        needs_v = True
        v_reason = "Chuyển về chuẩn H.264 YUV420P"

    needs_a = False
    a_reason = ""
    if audio_line:
        sr_match = re.search(r"(\d+)\s*Hz", audio_line)
        if sr_match and int(sr_match.group(1)) > 48000:
            needs_a = True
            a_reason = f"Âm thanh {sr_match.group(1)}Hz vượt ngưỡng 48kHz"
        elif "aac" not in audio_line:
            needs_a = True
            a_reason = "Âm thanh chuyển sang AAC"

    needs_container = src.suffix.lower() != ".mp4"
    c_reason = f"Đổi container {src.suffix} sang MP4" if needs_container else ""

    if not needs_v and not needs_a and not needs_container:
        return None

    reasons = [r for r in [v_reason, a_reason, c_reason] if r]
    logger(f"⚙️ [Tối ưu Video] {' | '.join(reasons)}...")

    config.ensure_runtime_dirs()
    out_dir = config.WORK_DIR / "uploads"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"norm_{uuid.uuid4().hex}.mp4"

    # Thử NVENC trước, nếu không có GPU NVIDIA thì fallback libx264
    def _build_cmd(use_nvenc: bool) -> list[str]:
        c = [str(ffmpeg_bin), "-y", "-i", str(src)]
        if needs_v:
            if use_nvenc:
                c += [
                    "-c:v", "h264_nvenc", "-preset", "p6", "-tune", "hq",
                    "-profile:v", "high", "-rc:v", "vbr", "-cq:v", "17",
                    "-b:v", "0", "-pix_fmt", "yuv420p",
                ]
            else:
                c += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p"]
        else:
            c += ["-c:v", "copy"]

        if needs_a:
            c += ["-c:a", "aac", "-ar", "44100", "-b:a", "192k"]
        else:
            c += ["-c:a", "copy"]
        c += ["-movflags", "+faststart", str(out_file)]
        return c

    try:
        t0 = time.time()
        res = subprocess.run(_build_cmd(True), capture_output=True, timeout=120, creationflags=CREATE_NO_WINDOW)
        if (res.returncode != 0 or not out_file.is_file() or out_file.stat().st_size < 1000) and needs_v:
            res = subprocess.run(_build_cmd(False), capture_output=True, timeout=120, creationflags=CREATE_NO_WINDOW)
        if res.returncode == 0 and out_file.is_file() and out_file.stat().st_size > 1000:
            elapsed = time.time() - t0
            logger(f"✅ [Tối ưu Video] Hoàn tất sau {elapsed:.1f}s ({out_file.stat().st_size / (1024*1024):.1f} MB)")
            return out_file
    except Exception as e:
        logger(f"⚠️ [Tối ưu Video] Bỏ qua chuẩn hóa, dùng file gốc: {e}")
    return None


class App:
    def __init__(self):
        config.ensure_runtime_dirs()
        self.log = LogBuffer()
        self.adb = Adb(config.ADB_PATH)
        self.hub = WsHub()
        self.log.hub = self.hub
        self.devices = DeviceManager(self.adb, self.log)
        self.streams = StreamManager(
            self.adb,
            config.SCRCPY_SERVER,
            config.SCRCPY_VERSION,
            self.hub,
            self.devices.video_mode,
            self.log,
            on_clipboard=self._on_device_clipboard,
        )
        self.transfer = MediaTransfer(self.adb)
        self.storage = StorageMonitor(self.transfer, lambda _s: False, self.log)
        self.research = ResearchStore(config.STORE_RESEARCH)
        self.research.on_update = lambda: self.hub.broadcast({
            "type": "research-update",
            "products": self.research.list_products(),
        })

        self.auto_capture_clipboard: bool = True
        self.last_device_clipboard: str = ""
        self._suppressed_clipboards: dict[str, float] = {}
        self._adbime_ready_serials: set[str] = set()
        self._adbime_active_serials: set[str] = set()
        self._saved_user_imes: dict[str, str] = {}
        self._ime_restore_timers: dict[str, threading.Timer] = {}
        self._ime_lock = threading.Lock()

        self.hub.resync_provider = self.streams.resync_packets
        self.hub.on_connect = self._greet
        self.hub.on_message = self._on_ws_message

        self.autostream = True
        self.stopping = threading.Event()
        self._last_signature = ""
        self._last_bs_sig = ""
        self._device_cache: list[dict] = []
        self._device_cache_ts: float = 0.0
        self._woken_devices: set[str] = set()

        self.wireless = WirelessManager(self.adb, self.log, self.hub.broadcast, store_path=config.STORE_WIRELESS)
        self.devices.wireless = self.wireless
        self._discovered: list[dict] = []
        self.auto_wifi = True

        self.research_batch_state: dict[str, Any] = {
            "running": False,
            "current": 0,
            "total": 0,
            "current_id": "",
            "current_title": "",
            "success": 0,
            "failed": 0,
            "message": "",
        }
        self._batch_fetch_stop_event = threading.Event()

    # ---- WebSocket ------------------------------------------------------
    def _greet(self, client) -> None:
        self.streams.greet(client)
        client.send({"type": "device-update", "devices": self.device_list()})
        client.send({"type": "bluestacks-update", "instances": self.devices.bluestacks_instances()})
        client.send({"type": "wireless-discovered", "services": self._discovered})
        client.send({"type": "auto-capture-state", "enabled": self.auto_capture_clipboard})

    def _on_ws_message(self, client, message: dict) -> None:
        if message.get("type") == "resync":
            stream = self.streams.get(require_serial(message.get("serial")))
            if stream:
                client.send({"type": "stream-config", "serial": stream.serial, "codec": stream.parser.codec or "h264"})
                if stream.parser.width:
                    client.send({
                        "type": "stream-size",
                        "serial": stream.serial,
                        "width": stream.parser.width,
                        "height": stream.parser.height,
                    })
                for packet in stream.resync_packets():
                    client.send(packet)
                stream.reset_video()
            return
        if message.get("type") != "control":
            return
        serial = require_serial(message.get("serial"))
        stream = self.streams.get(serial)
        if not stream:
            raise RuntimeError("Stream chưa sẵn sàng. Dùng chế độ ảnh chụp hoặc bật lại stream.")
        stream.handle_message(message)

    # ---- Clipboard tự động bắt link TikTok từ Phone ---------------------
    def _on_device_clipboard(self, serial: str, text: str) -> None:
        if not text or not isinstance(text, str):
            return
        t = text.strip()
        if not t:
            return

        now = time.time()
        self._suppressed_clipboards = {k: ts for k, ts in self._suppressed_clipboards.items() if now - ts < 120}

        # Bỏ qua nội dung do chính Tool vừa nạp/paste vào điện thoại (ví dụ ID sản phẩm)
        if t in self._suppressed_clipboards:
            return

        self.last_device_clipboard = t
        try:
            copy_to_windows_clipboard(t)
        except Exception:
            pass

        self.hub.broadcast({
            "type": "device-clipboard-synced",
            "serial": serial,
            "text": t,
        })

        if not self.auto_capture_clipboard:
            return

        has_url = bool(re.search(r"https?://[^\s<>\"'()]+", t))
        is_tiktok = any(k in t.lower() for k in ("tiktok.com", "douyin.com", "vt.tiktok", "vm.tiktok", "shop.tiktok"))
        if not (has_url and is_tiktok):
            return

        threading.Thread(
            target=self._process_device_clip,
            args=(serial, t),
            daemon=True,
            name=f"clip-proc-{serial}",
        ).start()

    def _process_device_clip(self, serial: str, text: str) -> None:
        try:
            existing = self.research.check_duplicate(text)
            if existing:
                self.log(f"⚠️ [Trùng lặp] Bỏ qua sản phẩm đã có: {existing.get('title')} (ID: {existing.get('id')})")
                self.hub.broadcast({
                    "type": "product-duplicate-detected",
                    "serial": serial,
                    "productId": existing.get("id"),
                    "product": existing,
                    "message": f"Sản phẩm \"{existing.get('title')}\" đã có trong danh sách!",
                })
                if not existing.get("has_details"):
                    pid = existing.get("id")
                    if pid:
                        threading.Thread(target=self.auto_enrich_and_broadcast, args=(pid,), daemon=True).start()
                return

            prod = self.research.add_from_clipboard(text, source=f"Điện thoại {serial[-6:]}", auto_enrich=False)
            if not prod:
                return
            self.log(f"✨ [Auto-Capture] Bắt link sản phẩm từ điện thoại: {prod.get('title')} (ID: {prod.get('id')})")
            self.hub.broadcast({
                "type": "product-auto-captured",
                "serial": serial,
                "product": prod,
                "raw": text,
            })
            self.hub.broadcast({
                "type": "research-update",
                "products": self.research.list_products(),
            })
            pid = prod.get("id")
            if pid:
                threading.Thread(target=self.auto_enrich_and_broadcast, args=(pid,), daemon=True).start()
        except Exception as e:
            self.log(f"Lỗi tự động thêm sản phẩm từ clipboard: {e}")

    # ---- Copy & Paste ID sản phẩm thẳng vào điện thoại ------------------
    def copy_and_paste_product_id_to_device(self, serial: str, product_id: str, paste: bool = True) -> dict:
        """Sao chép ID sản phẩm vào Clipboard Điện thoại + PC và Paste thẳng vào ô đang mở trên Điện thoại."""
        pid = str(product_id or "").strip()
        if not pid:
            return {"ok": False, "copied": False, "pasted": False}

        # Chặn vòng lặp Auto-Capture khi phone báo ngược clipboard về PC
        self._suppressed_clipboards[pid] = time.time()

        # 1. Copy vào Windows PC Clipboard
        try:
            copy_to_windows_clipboard(pid)
        except Exception:
            pass

        # 2. Copy vào Phone Clipboard qua scrcpy
        copied = False
        try:
            stream = self.streams.get(serial)
            if stream and stream.running and stream.controllable:
                copied = stream.set_clipboard(pid, paste=False)
        except Exception as e:
            self.log(f"Lưu ý copy clipboard scrcpy: {e}")

        # 3. Paste trực tiếp vào điện thoại (nếu bật paste=True)
        pasted = False
        paste_method = ""
        if paste:
            try:
                res = self.send_text_to_device(serial, pid)
                pasted = bool(res.get("ok"))
                paste_method = str(res.get("method") or "")
            except Exception as e:
                self.log(f"Lưu ý khi paste ID vào điện thoại: {e}")

        if pasted:
            self.log(f"📋✍️ [Copy & Paste Phone] Đã copy và paste thẳng ID '{pid}' vào điện thoại ({paste_method})!")
        elif copied:
            self.log(f"📋 [Clipboard Phone] Đã copy ID '{pid}' vào bảng nhớ điện thoại & PC!")
        else:
            self.log(f"📋 [Clipboard PC] Đã copy ID '{pid}' (Mở khóa máy để paste trực tiếp)")

        try:
            self.hub.broadcast({
                "type": "video-product-id-copied",
                "serial": serial,
                "productId": pid,
                "phoneClipboard": copied,
                "phonePasted": pasted,
            })
        except Exception:
            pass

        return {"ok": True, "productId": pid, "copied": copied, "pasted": pasted, "method": paste_method}

    # ---- Kéo thả & Nạp video vào điện thoại + Tự Copy & Paste ID --------
    def push_manual_video(self, serial: str, source_path: str, filename: str, auto_paste: bool = True) -> dict:
        serial = require_serial(serial)
        src = Path(source_path)
        if not src.is_file():
            raise FileNotFoundError(f"Không thấy file video: {source_path}")
        size = src.stat().st_size
        if size <= 0:
            raise ValueError("File video rỗng (0 bytes).")

        self.devices.require_ready(serial)

        converted_file = normalize_video_for_tiktok(src, self.log)
        file_to_push = converted_file if converted_file else src
        push_size = file_to_push.stat().st_size

        ext = file_to_push.suffix.lower()
        if ext not in {".mp4", ".mov", ".m4v", ".webm"}:
            ext = ".mp4"
        base_name = re.sub(r"[^\w\.-]", "_", Path(filename).stem).strip() or "video"
        safe_name = f"{base_name}_{int(time.time())}{ext}"

        self.adb.shell(serial, "mkdir -p /sdcard/DCIM/Camera", timeout=8, check=False)
        target_path = f"/sdcard/DCIM/Camera/{safe_name}"

        self.log(f"📲 [Kéo thả Video] Đang nạp {safe_name} ({push_size / (1024*1024):.1f} MB) sang điện thoại...")

        try:
            self.adb.run(["push", str(file_to_push), target_path], serial=serial, timeout=300)

            # Cập nhật thời gian mới nhất và quét MediaStore để video đứng đầu Thư viện TikTok
            try:
                self.adb.shell(serial, f"touch '{target_path}'", timeout=5, check=False)
                self.adb.shell(
                    serial,
                    f"am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE -d 'file://{target_path}'",
                    timeout=8,
                    check=False,
                )
                self.adb.shell(
                    serial,
                    "content call --uri content://media/none/media_scanner --method scan_volume --extra volume:s:external_primary",
                    timeout=8,
                    check=False,
                )
            except Exception:
                pass

            self.log(f"✅ [Kéo thả Video] Đã nạp {safe_name} vào Thư viện máy ({target_path})")

            # Tự động trích xuất ID sản phẩm -> Copy vào Clipboard & Paste thẳng vào Điện thoại!
            product_id = extract_video_product_id(filename)
            clipboard_copied = False
            phone_pasted = False
            if product_id:
                cp_res = self.copy_and_paste_product_id_to_device(serial, product_id, paste=auto_paste)
                clipboard_copied = bool(cp_res.get("copied"))
                phone_pasted = bool(cp_res.get("pasted"))

            self.hub.broadcast({
                "type": "video-pushed",
                "serial": serial,
                "filename": safe_name,
                "path": target_path,
                "size": push_size,
                "productId": product_id,
                "clipboardCopied": clipboard_copied,
                "phonePasted": phone_pasted,
            })

            return {
                "ok": True,
                "filename": safe_name,
                "targetPath": target_path,
                "size": push_size,
                "productId": product_id,
                "clipboardCopied": clipboard_copied,
                "phonePasted": phone_pasted,
            }
        finally:
            if converted_file and converted_file.is_file():
                converted_file.unlink(missing_ok=True)

    # ---- Quản lý danh sách thiết bị & tự động kết nối -------------------
    def device_list(self, max_age: float = 2.0) -> list[dict]:
        if max_age > 0 and self._device_cache and (time.time() - self._device_cache_ts) < max_age:
            stream_state = self.streams.state()
            result = []
            for d in self._device_cache:
                serial = d.get("serial", "")
                result.append({
                    **d,
                    "storage": self.storage.view(serial),
                    "stream": stream_state.get(serial),
                    "run": None,
                })
            return result
        try:
            devices = self.devices.list_view(self.streams.state(), self.storage.view, lambda _s: None)
        except AdbError as exc:
            self.log(f"[ADB] Đang khởi tạo kết nối ADB: {exc}")
            return list(self._device_cache)
        self._device_cache = devices
        self._device_cache_ts = time.time()
        return devices

    def _auto_connect_emulators(self) -> None:
        try:
            connected = {d["serial"] for d in self.devices.raw_devices(max_age=8.0)}
        except Exception:
            return

        try:
            for inst in self.devices.bluestacks_instances(max_age=1.5):
                port = inst.get("port")
                if not port:
                    continue
                serial = f"127.0.0.1:{port}"
                if serial in connected or ("emulator-5554" in connected and port == 5555):
                    continue
                if inst.get("online") or bluestacks.port_open(port, timeout=0.15):
                    self.log(f"[BlueStacks] Phát hiện {inst.get('name')} đang bật (port {port}) -> Tự động kết nối...")
                    self.adb.run(["connect", serial], timeout=4, check=False)
        except Exception:
            pass

        common_ports = [5555, 5554, 5556, 5558, 5575, 5595, 62001, 62025, 7555, 16384, 21503]
        candidates = [
            p for p in common_ports
            if f"127.0.0.1:{p}" not in connected
            and f"emulator-{p-1}" not in connected
            and not (p == 5555 and "emulator-5554" in connected)
        ]
        if candidates:
            statuses = bluestacks.check_ports(candidates, timeout=0.12)
            for p, is_open in statuses.items():
                if is_open:
                    self.adb.run(["connect", f"127.0.0.1:{p}"], timeout=4, check=False)

    def auto_wakeup(self, serial: str) -> None:
        try:
            self.adb.shell(serial, "input", "keyevent", "224", timeout=5, check=False)
            time.sleep(0.15)
            self.adb.shell(serial, "input", "keyevent", "82", timeout=5, check=False)
            is_net = ":" in serial or "._tcp" in serial
            try:
                self.adb.shell(serial, "svc", "power", "stayon", "true" if is_net else "usb", timeout=4, check=False)
            except Exception:
                pass
            self.log(f"[{serial}] Đã đánh thức màn hình & mở khóa.")
        except Exception as exc:
            self.log(f"[{serial}] Ghi chú auto wakeup: {exc}")

    def monitor_loop(self) -> None:
        while not self.stopping.wait(6.0):
            try:
                self._auto_connect_emulators()
            except Exception:
                pass

            try:
                ready = self.devices.ready_serials()
            except AdbError as exc:
                self.log(f"[ADB] {exc}")
                continue
            except Exception as exc:
                self.log(f"[MONITOR] {exc}")
                continue

            try:
                self.storage.update(ready)
                for serial in list(self.streams.state()):
                    if serial not in ready:
                        self.streams.stop(serial, "Thiết bị đã ngắt kết nối.")
                        self.devices.forget(serial)

                current_ready = set(ready)
                for s in list(self._woken_devices):
                    if s not in current_ready:
                        self._woken_devices.discard(s)
                for s in ready:
                    if s not in self._woken_devices:
                        self._woken_devices.add(s)
                        threading.Thread(target=self.auto_wakeup, args=(s,), daemon=True, name=f"wakeup-{s}").start()

                if self.autostream:
                    for serial in ready:
                        if self.streams.may_autostart(serial):
                            threading.Thread(target=self._autostart, args=(serial,), daemon=True, name=f"autostart-{serial}").start()

                devices = self.device_list(max_age=0.0)
                signature = json.dumps(
                    [
                        {k: v for k, v in d.items() if k != "profile"}
                        | {"w": (d.get("profile") or {}).get("width"), "h": (d.get("profile") or {}).get("height")}
                        for d in devices
                    ],
                    sort_keys=True,
                    default=str,
                )
                if signature != self._last_signature:
                    self._last_signature = signature
                    self.hub.broadcast({"type": "device-update", "devices": devices})

                bs_instances = self.devices.bluestacks_instances(max_age=2.0)
                bs_sig = json.dumps([f"{i['id']}:{i['port']}:{i['online']}" for i in bs_instances])
                if bs_sig != self._last_bs_sig:
                    self._last_bs_sig = bs_sig
                    self.hub.broadcast({"type": "bluestacks-update", "instances": bs_instances})
            except Exception as exc:
                self.log(f"[MONITOR] {exc}")

    def _autostart(self, serial: str) -> None:
        try:
            self.streams.start(serial)
        except Exception as exc:
            self.log(f"[{serial}] Không bật được màn hình realtime: {exc}")

    def wireless_loop(self) -> None:
        while not self.stopping.wait(15.0):
            try:
                connected = {d["serial"] for d in self.devices.raw_devices(max_age=10.0) if d.get("state") == "device"}
                if self.auto_wifi:
                    self.wireless.auto_connect(connected)
                discovered = self.wireless.discovered(connected)
                if discovered != self._discovered:
                    self._discovered = discovered
                    self.hub.broadcast({"type": "wireless-discovered", "services": discovered})
            except Exception as exc:
                self.log(f"[Wi-Fi] {exc}")

    # ---- Bàn phím thông minh (ADBKeyboard + Gboard tự phục hồi) ----------
    def _ensure_adbime_ready(self, serial: str) -> bool:
        if serial in self._adbime_ready_serials:
            return True
        try:
            raw_imes = self.adb.shell(serial, "ime", "list", "-a", "-s", timeout=4, check=False) or ""
            if "com.android.adbkeyboard" not in raw_imes:
                apk_path = config.ROOT / "ADBKeyboard.apk"
                if apk_path.is_file():
                    self.log(f"[{serial}] ⌨️ Đang cài đặt tự động ADBKeyboard.apk hỗ trợ gõ & paste chữ từ PC...")
                    self.adb.run(["install", "-r", str(apk_path)], serial, timeout=20, check=False)
                    raw_imes = self.adb.shell(serial, "ime", "list", "-a", "-s", timeout=4, check=False) or ""
            if "com.android.adbkeyboard" in raw_imes:
                self.adb.shell(serial, "ime", "enable", "com.android.adbkeyboard/.AdbIME", timeout=4, check=False)
                self._adbime_ready_serials.add(serial)
                return True
        except Exception as exc:
            self.log(f"[{serial}] ⚠️ Lưu ý kiểm tra ADBKeyboard: {exc}")
        return False

    def _activate_adbime_session(self, serial: str) -> bool:
        if not self._ensure_adbime_ready(serial):
            return False
        adb_ime = "com.android.adbkeyboard/.AdbIME"
        with self._ime_lock:
            already_active = serial in self._adbime_active_serials
        if not already_active:
            cur = (self.adb.shell(serial, "settings", "get", "secure", "default_input_method", timeout=4, check=False) or "").strip()
            if cur and "adbkeyboard" not in cur.lower() and cur != "null":
                self._saved_user_imes[serial] = cur
            if "adbkeyboard" not in cur.lower():
                self.adb.shell(
                    serial,
                    f"ime enable {adb_ime} >/dev/null 2>&1; ime set {adb_ime} >/dev/null 2>&1",
                    timeout=5,
                    check=False,
                )
                time.sleep(0.32)
            with self._ime_lock:
                self._adbime_active_serials.add(serial)
        self._schedule_ime_restore(serial, delay=4.5)
        return True

    def _schedule_ime_restore(self, serial: str, delay: float = 4.5) -> None:
        with self._ime_lock:
            old_timer = self._ime_restore_timers.pop(serial, None)
        if old_timer:
            try:
                old_timer.cancel()
            except Exception:
                pass

        def _restore():
            with self._ime_lock:
                self._ime_restore_timers.pop(serial, None)
                self._adbime_active_serials.discard(serial)
            saved_ime = self._saved_user_imes.get(serial, "")
            try:
                if saved_ime and "adbkeyboard" not in saved_ime.lower():
                    self.adb.shell(serial, "ime", "set", saved_ime, timeout=4, check=False)
                else:
                    self.device_ime_switch(serial, "normal")
            except Exception:
                pass

        timer = threading.Timer(delay, _restore)
        timer.daemon = True
        with self._ime_lock:
            self._ime_restore_timers[serial] = timer
        timer.start()

    def send_text_to_device(self, serial: str, text: str) -> dict:
        """Gửi văn bản hoặc Paste ID sản phẩm vào ô đang chọn trên điện thoại."""
        text = str(text or "")[:2200]
        if not text:
            return {"ok": True, "method": "empty"}

        t_clean = text.strip()
        if t_clean:
            self._suppressed_clipboards[t_clean] = time.time()

        stream = self.streams.get(serial)
        if stream and stream.controllable:
            try:
                stream.set_clipboard(text, paste=False)
            except Exception:
                pass

        # Ưu tiên 1: ADBKeyboard Broadcast (vượt chặn INJECT_EVENTS trên Xiaomi/MIUI, hỗ trợ 100% Unicode)
        if self._activate_adbime_session(serial):
            b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
            out = self.adb.shell(
                serial,
                "am", "broadcast", "-a", "ADB_INPUT_B64", "--es", "msg", b64,
                timeout=6,
                check=False,
            ) or ""
            if "Broadcast completed" in out or "result=0" in out:
                return {"ok": True, "method": "adbime_b64"}

        # Dự phòng 2: scrcpy paste
        if stream and stream.controllable:
            try:
                if stream.paste(text):
                    return {"ok": True, "method": "scrcpy_paste"}
            except Exception:
                pass

        # Dự phòng 3: adb shell input text (ASCII / số ID sản phẩm)
        if text.isascii():
            escaped = text.replace("%", r"\%").replace(" ", "%s").replace("'", "")
            self.adb.run(["shell", f"input text '{escaped}'"], serial, timeout=15, check=False)
            return {"ok": True, "method": "adb_input_text"}

        # Dự phòng 4: KEYCODE_PASTE (279)
        self.adb.shell(serial, "input", "keyevent", "279", timeout=6, check=False)
        return {"ok": True, "method": "keyevent_paste"}

    def adb_input(self, serial: str, body: dict) -> dict:
        self.devices.require_ready(serial)
        action = body.get("action")

        def num(key: str) -> str:
            value = int(float(body.get(key)))
            if not 0 <= value <= 20000:
                raise ValueError("Tọa độ không hợp lệ.")
            return str(value)

        if action == "tap":
            self.adb.shell(serial, "input", "tap", num("x"), num("y"), timeout=10)
        elif action == "swipe":
            duration = str(max(50, min(5000, int(body.get("duration") or 300))))
            self.adb.shell(serial, "input", "swipe", num("x1"), num("y1"), num("x2"), num("y2"), duration, timeout=12)
        elif action == "key":
            keycode = int(body.get("keycode"))
            if not 0 < keycode < 400:
                raise ValueError("Mã phím không hợp lệ.")
            if keycode in (66, 67, 112) and self._activate_adbime_session(serial):
                if keycode == 66:
                    self.adb.shell(serial, "am", "broadcast", "-a", "ADB_EDITOR_CODE", "--ei", "code", "3", timeout=5, check=False)
                    self.adb.shell(serial, "am", "broadcast", "-a", "ADB_INPUT_CODE", "--ei", "code", "66", timeout=5, check=False)
                else:
                    self.adb.shell(serial, "am", "broadcast", "-a", "ADB_INPUT_CODE", "--ei", "code", str(keycode), timeout=5, check=False)
                return {"ok": True, "method": "adbime_keycode"}
            self.adb.shell(serial, "input", "keyevent", str(keycode), timeout=10, check=False)
        elif action == "clear_text":
            if self._activate_adbime_session(serial):
                self.adb.shell(serial, "am", "broadcast", "-a", "ADB_CLEAR_TEXT", timeout=5, check=False)
            else:
                self.adb.shell(serial, "input", "keyevent", "123", *(["67"] * 50), timeout=10, check=False)
        elif action == "paste_pc":
            text = str(body.get("text") or "") or read_from_windows_clipboard()
            if not text:
                raise ValueError("Bảng nhớ (Clipboard) trên máy tính đang trống.")
            res = self.send_text_to_device(serial, text)
            return {**res, "text": text[:120]}
        elif action == "text":
            text = str(body.get("text") or "")[:2200]
            return self.send_text_to_device(serial, text)
        else:
            raise ValueError("Thao tác không hợp lệ.")
        return {"ok": True}

    def screenshot(self, serial: str) -> bytes:
        self.devices.require_ready(serial)
        return self.adb.exec_out(serial, "screencap", "-p", timeout=15)

    def _detect_tiktok_package(self, serial: str) -> str:
        listing = self.adb.shell(serial, "pm", "list", "packages", timeout=12, check=False) or ""
        for pkg in config.TIKTOK_PACKAGES:
            if f"package:{pkg}" in listing:
                return pkg
        raise RuntimeError("Không tìm thấy ứng dụng TikTok trên thiết bị.")

    def _foreground_window(self, serial: str) -> str:
        out = self.adb.shell(serial, "dumpsys", "window", timeout=6, check=False) or ""
        for pat in (r"mCurrentFocus=Window\{[^}]*\s(\S+/\S+)\}", r"mFocusedApp=.*?\s(\S+/\S+)[\s}]"):
            m = re.search(pat, out)
            if m:
                return m.group(1)
        return ""

    def open_tiktok(self, serial: str) -> dict:
        self.devices.require_ready(serial)
        self.auto_wakeup(serial)
        pkg = self._detect_tiktok_package(serial)
        if pkg == "com.zhiliaoapp.musically":
            self.adb.shell(
                serial,
                "am", "start",
                "-a", "android.intent.action.MAIN",
                "-c", "android.intent.category.LAUNCHER",
                "-f", "0x10200000",
                "-n", f"{pkg}/{config.TIKTOK_MAIN_ACTIVITY_CLASS}",
                timeout=12,
                check=False,
            )
            time.sleep(1.2)
        if pkg not in self._foreground_window(serial):
            self.adb.shell(serial, "monkey", "-p", pkg, "1", timeout=12, check=False)
            time.sleep(1.2)
        if pkg not in self._foreground_window(serial):
            self.adb.shell(
                serial,
                "am", "start",
                "-a", "android.intent.action.MAIN",
                "-c", "android.intent.category.LAUNCHER",
                "-f", "0x10200000",
                "-n", f"{pkg}/{config.TIKTOK_MAIN_ACTIVITY_CLASS}",
                timeout=12,
                check=False,
            )
        return {"ok": True, "package": pkg, "message": f"Đã mở TikTok ({pkg})"}

    def stop_tiktok(self, serial: str) -> dict:
        self.devices.require_ready(serial)
        for pkg in config.TIKTOK_PACKAGES:
            self.adb.shell(serial, "am", "force-stop", pkg, timeout=5, check=False)
        return {"ok": True, "message": "Đã dừng ứng dụng TikTok"}

    def device_ime_info(self, serial: str) -> dict:
        self.devices.require_ready(serial)
        cur = (self.adb.shell(serial, "settings", "get", "secure", "default_input_method", timeout=4, check=False) or "").strip()
        raw_all = self.adb.shell(serial, "ime", "list", "-a", "-s", timeout=4, check=False) or ""
        imes = [line.strip() for line in raw_all.splitlines() if line.strip() and "/" in line]
        is_adb = "adbkeyboard" in cur.lower()
        return {
            "ok": True,
            "serial": serial,
            "current": cur,
            "mode": "adb" if is_adb else "normal",
            "isAdbKeyboard": is_adb,
            "available": imes,
        }

    def device_ime_switch(self, serial: str, mode: str = "normal") -> dict:
        self.devices.require_ready(serial)
        info = self.device_ime_info(serial)
        imes = info["available"]

        if mode == "toggle":
            mode = "normal" if info["isAdbKeyboard"] else "adb"

        if mode == "adb":
            adb_ime = "com.android.adbkeyboard/.AdbIME"
            self._ensure_adbime_ready(serial)
            self.adb.shell(serial, "ime", "enable", adb_ime, timeout=4, check=False)
            self.adb.shell(serial, "ime", "set", adb_ime, timeout=4, check=False)
            self.log(f"[{serial}] ⌨️ Đã chuyển sang ADB Keyboard (chạy ngầm).")
            return {
                "ok": True,
                "serial": serial,
                "mode": "adb",
                "current": adb_ime,
                "message": "Đã chuyển sang ADB Keyboard (chạy ngầm).",
            }

        preferred = [
            "com.google.android.inputmethod.latin/com.android.inputmethod.latin.LatinIME",
            "com.samsung.android.honeyboard/.service.HoneyBoardService",
            "com.sec.android.inputmethod/.SamsungKeypad",
            "com.miui.inputmethod/.InputMethodService",
            "com.sohu.inputmethod.sogou.xiaomi/.SogouIME",
        ]
        target_ime = ""
        for cand in preferred:
            if cand in imes:
                target_ime = cand
                break
        if not target_ime:
            for item in imes:
                low = item.lower()
                if "adbkeyboard" not in low and "voice" not in low and "tts" not in low:
                    target_ime = item
                    break
        if not target_ime:
            target_ime = "com.google.android.inputmethod.latin/com.android.inputmethod.latin.LatinIME"

        with self._ime_lock:
            old_timer = self._ime_restore_timers.pop(serial, None)
            self._adbime_active_serials.discard(serial)
        if old_timer:
            try:
                old_timer.cancel()
            except Exception:
                pass
        self._saved_user_imes[serial] = target_ime
        self.adb.shell(serial, "ime", "enable", target_ime, timeout=4, check=False)
        self.adb.shell(serial, "ime", "set", target_ime, timeout=4, check=False)
        self.adb.shell(serial, "ime", "enable", "com.android.adbkeyboard/.AdbIME", timeout=4, check=False)
        self.log(f"[{serial}] ⌨️ Đã khôi phục bàn phím ảo trên màn hình ({target_ime}).")
        return {
            "ok": True,
            "serial": serial,
            "mode": "normal",
            "current": target_ime,
            "message": f"Đã bật lại bàn phím ảo trên điện thoại ({target_ime.split('/')[0]})!",
        }

    def device_quick_action(self, serial: str, body: dict) -> dict:
        self.devices.require_ready(serial)
        action = str(body.get("action") or "").strip().lower()
        prof = self.devices.cached_profile(serial) or {}
        w = int(prof.get("width") or 1080)
        h = int(prof.get("height") or 2400)

        if action == "wakeup":
            self.auto_wakeup(serial)
            return {"ok": True, "message": "Đã đánh thức & mở khóa màn hình."}
        if action == "lock":
            self.adb.shell(serial, "input", "keyevent", "26", timeout=4, check=False)
            return {"ok": True, "message": "Đã khóa màn hình."}
        if action == "home":
            self.adb.shell(serial, "input", "keyevent", "3", timeout=4, check=False)
            return {"ok": True, "message": "Đã về Màn hình chính."}
        if action == "back":
            self.adb.shell(serial, "input", "keyevent", "4", timeout=4, check=False)
            return {"ok": True, "message": "Đã bấm Quay lại."}
        if action == "recents":
            self.adb.shell(serial, "input", "keyevent", "187", timeout=4, check=False)
            return {"ok": True, "message": "Đã mở Đa nhiệm."}
        if action == "vol_up":
            self.adb.shell(serial, "input", "keyevent", "24", timeout=4, check=False)
            return {"ok": True, "message": "Đã tăng âm lượng máy."}
        if action == "vol_down":
            self.adb.shell(serial, "input", "keyevent", "25", timeout=4, check=False)
            return {"ok": True, "message": "Đã giảm âm lượng máy."}
        if action == "mute":
            self.adb.shell(serial, "input", "keyevent", "164", timeout=4, check=False)
            return {"ok": True, "message": "Đã tắt/bật tiếng máy."}
        if action == "swipe_up":
            self.adb.shell(serial, "input", "swipe", str(w // 2), str(int(h * 0.78)), str(w // 2), str(int(h * 0.22)), "260", timeout=5, check=False)
            return {"ok": True, "message": "Đã vuốt lên."}
        if action == "swipe_down":
            self.adb.shell(serial, "input", "swipe", str(w // 2), str(int(h * 0.24)), str(w // 2), str(int(h * 0.78)), "260", timeout=5, check=False)
            return {"ok": True, "message": "Đã vuốt xuống."}
        if action == "settings":
            self.adb.shell(serial, "am", "start", "-a", "android.settings.SETTINGS", timeout=5, check=False)
            return {"ok": True, "message": "Đã mở Cài đặt Android."}
        if action == "wifi_settings":
            self.adb.shell(serial, "am", "start", "-a", "android.settings.WIFI_SETTINGS", timeout=5, check=False)
            return {"ok": True, "message": "Đã mở Cài đặt Wi-Fi."}
        if action == "ime_settings":
            self.adb.shell(serial, "am", "start", "-a", "android.settings.INPUT_METHOD_SETTINGS", timeout=5, check=False)
            return {"ok": True, "message": "Đã mở Cài đặt Bàn phím."}
        if action == "tcpip":
            ip_out = self.adb.shell(serial, "ip -f inet addr show wlan0", timeout=4, check=False) or ""
            m_ip = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+)", ip_out)
            ip_addr = m_ip.group(1) if m_ip else ""
            self.adb.run(["-s", serial, "tcpip", "5555"], timeout=8, check=False)
            if ip_addr:
                time.sleep(1.5)
                try:
                    self.devices.wireless_connect(f"{ip_addr}:5555")
                except Exception:
                    pass
                return {"ok": True, "ip": ip_addr, "message": f"Đã bật Wi-Fi ADB cổng 5555 ({ip_addr}:5555)!"}
            return {"ok": True, "message": "Đã mở cổng TCP/IP 5555 trên thiết bị."}
        raise ValueError(f"Thao tác nhanh không hợp lệ: {action}")

    def devices_batch_action(self, body: dict) -> dict:
        raw_serials = body.get("serials") or self.devices.ready_serials()
        serials = [require_serial(s) for s in raw_serials if s]
        action = str(body.get("action") or "").strip().lower()
        results = []

        def worker(s: str):
            try:
                if action in ("wakeup", "lock", "home", "back", "recents", "vol_up", "vol_down", "mute", "swipe_up", "swipe_down"):
                    self.device_quick_action(s, {"action": action})
                elif action == "open_tiktok":
                    self.open_tiktok(s)
                elif action == "stop_tiktok":
                    self.stop_tiktok(s)
                elif action == "ime_normal":
                    self.device_ime_switch(s, "normal")
                elif action == "ime_adb":
                    self.device_ime_switch(s, "adb")
                elif action == "text":
                    self.adb_input(s, {"action": "text", "text": body.get("text", "")})
                elif action == "key":
                    self.adb_input(s, {"action": "key", "keycode": body.get("keycode", 3)})
                results.append({"serial": s, "ok": True})
            except Exception as exc:
                results.append({"serial": s, "ok": False, "error": str(exc)})

        threads = [threading.Thread(target=worker, args=(s,), daemon=True) for s in serials]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=8.0)

        ok_count = sum(1 for r in results if r.get("ok"))
        return {"ok": True, "total": len(serials), "success": ok_count, "results": results}

    def health(self) -> dict:
        return {
            "ok": True,
            "app": config.APP_NAME,
            "version": config.APP_VERSION,
            "port": config.PORT,
            "adb": self.adb.available(),
            "adbPath": str(config.ADB_PATH),
            "scrcpyServer": config.SCRCPY_SERVER.is_file(),
            "ocr": ocr.available(),
            "autostream": self.autostream,
            "autoCapture": self.auto_capture_clipboard,
            "hasLegacyResearch": config.LEGACY_RESEARCH_JSON.is_file(),
        }

    def shutdown(self) -> None:
        self.stopping.set()
        try:
            self.streams.stop_all()
        except Exception:
            pass

    # ---- AI Research Batch & Auto-Enrich (Ưu tiên Cách 1 SPAI Auto) ------
    def research_batch_status(self) -> dict[str, Any]:
        return dict(self.research_batch_state)

    def start_research_batch_fetch(self, target_ids: list[str], method: str = "spaiauto") -> bool:
        if self.research_batch_state.get("running"):
            return False
        self._batch_fetch_stop_event.clear()
        self.research_batch_state = {
            "running": True,
            "current": 0,
            "total": len(target_ids),
            "current_id": "",
            "current_title": "",
            "success": 0,
            "failed": 0,
            "method": method,
            "message": f"Bắt đầu tải {len(target_ids)} sản phẩm...",
        }
        threading.Thread(
            target=self._run_research_batch_fetch,
            args=(target_ids, method),
            daemon=True,
            name="research-batch-fetch",
        ).start()
        return True

    def _run_research_batch_fetch(self, target_ids: list[str], method: str = "spaiauto") -> None:
        total = len(target_ids)
        succ = 0
        fail = 0
        mode_label = (
            "Thử Cách 2 (Browser) -> Nếu lỗi tự dùng Cách 1 (SPAI Auto)"
            if method == "browser"
            else "Cách 1 (SPAI Auto tốc độ cao)"
        )
        self.log(f"⚡ Bắt đầu tải thông tin cho {total} sản phẩm [{mode_label}]...")

        for i, pid in enumerate(target_ids, 1):
            if self._batch_fetch_stop_event.is_set():
                self.research_batch_state["message"] = "Đã dừng tải theo yêu cầu."
                self.log("⚡ Đã dừng tải hàng loạt theo yêu cầu.")
                break

            p = self.research.get_product(pid)
            title = p.get("title", "") if p else f"Sản phẩm {pid}"
            self.research_batch_state.update({
                "current": i,
                "current_id": pid,
                "current_title": title,
                "message": f"Đang tải ({i}/{total}): {title[:40]}...",
            })
            self.hub.broadcast({
                "type": "research-fetch-progress",
                "batch": self.research_batch_status(),
            })

            try:
                res = self.research.enrich_product(pid, download_images=True, method=method, log_fn=self.log)
                if res and res.get("has_details"):
                    succ += 1
                else:
                    fail += 1
            except Exception as exc:
                self.log(f"⚠️ Lỗi tải SP {pid}: {exc}")
                fail += 1

            self.research_batch_state["success"] = succ
            self.research_batch_state["failed"] = fail

            if i % 2 == 0 or i == total:
                self.hub.broadcast({
                    "type": "research-update",
                    "products": self.research.list_products(),
                })

            time.sleep(0.9)

        self.research_batch_state["running"] = False
        self.research_batch_state["message"] = f"Hoàn tất tải ảnh & mô tả! Thành công: {succ}/{total}, Thất bại: {fail}."
        self.log(f"⚡ Hoàn tất tải hàng loạt: {succ}/{total} thành công, {fail} thất bại.")
        self.hub.broadcast({
            "type": "research-fetch-progress",
            "batch": self.research_batch_status(),
        })
        self.hub.broadcast({
            "type": "research-update",
            "products": self.research.list_products(),
        })

    def auto_enrich_and_broadcast(self, product_id: str) -> None:
        if not product_id:
            return
        try:
            self.log(f"⚡ [Cách 1 - SPAI Auto] Đang tự động tải ảnh & mô tả cho sản phẩm {product_id}...")
            enriched = self.research.enrich_product(product_id, download_images=True, method="spaiauto", log_fn=self.log)
            if enriched:
                name = enriched.get("title") or product_id
                self.log(f"✨ [Cách 1 - SPAI Auto] Đã tải xong: {name[:50]}...")
                self.hub.broadcast({
                    "type": "research-update",
                    "products": self.research.list_products(),
                    "highlightId": product_id,
                })
        except Exception as e:
            self.log(f"⚠️ Lỗi tự động tải sản phẩm {product_id}: {e}")


APP: App | None = None


class HttpError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


ROUTES: list[tuple[str, re.Pattern, str]] = []


def route(method: str, pattern: str):
    def decorator(func):
        ROUTES.append((method, re.compile("^" + pattern + "$"), func.__name__))
        return func
    return decorator


class Handler(BaseHTTPRequestHandler):
    server_version = "TikTokResearchPhone/2.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args) -> None:
        return

    def _send(self, status: int, body: bytes, content_type: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.close_connection = True
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

    def _json(self, value, status: int = 200) -> None:
        self._send(
            status,
            json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise HttpError(413, "Dữ liệu gửi lên quá lớn.")
        if not length:
            return {}
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HttpError(400, "JSON không hợp lệ.") from exc
        if not isinstance(value, dict):
            raise HttpError(400, "Body phải là object JSON.")
        return value

    def _check_origin(self) -> None:
        origin = self.headers.get("Origin")
        host = self.headers.get("Host", "")
        if origin and not origin.endswith("//" + host):
            raise HttpError(403, "Origin không được phép.")

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        self.query = {k: v[-1] for k, v in parse_qs(parsed.query).items()}
        if method == "GET" and path == "/ws/screen":
            APP.hub.serve(self)
            return
        try:
            if method != "GET":
                self._check_origin()
            for route_method, pattern, name in ROUTES:
                if route_method != method:
                    continue
                match = pattern.match(path)
                if match:
                    args = [unquote(g) for g in match.groups()]
                    result = getattr(self, name)(*args)
                    if result is not None:
                        self._json(result)
                    return
            if method == "GET" and not path.startswith("/api/"):
                self._static(path)
                return
            raise HttpError(404, "Không tìm thấy API.")
        except HttpError as exc:
            self._json({"error": str(exc)}, exc.status)
        except (ValueError, KeyError, TypeError) as exc:
            self._json({"error": str(exc) or exc.__class__.__name__}, 400)
        except (RuntimeError, AdbError, FileNotFoundError) as exc:
            self._json({"error": str(exc)}, 409)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception as exc:
            APP.log(f"[HTTP] Lỗi {method} {path}: {traceback.format_exc()}")
            try:
                self._json({"error": f"Lỗi máy chủ: {exc}"}, 500)
            except OSError:
                pass

    def do_OPTIONS(self):  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    def do_GET(self):  # noqa: N802
        self._dispatch("GET")

    def do_POST(self):  # noqa: N802
        self._dispatch("POST")

    def do_DELETE(self):  # noqa: N802
        self._dispatch("DELETE")

    def _static(self, path: str) -> None:
        relative = "index.html" if path in ("", "/") else path.lstrip("/")
        target = (config.WEB_DIR / relative).resolve()
        root = config.WEB_DIR.resolve()
        if root not in target.parents and target != root or not target.is_file():
            raise HttpError(404, "Không tìm thấy file.")
        content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in ("application/javascript",):
            content_type += "; charset=utf-8"
        self._send(200, target.read_bytes(), content_type)

    # ---- Hệ thống -------------------------------------------------------
    @route("GET", r"/api/health")
    def api_health(self):
        return APP.health()

    @route("GET", r"/api/logs")
    def api_logs(self):
        return {"items": APP.log.since(int(self.query.get("after") or 0))}

    @route("POST", r"/api/shutdown")
    def api_shutdown(self):
        self._json({"ok": True})
        threading.Thread(target=lambda: (time.sleep(0.3), APP.shutdown(), os._exit(0)), daemon=True).start()

    @route("POST", r"/api/settings/autostream")
    def api_autostream(self):
        APP.autostream = bool(self._body().get("enabled"))
        return {"autostream": APP.autostream}

    # ---- Thiết bị & Kết nối Phone ---------------------------------------
    @route("GET", r"/api/devices")
    def api_devices(self):
        return {"devices": APP.device_list()}

    @route("POST", r"/api/devices/batch-action")
    def api_devices_batch_action(self):
        return APP.devices_batch_action(self._body())

    @route("GET", r"/api/devices/([^/]+)/ime")
    def api_device_ime_get(self, serial):
        return APP.device_ime_info(require_serial(serial))

    @route("POST", r"/api/devices/([^/]+)/ime")
    def api_device_ime_set(self, serial):
        body = self._body()
        mode = str(body.get("mode") or "normal").strip().lower()
        return APP.device_ime_switch(require_serial(serial), mode=mode)

    @route("POST", r"/api/devices/([^/]+)/quick-action")
    def api_device_quick_action(self, serial):
        return APP.device_quick_action(require_serial(serial), self._body())

    @route("POST", r"/api/devices/([^/]+)")
    def api_device_update(self, serial):
        return APP.devices.set_device(require_serial(serial), self._body())

    @route("POST", r"/api/devices/([^/]+)/video-mode")
    def api_video_mode(self, serial):
        serial = require_serial(serial)
        mode = self._body().get("videoMode")
        saved = APP.devices.set_device(serial, {"videoMode": mode})
        if APP.streams.get(serial):
            threading.Thread(target=APP.streams.restart, args=(serial, "Đổi chế độ màn hình."), daemon=True).start()
        return saved

    @route("POST", r"/api/devices/([^/]+)/profile")
    def api_profile_refresh(self, serial):
        serial = require_serial(serial)
        APP.devices.require_ready(serial)
        return APP.devices.profile(serial, force=True)

    @route("POST", r"/api/devices/([^/]+)/input")
    def api_input(self, serial):
        return APP.adb_input(require_serial(serial), self._body())

    @route("POST", r"/api/devices/([^/]+)/wakeup")
    def api_wakeup(self, serial):
        serial = require_serial(serial)
        APP.devices.require_ready(serial)
        APP.auto_wakeup(serial)
        return {"ok": True, "serial": serial}

    @route("POST", r"/api/devices/([^/]+)/open-tiktok")
    def api_open_tiktok(self, serial):
        return APP.open_tiktok(require_serial(serial))

    @route("POST", r"/api/devices/([^/]+)/stop-tiktok")
    def api_stop_tiktok(self, serial):
        return APP.stop_tiktok(require_serial(serial))

    @route("GET", r"/api/devices/([^/]+)/screenshot")
    def api_screenshot(self, serial):
        data = APP.screenshot(require_serial(serial))
        self._send(200, data, "image/png")

    @route("GET", r"/api/devices/([^/]+)/storage")
    def api_storage(self, serial):
        return APP.storage.view(require_serial(serial)) or {"status": "UNKNOWN"}

    @route("POST", r"/api/devices/([^/]+)/storage/scan")
    def api_storage_scan(self, serial):
        serial = require_serial(serial)
        APP.devices.require_ready(serial)
        return APP.storage.rescan(serial)

    # ---- Kéo thả Video vào Phone + Copy & Paste ID ----------------------
    @route("POST", r"/api/devices/([^/]+)/upload-video")
    def api_device_upload_video(self, serial):
        serial = require_serial(serial)
        APP.devices.require_ready(serial)
        raw_name = self.query.get("filename") or self.headers.get("X-Filename") or f"video_{int(time.time())}.mp4"
        raw_name = unquote(raw_name)
        auto_paste = str(self.query.get("autoPaste") or self.headers.get("X-Auto-Paste") or "1").lower() not in ("0", "false", "no")

        content_length = int(self.headers.get("Content-Length") or 0)
        if content_length <= 0:
            raise ValueError("File video không có nội dung (Content-Length = 0).")
        if content_length > 1024 * 1024 * 1024:
            raise ValueError("Kích thước video vượt quá giới hạn 1 GB.")

        config.ensure_runtime_dirs()
        temp_dir = config.WORK_DIR / "uploads"
        temp_dir.mkdir(parents=True, exist_ok=True)
        safe_prefix = re.sub(r"[^\w\.-]", "_", Path(raw_name).stem) or "upload"
        ext = Path(raw_name).suffix.lower()
        if ext not in config.VIDEO_EXTENSIONS:
            ext = ".mp4"
        temp_path = temp_dir / f"tmp_{uuid.uuid4().hex}_{safe_prefix}{ext}"

        remaining = content_length
        chunk_size = 128 * 1024
        with open(temp_path, "wb") as f:
            while remaining > 0:
                n = min(remaining, chunk_size)
                chunk = self.rfile.read(n)
                if not chunk:
                    break
                f.write(chunk)
                remaining -= len(chunk)

        if temp_path.stat().st_size != content_length:
            temp_path.unlink(missing_ok=True)
            raise ValueError("Quá trình truyền file bị gián đoạn.")

        try:
            return APP.push_manual_video(serial, str(temp_path), raw_name, auto_paste=auto_paste)
        finally:
            temp_path.unlink(missing_ok=True)

    # ---- Stream & Clipboard ---------------------------------------------
    @route("POST", r"/api/stream/start")
    def api_stream_start(self):
        serial = require_serial(self._body().get("serial"))
        APP.devices.require_ready(serial)
        return APP.streams.start(serial)

    @route("POST", r"/api/stream/stop")
    def api_stream_stop(self):
        serial = require_serial(self._body().get("serial"))
        APP.streams.stop(serial, "Người dùng dừng stream.")
        return APP.streams.state_message()

    @route("POST", r"/api/stream/restart")
    def api_stream_restart(self):
        serial = require_serial(self._body().get("serial"))
        APP.devices.require_ready(serial)
        return APP.streams.restart(serial, "Khởi động lại stream.")

    @route("GET", r"/api/stream/([^/]+)/clipboard")
    def api_stream_clipboard(self, serial):
        serial = require_serial(serial)
        stream = APP.streams.get(serial)
        if stream and stream.controllable:
            stream.request_clipboard()
            time.sleep(0.12)
            return {"serial": serial, "clipboard": getattr(stream, "last_clipboard", "")}
        return {"serial": serial, "clipboard": APP.last_device_clipboard}

    @route("POST", r"/api/stream/([^/]+)/clipboard")
    def api_stream_set_clipboard(self, serial):
        serial = require_serial(serial)
        body = self._body()
        text = str(body.get("text") or "").strip()
        paste = bool(body.get("paste", False))
        if not text:
            raise ValueError("Văn bản clipboard không được để trống.")
        res = APP.copy_and_paste_product_id_to_device(serial, text, paste=paste)
        return {**res, "serial": serial, "text": text}

    @route("POST", r"/api/stream/([^/]+)/paste")
    def api_stream_paste(self, serial):
        serial = require_serial(serial)
        body = self._body()
        text = str(body.get("text") or "").strip()
        if not text:
            text = read_from_windows_clipboard()
        if text:
            res = APP.copy_and_paste_product_id_to_device(serial, text, paste=True)
            return {**res, "serial": serial, "text": text[:120]}
        ok = bool(APP.adb.shell(serial, "input keyevent 279", timeout=5, check=False))
        return {"ok": ok, "serial": serial}

    @route("GET", r"/api/stream/audio")
    def api_stream_audio_get(self):
        return APP.streams.audio_state()

    @route("POST", r"/api/stream/audio")
    def api_stream_audio_set(self):
        body = self._body()
        raw_serial = body.get("serial")
        serial = require_serial(raw_serial) if raw_serial else ""
        enabled = body.get("enabled") if "enabled" in body else None
        volume_step = int(body.get("volumeStep") or 0)
        return APP.streams.set_audio(serial=serial, enabled=enabled, volume_step=volume_step)

    # ---- BlueStacks & Wi-Fi ADB & USB -----------------------------------
    @route("GET", r"/api/bluestacks/instances")
    def api_bs_instances(self):
        return {"instances": APP.devices.bluestacks_instances(max_age=0)}

    @route("POST", r"/api/bluestacks/connect")
    def api_bs_connect(self):
        body = self._body()
        port = int(body.get("port") or 0)
        instance_id = str(body.get("id") or "")
        if instance_id:
            insts = bluestacks.list_instances()
            target = next((i for i in insts if i["id"] == instance_id), None)
            if target and target.get("port"):
                port = target["port"]
        if not 1 <= port <= 65535:
            raise ValueError("Port ADB không hợp lệ.")
        out = APP.devices.wireless_connect(f"127.0.0.1:{port}")
        APP.log(f"[BlueStacks] {out}")
        return {"message": out, "port": port}

    @route("POST", r"/api/bluestacks/launch")
    def api_bs_launch(self):
        instance = str(self._body().get("id") or "")
        bluestacks.launch_instance(instance)
        APP.log(f"[BlueStacks] Đang bật instance {instance}")
        return {"ok": True}

    @route("POST", r"/api/wireless/pair")
    def api_wireless_pair(self):
        body = self._body()
        address = str(body.get("address") or "").strip()
        code = str(body.get("code") or "").strip()
        initial_serials = {d["serial"] for d in APP.devices.raw_devices(max_age=0)}
        out = APP.devices.wireless_pair(address, code)
        APP.log(f"[Wi-Fi ADB] {out}")
        host = address.split(":")[0]

        connect_addr = str(body.get("connectAddress") or "").strip()
        if connect_addr:
            try:
                msg = APP.devices.wireless_connect(connect_addr)
                APP.log(f"[Wi-Fi ADB] {msg}")
                APP.devices._last_raw_ts = 0.0
                return {"message": f"{out} · {msg}", "connected": connect_addr}
            except Exception:
                pass

        time.sleep(0.8)
        for d in APP.devices.raw_devices(max_age=0):
            ser = d["serial"]
            if d["state"] == "device" and (ser.startswith(f"{host}:") or (ser not in initial_serials and "._adb-tls-connect._tcp" in ser)):
                APP.log(f"[Wi-Fi ADB] Đã tự động kết nối qua TLS: {ser}")
                return {"message": f"{out} · Đã kết nối {ser}", "connected": ser}

        candidates = [
            s["address"] for s in APP.wireless.services(max_age=0, listen_sec=0.9)
            if s["type"] == "_adb-tls-connect._tcp" and s["host"] == host
        ]
        for candidate in dict.fromkeys(candidates):
            try:
                msg = APP.devices.wireless_connect(candidate)
                APP.log(f"[Wi-Fi ADB] {msg}")
                APP.devices._last_raw_ts = 0.0
                return {"message": f"{out} · {msg}", "connected": candidate}
            except Exception:
                continue

        try:
            msg = APP.devices.wireless_connect(host)
            APP.log(f"[Wi-Fi ADB] {msg}")
            APP.devices._last_raw_ts = 0.0
            return {"message": f"{out} · {msg}", "connected": host}
        except Exception:
            pass

        return {
            "message": f"{out} · Ghép nối thành công! Nhập cổng hiển thị ở 'Địa chỉ IP và cổng' trên điện thoại rồi bấm Kết nối.",
            "connected": "",
        }

    @route("POST", r"/api/wireless/connect")
    def api_wireless_connect(self):
        out = APP.devices.wireless_connect(str(self._body().get("address") or "").strip())
        APP.devices._last_raw_ts = 0.0
        APP.log(f"[Wi-Fi ADB] {out}")
        return {"message": out}

    @route("POST", r"/api/wireless/disconnect")
    def api_wireless_disconnect(self):
        address = str(self._body().get("address") or "").strip()
        APP.streams.stop(address, "Ngắt kết nối Wi-Fi ADB.")
        res = APP.devices.disconnect(address)
        APP.devices._last_raw_ts = 0.0
        return {"message": res}

    @route("GET", r"/api/wireless/discover")
    def api_wireless_discover(self):
        refresh = str(self.query.get("refresh") or "") in ("1", "true", "yes")
        connected = {d["serial"] for d in APP.devices.raw_devices(max_age=0 if refresh else 3.0) if d.get("state") == "device"}
        APP._discovered = APP.wireless.discovered(connected, refresh=refresh)
        if refresh:
            APP.hub.broadcast({"type": "wireless-discovered", "services": APP._discovered})
        return {"services": APP._discovered}

    @route("POST", r"/api/wireless/qr/start")
    def api_wireless_qr_start(self):
        return APP.wireless.start_qr()

    @route("POST", r"/api/wireless/qr/cancel")
    def api_wireless_qr_cancel(self):
        return APP.wireless.cancel_qr()

    @route("GET", r"/api/wireless/qr/status")
    def api_wireless_qr_status(self):
        return APP.wireless.view()

    @route("GET", r"/api/usb/diagnose")
    def api_usb_diagnose(self):
        return usb_diagnose(APP.devices.raw_devices())

    @route("POST", r"/api/adb/restart")
    def api_adb_restart(self):
        APP.streams.stop_all()
        APP.devices._last_raw_ts = 0.0
        APP.log("[ADB] Đã làm mới kết nối stream và danh sách thiết bị (an toàn cho BoxPhone).")
        return {"devices": APP.device_list(max_age=0.0)}

    # ---- AI Research Sản phẩm -------------------------------------------
    @route("GET", r"/api/research/products")
    def api_research_products(self):
        return {
            "products": APP.research.list_products(),
            "criteria": APP.research.criteria,
            "steps": RESEARCH_STEPS,
            "autoCapture": APP.auto_capture_clipboard,
            "hasLegacyResearch": config.LEGACY_RESEARCH_JSON.is_file(),
        }

    @route("POST", r"/api/research/auto-capture")
    def api_research_auto_capture(self):
        body = self._body()
        if "enabled" in body:
            APP.auto_capture_clipboard = bool(body.get("enabled"))
        else:
            APP.auto_capture_clipboard = not APP.auto_capture_clipboard
        APP.hub.broadcast({"type": "auto-capture-state", "enabled": APP.auto_capture_clipboard})
        return {"ok": True, "enabled": APP.auto_capture_clipboard}

    @route("POST", r"/api/research/products")
    def api_research_add_product(self):
        body = self._body()
        prod_id = str(body.get("id") or "").strip()
        url = str(body.get("share_url") or "").strip()
        existing = None
        if prod_id:
            existing = APP.research.get_product(prod_id)
        if not existing and url:
            existing = APP.research.check_duplicate(url)
        if existing and not body.get("force_update"):
            if not existing.get("has_details"):
                pid = existing.get("id")
                if pid:
                    threading.Thread(target=APP.auto_enrich_and_broadcast, args=(pid,), daemon=True).start()
            return {
                "ok": True,
                "duplicate": True,
                "product": existing,
                "message": f"Sản phẩm \"{existing.get('title')}\" đã có trong danh sách! (ID: {existing.get('id')})",
            }
        prod = APP.research.add_or_update(body)
        APP.hub.broadcast({
            "type": "research-update",
            "products": APP.research.list_products(),
        })
        pid = prod.get("id")
        if pid:
            threading.Thread(target=APP.auto_enrich_and_broadcast, args=(pid,), daemon=True).start()
        return {"ok": True, "duplicate": False, "product": prod}

    @route("POST", r"/api/research/paste-id-to-phone")
    def api_research_paste_id_to_phone(self):
        body = self._body()
        serial = require_serial(body.get("serial"))
        APP.devices.require_ready(serial)
        pid = str(body.get("productId") or body.get("id") or "").strip()
        if not pid:
            raise ValueError("Thiếu ID sản phẩm cần dán vào điện thoại.")
        paste = bool(body.get("paste", True))
        return APP.copy_and_paste_product_id_to_device(serial, pid, paste=paste)

    @route("POST", r"/api/research/import-legacy")
    def api_research_import_legacy(self):
        body = self._body()
        mode = str(body.get("mode") or "legacy").strip().lower()
        if mode == "pick":
            chosen = file_picker.pick_json()
            if not chosen:
                return {"ok": True, "cancelled": True}
            res = APP.research.import_from_json_path(Path(chosen))
        else:
            if not config.LEGACY_RESEARCH_JSON.is_file():
                raise FileNotFoundError("Không tìm thấy dữ liệu kho sản phẩm từ tool cũ.")
            res = APP.research.import_from_json_path(
                config.LEGACY_RESEARCH_JSON,
                src_images_dir=config.LEGACY_PRODUCT_IMAGES_DIR,
            )
        APP.hub.broadcast({
            "type": "research-update",
            "products": APP.research.list_products(),
        })
        return {"ok": True, **res}

    @route("POST", r"/api/research/criteria")
    def api_research_criteria(self):
        APP.research.criteria.update(self._body())
        APP.research.save()
        return {"ok": True, "criteria": APP.research.criteria}

    @route("POST", r"/api/research/delete")
    def api_research_delete(self):
        product_id = str(self._body().get("id") or "").strip()
        ok = APP.research.remove(product_id)
        if ok:
            APP.hub.broadcast({
                "type": "research-update",
                "products": APP.research.list_products(),
            })
        return {"ok": ok}

    @route("POST", r"/api/research/toggle-hot")
    def api_research_toggle_hot(self):
        body = self._body()
        prod_id = str(body.get("id") or "").strip()
        hot = body.get("hot")
        if hot is None:
            is_hot = APP.research.toggle_hot(prod_id)
        else:
            is_hot = APP.research.set_hot(prod_id, bool(hot))
        APP.hub.broadcast({
            "type": "research-update",
            "products": APP.research.list_products(),
        })
        return {"ok": True, "id": prod_id, "is_hot": is_hot}

    def _open_folder_desktop(self, target_path: Path):
        target_path = Path(target_path).resolve()
        target_path.mkdir(parents=True, exist_ok=True)
        target_str = str(target_path)

        if sys.platform != "win32":
            try:
                subprocess.Popen(["xdg-open", target_str])
            except Exception:
                pass
            return True

        sample_file = None
        if target_path.is_dir():
            for fname in ["thumb.webp", "info.json", "thong_tin_san_pham.txt", "Mo_Tren_Web.url"]:
                candidate = target_path / fname
                if candidate.is_file():
                    sample_file = candidate
                    break
            if not sample_file:
                for f in target_path.iterdir():
                    if f.is_file():
                        sample_file = f
                        break

        try:
            if sample_file:
                subprocess.Popen(f'explorer.exe /select,"{sample_file.resolve()}"', shell=True)
            else:
                os.startfile(target_str)
        except Exception:
            try:
                subprocess.Popen(f'explorer.exe "{target_str}"', shell=True)
            except Exception:
                pass
        return True

    @route("GET", r"/api/research/product-files")
    def api_research_product_files(self):
        prod_id = str(self.query.get("id") or "").strip()
        if not prod_id:
            return {"ok": False, "error": "Thiếu id sản phẩm"}
        p = APP.research.get_product(prod_id)
        is_hot = bool(p and p.get("is_hot"))
        hot_path = config.HOT_PRODUCTS_DIR / prod_id
        prod_path = config.PRODUCT_IMAGES_DIR / prod_id
        target_dir = hot_path if (is_hot or (hot_path.is_dir() and not prod_path.is_dir())) else prod_path

        files_list = []
        if target_dir.is_dir():
            for item in sorted(target_dir.iterdir()):
                try:
                    if item.is_file():
                        files_list.append({"name": item.name, "size": item.stat().st_size, "is_dir": False})
                    elif item.is_dir():
                        count = len(list(item.glob("*")))
                        files_list.append({"name": item.name, "is_dir": True, "count": count})
                except Exception:
                    pass
        return {
            "ok": True,
            "id": prod_id,
            "is_hot": is_hot,
            "exists": target_dir.is_dir(),
            "path": str(target_dir.resolve()),
            "files": files_list,
        }

    @route("POST", r"/api/research/open-folder")
    def api_research_open_folder(self):
        body = self._body()
        folder_type = str(body.get("type") or "").strip().lower()
        prod_id = str(body.get("id") or "").strip()

        is_hot = False
        if prod_id:
            p = APP.research.get_product(prod_id)
            is_hot = bool(p and p.get("is_hot"))
            hot_path = config.HOT_PRODUCTS_DIR / prod_id
            prod_path = config.PRODUCT_IMAGES_DIR / prod_id
            if folder_type == "hot" or is_hot or (hot_path.is_dir() and not prod_path.is_dir()):
                target_dir = hot_path
                p_obj = APP.research._cache.get(prod_id)
                if p_obj and p_obj.is_hot:
                    try:
                        APP.research._sync_hot_folder(p_obj)
                    except Exception:
                        pass
            else:
                target_dir = prod_path
            target_dir.mkdir(parents=True, exist_ok=True)
        elif folder_type == "hot":
            target_dir = config.HOT_PRODUCTS_DIR
            target_dir.mkdir(parents=True, exist_ok=True)
            try:
                APP.research.sync_all_hot()
            except Exception:
                pass
        elif folder_type in ("data", "root"):
            target_dir = config.DATA_DIR
            target_dir.mkdir(parents=True, exist_ok=True)
        else:
            target_dir = config.PRODUCT_IMAGES_DIR
            target_dir.mkdir(parents=True, exist_ok=True)

        target_str = str(target_dir.resolve())
        try:
            self._open_folder_desktop(target_dir)
            files_list = []
            if target_dir.is_dir():
                for item in sorted(target_dir.iterdir()):
                    try:
                        if item.is_file():
                            files_list.append({"name": item.name, "size": item.stat().st_size, "is_dir": False})
                        elif item.is_dir():
                            count = len(list(item.glob("*")))
                            files_list.append({"name": item.name, "is_dir": True, "count": count})
                    except Exception:
                        pass
            APP.log(f"[Explorer] Đã mở thư mục: {target_str}")
            return {"ok": True, "path": target_str, "id": prod_id, "is_hot": is_hot, "files": files_list}
        except Exception as e:
            return {"ok": False, "error": str(e), "path": target_str}

    @route("POST", r"/api/research/delete-selected")
    def api_research_delete_selected(self):
        body = self._body()
        ids = [str(x).strip() for x in (body.get("ids") or []) if str(x).strip()]
        count = APP.research.remove_multiple(ids)
        APP.hub.broadcast({
            "type": "research-update",
            "products": APP.research.list_products(),
        })
        return {"ok": True, "deleted": count, "remaining": len(APP.research.list_products())}

    @route("POST", r"/api/research/clear")
    def api_research_clear(self):
        APP.research.clear()
        APP.hub.broadcast({
            "type": "research-update",
            "products": APP.research.list_products(),
        })
        return {"ok": True}

    @route("POST", r"/api/research/restore-backup")
    def api_research_restore_backup(self):
        count = APP.research.restore_backup()
        APP.hub.broadcast({
            "type": "research-update",
            "products": APP.research.list_products(),
        })
        return {"ok": True, "restored": count}

    @route("GET", r"/api/research/export")
    def api_research_export(self):
        data = APP.research.export_excel()
        self.send_response(200)
        self.send_header("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        self.send_header("Content-Disposition", 'attachment; filename="AI_Research_TikTok_Shop.xlsx"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    @route("POST", r"/api/research/inspect-current")
    def api_research_inspect_current(self):
        body = self._body()
        serial = require_serial(body.get("serial"))
        APP.devices.require_ready(serial)
        shot_png = APP.screenshot(serial)
        ocr_words = ocr.words(shot_png)
        full_text = " ".join(w["text"] for w in ocr_words)
        extracted_id = extract_product_id(full_text)
        return {"ok": True, "ocrText": full_text[:400], "extractedId": extracted_id}

    @route("POST", r"/api/research/resolve")
    def api_research_resolve(self):
        body = self._body()
        raw = str(body.get("url") or body.get("text") or "").strip()
        if not raw:
            raise ValueError("Chưa cung cấp link hoặc text cần phân tích.")
        return resolve_tiktok_share(raw)

    @route("POST", r"/api/research/batch-add")
    def api_research_batch_add(self):
        body = self._body()
        raw = str(body.get("text") or "").strip()
        if not raw:
            raise ValueError("Nội dung dán trống.")
        source = str(body.get("source") or "Dán danh sách").strip()
        res = APP.research.batch_add_text(raw, source=source)
        APP.hub.broadcast({
            "type": "research-update",
            "products": APP.research.list_products(),
        })
        new_ids = [p["id"] for p in res.get("products", []) if p.get("id")]
        if new_ids:
            APP.start_research_batch_fetch(new_ids, method="spaiauto")
        return {
            "ok": True,
            "added": res["added"],
            "duplicates": res["duplicates"],
            "duplicate_items": res.get("duplicate_items", []),
            "count": res["added"],
            "products": res.get("products", []),
        }

    @route("POST", r"/api/research/save")
    def api_research_save(self):
        APP.research.save()
        return {"ok": True, "count": len(APP.research.list_products())}

    @route("GET", r"/api/research/products/([^/]+)/image")
    def api_research_product_image(self, product_id):
        pid = re.sub(r"[^\w\.-]", "", str(product_id).strip())
        thumb_path = config.PRODUCT_IMAGES_DIR / pid / "thumb.webp"
        if thumb_path.is_file() and thumb_path.stat().st_size > 0:
            self.send_response(200)
            self.send_header("Content-Type", "image/webp")
            self.send_header("Cache-Control", "public, max-age=86400")
            self.send_header("Content-Length", str(thumb_path.stat().st_size))
            self.end_headers()
            self.wfile.write(thumb_path.read_bytes())
            return

        p = APP.research.get_product(pid)
        if p and p.get("image_url"):
            try:
                import requests
                r = requests.get(p["image_url"], timeout=10)
                if r.status_code == 200 and r.content:
                    thumb_path.parent.mkdir(parents=True, exist_ok=True)
                    thumb_path.write_bytes(r.content)
                    self.send_response(200)
                    self.send_header("Content-Type", r.headers.get("Content-Type", "image/webp"))
                    self.send_header("Cache-Control", "public, max-age=86400")
                    self.send_header("Content-Length", str(len(r.content)))
                    self.end_headers()
                    self.wfile.write(r.content)
                    return
            except Exception:
                pass

        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" width="60" height="60" viewBox="0 0 60 60">'
            '<rect width="60" height="60" fill="#1e2430" rx="8"/>'
            '<text x="50%" y="54%" font-family="-apple-system,BlinkMacSystemFont,sans-serif" '
            'font-size="22" text-anchor="middle" dominant-baseline="middle" fill="#64748b">🛍️</text>'
            '</svg>'
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "image/svg+xml; charset=utf-8")
        self.send_header("Cache-Control", "public, max-age=3600")
        self.send_header("Content-Length", str(len(svg)))
        self.end_headers()
        self.wfile.write(svg)

    @route("GET", r"/api/research/products/([^/]+)/images/([^/]+)")
    def api_research_product_sub_image(self, product_id, filename):
        pid = re.sub(r"[^\w\.-]", "", str(product_id).strip())
        fname = re.sub(r"[^\w\.-]", "", str(filename).strip())
        img_path = config.PRODUCT_IMAGES_DIR / pid / "images" / fname
        if img_path.is_file() and img_path.stat().st_size > 0:
            self.send_response(200)
            self.send_header("Content-Type", "image/webp")
            self.send_header("Cache-Control", "public, max-age=86400")
            self.send_header("Content-Length", str(img_path.stat().st_size))
            self.end_headers()
            self.wfile.write(img_path.read_bytes())
            return
        raise FileNotFoundError(f"Không tìm thấy ảnh {fname} của sản phẩm {pid}.")

    @route("GET", r"/api/research/products/([^/]+)")
    def api_research_get_product(self, product_id):
        pid = str(product_id).strip()
        prod = APP.research.get_product(pid)
        if not prod:
            raise FileNotFoundError(f"Không tìm thấy sản phẩm {pid}")
        return {"ok": True, "product": prod}

    @route("POST", r"/api/research/products/([^/]+)/fetch-details")
    def api_research_fetch_product_details(self, product_id):
        pid = str(product_id).strip()
        body = self._body() if int(self.headers.get("Content-Length", "0") or 0) > 0 else {}
        method = str(body.get("method") or "spaiauto").strip().lower()
        prod = APP.research.enrich_product(pid, download_images=True, method=method, log_fn=APP.log)
        if not prod:
            raise RuntimeError(f"Không thể tải thông tin sản phẩm {pid} qua SPAI Auto (kiểm tra kết nối mạng hoặc ID sản phẩm).")
        APP.hub.broadcast({
            "type": "research-update",
            "products": APP.research.list_products(),
        })
        return {
            "ok": True,
            "product": prod,
            "source": prod.get("fetch_source", "spaiauto"),
            "fallback_from": prod.get("fallback_from", ""),
        }

    @route("POST", r"/api/research/batch-fetch-details")
    def api_research_batch_fetch_details(self):
        body = self._body()
        target_ids = body.get("ids") or []
        force = bool(body.get("force", False))
        method = str(body.get("method") or "spaiauto").strip().lower()
        if not target_ids:
            all_prods = APP.research.list_products()
            if force:
                target_ids = [p["id"] for p in all_prods]
            else:
                target_ids = [p["id"] for p in all_prods if not p.get("has_details") or not p.get("image_url")]
        if not target_ids:
            return {"ok": True, "message": "Tất cả sản phẩm đều đã có đầy đủ thông tin và ảnh.", "total": 0}

        started = APP.start_research_batch_fetch(target_ids, method=method)
        if not started:
            return {"ok": False, "message": "Tiến trình tải thông tin đang chạy, vui lòng chờ."}
        return {"ok": True, "total": len(target_ids), "status": APP.research_batch_status()}

    @route("POST", r"/api/research/batch-fetch-stop")
    def api_research_batch_fetch_stop(self):
        APP._batch_fetch_stop_event.set()
        return {"ok": True}

    @route("GET", r"/api/research/batch-fetch-status")
    def api_research_batch_fetch_status(self):
        return APP.research_batch_status()


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def handle_error(self, request, client_address) -> None:
        error = sys.exc_info()[1]
        if isinstance(error, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, TimeoutError)):
            return
        super().handle_error(request, client_address)


def launch_native_window(url: str) -> None:
    time.sleep(0.35)
    profile = os.path.join(tempfile.gettempdir(), "tiktok_research_phone_profile")
    candidates = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]
    for browser in candidates:
        if os.path.isfile(browser):
            try:
                subprocess.Popen([
                    browser,
                    f"--app={url}",
                    f"--user-data-dir={profile}",
                    "--proxy-bypass-list=127.0.0.1;localhost;<-loopback>",
                    "--start-maximized",
                    "--window-size=1680,960",
                    "--window-position=20,15",
                ])
                return
            except OSError:
                continue
    import webbrowser
    webbrowser.open(url)


def kill_port(port: int) -> None:
    try:
        result = subprocess.run(["netstat", "-ano"], capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
    except OSError:
        return
    me = os.getpid()
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[1].endswith(f":{port}") and "LISTENING" in line:
            try:
                pid = int(parts[-1])
            except ValueError:
                continue
            if pid > 0 and pid != me:
                subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, creationflags=CREATE_NO_WINDOW)


def main() -> None:
    global APP
    no_window = "--no-window" in sys.argv
    kill_port(config.PORT)
    time.sleep(0.25)
    APP = App()
    server = Server((config.HOST, config.PORT), Handler)
    url = f"http://{config.HOST}:{config.PORT}"
    print("\n" + "=" * 72)
    print(f"      {config.APP_NAME.upper()} v{config.APP_VERSION}")
    print("=" * 72)
    print(f"  * Giao diện:     {url}")
    print(f"  * ADB:           {config.ADB_PATH}")
    print(f"  * scrcpy-server: {config.SCRCPY_SERVER}")
    print("=" * 72 + "\n")

    def _boot_adb_and_monitors() -> None:
        if not APP.adb.available():
            APP.log(f"[CẢNH BÁO] Không thấy adb.exe tại {config.ADB_PATH}")
        else:
            APP.adb.start_server()
        threading.Thread(target=APP.monitor_loop, daemon=True, name="device-monitor").start()
        threading.Thread(target=APP.wireless_loop, daemon=True, name="wireless-monitor").start()

    threading.Thread(target=_boot_adb_and_monitors, daemon=True, name="adb-bootstrap").start()
    if not no_window:
        threading.Thread(target=launch_native_window, args=(url,), daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nĐang tắt…")
    finally:
        APP.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
