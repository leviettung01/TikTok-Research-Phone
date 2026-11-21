"""Wrapper ADB dùng chung: timeout rõ ràng, ẩn cửa sổ console, lỗi có thông điệp tiếng Việt."""
from __future__ import annotations

import os
import subprocess
import threading
from pathlib import Path
from typing import Iterable, Sequence

# Ép ADB dùng Google Openscreen mDNS engine thay vì Bonjour/legacy cũ (vốn thường lỗi trên Windows nhiều card mạng)
os.environ.setdefault("ADB_MDNS_OPENSCREEN", "1")
os.environ["ADB_MDNS_OPENSCREEN"] = "1"


CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


def _startupinfo():
    if hasattr(subprocess, "STARTUPINFO"):
        si = subprocess.STARTUPINFO()
        si.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 1)
        si.wShowWindow = 0  # SW_HIDE
        return si
    return None


class AdbError(RuntimeError):
    def __init__(self, message: str, *, returncode: int | None = None, stdout: str = "", stderr: str = ""):
        super().__init__(message)
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _decode(value: bytes | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return value.decode("utf-8", errors="replace")


class Adb:
    """Gọi adb.exe. Thread-safe, giới hạn đồng thời để tránh xung đột với BoxPhone/XiaoWei."""

    # Giới hạn tối đa 5 lệnh ADB chạy đồng thời (tránh nghẽn ADB server khi cùng dùng với XiaoWei)
    _global_semaphore = threading.Semaphore(5)

    def __init__(self, path: str | Path):
        self.path = str(path)
        self._server_lock = threading.Lock()

    def available(self) -> bool:
        return Path(self.path).is_file()

    def command(self, args: Sequence[str], serial: str | None = None) -> list[str]:
        cmd = [self.path]
        if serial:
            cmd += ["-s", serial]
        cmd += [str(a) for a in args]
        return cmd

    def run(self, args: Sequence[str], serial: str | None = None, *, timeout: float = 20,
            binary: bool = False, check: bool = True, input_bytes: bytes | None = None):
        cmd = self.command(args, serial)
        si = _startupinfo()
        # Giới hạn số lệnh ADB đồng thời để tránh nghẽn ADB server (chung với BoxPhone)
        acquired = self._global_semaphore.acquire(timeout=timeout + 5)
        if not acquired:
            raise AdbError(f"ADB bận (quá tải đồng thời): {' '.join(map(str, args))[:160]}")
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=timeout, input=input_bytes,
                                  creationflags=CREATE_NO_WINDOW, startupinfo=si)
        except subprocess.TimeoutExpired as exc:
            raise AdbError(f"ADB quá thời gian {timeout:g}s: {' '.join(map(str, args))[:160]}",
                           stdout=_decode(exc.stdout), stderr=_decode(exc.stderr)) from exc
        except OSError as exc:
            raise AdbError(f"Không chạy được adb.exe ({self.path}): {exc}") from exc
        finally:
            self._global_semaphore.release()
        stdout = proc.stdout if binary else _decode(proc.stdout)
        stderr = _decode(proc.stderr)
        if check and proc.returncode != 0:
            detail = (stderr or _decode(proc.stdout) if not binary else stderr).strip()
            raise AdbError(f"ADB lỗi ({proc.returncode}): {detail[:300] or ' '.join(map(str, args))[:160]}",
                           returncode=proc.returncode, stdout="" if binary else stdout, stderr=stderr)
        return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)

    def shell(self, serial: str, *args: str, timeout: float = 20, check: bool = True) -> str:
        return str(self.run(["shell", *args], serial, timeout=timeout, check=check).stdout).strip()

    def shell_ok(self, serial: str, *args: str, timeout: float = 10) -> bool:
        try:
            return self.run(["shell", *args], serial, timeout=timeout, check=False).returncode == 0
        except AdbError:
            return False

    def exec_out(self, serial: str, *args: str, timeout: float = 15) -> bytes:
        return self.run(["exec-out", *args], serial, timeout=timeout, binary=True).stdout

    def popen(self, args: Iterable[str], serial: str | None = None) -> subprocess.Popen:
        si = _startupinfo()
        return subprocess.Popen(self.command(list(args), serial), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                stdin=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW, startupinfo=si)

    def start_server(self) -> None:
        """Khởi động ADB server nếu chưa chạy. Không kill server hiện tại (tránh xung đột BoxPhone)."""
        with self._server_lock:
            # Kiểm tra nhanh xem ADB server đã chạy chưa bằng cách thử kết nối
            try:
                import socket as _sock
                with _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM) as probe:
                    probe.settimeout(0.5)
                    probe.connect(("127.0.0.1", 5037))
                    return  # ADB server đã chạy (có thể do BoxPhone start), không cần start lại
            except OSError:
                pass  # ADB server chưa chạy, cần start
            try:
                self.run(["start-server"], timeout=20, check=False)
            except AdbError:
                pass

    def devices(self) -> list[dict]:
        out = str(self.run(["devices", "-l"], timeout=4).stdout)
        return parse_devices_output(out)


KNOWN_STATES = {
    "device", "offline", "unauthorized", "authorizing",
    "connecting", "recovery", "bootloader", "sideload", "no permissions"
}


def parse_devices_output(output: str) -> list[dict]:
    """Parse `adb devices -l`. Bỏ dòng tiêu đề và dòng daemon."""
    result = []
    for line in output.splitlines():
        line = line.strip()
        if not line or line.startswith("List of devices") or line.startswith("*"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue

        # Tìm vị trí của trạng thái thiết bị (device, offline, unauthorized, ...)
        state_idx = -1
        for i in range(1, len(parts)):
            if parts[i] in KNOWN_STATES:
                state_idx = i
                break

        if state_idx != -1:
            serial = " ".join(parts[:state_idx])
            state = parts[state_idx]
            details = parts[state_idx + 1:]
        else:
            serial, state, details = parts[0], parts[1], parts[2:]

        def value(key: str) -> str:
            for item in details:
                if item.startswith(key + ":"):
                    return item[len(key) + 1:]
            return ""

        result.append({
            "serial": serial,
            "state": state,
            "model": value("model").replace("_", " "),
            "product": value("product"),
            "device": value("device"),
            "transportId": value("transport_id"),
            "transport": "network" if (":" in serial or "._tcp" in serial) else ("emulator" if serial.startswith("emulator-") else "usb"),
        })
    return result
