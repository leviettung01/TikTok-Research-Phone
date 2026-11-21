"""Dò thư mục video và đẩy video vào thư viện điện thoại có xác minh MediaStore.

Quy trình đẩy (giống Position Lab, viết lại bằng Python):
  push → /data/local/tmp (stage) → cp sang <thư mục>/.tiktok-transfer-*.part → so size
  → mv -n sang tên cuối (không ghi đè) → broadcast MediaScanner + scan_volume
  → sau 3 lần chưa thấy thì content insert → query MediaStore khớp path + size.
"User fence": user Android đổi giữa chừng thì dừng ngay.
"""
from __future__ import annotations

import posixpath
import re
import threading
import time
import uuid
from typing import Callable

from core.adb import Adb, AdbError

MANAGED_NAME_RE = re.compile(r"^tiktok-affiliate-[a-f0-9]{8}-\d{3}\.(mp4|mov|m4v|webm)$", re.I)
SUFFIXES = ("DCIM/Camera", "DCIM/TikTokAffiliateAuto", "Movies/TikTokAffiliateAuto")
RESERVE_BYTES = 16 * 1024 * 1024
SAFE_PATH_RE = re.compile(r"^/(storage|mnt|sdcard)[A-Za-z0-9_./-]*$")


class TransferError(RuntimeError):
    def __init__(self, message: str, code: str = "MEDIA_TRANSFER_FAILED"):
        super().__init__(message)
        self.code = code


TERMINAL_CODES = {"RUN_STOPPED", "DEVICE_LEASE_LOST", "MEDIA_CONTEXT_CHANGED", "MEDIA_USER_UNKNOWN"}


def is_terminal(error: Exception) -> bool:
    return getattr(error, "code", "") in TERMINAL_CODES or type(error).__name__ == "AutomationStopped"


def is_managed_video_path(remote: str, camera_directory: str = "") -> bool:
    remote = str(remote or "").replace("\\", "/")
    if not MANAGED_NAME_RE.match(posixpath.basename(remote)):
        return False
    directory = posixpath.dirname(remote)
    if camera_directory:
        return directory == camera_directory.rstrip("/")
    return bool(re.search(r"/(DCIM/Camera|DCIM/TikTokAffiliateAuto|Movies/TikTokAffiliateAuto)$", directory))


def parse_df_free(output: str) -> int | None:
    lines = [l for l in output.splitlines() if l.strip()]
    if not lines:
        return None
    fields = lines[-1].split()
    if len(fields) > 3 and fields[3].isdigit():
        return int(fields[3]) * 1024
    return None


def parse_mediastore_rows(output: str, target_path: str, remote_name: str, size: int) -> list[str]:
    ids = []
    for line in output.splitlines():
        match = re.match(r"^Row: \d+ _id=(\d+), _data=(.*), _size=(\d+), mime_type=(\S+)$", line.strip())
        if not match:
            continue
        path_ok = match.group(2) == target_path or match.group(2).endswith(remote_name)
        if path_ok and int(match.group(3)) == size and match.group(4).startswith("video/"):
            ids.append(match.group(1))
    return ids


