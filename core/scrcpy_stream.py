"""Stream màn hình realtime + điều khiển qua scrcpy-server (giao thức scrcpy 4.x).

Luồng: push scrcpy-server → adb forward tcp:<port> localabstract:scrcpy_<scid>
→ chạy app_process trên máy → socket video (dummy byte 0) + socket control.
Gói video gửi lên trình duyệt: [1][len serial][serial][flags][pts 8B][data].
flags: bit7 = session (đổi kích thước), bit6 = config (SPS/PPS), bit5 = keyframe.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import secrets
import socket
import struct
import threading
import time
from pathlib import Path
from typing import Callable

from core.adb import Adb, AdbError

REMOTE_JAR = "/data/local/tmp/scrcpy-tiktokresearch.jar"
GOP_LIMIT = 8 * 1024 * 1024
KEYCODES = {
    "back": 4, "home": 3, "overview": 187, "enter": 66, "del": 67, "space": 62,
    "vol_up": 24, "vol_down": 25, "mute": 164,
}
TOUCH_ACTIONS = {"down": 0, "up": 1, "move": 2, "cancel": 3}


# ---- Phát âm thanh trực tiếp ra Loa máy tính (Windows WaveOut 48kHz Stereo) ----
class _WAVEFORMATEX(ctypes.Structure):
    _fields_ = [
        ("wFormatTag", wintypes.WORD),
        ("nChannels", wintypes.WORD),
        ("nSamplesPerSec", wintypes.DWORD),
        ("nAvgBytesPerSec", wintypes.DWORD),
        ("nBlockAlign", wintypes.WORD),
        ("wBitsPerSample", wintypes.WORD),
        ("cbSize", wintypes.WORD),
    ]


class _WAVEHDR(ctypes.Structure):
    pass


_WAVEHDR._fields_ = [
    ("lpData", ctypes.c_char_p),
    ("dwBufferLength", wintypes.DWORD),
    ("dwBytesRecorded", wintypes.DWORD),
    ("dwUser", ctypes.c_size_t),
    ("dwFlags", wintypes.DWORD),
    ("dwLoops", wintypes.DWORD),
    ("lpNext", ctypes.POINTER(_WAVEHDR)),
    ("reserved", ctypes.c_size_t),
]


class WinmmWaveOutPlayer:
    """Bộ phát âm thanh PCM 48000Hz 16-bit Stereo trực tiếp ra loa Windows độ trễ cực thấp."""
    WAVE_MAPPER = 0xFFFFFFFF
    WAVE_FORMAT_PCM = 1
    WHDR_DONE = 0x00000001
    WHDR_PREPARED = 0x00000002
    NUM_BUFFERS = 24
    MAX_BUF_SIZE = 32768

    def __init__(self):
        self.lock = threading.Lock()
        self.winmm = getattr(ctypes, "windll", None).winmm if os.name == "nt" else None
        self.hwave = wintypes.HANDLE(0)
        self.buffers = [ctypes.create_string_buffer(self.MAX_BUF_SIZE) for _ in range(self.NUM_BUFFERS)]
        self.headers = [_WAVEHDR() for _ in range(self.NUM_BUFFERS)]
        self.prepared = [False] * self.NUM_BUFFERS
        self.idx = 0

    def _ensure_open(self) -> bool:
        if not self.winmm:
            return False
        if self.hwave.value:
            return True
        wfx = _WAVEFORMATEX(
            wFormatTag=self.WAVE_FORMAT_PCM,
            nChannels=2,
            nSamplesPerSec=48000,
            nAvgBytesPerSec=48000 * 4,
            nBlockAlign=4,
            wBitsPerSample=16,
            cbSize=0,
        )
        res = self.winmm.waveOutOpen(ctypes.byref(self.hwave), self.WAVE_MAPPER, ctypes.byref(wfx), 0, 0, 0)
        return res == 0 and bool(self.hwave.value)

    def feed(self, pcm: bytes) -> None:
        if not pcm or not self.winmm:
            return
        with self.lock:
            if not self._ensure_open():
                return
            # Kiểm tra số buffer đang phát để tránh trễ tiếng
            in_flight = sum(
                1 for i in range(self.NUM_BUFFERS)
                if self.prepared[i] and not (self.headers[i].dwFlags & self.WHDR_DONE)
            )
            if in_flight > 16:
                return

            slot = self.idx
            hdr = self.headers[slot]
            if self.prepared[slot]:
                if not (hdr.dwFlags & self.WHDR_DONE):
                    return
                self.winmm.waveOutUnprepareHeader(self.hwave, ctypes.byref(hdr), ctypes.sizeof(_WAVEHDR))
                self.prepared[slot] = False

            n = min(len(pcm), self.MAX_BUF_SIZE)
            buf = self.buffers[slot]
            ctypes.memmove(buf, pcm, n)
            hdr.lpData = ctypes.cast(buf, ctypes.c_char_p)
            hdr.dwBufferLength = n
            hdr.dwFlags = 0
            hdr.dwLoops = 0
            if self.winmm.waveOutPrepareHeader(self.hwave, ctypes.byref(hdr), ctypes.sizeof(_WAVEHDR)) == 0:
                self.prepared[slot] = True
                self.winmm.waveOutWrite(self.hwave, ctypes.byref(hdr), ctypes.sizeof(_WAVEHDR))
                self.idx = (slot + 1) % self.NUM_BUFFERS

    def reset(self) -> None:
        if not self.winmm:
            return
        with self.lock:
            if not self.hwave.value:
                return
            try:
                self.winmm.waveOutReset(self.hwave)
                for i in range(self.NUM_BUFFERS):
                    if self.prepared[i]:
                        self.winmm.waveOutUnprepareHeader(
                            self.hwave, ctypes.byref(self.headers[i]), ctypes.sizeof(_WAVEHDR)
                        )
                        self.prepared[i] = False
            except Exception:
                pass


def free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _coordinate(value, name: str, maximum: int = 10000) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"Toạ độ {name} không hợp lệ.") from None
    if number != float(value) or number < 0 or number > maximum:
        raise ValueError(f"Toạ độ {name} không hợp lệ.")
    return number


def touch_packet(action: str, x, y, width, height) -> bytes:
    if action not in TOUCH_ACTIONS:
        raise ValueError("Sự kiện chạm không hợp lệ.")
    px, py = _coordinate(x, "x"), _coordinate(y, "y")
    w, h = _coordinate(width, "width", 65535), _coordinate(height, "height", 65535)
    pressure = 0 if action in ("up", "cancel") else 0xFFFF
    # type, action, pointerId(-2 = generic finger), x, y, w, h, pressure, actionButton, buttons
    return struct.pack("!BBQiiHHHII", 2, TOUCH_ACTIONS[action], 0xFFFFFFFFFFFFFFFE, px, py, w, h, pressure, 0, 0)


def key_packets(key: str | None = None, keycode: int | None = None) -> list[bytes]:
    code = KEYCODES.get(str(key or "")) if keycode is None else int(keycode)
    if code is None or not 0 <= code <= 300:
        raise ValueError("Phím điều khiển không hợp lệ.")
    return [struct.pack("!BBIII", 0, action, code, 0, 0) for action in (0, 1)]


def _chunks(text: str, limit: int):
    chunk, size = [], 0
    for character in text:
        length = len(character.encode("utf-8"))
        if size + length > limit and chunk:
            yield "".join(chunk)
            chunk, size = [], 0
        chunk.append(character)
        size += length
    if chunk:
        yield "".join(chunk)


def text_packets(text: str) -> list[bytes]:
    packets = []
    for part in _chunks(str(text or ""), 300):
        payload = part.encode("utf-8")
        packets.append(struct.pack("!BI", 1, len(payload)) + payload)
    return packets


def clipboard_packets(text: str, paste: bool = True) -> list[bytes]:
    packets = []
    for part in _chunks(str(text or ""), 250 * 1024):
        payload = part.encode("utf-8")
        packets.append(struct.pack("!BQBI", 9, 0, 1 if paste else 0, len(payload)) + payload)
    return packets


def wrap_video_packet(serial: str, packet: bytes) -> bytes:
    serial_bytes = serial.encode("utf-8")[:255]
    return bytes([1, len(serial_bytes)]) + serial_bytes + packet


def wrap_audio_packet(serial: str, pcm: bytes) -> bytes:
    serial_bytes = serial.encode("utf-8")[:255]
    return bytes([2, len(serial_bytes)]) + serial_bytes + pcm


class PacketParser:
    """Tách gói scrcpy từ luồng TCP. Tách riêng để test không cần thiết bị."""

    def __init__(self):
        self.buffer = bytearray()
        self.codec: str | None = None
        self.width = 0
        self.height = 0
        self.config_packet: bytes | None = None
        self.last_key_packet: bytes | None = None
        self.gop: list[bytes] = []
        self.gop_bytes = 0

    def feed(self, data: bytes):
        """Trả về list sự kiện: ('codec', str) | ('size', (w, h)) | ('packet', bytes)."""
        self.buffer.extend(data)
        events = []
        while self.buffer:
            if self.codec is None:
                if len(self.buffer) < 4:
                    break
                self.codec = bytes(self.buffer[:4]).decode("ascii", errors="replace")
                del self.buffer[:4]
                events.append(("codec", self.codec))
                continue
            if len(self.buffer) < 12:
                break
            flags = self.buffer[0]
            if flags & 0x80:
                self.width, self.height = struct.unpack_from("!II", self.buffer, 4)
                del self.buffer[:12]
                events.append(("size", (self.width, self.height)))
                continue
            size = struct.unpack_from("!I", self.buffer, 8)[0]
            if len(self.buffer) < 12 + size:
                break
            pts = struct.unpack_from("!Q", self.buffer, 0)[0] & 0x1FFFFFFFFFFFFFFF
            packet = bytes([flags]) + struct.pack("!Q", pts) + bytes(self.buffer[12:12 + size])
            del self.buffer[:12 + size]
            if flags & 0x40:
                self.config_packet = packet
                self.gop, self.gop_bytes = [], 0
            elif flags & 0x20:
                self.last_key_packet = packet
                self.gop, self.gop_bytes = [packet], len(packet)
            elif self.gop:
                self.gop_bytes += len(packet)
                if self.gop_bytes <= GOP_LIMIT:
                    self.gop.append(packet)
                else:
                    self.gop, self.gop_bytes = [], 0
            events.append(("packet", packet))
        return events


class ScrcpyStream:
    def __init__(self, adb: Adb, serial: str, server_path: Path, version: str, video_mode: str,
                 on_packet: Callable[[str, bytes], None], on_event: Callable[[dict], None],
                 on_audio: Callable[[str, bytes], None] | None = None):
        self.adb = adb
        self.serial = serial
        self.server_path = Path(server_path)
        self.version = version
        self.video_mode = video_mode
        self.on_packet = on_packet
        self.on_event = on_event
        self.on_audio = on_audio
        self.port = 0
        self.scid = ""
        self.process = None
        self.video_sock: socket.socket | None = None
        self.audio_sock: socket.socket | None = None
        self.control_sock: socket.socket | None = None
        self.control_lock = threading.RLock()
        self.parser = PacketParser()
        self.last_clipboard = ""
        self.audio_available = False
        self.running = False
        self.started_at = 0.0
        self._last_reset_at = 0.0


    # ---- lifecycle ------------------------------------------------------
    def _connect(self, expect_dummy: bool, deadline: float) -> socket.socket:
        while time.time() < deadline:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(1.0)
            try:
                sock.connect(("127.0.0.1", self.port))
                if expect_dummy:
                    first = sock.recv(1)
                    if first != b"\x00":
                        raise OSError("scrcpy chưa sẵn sàng")
                sock.settimeout(None)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                return sock
            except OSError:
                sock.close()
                time.sleep(0.25)
        raise TimeoutError("scrcpy không phản hồi trong 10 giây.")

    def start(self) -> None:
        if not self.server_path.is_file():
            raise FileNotFoundError(f"Không thấy scrcpy-server: {self.server_path}")
        # Chỉ dọn dẹp tiến trình scrcpy-tiktokresearch.jar cũ của chính tool này,
        # tuyệt đối không pkill -f scrcpy chung để tránh tắt XWCaptureScreen.jar của BoxPhone chạy song song!
        try:
            self.adb.shell(self.serial, "pkill -f scrcpy-tiktokresearch.jar", check=False)
        except Exception:
            pass
        self.port = free_tcp_port()
        # scid là số hex 31 bit có dấu: giữ bit cao = 0, đúng 8 chữ số.
        self.scid = f"{secrets.randbelow(0x7FFFFFFE) + 1:08x}"
        # Chỉ push scrcpy-server nếu chưa có trên máy (giảm 1 lệnh ADB khi restart stream)
        local_size = self.server_path.stat().st_size
        try:
            remote_size_str = self.adb.shell(self.serial, f"stat -c %s {REMOTE_JAR}", timeout=3, check=False)
            remote_size = int(remote_size_str.strip()) if remote_size_str.strip().isdigit() else -1
        except Exception:
            remote_size = -1
        if remote_size != local_size:
            self.adb.run(["push", str(self.server_path), REMOTE_JAR], self.serial, timeout=30)
        self.adb.run(["forward", f"tcp:{self.port}", f"localabstract:scrcpy_{self.scid}"], self.serial, timeout=10)
        if self.video_mode == "compatible":
            video = "max_size=1024 max_fps=25 video_bit_rate=2500000 video_codec=h264"
        else:
            video = "max_size=1280 max_fps=30 video_bit_rate=3000000 video_codec=h264"
        command = (f"CLASSPATH={REMOTE_JAR} app_process / com.genymobile.scrcpy.Server {self.version} "
                   f"scid={self.scid} log_level=warn tunnel_forward=true audio=false control=true cleanup=false "
                   f"send_device_meta=false send_dummy_byte=true {video}")
        self.process = self.adb.popen(["shell", command], self.serial)
        threading.Thread(target=self._stderr_loop, daemon=True, name=f"scrcpy-err-{self.serial}").start()
        time.sleep(0.6)
        deadline = time.time() + 10
        try:
            self.video_sock = self._connect(True, deadline)
            # audio=false → scrcpy chỉ mở 2 socket (video + control), KHÔNG mở audio socket
            self.audio_sock = None
            self.control_sock = self._connect(False, deadline)
        except Exception:
            self.stop("Không khởi động được scrcpy.", emit=False)
            raise
        self.running = True
        self.started_at = time.time()
        threading.Thread(target=self._read_loop, daemon=True, name=f"scrcpy-video-{self.serial}").start()
        # audio=false → không cần audio loop
        threading.Thread(target=self._drain_control, daemon=True, name=f"scrcpy-ctl-{self.serial}").start()

    def _stderr_loop(self) -> None:
        proc = self.process
        if not proc or not proc.stderr:
            return
        for raw in iter(proc.stderr.readline, b""):
            message = raw.decode("utf-8", errors="replace").strip()
            if message:
                self.on_event({"type": "stream-log", "serial": self.serial, "message": message[:300]})

    def _drain_control(self) -> None:
        # Kênh control nhận tin từ thiết bị (clipboard, ack...).
        sock = self.control_sock
        buf = bytearray()
        try:
            while self.running and sock:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf.extend(chunk)
                while len(buf) >= 5:
                    msg_type = buf[0]
                    if msg_type == 0:  # DEVICE_MSG_TYPE_CLIPBOARD
                        text_len = int.from_bytes(buf[1:5], "big")
                        if len(buf) < 5 + text_len:
                            break  # Chờ thêm dữ liệu
                        text = bytes(buf[5:5 + text_len]).decode("utf-8", errors="replace")
                        del buf[:5 + text_len]
                        self.last_clipboard = text
                        self.on_event({"type": "clipboard", "serial": self.serial, "text": text})
                    elif msg_type == 1:  # DEVICE_MSG_TYPE_ACK_CLIPBOARD
                        if len(buf) < 9:
                            break
                        del buf[:9]
                    elif msg_type == 2:  # DEVICE_MSG_TYPE_UHID_OUTPUT
                        if len(buf) < 5:
                            break
                        uhid_len = int.from_bytes(buf[3:5], "big")
                        if len(buf) < 5 + uhid_len:
                            break
                        del buf[:5 + uhid_len]
                    else:
                        del buf[:1]
        except OSError:
            pass


    def _recv_exact(self, sock: socket.socket, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n and self.running:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                return b""
            buf.extend(chunk)
        return bytes(buf)

    def _read_audio_loop(self) -> None:
        sock = self.audio_sock
        if not sock:
            return
        try:
            codec_meta = self._recv_exact(sock, 4)
            if len(codec_meta) < 4 or codec_meta == b"\x00\x00\x00\x00":
                self.audio_available = False
                return
            self.audio_available = True
            while self.running:
                hdr = self._recv_exact(sock, 12)
                if len(hdr) < 12:
                    break
                pts_flags, size = struct.unpack("!QI", hdr)
                if size <= 0 or size > 262144:
                    continue
                pcm = self._recv_exact(sock, size)
                if len(pcm) < size:
                    break
                if self.on_audio:
                    self.on_audio(self.serial, pcm)
        except OSError:
            pass

    def _read_loop(self) -> None:
        sock = self.video_sock
        reason = "Kết nối video đã đóng."
        try:
            while self.running and sock:
                data = sock.recv(262144)
                if not data:
                    break
                for kind, value in self.parser.feed(data):
                    if kind == "packet":
                        self.on_packet(self.serial, wrap_video_packet(self.serial, value))
                    elif kind == "size":
                        self.on_event({"type": "stream-size", "serial": self.serial, "width": value[0], "height": value[1]})
                    elif kind == "codec":
                        self.on_event({"type": "stream-config", "serial": self.serial, "codec": value})
        except OSError as exc:
            reason = f"Kết nối video lỗi: {exc}"
        if self.running:
            self.stop(reason)

    def stop(self, reason: str = "Đã dừng stream.", emit: bool = True) -> None:
        was_running = self.running
        self.running = False
        for sock in (self.video_sock, self.audio_sock, self.control_sock):
            if sock:
                try:
                    sock.close()
                except OSError:
                    pass
        if self.process:
            try:
                self.process.kill()
            except OSError:
                pass
        if self.scid:
            try:
                self.adb.shell(self.serial, f"pkill -f scid={self.scid}", check=False)
            except Exception:
                pass
        if self.port:
            try:
                self.adb.run(["forward", "--remove", f"tcp:{self.port}"], self.serial, timeout=6, check=False)
            except AdbError:
                pass
        if emit and (was_running or reason):
            self.on_event({"type": "stream-ended", "serial": self.serial, "message": reason})

    # ---- control --------------------------------------------------------
    @property
    def controllable(self) -> bool:
        return self.running and self.control_sock is not None

    def send(self, packets: list[bytes]) -> None:
        if not self.controllable:
            raise RuntimeError("Kênh điều khiển scrcpy chưa sẵn sàng.")
        with self.control_lock:
            for packet in packets:
                self.control_sock.sendall(packet)

    def handle_message(self, message: dict) -> None:
        event = message.get("event")
        if event == "touch":
            self.send([touch_packet(message.get("action"), message.get("x"), message.get("y"),
                                    message.get("width"), message.get("height"))])
        elif event == "key":
            self.send(key_packets(message.get("key")))
        elif event == "keycode":
            self.send(key_packets(keycode=message.get("keycode")))
        elif event == "text":
            self.send(text_packets(message.get("text", "")))
        elif event == "paste":
            self.send(clipboard_packets(message.get("text", "")))
        elif event == "set_clipboard":
            self.set_clipboard(message.get("text", ""), paste=bool(message.get("paste", False)))
        elif event == "get_clipboard":
            self.request_clipboard()
        elif event == "reset_video":
            self.reset_video()
        else:
            raise ValueError("Lệnh điều khiển không hợp lệ.")

    def request_clipboard(self) -> None:
        if self.controllable:
            try:
                self.send([bytes([8, 0])])
            except Exception:
                pass


    def reset_video(self) -> bool:
        """Yêu cầu encoder scrcpy trên điện thoại phát ngay một khung hình khóa (IDR keyframe)."""
        if not self.controllable:
            return False
        now = time.time()
        # Không gửi lệnh 0x11 (RESET_VIDEO) khi encoder chưa phát xong SPS/PPS đầu tiên
        # hoặc quá sát lúc vừa khởi động (tránh làm scrcpy-server 4.1 trên Android 13 đóng luồng video ngay ở t=0).
        if not self.parser.config_packet or (now - self.started_at) < 1.5:
            return False
        if (now - self._last_reset_at) < 1.5:
            return False
        try:
            with self.control_lock:
                self.control_sock.sendall(b"\x11")
                self._last_reset_at = now
            return True
        except Exception:
            return False

    def tap_device(self, x: int, y: int, device_w: int, device_h: int, hold: float = 0.072) -> bool:
        """Chạm theo toạ độ vật lý (UI dump) → đổi sang toạ độ video đã scale."""
        if not self.controllable or not self.parser.width:
            return False
        vw, vh = self.parser.width, self.parser.height
        sx = max(0, min(vw, round(x * vw / max(1, device_w))))
        sy = max(0, min(vh, round(y * vh / max(1, device_h))))
        self.send([touch_packet("down", sx, sy, vw, vh)])
        time.sleep(hold)
        self.send([touch_packet("up", sx, sy, vw, vh)])
        return True

    def paste(self, text: str) -> bool:
        if not self.controllable:
            return False
        self.send(clipboard_packets(text))
        return True

    def set_clipboard(self, text: str, paste: bool = False) -> bool:
        """Nạp trực tiếp vào Clipboard của điện thoại qua scrcpy control socket."""
        if not self.controllable:
            return False
        self.send(clipboard_packets(text, paste=paste))
        return True

    def state(self) -> dict:
        return {
            "serial": self.serial, "codec": self.parser.codec or "h264", "videoMode": self.video_mode,
            "width": self.parser.width or None, "height": self.parser.height or None,
            "hasConfig": self.parser.config_packet is not None, "controllable": self.controllable,
            "running": self.running,
        }

    def resync_packets(self) -> list[bytes]:
        packets = []
        if self.parser.config_packet:
            packets.append(wrap_video_packet(self.serial, self.parser.config_packet))
        if self.parser.last_key_packet:
            packets.append(wrap_video_packet(self.serial, self.parser.last_key_packet))
        elif self.parser.gop:
            packets.append(wrap_video_packet(self.serial, self.parser.gop[0]))
        return packets


class StreamManager:
    def __init__(self, adb: Adb, server_path: Path, version: str, hub, video_mode_for: Callable[[str], str], logger=print,
                 on_clipboard: Callable[[str, str], None] | None = None):
        self.adb = adb
        self.server_path = server_path
        self.version = version
        self.hub = hub
        self.video_mode_for = video_mode_for
        self.log = logger
        self.on_clipboard = on_clipboard
        self.streams: dict[str, ScrcpyStream] = {}
        self.starting: dict[str, threading.Event] = {}
        self.failures: dict[str, tuple[float, int]] = {}
        self.lock = threading.RLock()
        self.audio_enabled: bool = True
        self.audio_serial: str = ""
        self.pc_player = WinmmWaveOutPlayer()
        self._unmuted_serials: set[str] = set()

    def _ensure_device_media_volume(self, serial: str) -> None:
        if not serial or serial in self._unmuted_serials:
            return
        self._unmuted_serials.add(serial)
        def _boost():
            try:
                self.adb.shell(serial, "cmd", "media_session", "volume", "--stream", "3", "--set", "11", timeout=3, check=False)
            except Exception:
                pass
        threading.Thread(target=_boost, daemon=True, name=f"audio-vol-{serial}").start()

    def _on_audio(self, serial: str, pcm: bytes) -> None:
        # Audio capture vẫn bật để giữ tương thích scrcpy v4, nhưng KHÔNG phát ra loa PC
        # và KHÔNG broadcast lên web để tránh xung đột với Fake Camera / TikTok Live.
        # Chỉ drain dữ liệu âm thanh để socket không bị nghẽn.
        pass

    def audio_state(self) -> dict:
        return {
            "type": "audio-state",
            "enabled": self.audio_enabled,
            "serial": self.audio_serial,
        }

    def set_audio(self, serial: str = "", enabled: bool | None = None, volume_step: int = 0) -> dict:
        if serial:
            if serial != self.audio_serial:
                self.pc_player.reset()
            self.audio_serial = serial
        if enabled is not None:
            self.audio_enabled = bool(enabled)
            if not self.audio_enabled:
                self.pc_player.reset()
        target = serial or self.audio_serial
        if self.audio_enabled and target:
            self._ensure_device_media_volume(target)
        if volume_step != 0 and target:
            keycode = 24 if volume_step > 0 else 25
            stream = self.get(target)
            if stream and stream.controllable:
                try:
                    stream.send(key_packets(keycode=keycode))
                except Exception:
                    pass
            else:
                try:
                    self.adb.shell(target, "input", "keyevent", str(keycode), timeout=3, check=False)
                except Exception:
                    pass
        state = self.audio_state()
        self.hub.broadcast(state)
        return state

    def _on_event(self, message: dict) -> None:
        if message.get("type") == "stream-ended":
            with self.lock:
                stream = self.streams.get(message["serial"])
                if stream and not stream.running:
                    self.streams.pop(message["serial"], None)
            self.hub.broadcast(message)
            self.hub.broadcast(self.state_message())
        else:
            if message.get("type") == "clipboard" and callable(self.on_clipboard):
                try:
                    self.on_clipboard(message.get("serial", ""), message.get("text", ""))
                except Exception as e:
                    self.log(f"Lỗi hook clipboard: {e}")
            self.hub.broadcast(message)

    def _on_packet(self, _serial: str, packet: bytes) -> None:
        self.hub.broadcast_binary(packet)

    def get(self, serial: str) -> ScrcpyStream | None:
        with self.lock:
            stream = self.streams.get(serial)
            return stream if stream and stream.running else None

    def set_clipboard(self, serial: str, text: str, paste: bool = False) -> bool:
        with self.lock:
            stream = self.streams.get(serial)
        if stream and stream.running and stream.controllable:
            try:
                return stream.set_clipboard(text, paste=paste)
            except Exception as e:
                self.log(f"Lỗi set_clipboard cho {serial}: {e}")
        return False

    def start(self, serial: str) -> dict:
        already_running = False
        with self.lock:
            if serial in self.streams and self.streams[serial].running:
                already_running = True
            else:
                waiter = self.starting.get(serial)
                owner = waiter is None
                if owner:
                    waiter = threading.Event()
                    self.starting[serial] = waiter
        if already_running:
            return self.state_message()
        if not owner:
            waiter.wait(15)
            return self.state_message()
        try:
            stream = ScrcpyStream(self.adb, serial, self.server_path, self.version, self.video_mode_for(serial),
                                  self._on_packet, self._on_event, on_audio=self._on_audio)
            stream.start()
            with self.lock:
                self.streams[serial] = stream
                self.failures.pop(serial, None)
            if not self.audio_serial:
                self.audio_serial = serial
            if self.audio_enabled and self.audio_serial == serial:
                self._ensure_device_media_volume(serial)
            self.hub.broadcast(self.state_message(message=f"Đang nhận video & âm thanh realtime từ {serial}."))
            self.hub.broadcast(self.audio_state())
            return self.state_message()
        except Exception:
            with self.lock:
                _, count = self.failures.get(serial, (0, 0))
                self.failures[serial] = (time.time(), count + 1)
            raise
        finally:
            with self.lock:
                self.starting.pop(serial, None)
            waiter.set()

    def may_autostart(self, serial: str) -> bool:
        """Lùi dần thời gian thử lại khi scrcpy lỗi liên tục (5s, 10s, 20s… tối đa 120s)."""
        with self.lock:
            if serial in self.streams or serial in self.starting:
                return False
            last, count = self.failures.get(serial, (0, 0))
        return time.time() - last >= min(120, 5 * (2 ** max(0, count - 1))) if count else True

    def stop(self, serial: str, reason: str = "Đã dừng stream.") -> None:
        with self.lock:
            stream = self.streams.pop(serial, None)
        if stream:
            stream.stop(reason)
            self.hub.broadcast(self.state_message())

    def stop_all(self) -> None:
        for serial in list(self.streams):
            self.stop(serial, "Tool đang tắt.")

    def restart(self, serial: str, reason: str) -> dict:
        self.stop(serial, reason)
        # MediaCodec trên Samsung/Realme giải phóng bất đồng bộ; bật lại ngay dễ crash encoder.
        time.sleep(1.8)
        return self.start(serial)

    def state(self) -> dict:
        with self.lock:
            return {serial: stream.state() for serial, stream in self.streams.items() if stream.running}

    def state_message(self, **extra) -> dict:
        streams = list(self.state().values())
        return {"type": "stream-state", "active": bool(streams), "streams": streams, **extra}

    def resync_packets(self) -> list[bytes]:
        with self.lock:
            streams = [s for s in self.streams.values() if s.running]
        packets = []
        for stream in streams:
            packets.extend(stream.resync_packets())
        return packets

    def greet(self, client) -> None:
        """Gửi trạng thái + config/GOP cho client vừa kết nối để hiện hình ngay."""
        client.send(self.state_message())
        client.send(self.audio_state())
        with self.lock:
            streams = [s for s in self.streams.values() if s.running]
        for stream in streams:
            client.send({"type": "stream-config", "serial": stream.serial, "codec": stream.parser.codec or "h264"})
            if stream.parser.width:
                client.send({"type": "stream-size", "serial": stream.serial,
                             "width": stream.parser.width, "height": stream.parser.height})
            for packet in stream.resync_packets():
                client.send(packet)
            stream.reset_video()

    def reset_video(self, serial: str) -> bool:
        with self.lock:
            stream = self.streams.get(serial)
        return stream.reset_video() if stream else False
