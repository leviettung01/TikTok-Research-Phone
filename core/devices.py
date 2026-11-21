"""Phát hiện thiết bị, đọc hồ sơ điện thoại, cấu hình nhãn/nhóm, ADB không dây."""
from __future__ import annotations

import re
import threading
import time

import config
from core import bluestacks
from core.adb import Adb, AdbError
from core.jsonstore import read_json, update_json

PROFILE_TTL = 600.0
SERIAL_RE = re.compile(r"^[A-Za-z0-9._:\- ()]{1,200}$")
HOSTPORT_RE = re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}:\d{2,5}$")


def require_serial(value) -> str:
    serial = str(value or "").strip()
    if not SERIAL_RE.match(serial):
        raise ValueError("Mã thiết bị không hợp lệ.")
    return serial


def parse_getprop(output: str) -> dict:
    return {m.group(1): m.group(2) for m in re.finditer(r"^\[([^\]]+)\]: \[([^\]]*)\]\s*$", output, re.M)}


def last_dims(output: str) -> tuple[int, int]:
    matches = re.findall(r"(\d+)x(\d+)", output or "")
    if not matches:
        return 0, 0
    w, h = matches[-1]
    return int(w), int(h)


def device_family(props: dict) -> str:
    identity = " ".join(props.get(k, "") for k in ("ro.product.manufacturer", "ro.product.brand", "ro.product.model")).lower()
    if "samsung" in identity:
        return "samsung"
    if "realme" in identity or "oppo" in identity or props.get("ro.build.version.opporom"):
        return "coloros"
    if any(k in identity for k in ("xiaomi", "redmi", "poco")) or props.get("ro.miui.ui.version.name"):
        return "xiaomi"
    if "vivo" in identity or "iqoo" in identity:
        return "vivo"
    if "oneplus" in identity:
        return "oneplus"
    return "android"


FAMILY_LABELS = {
    "samsung": "Samsung / One UI", "coloros": "Realme / OPPO / ColorOS", "xiaomi": "Xiaomi / Redmi / POCO",
    "vivo": "Vivo / iQOO", "oneplus": "OnePlus", "android": "Android tiêu chuẩn",
}

# Thời gian chờ UI dump/xác nhận màn hình theo hãng (máy chậm cần lâu hơn).
FAMILY_WORKFLOW = {
    "samsung": {"profileId": "samsung-oneui", "uiDumpTimeout": 2.6, "screenConfirmTimeout": 3.6},
    "coloros": {"profileId": "realme-oppo-coloros", "uiDumpTimeout": 2.8, "screenConfirmTimeout": 4.0},
    "xiaomi": {"profileId": "xiaomi-miui-hyperos", "uiDumpTimeout": 2.5, "screenConfirmTimeout": 3.8},
    "vivo": {"profileId": "vivo-funtouch", "uiDumpTimeout": 2.5, "screenConfirmTimeout": 3.8},
    "oneplus": {"profileId": "oneplus-oxygenos", "uiDumpTimeout": 2.5, "screenConfirmTimeout": 3.8},
    "android": {"profileId": "generic-android", "uiDumpTimeout": 2.2, "screenConfirmTimeout": 3.2},
}


def screen_shape(width: int, height: int) -> tuple[str, str]:
    if width <= 0 or height <= 0:
        return "unknown", "Chưa xác định"
    ratio = max(width, height) / min(width, height)
    if ratio < 1.55:
        return "tablet", "Máy tính bảng"
    if ratio <= 1.9:
        return "standard", "Màn hình chuẩn"
    if ratio <= 2.2:
        return "tall", "Màn hình dài"
    return "ultra-tall", "Màn hình siêu dài"


def parse_window_insets(dump: str) -> dict:
    """Tìm khung thanh trạng thái / điều hướng trong `dumpsys window` (Android 9–14)."""
    def frame(patterns):
        for pattern in patterns:
            for match in re.finditer(pattern + r"[^\n]*?frame=\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]", dump):
                left, top, right, bottom = map(int, match.groups()[-4:])
                if right > left and bottom > top:
                    return left, top, right, bottom
        return None

    nav = frame([r"ITYPE_NAVIGATION_BAR", r"type=navigationBars", r"NavigationBar"])
    status = frame([r"ITYPE_STATUS_BAR", r"type=statusBars", r"StatusBar"])
    return {
        "navigationInsetBottom": (nav[3] - nav[1]) if nav else None,
        "statusInsetTop": (status[3] - status[1]) if status else None,
        "onScreenNavigationBar": bool(nav),
    }