class MediaTransfer:
    def __init__(self, adb: Adb, pause: Callable[[float], None] = time.sleep, poll_attempts: int = 12):
        self.adb = adb
        self.pause = pause
        self.poll_attempts = poll_attempts

    def _execute(self, serial: str, *, source: str = "", size: int = 0, remote_name: str = "",
                 ensure_active: Callable[[], None] = lambda: None, on_created=None, inspect_only: bool = False) -> dict:
        ensure_active()
        if inspect_only:
            size = 3
        elif size <= 0 or not MANAGED_NAME_RE.match(remote_name):
            raise TransferError("Video nguồn hoặc tên video tạm không hợp lệ.")

        def shell(*args: str, timeout: float = 8) -> str:
            ensure_active()
            value = self.adb.shell(serial, *args, timeout=timeout)
            ensure_active()
            return value

        def read_user() -> str:
            for args in (("cmd", "activity", "get-current-user"), ("am", "get-current-user")):
                try:
                    value = shell(*args)
                    if value.isdigit():
                        return value
                except AdbError:
                    continue
            raise TransferError("Không xác minh được người dùng Android đang hoạt động.", "MEDIA_USER_UNKNOWN")

        user = read_user()

        def fence():
            if read_user() != user:
                raise TransferError("Người dùng Android đã thay đổi; dừng đẩy video.", "MEDIA_CONTEXT_CHANGED")

        def resolve(target: str) -> str:
            for args in (("realpath", target), ("readlink", "-f", target)):
                try:
                    output = shell(*args)
                    value = output.splitlines()[-1].strip() if output else ""
                    if value and SAFE_PATH_RE.match(value):
                        return value.rstrip("/")
                except (AdbError, IndexError):
                    continue
            return target

        roots = [f"/storage/emulated/{user}", f"/mnt/user/{user}/primary"]
        for alias in ("/sdcard", "/storage/self/primary"):
            resolved = resolve(alias)
            if resolved in roots:
                roots.insert(0, resolved)
        candidates = list(dict.fromkeys(f"{root}/{suffix}" for root in dict.fromkeys(roots) for suffix in SUFFIXES))

        def exists(target: str) -> bool:
            # Đường dẫn được sinh từ user số + tên cố định đã kiểm tra, không chứa ký tự shell.
            result = shell(f"if [ -e {target} ]; then echo EXISTS; else echo ABSENT; fi")
            if result not in ("EXISTS", "ABSENT"):
                raise TransferError("Không kiểm tra được file trên điện thoại.")
            return result == "EXISTS"

        tried: set[str] = set()
        last_error: Exception | None = None
        for candidate in candidates:
            fence()
            stage = ""
            push_started = commit_attempted = False
            try:
                shell("mkdir", "-p", candidate)
                directory = resolve(candidate)
                if directory not in candidates and candidate not in candidates:
                    raise TransferError("Thư mục video không thuộc bộ nhớ người dùng hiện tại.")
                if directory in tried:
                    continue
                tried.add(directory)
                parent = directory
                while parent not in ("/storage", "/mnt", "/", ""):
                    if exists(f"{parent}/.nomedia"):
                        raise TransferError("Thư mục bị ẩn khỏi thư viện bởi .nomedia.")
                    parent = posixpath.dirname(parent)
                free = parse_df_free(shell("df", "-k", directory))
                if free is None:
                    raise TransferError("Không xác minh được dung lượng trống.")
                if free < size + RESERVE_BYTES:
                    raise TransferError("Không đủ dung lượng trống cho video và vùng dự phòng 16 MB.")
                if inspect_only:
                    probe = f"{directory}/.tiktok-probe-{uuid.uuid4().hex}.tmp"
                    fence()
                    if shell(f"( set -C; echo ok > {probe} ) && echo CREATED") != "CREATED":
                        raise TransferError("Không ghi được file kiểm tra bộ nhớ.")
                    try:
                        if shell("stat", "-c", "%s", probe) != "3":
                            raise TransferError("File kiểm tra bộ nhớ không đúng dung lượng.")
                    finally:
                        shell("rm", "-f", probe)
                    return {"storage": {"currentUser": user, "cameraDirectory": directory, "freeBytes": free,
                                        "checkedAt": time.time()}, "writable": True}
                target = f"{directory}/{remote_name}"
                if exists(target):
                    raise TransferError("Video cùng tên đã tồn tại; không ghi đè.", "MEDIA_FILE_EXISTS")
                stage = f"{directory}/.tiktok-transfer-{uuid.uuid4().hex}.part"
                fence()
                push_started = True
                tmp_stage = f"/data/local/tmp/.tiktok-stage-{uuid.uuid4().hex}.tmp"
                try:
                    ensure_active()
                    self.adb.run(["push", source, tmp_stage], serial, timeout=300)
                    shell("cp", tmp_stage, stage, timeout=120)
                finally:
                    try:
                        self.adb.shell(serial, "rm", "-f", tmp_stage, timeout=8, check=False)
                    except AdbError:
                        pass
                if shell("stat", "-c", "%s", stage) != str(size):
                    raise TransferError("Dung lượng video đã đẩy không khớp file gốc.")
                fence()
                commit_attempted = True
                shell("mv", "-n", stage, target)
                if exists(stage):
                    raise TransferError("Video cùng tên đã tồn tại; không ghi đè.", "MEDIA_FILE_EXISTS")
                if shell("stat", "-c", "%s", target) != str(size):
                    raise TransferError("Không xác minh được video sau khi chuyển tên.")
                storage = {"currentUser": user, "cameraDirectory": directory, "freeBytes": free, "checkedAt": time.time()}
                if on_created:
                    on_created(storage, target)
                fence()
                try:
                    shell("am", "broadcast", "--user", user, "-a", "android.intent.action.MEDIA_SCANNER_SCAN_FILE",
                          "-d", f"file://{target}")
                except AdbError:
                    pass
                try:
                    shell("content", "call", "--user", user, "--uri", "content://media/none/media_scanner",
                          "--method", "scan_volume", "--extra", "volume:s:external_primary")
                except AdbError:
                    pass
                for attempt in range(self.poll_attempts):
                    fence()
                    if attempt == 3:
                        # Android 11 / BlueStacks đôi khi không tự index: chèn trực tiếp vào MediaStore.
                        base = posixpath.splitext(remote_name)[0]
                        try:
                            shell("content", "insert", "--user", user, "--uri", "content://media/external/video/media",
                                  "--bind", f"_data:s:{target}", "--bind", f"_size:i:{size}",
                                  "--bind", f"_display_name:s:{remote_name}", "--bind", f"title:s:{base}",
                                  "--bind", "mime_type:s:video/mp4")
                        except AdbError:
                            pass
                    output = shell("content", "query", "--user", user, "--uri", "content://media/external/video/media",
                                   "--projection", "_id:_data:_size:mime_type", "--where", f"_size={size}")
                    rows = parse_mediastore_rows(output, target, remote_name, size)
                    if rows:
                        fence()
                        return {"storage": storage, "targetPath": target, "mediaId": rows[-1], "indexed": True}
                    if re.search(r"Error|Exception|Permission Denial", output, re.I):
                        raise TransferError("Không đọc được MediaStore; kiểm tra quyền thư viện trên điện thoại.")
                    if attempt + 1 < self.poll_attempts:
                        self.pause(0.3)
                        ensure_active()
                raise TransferError("Video đã đẩy nhưng MediaStore chưa xác nhận; tool dừng trước bước chọn video.")
            except Exception as error:  # noqa: BLE001 - thử thư mục kế tiếp nếu lỗi không nghiêm trọng
                if is_terminal(error) or getattr(error, "code", "") == "MEDIA_FILE_EXISTS" or commit_attempted:
                    raise
                last_error = error
                if push_started and stage:
                    try:
                        self.adb.shell(serial, "rm", "-f", stage, timeout=8, check=False)
                    except AdbError:
                        pass
        raise TransferError(f"Không tìm được thư mục video phù hợp: {last_error or 'không có thư mục ghi được'}")

    def inspect(self, serial: str, ensure_active=lambda: None) -> dict:
        return self._execute(serial, ensure_active=ensure_active, inspect_only=True)["storage"]

    def transfer(self, serial: str, source: str, size: int, remote_name: str, ensure_active=lambda: None,
                 on_created=None) -> dict:
        return self._execute(serial, source=source, size=size, remote_name=remote_name,
                             ensure_active=ensure_active, on_created=on_created)


