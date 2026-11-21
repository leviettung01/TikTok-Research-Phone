"""WebSocket RFC 6455 tối giản cho ThreadingHTTPServer (không cần thư viện ngoài).

Mỗi client có hàng đợi gửi riêng + thread ghi, nên một trình duyệt chậm không làm
nghẽn luồng video của thiết bị. Khi hàng đợi quá lớn, gói video bị bỏ và client
được đánh dấu `need_resync` để gửi lại config + GOP (khung hình khóa gần nhất).
"""
from __future__ import annotations

import base64
import hashlib
import json
import socket
import struct
import threading
from collections import deque

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_CLIENT_FRAME = 2 * 1024 * 1024
MAX_QUEUE_BYTES = 4 * 1024 * 1024


def accept_key(key: str) -> str:
    return base64.b64encode(hashlib.sha1((key + GUID).encode("ascii")).digest()).decode("ascii")


def encode_frame(payload: bytes, opcode: int = 2) -> bytes:
    size = len(payload)
    if size < 126:
        header = struct.pack("!BB", 0x80 | opcode, size)
    elif size < 65536:
        header = struct.pack("!BBH", 0x80 | opcode, 126, size)
    else:
        header = struct.pack("!BBQ", 0x80 | opcode, 127, size)
    return header + payload


class FrameReader:
    """Tách frame từ luồng byte (hỗ trợ mask, độ dài 7/16/64 bit, frame bị cắt)."""

    def __init__(self):
        self.buffer = bytearray()

    def feed(self, data: bytes):
        self.buffer.extend(data)
        frames = []
        while len(self.buffer) >= 2:
            first, second = self.buffer[0], self.buffer[1]
            opcode = first & 0x0F
            masked = bool(second & 0x80)
            length = second & 0x7F
            offset = 2
            if length == 126:
                if len(self.buffer) < 4:
                    break
                length = struct.unpack_from("!H", self.buffer, 2)[0]
                offset = 4
            elif length == 127:
                if len(self.buffer) < 10:
                    break
                length = struct.unpack_from("!Q", self.buffer, 2)[0]
                offset = 10
            if length > MAX_CLIENT_FRAME:
                raise ValueError("Frame WebSocket quá lớn.")
            mask = b""
            if masked:
                if len(self.buffer) < offset + 4:
                    break
                mask = bytes(self.buffer[offset:offset + 4])
                offset += 4
            if len(self.buffer) < offset + length:
                break
            payload = bytearray(self.buffer[offset:offset + length])
            del self.buffer[:offset + length]
            if masked:
                for index in range(len(payload)):
                    payload[index] ^= mask[index % 4]
            frames.append((opcode, bytes(payload), masked))
        return frames


class WsClient:
    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.queue: deque = deque()
        self.queued_bytes = 0
        self.cond = threading.Condition()
        self.closed = False
        self.need_resync = False
        self.writer = threading.Thread(target=self._write_loop, daemon=True, name="ws-writer")
        self.writer.start()

    def send(self, payload: bytes | str | dict, droppable: bool = False) -> None:
        if isinstance(payload, dict):
            payload = json.dumps(payload, ensure_ascii=False)
        if isinstance(payload, str):
            frame = encode_frame(payload.encode("utf-8"), 1)
        else:
            frame = encode_frame(payload, 2)
        with self.cond:
            if self.closed:
                return
            if droppable and self.queued_bytes + len(frame) > MAX_QUEUE_BYTES:
                # Bỏ bớt các gói video cũ ở đầu hàng đợi để nhường chỗ cho khung hình mới nhất theo thời gian thực
                while self.queue and (self.queued_bytes + len(frame) > MAX_QUEUE_BYTES):
                    found_idx = -1
                    for idx, item in enumerate(self.queue):
                        if item[1]:  # droppable
                            found_idx = idx
                            break
                    if found_idx >= 0:
                        dropped_frame, _ = self.queue[found_idx]
                        self.queued_bytes -= len(dropped_frame)
                        del self.queue[found_idx]
                    else:
                        break
                if self.queued_bytes + len(frame) > MAX_QUEUE_BYTES:
                    return
            self.queue.append((frame, droppable))
            self.queued_bytes += len(frame)
            self.cond.notify()

    def pong(self, payload: bytes) -> None:
        with self.cond:
            if self.closed:
                return
            frame = encode_frame(payload[:125], 10)
            self.queue.appendleft((frame, False))
            self.queued_bytes += len(frame)
            self.cond.notify()

    def _write_loop(self) -> None:
        while True:
            with self.cond:
                while not self.queue and not self.closed:
                    self.cond.wait()
                if self.closed:
                    return
                frame, _ = self.queue.popleft()
                self.queued_bytes -= len(frame)
            try:
                self.sock.sendall(frame)
            except OSError:
                self.close()
                return

    def close(self) -> None:
        with self.cond:
            if self.closed:
                return
            self.closed = True
            self.cond.notify_all()
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


class WsHub:
    def __init__(self):
        self.clients: set[WsClient] = set()
        self.lock = threading.Lock()
        self.on_connect = None  # callable(client)
        self.on_message = None  # callable(client, dict)
        self.resync_provider = None  # callable() -> list[bytes]

    def broadcast(self, message: dict) -> None:
        text = json.dumps(message, ensure_ascii=False)
        for client in self._snapshot():
            client.send(text)

    def broadcast_binary(self, payload: bytes) -> None:
        for client in self._snapshot():
            client.send(payload, droppable=True)

    def _snapshot(self) -> list[WsClient]:
        with self.lock:
            dead = [c for c in self.clients if c.closed]
            for c in dead:
                self.clients.discard(c)
            return list(self.clients)

    def serve(self, handler) -> None:
        """Gọi trong do_GET của BaseHTTPRequestHandler khi path là /ws/..."""
        key = handler.headers.get("Sec-WebSocket-Key", "")
        if handler.headers.get("Upgrade", "").lower() != "websocket" or not key:
            handler.send_error(400, "Expected WebSocket")
            return
        origin = handler.headers.get("Origin", "")
        host = handler.headers.get("Host", "")
        if origin and not (origin.endswith("//" + host)):
            # Chặn trang web lạ mở WebSocket tới tool (điều khiển điện thoại).
            handler.send_error(403, "Origin not allowed")
            return
        handler.send_response(101, "Switching Protocols")
        handler.send_header("Upgrade", "websocket")
        handler.send_header("Connection", "Upgrade")
        handler.send_header("Sec-WebSocket-Accept", accept_key(key))
        handler.end_headers()
        handler.wfile.flush()
        sock = handler.connection
        sock.settimeout(None)
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        client = WsClient(sock)
        with self.lock:
            self.clients.add(client)
        if self.on_connect:
            try:
                self.on_connect(client)
            except Exception as exc:  # noqa: BLE001
                client.send({"type": "error", "message": str(exc)})
        reader = FrameReader()
        try:
            while not client.closed:
                data = sock.recv(65536)
                if not data:
                    break
                for opcode, payload, masked in reader.feed(data):
                    if opcode == 8:
                        raise ConnectionResetError
                    if opcode == 9:
                        client.pong(payload)
                        continue
                    if opcode != 1 or not masked:
                        continue
                    try:
                        message = json.loads(payload.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    if self.on_message:
                        try:
                            self.on_message(client, message)
                        except Exception as exc:  # noqa: BLE001
                            client.send({"type": "control-error", "message": str(exc)})
        except (OSError, ValueError, ConnectionResetError):
            pass
        finally:
            client.close()
            with self.lock:
                self.clients.discard(client)
        handler.close_connection = True