def build_profile(serial: str, props: dict, wm_size: str, wm_density: str, nav_mode_setting: str, window_dump: str) -> dict:
    family = device_family(props)
    width, height = last_dims(wm_size)
    density_matches = re.findall(r"(\d+)", wm_density or "")
    density = int(density_matches[-1]) if density_matches else 0
    insets = parse_window_insets(window_dump or "")
    hardware = props.get("qemu.hw.mainkeys") == "1" or props.get("ro.show.navigationbar") == "0"
    nav_setting = (nav_mode_setting or "").strip()
    if hardware:
        navigation = "hardware"
    elif nav_setting == "2":
        navigation = "gesture"
    else:
        navigation = "buttons"
    nav_inset = insets["navigationInsetBottom"]
    if navigation == "gesture" and nav_inset is not None:
        # Một số bản One UI/MIUI vẫn báo khung cũ khi đang dùng cử chỉ: giới hạn ở vùng tay nắm.
        nav_inset = min(nav_inset, round(width * 0.06))
    if nav_inset is None:
        nav_inset = round(width * 0.115) if navigation == "buttons" else 0
    status_inset = insets["statusInsetTop"] or round(width * 0.065)
    shape_id, shape_label = screen_shape(width, height)
    one_ui_raw = props.get("ro.build.version.oneui", "")
    ui_version = ""
    if one_ui_raw.isdigit() and int(one_ui_raw):
        raw = int(one_ui_raw)
        ui_version = f"One UI {raw // 10000}.{(raw % 10000) // 100}"
    elif props.get("ro.build.version.opporom"):
        ui_version = f"ColorOS {props['ro.build.version.opporom']}"
    elif props.get("ro.miui.ui.version.name"):
        ui_version = f"MIUI {props['ro.miui.ui.version.name']}"
    return {
        "serial": serial,
        "manufacturer": props.get("ro.product.manufacturer", ""),
        "brand": props.get("ro.product.brand", ""),
        "model": props.get("ro.product.model", ""),
        "device": props.get("ro.product.device", ""),
        "androidVersion": props.get("ro.build.version.release", ""),
        "sdk": int(props.get("ro.build.version.sdk", "0") or 0),
        "family": family,
        "familyLabel": FAMILY_LABELS[family],
        "uiVersion": ui_version,
        "width": width,
        "height": height,
        "density": density,
        "orientation": "landscape" if width > height else "portrait",
        "screenShape": shape_id,
        "screenShapeLabel": shape_label,
        "navigationMode": navigation,
        "navigationLabel": {"hardware": "Phím vật lý", "gesture": "Cử chỉ", "buttons": "Phím điều hướng"}[navigation],
        "navigationInsetBottom": max(0, int(nav_inset)),
        "statusInsetTop": max(0, int(status_inset)),
        "workflow": dict(FAMILY_WORKFLOW[family]),
        "hardwareSerial": (props.get("ro.serialno") or "").strip() or serial,
        "scannedAt": time.time(),
    }