class StorageMonitor:
    """Trạng thái bộ nhớ chỉ mang tính tham khảo; mỗi lần đẩy video luôn tự kiểm tra lại."""

    def __init__(self, transfer: MediaTransfer, is_busy: Callable[[str], bool], logger=print):
        self.transfer = transfer
        self.is_busy = is_busy
        self.log = logger
        self._entries: dict[str, dict] = {}
        self._lock = threading.Lock()

    def view(self, serial: str) -> dict | None:
        with self._lock:
            entry = self._entries.get(serial)
            return dict(entry["value"]) if entry and entry.get("value") else None

    def _check(self, serial: str, entry: dict) -> None:
        def active():
            with self._lock:
                if self._entries.get(serial) is not entry or entry.get("cancelled"):
                    raise TransferError("Quét bộ nhớ đã nhường thiết bị.", "RUN_STOPPED")
        try:
            if self.is_busy(serial):
                entry["value"] = {"status": "WAITING", "message": "Chờ máy hoàn tất thao tác để quét bộ nhớ"}
                return
            entry["value"] = {"status": "SCANNING", "message": "Đang kiểm tra thư mục video…"}
            storage = self.transfer.inspect(serial, active)
            entry["value"] = {**storage, "status": "READY", "message": f"Sẵn sàng: {storage['cameraDirectory']}"}
        except Exception as error:  # noqa: BLE001
            if self._entries.get(serial) is entry:
                if getattr(error, "code", "") == "RUN_STOPPED":
                    entry["value"] = {"status": "WAITING", "message": "Đã nhường thiết bị cho thao tác khác"}
                else:
                    entry["value"] = {"status": "ERROR", "message": str(error)}
        finally:
            status = (entry.get("value") or {}).get("status")
            entry["nextCheck"] = time.time() + (30 if status == "ERROR" else 300 if status == "READY" else 10)
            entry["thread"] = None

    def _launch(self, serial: str, entry: dict) -> threading.Thread:
        thread = threading.Thread(target=self._check, args=(serial, entry), daemon=True, name=f"storage-{serial}")
        entry["thread"] = thread
        thread.start()
        return thread

    def update(self, serials: list[str]) -> None:
        ready = set(serials)
        with self._lock:
            for serial in list(self._entries):
                if serial not in ready:
                    self._entries.pop(serial, None)
            for serial in ready:
                entry = self._entries.get(serial)
                if entry is None:
                    entry = {"value": {"status": "SCANNING", "message": "Đang kiểm tra thư mục video…"}, "nextCheck": 0,
                             "thread": None}
                    self._entries[serial] = entry
                if entry.get("thread") is None and time.time() >= entry["nextCheck"] and not self.is_busy(serial):
                    self._launch(serial, entry)

    def rescan(self, serial: str) -> dict:
        if self.is_busy(serial):
            return {"status": "WAITING", "message": "Thiết bị đang chạy quy trình."}
        with self._lock:
            entry = self._entries.get(serial)
            if entry is None:
                entry = {"value": None, "nextCheck": 0, "thread": None}
                self._entries[serial] = entry
            thread = entry.get("thread") or self._launch(serial, entry)
        thread.join(timeout=60)
        return self.view(serial) or {"status": "DISCONNECTED"}

    def suspend(self, serial: str) -> None:
        with self._lock:
            entry = self._entries.get(serial)
            thread = entry.get("thread") if entry else None
            if entry:
                entry["cancelled"] = True
        if thread:
            thread.join(timeout=15)
        with self._lock:
            entry = self._entries.get(serial)
            if entry:
                entry["cancelled"] = False
                entry["nextCheck"] = time.time() + 10
                entry["value"] = {"status": "WAITING", "message": "Đã nhường thiết bị cho thao tác của bạn"}