class DeviceManager:
    def __init__(self, adb: Adb, logger=print):
        self.adb = adb
        self.log = logger
        self._profiles: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._last_raw: list[dict] = []
        self._last_raw_ts: float = 0.0
        self._bs_cache: tuple[float, list[dict]] = (0.0, [])
        self._hw_serial_cache: dict[str, str] = {}  # cache getprop ro.serialno cho WiFi devices
        self.wireless = None  # được gắn bởi App để lưu cổng Wi-Fi đã kết nối

    # ---- discovery -----------------------------------------------------
    def raw_devices(self, max_age: float = 3.0) -> list[dict]:
        """Cache kết quả adb devices để giảm tải ADB server (tránh xung đột BoxPhone)."""
        now = time.time()
        if self._last_raw and (now - self._last_raw_ts) < max_age:
            return self._last_raw
        devices = self.adb.devices()
        self._last_raw = devices
        self._last_raw_ts = time.time()
        return devices

    def ready_serials(self, raw: list[dict] | None = None) -> list[str]:
        raw = raw if raw is not None else self.raw_devices()
        ready = [d for d in raw if d["state"] == "device"]
        seen_hw: dict[str, str] = {}
        deduped = []
        for d in ready:
            s = d["serial"]
            p = self.cached_profile(s)
            hw = (p or {}).get("hardwareSerial")
            if not hw:
                m = re.match(r"^adb-([A-Za-z0-9]+)-", s)
                if m:
                    hw = m.group(1)
                elif ":" in s and not s.startswith("127.0.0.1"):
                    # Dùng cache để tránh gọi getprop mỗi lần poll (giảm tải ADB)
                    if s in self._hw_serial_cache:
                        hw = self._hw_serial_cache[s]
                    else:
                        try:
                            sn = str(self.adb.run(["-s", s, "shell", "getprop", "ro.serialno"], timeout=2, check=False).stdout).strip()
                            if sn and "\n" not in sn and len(sn) < 40:
                                hw = sn
                                self._hw_serial_cache[s] = sn
                        except Exception:
                            pass
                hw = hw or s
            if hw in seen_hw:
                prev = seen_hw[hw]
                prev_d = next((x for x in ready if x["serial"] == prev), None)
                if d["transport"] == "usb" and (not prev_d or prev_d["transport"] != "usb"):
                    deduped.remove(prev)
                    deduped.append(s)
                    seen_hw[hw] = s
                    if HOSTPORT_RE.match(prev):
                        try:
                            self.adb.run(["disconnect", prev], timeout=4, check=False)
                        except Exception:
                            pass
                elif HOSTPORT_RE.match(prev) and not HOSTPORT_RE.match(s):
                    deduped.remove(prev)
                    deduped.append(s)
                    seen_hw[hw] = s
                elif HOSTPORT_RE.match(s) and ("_tcp" in prev or HOSTPORT_RE.match(prev)):
                    # Tránh mở 2 đường truyền scrcpy song song vào cùng 1 điện thoại
                    if prev != s:
                        try:
                            self.adb.run(["disconnect", s], timeout=4, check=False)
                        except Exception:
                            pass
                continue
            seen_hw[hw] = s
            deduped.append(s)
        return deduped

    def is_ready(self, serial: str) -> bool:
        return any(d["serial"] == serial and d["state"] == "device" for d in self.raw_devices())

    def require_ready(self, serial: str) -> None:
        if not self.is_ready(serial):
            raise RuntimeError(f"Thiết bị {serial} chưa sẵn sàng. Mở khóa máy và chấp nhận USB debugging.")

    def bluestacks_instances(self, max_age: float = 5.0) -> list[dict]:
        stamp, cached = self._bs_cache
        if time.time() - stamp < max_age:
            return cached
        instances = bluestacks.list_instances()
        self._bs_cache = (time.time(), instances)
        return instances

    # ---- profile -------------------------------------------------------
    def profile(self, serial: str, force: bool = False) -> dict:
        with self._lock:
            cached = self._profiles.get(serial)
        if cached and not force and time.time() - cached["scannedAt"] < PROFILE_TTL:
            return cached
        props = parse_getprop(self.adb.shell(serial, "getprop", timeout=4, check=False))
        wm_size = self.adb.shell(serial, "wm", "size", timeout=3, check=False)
        wm_density = self.adb.shell(serial, "wm", "density", timeout=3, check=False)
        nav_mode = self.adb.shell(serial, "settings", "get", "secure", "navigation_mode", timeout=3, check=False)
        try:
            window_dump = self.adb.shell(serial, "dumpsys", "window", timeout=4, check=False)
        except Exception:
            window_dump = ""
        profile = build_profile(serial, props, wm_size, wm_density, nav_mode, window_dump)
        with self._lock:
            self._profiles[serial] = profile
        return profile

    def cached_profile(self, serial: str) -> dict | None:
        with self._lock:
            return self._profiles.get(serial)

    def forget(self, serial: str) -> None:
        with self._lock:
            self._profiles.pop(serial, None)
            self._hw_serial_cache.pop(serial, None)

    # ---- config --------------------------------------------------------
    def device_config(self) -> dict:
        return read_json(config.STORE_DEVICES, {})

    def set_device(self, serial: str, body: dict) -> dict:
        def mutate(store):
            current = store.get(serial, {})
            for key in ("label", "group"):
                if key in body:
                    current[key] = str(body.get(key) or "").strip()[:40]
            if "number" in body:
                raw_num = str(body.get("number") or "").strip()[:8]
                if raw_num.isdigit() and len(raw_num) == 1:
                    raw_num = f"0{raw_num}"
                current["number"] = raw_num
            if "videoMode" in body:
                mode = str(body.get("videoMode"))
                if mode not in ("auto", "compatible"):
                    raise ValueError("Chế độ màn hình không hợp lệ.")
                current["videoMode"] = mode
            current["updatedAt"] = time.time()
            store[serial] = current
        store = update_json(config.STORE_DEVICES, {}, mutate)
        return store[serial]

    def video_mode(self, serial: str) -> str:
        return "compatible" if self.device_config().get(serial, {}).get("videoMode") == "compatible" else "auto"

    # ---- wireless ADB --------------------------------------------------
    def wireless_pair(self, host_port: str, code: str) -> str:
        if not HOSTPORT_RE.match(host_port or ""):
            raise ValueError("Địa chỉ ghép nối phải có dạng IP:port (ví dụ 192.168.1.20:37099).")
        if not re.fullmatch(r"\d{6}", code or ""):
            raise ValueError("Mã ghép nối gồm 6 chữ số.")
        result = self.adb.run(["pair", host_port, code], timeout=20, check=False)
        out = f"{result.stdout}\n{result.stderr}".strip()
        if "Successfully" not in out:
            raise RuntimeError(f"Ghép nối thất bại ({out or 'không có phản hồi'}). Mã chỉ dùng được khi hộp thoại "
                               "“Ghép nối với thiết bị” còn mở trên điện thoại: mở lại, nhập mã MỚI và IP:port trong hộp thoại đó.")
        return out

    def _try_adb_connect(self, target: str) -> tuple[bool, str]:
        res = self.adb.run(["connect", target], timeout=8, check=False)
        out = f"{res.stdout}\n{res.stderr}".strip()
        low = out.lower()
        ok = ("connected to" in low) and ("cannot" not in low) and ("failed" not in low) and ("unable" not in low)
        if ok:
            if self.wireless and ":" in target:
                h, p = target.split(":", 1)
                if p.isdigit():
                    self.wireless.remember_port(h, int(p))
        else:
            # Nếu adb connect thất bại nhưng để lại entry offline trong adb devices thì dọn luôn
            try:
                if any(d["serial"] == target and d.get("state") == "offline" for d in self.adb.devices()):
                    self.adb.run(["disconnect", target], timeout=3, check=False)
            except Exception:
                pass
        return ok, out

    def wireless_connect(self, host_port: str) -> str:
        clean = (host_port or "").strip().rstrip(":")
        if not clean:
            raise ValueError("Vui lòng nhập địa chỉ IP hoặc IP:port.")

        host = clean.split(":")[0].strip()
        port = clean.split(":")[1].strip() if ":" in clean else None
        last_err = ""

        # 1. Nếu có port chỉ định, thử kết nối port đó trước
        if port and port.isdigit():
            target = f"{host}:{port}"
            ok, out = self._try_adb_connect(target)
            if ok:
                return out
            last_err = out
            self.log(f"[Wi-Fi ADB] Cổng {target} không kết nối được ({out}). Đang tìm cổng Gỡ lỗi không dây mới trên {host}…")

        # 2. Hỏi nhanh mDNS trực tiếp cho host đó (~0.6s, tìm được ngay cổng mới nhất nếu Android có quảng bá)
        from core.wireless import mdns_discover, probe_wireless_port
        try:
            mdns_hits = [
                s for s in mdns_discover(listen_sec=0.65, extra_hosts=[host])
                if s.get("type") == "_adb-tls-connect._tcp" and s.get("host") == host
            ]
        except Exception:
            mdns_hits = []

        for hit in mdns_hits:
            cand = hit["address"]
            if port and cand == f"{host}:{port}":
                continue
            ok, out = self._try_adb_connect(cand)
            if ok:
                if self.wireless and hit.get("model"):
                    self.wireless.remember_port(host, hit["port"], hit["model"])
                return f"Đã tự động tìm thấy qua mDNS và kết nối thành công: {cand}"
            last_err = out

        # 3. Tự động dò quét cổng Gỡ lỗi không dây đang mở trên điện thoại (30000..65535 + 5555)
        known_ports = self.wireless.get_known_ports(host) if self.wireless else None
        found_port = probe_wireless_port(host, timeout=0.05, known_ports=known_ports)
        if found_port:
            target = f"{host}:{found_port}"
            ok, out = self._try_adb_connect(target)
            if ok:
                return f"Đã tự dò thấy cổng {found_port} và kết nối thành công: {target}"
            raise RuntimeError(
                f"Cổng {target} đang mở nhưng kết nối bị từ chối ({out}). "
                "Điện thoại chưa ghép nối với máy tính này hoặc đã đổi phiên — vui lòng dùng 📷 Quét mã QR hoặc Nhập mã 6 số để ghép nối lại."
            )

        if port:
            raise RuntimeError(f"Kết nối thất bại tới {clean} ({last_err or 'không có phản hồi'}). "
                               f"Đã tự động dò tìm nhưng không thấy cổng Gỡ lỗi không dây nào mở trên {host}. "
                               "Vui lòng mở điện thoại kiểm tra Gỡ lỗi không dây đang BẬT, cùng Wi-Fi/LAN và nhập lại IP & cổng.")
        raise RuntimeError(f"Không tìm thấy cổng Gỡ lỗi không dây nào đang mở trên {host}. "
                           "Vui lòng kiểm tra điện thoại đã kết nối cùng mạng Wi-Fi và bật 'Gỡ lỗi không dây'.")

    def disconnect(self, host_port: str) -> str:
        if not HOSTPORT_RE.match(host_port or ""):
            raise ValueError("Chỉ ngắt được thiết bị mạng dạng IP:port.")
        return str(self.adb.run(["disconnect", host_port], timeout=8, check=False).stdout).strip()

    # ---- view ----------------------------------------------------------
    def list_view(self, stream_state: dict, storage_view, run_state) -> list[dict]:
        raw = self.raw_devices()
        cfg = self.device_config()
        instances = self.bluestacks_instances()
        ready_set = set(self.ready_serials(raw=raw))
        result = []
        for device in raw:
            serial = device["serial"]
            if device["state"] == "device" and serial not in ready_set:
                continue
            bs = next((i for i in instances if i["serial"] == serial
                       or (serial == "emulator-5554" and i["port"] == 5555)), None)
            profile = None
            if device["state"] == "device":
                try:
                    profile = self.profile(serial)
                except AdbError as exc:
                    self.log(f"[{serial}] Không đọc được hồ sơ: {exc}")
            entry = cfg.get(serial, {})
            label = entry.get("label") or (f"{bs['name']} [Android {bs['androidVersion']}]" if bs else "") \
                or device.get("model") or serial
            num = entry.get("number") or f"{len(result) + 1:02d}"
            result.append({
                **device,
                "kind": "bluestacks" if bs else ("wireless" if device["transport"] == "network" else device["transport"]),
                "label": label,
                "number": num,
                "group": entry.get("group") or "Mặc định",
                "videoMode": "compatible" if entry.get("videoMode") == "compatible" else "auto",
                "profile": profile,
                "storage": storage_view(serial),
                "bluestacks": bs,
                "stream": stream_state.get(serial),
                "run": run_state(serial),
            })
        result.sort(key=lambda x: (str(x.get("number") or "99"), x.get("serial") or ""))
        return result
