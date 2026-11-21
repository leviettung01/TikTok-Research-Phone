"""Kết nối điện thoại không dây + chẩn đoán USB.

- Ghép nối QR (Android 11+): tool tạo mã QR `WIFI:T:ADB;S:<tên>;P:<mật khẩu>;;`, điện thoại quét
  → quảng bá mDNS `_adb-tls-pairing._tcp` với đúng tên đó → tool tự `adb pair` rồi `adb connect`.
- Tự phát hiện máy đã bật Gỡ lỗi không dây trong mạng LAN (`_adb-tls-connect._tcp`).
- Chẩn đoán USB: Windows có nhận điện thoại không (cáp chỉ sạc / thiếu driver / chưa cho phép).
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import ctypes
import io
import json
import re
import secrets
import select
import socket
import string
import struct
import subprocess
import threading
import time
from pathlib import Path

from core.adb import CREATE_NO_WINDOW, Adb, AdbError

MDNS_LINE = re.compile(r"^(.+?)\s+(_adb[-\w]*\._tcp)\.?\s+(\d+\.\d+\.\d+\.\d+):(\d+)\s*$")
MDNS_ADDR = ("224.0.0.251", 5353)
ADB_MDNS_TYPES = ("_adb-tls-connect._tcp.local", "_adb-tls-pairing._tcp.local")
# VID của các hãng Android phổ biến (Xiaomi 2717, Samsung 04E8, Google 18D1, Oppo/Realme 22D9, Vivo 2D95, Huawei 12D1...).
ANDROID_VIDS = {"2717": "Xiaomi/Redmi/POCO", "04E8": "Samsung", "18D1": "Google/AOSP", "22D9": "OPPO/Realme",
                "2D95": "vivo", "12D1": "Huawei/Honor", "0BB4": "HTC", "1004": "LG", "0FCE": "Sony",
                "2A70": "OnePlus", "05C6": "Qualcomm", "0E8D": "MediaTek", "19D2": "ZTE", "1EBF": "Nokia/HMD",
                "2A45": "Meizu", "17EF": "Lenovo/Motorola", "22B8": "Motorola"}


def parse_mdns(output: str) -> list[dict]:
    services = []
    for line in output.splitlines():
        match = MDNS_LINE.match(line.strip())
        if match:
            raw_name, kind, host, port = match.groups()
            clean_name = re.sub(r"\s+\(\d+\)$", "", raw_name.strip())
            services.append({"name": clean_name, "rawName": raw_name.strip(), "type": kind.rstrip("."),
                             "host": host, "port": int(port), "address": f"{host}:{port}", "model": ""})
    return services


def _local_lan_ips() -> list[str]:
    """Liệt kê tất cả IPv4 nội bộ trên PC (tránh loopback và VPN APIPA 169.254.x.x)."""
    ips: list[str] = []
    seen: set[str] = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip in seen or ip.startswith("127.") or ip.startswith("169.254."):
                continue
            seen.add(ip)
            ips.append(ip)
    except OSError:
        pass
    if not ips:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                probe.connect(("192.168.1.1", 80))
                ip = probe.getsockname()[0]
                if ip and not ip.startswith("127.") and not ip.startswith("169.254."):
                    ips.append(ip)
        except OSError:
            pass
    # Ưu tiên dải LAN phổ biến (192.168.1.x / 192.168.0.x / 10.x) trước các USB DCOM (192.168.8.x)
    ips.sort(key=lambda x: (1 if x.startswith("192.168.8.") else 0, x))
    return ips


def _lan_arp_hosts(local_ips: list[str] | None = None) -> list[str]:
    """Đọc bảng ARP của Windows qua WinAPI (~1ms) để lấy các thiết bị thực trong LAN cùng subnet."""
    local_ips = local_ips if local_ips is not None else _local_lan_ips()
    local_set = set(local_ips)
    subnets = {".".join(ip.split(".")[:3]) + "." for ip in local_ips if not ip.startswith("192.168.8.")}
    if not subnets:
        subnets = {".".join(ip.split(".")[:3]) + "." for ip in local_ips}
    hosts: list[str] = []
    seen: set[str] = set()
    try:
        iphlp = getattr(ctypes, "windll", None)
        if not iphlp:
            return hosts
        iphlpapi = iphlp.iphlpapi
        buf_size = ctypes.c_ulong(0)
        iphlpapi.GetIpNetTable(None, ctypes.byref(buf_size), False)
        if buf_size.value < 4:
            return hosts
        buf = ctypes.create_string_buffer(buf_size.value)
        if iphlpapi.GetIpNetTable(buf, ctypes.byref(buf_size), False) != 0:
            return hosts
        count = struct.unpack_from("<I", buf.raw, 0)[0]
        for i in range(min(count, (len(buf.raw) - 4) // 24)):
            off = 4 + i * 24
            _idx, plen = struct.unpack_from("<II", buf.raw, off)
            ip_str = socket.inet_ntoa(buf.raw[off + 16:off + 20])
            row_type = struct.unpack_from("<I", buf.raw, off + 20)[0]
            if row_type not in (3, 4) or plen <= 0:
                continue
            if ip_str in seen or ip_str in local_set:
                continue
            if ip_str.endswith((".1", ".255")) or ip_str.startswith(("127.", "169.254.", "224.", "239.", "255.")):
                continue
            if any(ip_str.startswith(sub) for sub in subnets):
                seen.add(ip_str)
                hosts.append(ip_str)
    except Exception:
        pass
    return hosts


def _tcp_port_open(host: str, port: int, timeout: float = 0.14) -> bool:
    if not host or not (1 <= port <= 65535):
        return False
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        return sock.connect_ex((host, port)) == 0
    except OSError:
        return False
    finally:
        sock.close()


def _encode_dns_ptr_query(service_name: str, qclass: int = 0x8001) -> bytes:
    header = b"\x00\x00\x00\x00\x00\x01\x00\x00\x00\x00\x00\x00"
    labels = bytearray()
    for part in service_name.strip(".").split("."):
        raw = part.encode("utf-8")
        if raw:
            labels.append(len(raw))
            labels.extend(raw)
    labels.append(0)
    return header + bytes(labels) + struct.pack("!HH", 12, qclass)


def _decode_dns_name(buf: bytes, offset: int) -> tuple[str, int]:
    labels: list[str] = []
    jumped = False
    orig_next = offset
    seen: set[int] = set()
    n = len(buf)
    while offset < n and offset not in seen:
        seen.add(offset)
        length = buf[offset]
        if length == 0:
            offset += 1
            break
        if (length & 0xC0) == 0xC0:
            if offset + 1 >= n:
                offset = n
                break
            ptr = ((length & 0x3F) << 8) | buf[offset + 1]
            if not jumped:
                orig_next = offset + 2
                jumped = True
            offset = ptr
        else:
            offset += 1
            if offset + length > n:
                break
            labels.append(buf[offset:offset + length].decode("utf-8", errors="replace"))
            offset += length
    return ".".join(labels), (orig_next if jumped else offset)


def _parse_txt_rdata(data: bytes) -> dict[str, str]:
    result: dict[str, str] = {}
    i, n = 0, len(data)
    while i < n:
        ln = data[i]
        i += 1
        if ln == 0 or i + ln > n:
            break
        item = data[i:i + ln].decode("utf-8", errors="replace")
        i += ln
        if "=" in item:
            k, v = item.split("=", 1)
            result[k] = v
    return result


def _parse_mdns_packet(data: bytes, sender_ip: str) -> list[dict]:
    if len(data) < 12:
        return []
    try:
        _tid, flags, qd, an, ns, ar = struct.unpack("!HHHHHH", data[:12])
    except struct.error:
        return []
    if not (flags & 0x8000):  # chỉ xử lý DNS Response
        return []
    off = 12
    for _ in range(qd):
        _name, off = _decode_dns_name(data, off)
        off += 4
        if off > len(data):
            return []

    ptrs: list[tuple[str, str]] = []
    srvs: dict[str, tuple[str, int]] = {}
    txts: dict[str, dict[str, str]] = {}
    addrs: dict[str, str] = {}

    for _ in range(an + ns + ar):
        if off + 10 > len(data):
            break
        rname, off = _decode_dns_name(data, off)
        if off + 10 > len(data):
            break
        rtype, _rclass, ttl, rdlen = struct.unpack("!HHIH", data[off:off + 10])
        off += 10
        rdata_off = off
        off += rdlen
        if off > len(data) or ttl == 0:
            continue
        rname_l = rname.lower().rstrip(".")
        if rtype == 12:  # PTR
            target, _ = _decode_dns_name(data, rdata_off)
            ptrs.append((rname_l, target.rstrip(".")))
        elif rtype == 33 and rdlen >= 6:  # SRV
            _prio, _weight, port = struct.unpack("!HHH", data[rdata_off:rdata_off + 6])
            target, _ = _decode_dns_name(data, rdata_off + 6)
            if port > 0:
                srvs[rname_l] = (target.lower().rstrip("."), port)
        elif rtype == 1 and rdlen == 4:  # A
            addrs[rname_l] = socket.inet_ntoa(data[rdata_off:rdata_off + 4])
        elif rtype == 16:  # TXT
            txts[rname_l] = _parse_txt_rdata(data[rdata_off:rdata_off + rdlen])

    found: list[dict] = []
    # Ghép qua PTR hoặc trực tiếp từ SRV (trường hợp Unsolicited announce đôi khi chỉ có SRV)
    candidates: list[tuple[str, str]] = list(ptrs)
    for srv_key in srvs:
        for svc_type in ("_adb-tls-connect._tcp", "_adb-tls-pairing._tcp"):
            suffix = f".{svc_type}.local"
            if srv_key.endswith(suffix) and not any(inst.lower() == srv_key for _, inst in candidates):
                candidates.append((f"{svc_type}.local", srv_key))

    seen_addr: set[tuple[str, str, int]] = set()
    for svc_q, full_instance in candidates:
        svc_type = ""
        if "_adb-tls-connect._tcp" in svc_q or "_adb-tls-connect._tcp" in full_instance.lower():
            svc_type = "_adb-tls-connect._tcp"
        elif "_adb-tls-pairing._tcp" in svc_q or "_adb-tls-pairing._tcp" in full_instance.lower():
            svc_type = "_adb-tls-pairing._tcp"
        if not svc_type:
            continue
        inst_key = full_instance.lower()
        srv = srvs.get(inst_key)
        if not srv:
            continue
        target_host, port = srv
        ip = addrs.get(target_host) or sender_ip
        if not ip or ip.startswith("127."):
            continue
        # Bỏ phần "._adb-tls-xxx._tcp.local" để lấy tên instance gốc (kể cả chữ hoa/thường nếu từ PTR)
        marker = f".{svc_type}"
        idx = full_instance.lower().find(marker)
        raw_name = full_instance[:idx] if idx > 0 else full_instance
        clean_name = re.sub(r"\s+\(\d+\)$", "", raw_name.strip())
        txt = txts.get(inst_key, {})
        key = (svc_type, ip, port)
        if key in seen_addr:
            continue
        seen_addr.add(key)
        found.append({
            "name": clean_name,
            "rawName": raw_name.strip(),
            "type": svc_type,
            "host": ip,
            "port": port,
            "address": f"{ip}:{port}",
            "model": txt.get("name", ""),
        })
    return found


def mdns_discover(listen_sec: float = 0.85, service_types: tuple[str, ...] = ADB_MDNS_TYPES,
                  extra_hosts: list[str] | None = None) -> list[dict]:
    """Dò mDNS trực tiếp qua UDP trên TẤT CẢ card mạng LAN (độc lập với adb mdns daemon & DCOM).

    Kết hợp:
    1) Gửi từ ephemeral port trên từng card LAN (nhận Unicast reply — vượt Firewall Public)
    2) Gửi trực tiếp tới các host trong bảng ARP LAN (nhận được cả khi Android ngủ multicast)
    3) Listener UDP 5353 multicast (nhận Unsolicited mDNS khi điện thoại vừa quét xong mã QR)
    """
    ips = _local_lan_ips()
    if not ips:
        return []
    targets_unicast = list(dict.fromkeys(list(extra_hosts or []) + _lan_arp_hosts(ips)))
    sockets: list[socket.socket] = []
    try:
        # Socket nghe multicast 5353 (không sao nếu bị OS/app khác chặn bind)
        try:
            mcast_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            mcast_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            mcast_sock.setblocking(False)
            mcast_sock.bind(("", 5353))
            for ip in ips:
                try:
                    mreq = socket.inet_aton(MDNS_ADDR[0]) + socket.inet_aton(ip)
                    mcast_sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
                except OSError:
                    pass
            sockets.append(mcast_sock)
        except OSError:
            pass

        for ip in ips:
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
                sock.setblocking(False)
                sock.bind((ip, 0))
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(ip))
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
                sockets.append(sock)
                for svc in service_types:
                    sock.sendto(_encode_dns_ptr_query(svc, 0x8001), MDNS_ADDR)
                    sock.sendto(_encode_dns_ptr_query(svc, 0x0001), MDNS_ADDR)
                subnet = ".".join(ip.split(".")[:3]) + "."
                for h in targets_unicast:
                    if h.startswith(subnet):
                        for svc in service_types:
                            sock.sendto(_encode_dns_ptr_query(svc, 0x8001), (h, 5353))
            except OSError:
                continue

        if not sockets:
            return []

        raw_found: list[dict] = []
        deadline = time.time() + max(0.25, listen_sec)
        while True:
            rem = deadline - time.time()
            if rem <= 0:
                break
            readable, _, _ = select.select(sockets, [], [], min(rem, 0.2))
            for sock in readable:
                try:
                    data, addr = sock.recvfrom(4096)
                    raw_found.extend(_parse_mdns_packet(data, addr[0]))
                except OSError:
                    continue

        # Khử trùng lặp và ưu tiên cổng thật sự đang mở nếu 1 máy trả nhiều bản ghi cũ/mới (vd 37077 & 42023)
        dedup: dict[tuple[str, str, int], dict] = {}
        for item in raw_found:
            dedup[(item["type"], item["host"], item["port"])] = item
        items = list(dedup.values())

        # Nếu cùng (type, host) có >= 2 cổng khác nhau, kiểm tra nhanh TCP port nào đang thực sự mở
        by_group: dict[tuple[str, str], list[dict]] = {}
        for item in items:
            by_group.setdefault((item["type"], item["host"]), []).append(item)

        resolved: list[dict] = []
        for (_t, _h), group in by_group.items():
            if len(group) == 1:
                resolved.append(group[0])
                continue
            # Ưu tiên bản ghi có port đang mở TCP; nếu chưa rõ, ưu tiên hậu tố số lớn nhất (cổng mới nhất)
            open_ones = [g for g in group if _tcp_port_open(g["host"], g["port"], timeout=0.12)]
            chosen = open_ones if open_ones else group
            chosen.sort(key=lambda g: (1 if g.get("rawName", "") != g.get("name", "") else 0, g.get("rawName", "")), reverse=True)
            resolved.append(chosen[0])
        return resolved
    finally:
        for sock in sockets:
            try:
                sock.close()
            except OSError:
                pass


def probe_wireless_port(host: str, timeout: float = 0.06, known_ports: list[int] | None = None) -> int | None:
    """Quét tìm nhanh cổng Gỡ lỗi không dây đang mở trên điện thoại (Android 11+ dùng dải 30000..65535)."""
    # 1. Thử các cổng đã biết / cổng 5555 trước (cực nhanh, chỉ ~20-50ms)
    check_first = []
    for p in list(known_ports or []) + [5555]:
        if 1024 <= p <= 65535 and p not in check_first:
            check_first.append(p)
    for p in check_first:
        if _tcp_port_open(host, p, timeout=0.15):
            return p

    # 2. Quét nhanh không đồng bộ (non-blocking) qua toàn dải 30000..65535 (Winsock select limit < 512)
    start, end, batch = 30000, 65536, 250
    for b_start in range(start, end, batch):
        socks = {}
        for p in range(b_start, min(b_start + batch, end)):
            if p in check_first:
                continue
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setblocking(False)
            s.connect_ex((host, p))
            socks[s] = p
        _, writable, _ = select.select([], list(socks.keys()), [], timeout)
        found = None
        for s in writable:
            try:
                if s.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR) == 0:
                    found = socks[s]
                    break
            except Exception:
                pass
        for s in socks:
            try:
                s.close()
            except Exception:
                pass
        if found:
            return found
    return None


def qr_svg(payload: str) -> str:
    try:
        import qrcode
        import qrcode.image.svg
    except ImportError as exc:  # pragma: no cover - launcher tự cài
        raise RuntimeError("Thiếu thư viện qrcode. Chạy: python -m pip install qrcode") from exc
    image = qrcode.make(payload, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=4)
    buffer = io.BytesIO()
    image.save(buffer)
    svg = buffer.getvalue().decode("utf-8")
    svg = svg[svg.find("<svg"):]
    # Chuẩn hóa kích thước tự co theo khung + luôn có nền trắng tinh & nét đen sắc nét
    svg = re.sub(r'(<svg\b[^>]*?)\s+width="[^"]*"\s+height="[^"]*"', r'\1 width="100%" height="100%"', svg, count=1)
    if 'style="' not in svg[:svg.find(">") + 1]:
        svg = svg.replace(">", ' style="background:#ffffff;display:block;shape-rendering:crispEdges;">', 1)
    if 'fill="#ffffff"' not in svg and "fill='#ffffff'" not in svg:
        svg = svg.replace(">", '><rect x="0" y="0" width="100%" height="100%" fill="#ffffff"/>', 1)
    return svg


class WirelessManager:
    def __init__(self, adb: Adb, logger=print, notify=None, store_path: str | Path | None = None):
        self.adb = adb
        self.log = logger
        self.notify = notify or (lambda message: None)
        self.store_path = Path(store_path) if store_path else None
        self.lock = threading.Lock()
        self.session: dict | None = None
        self._services: list[dict] = []
        self._services_at = 0.0
        self._adb_mdns_unavailable_until = 0.0
        self._attempts: dict[str, float] = {}
        self._known_ports: dict[str, list[int]] = {}
        self._known_labels: dict[str, str] = {}
        self._load_store()

    def _load_store(self) -> None:
        if not self.store_path or not self.store_path.is_file():
            return
        try:
            data = json.loads(self.store_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                for host, info in data.items():
                    if not isinstance(info, dict):
                        continue
                    ports = [int(p) for p in (info.get("ports") or []) if isinstance(p, int) or str(p).isdigit()]
                    if ports:
                        self._known_ports[host] = ports[:8]
                    if info.get("label"):
                        self._known_labels[host] = str(info["label"])
        except Exception:
            pass

    def _save_store(self) -> None:
        if not self.store_path:
            return
        try:
            with self.lock:
                payload = {
                    host: {
                        "ports": list(ports[:8]),
                        "label": self._known_labels.get(host, ""),
                    }
                    for host, ports in self._known_ports.items()
                    if ports
                }
            self.store_path.parent.mkdir(parents=True, exist_ok=True)
            self.store_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    def remember_port(self, host: str, port: int, label: str = "") -> None:
        if not host or not (1024 <= port <= 65535):
            return
        with self.lock:
            ports = self._known_ports.setdefault(host, [])
            if port in ports:
                ports.remove(port)
            ports.insert(0, port)
            if len(ports) > 8:
                del ports[8:]
            if label:
                self._known_labels[host] = label
        self._save_store()

    def get_known_ports(self, host: str) -> list[int]:
        with self.lock:
            return list(self._known_ports.get(host, []))

    def known_hosts(self) -> list[str]:
        with self.lock:
            return list(self._known_ports.keys())

    # ---- mDNS -----------------------------------------------------------
    def services(self, max_age: float = 8.0, *, listen_sec: float = 0.85, include_adb_mdns: bool = True) -> list[dict]:
        if time.time() - self._services_at < max_age:
            return self._services
        with self.lock:
            extra_hosts = list(self._known_ports.keys())

        # 1. Pure-Python mDNS trên MỌI card mạng LAN (hoạt động ngay cả khi adb mdns daemon unavailable)
        py_services = mdns_discover(listen_sec=listen_sec, extra_hosts=extra_hosts)

        # 2. Kết hợp thêm `adb mdns services` (nếu ADB server có bật mDNS openscreen)
        adb_services: list[dict] = []
        if include_adb_mdns and time.time() >= self._adb_mdns_unavailable_until:
            try:
                res = self.adb.run(["mdns", "services"], timeout=3, check=False)
                combined = f"{res.stdout}\n{res.stderr}"
                if "mdns daemon unavailable" in combined.lower():
                    # Tạm bỏ qua adb mdns services 60s để đỡ gọi ADB thừa khi dùng chung server XiaoWei
                    self._adb_mdns_unavailable_until = time.time() + 60.0
                else:
                    adb_services = parse_mdns(str(res.stdout))
            except AdbError:
                adb_services = []

        merged: dict[tuple[str, str, int], dict] = {}
        for item in adb_services + py_services:
            k = (item["type"], item["host"], item["port"])
            prev = merged.get(k)
            if not prev or (item.get("model") and not prev.get("model")):
                merged[k] = item
        self._services = list(merged.values())
        self._services_at = time.time()
        return self._services

    def discovered(self, connected: set[str], *, refresh: bool = False) -> list[dict]:
        """Máy đang bật Gỡ lỗi không dây trong LAN (đã / chưa kết nối)."""
        result = []
        seen_hosts = set()
        svc_list = self.services(max_age=0 if refresh else 8.0)
        for service in svc_list:
            if service["type"] != "_adb-tls-connect._tcp":
                continue
            serial_hits = {service["address"], f"{service['name']}._adb-tls-connect._tcp",
                           f"{service.get('rawName', service['name'])}._adb-tls-connect._tcp"}
            is_conn = bool(serial_hits & connected) or any(
                c.startswith(service["name"]) or c.startswith(service["host"] + ":") for c in connected)
            display_name = service["model"] or service["name"]
            result.append({**service, "displayName": display_name, "connected": is_conn})
            seen_hosts.add(service["host"])

        # Kiểm tra các host đã từng ghép nối nhưng hiện đang tắt mDNS (vd MIUI ngủ mDNS khi tắt màn hình)
        with self.lock:
            known_items = [(h, list(ps), self._known_labels.get(h, "")) for h, ps in self._known_ports.items()]
        for host, ports, label in known_items:
            if host in seen_hosts:
                continue
            matching = [c for c in connected if c.startswith(f"{host}:")]
            if matching:
                port_val = int(matching[0].split(":")[1]) if ":" in matching[0] and matching[0].split(":")[1].isdigit() else 0
                result.append({"name": label or "Android Wi-Fi", "displayName": label or "Android Wi-Fi",
                               "type": "_adb-tls-connect._tcp", "host": host, "port": port_val,
                               "address": matching[0], "model": label, "connected": True})
                seen_hosts.add(host)
                continue
            # Nếu đang quét làm mới (hoặc có known port còn mở TCP), đưa vào danh sách LAN để user bấm Kết nối 1 chạm!
            open_port = next((p for p in ports if _tcp_port_open(host, p, timeout=0.12)), None)
            if not open_port and refresh:
                open_port = probe_wireless_port(host, timeout=0.05, known_ports=ports)
            if open_port:
                self.remember_port(host, open_port, label)
                result.append({"name": label or "Android Wi-Fi", "displayName": label or "Android Wi-Fi",
                               "type": "_adb-tls-connect._tcp", "host": host, "port": open_port,
                               "address": f"{host}:{open_port}", "model": label, "connected": False})
                seen_hosts.add(host)

        # Nhận diện luôn các máy Wi-Fi đang kết nối ở dạng IP:port (vd 192.168.1.40:38667) dù mDNS tắt
        for ser in connected:
            if ":" in ser and not ser.startswith("127.0.0.1"):
                h, p_str = ser.split(":", 1)
                if h not in seen_hosts and p_str.isdigit():
                    self.remember_port(h, int(p_str))
                    lbl = self._known_labels.get(h, "")
                    result.append({"name": lbl or "Android Wi-Fi", "displayName": lbl or "Android Wi-Fi",
                                   "type": "_adb-tls-connect._tcp", "host": h, "port": int(p_str),
                                   "address": ser, "model": lbl, "connected": True})
                    seen_hosts.add(h)

        # Khi user chủ động bấm "Quét LAN" (refresh=True), quét nhanh thêm các IP trong ARP chưa phản hồi mDNS
        if refresh:
            arp_candidates = [h for h in _lan_arp_hosts() if h not in seen_hosts][:12]
            if arp_candidates:
                def _scan_one(target_host: str):
                    p = probe_wireless_port(target_host, timeout=0.04, known_ports=self.get_known_ports(target_host))
                    return (target_host, p)

                with ThreadPoolExecutor(max_workers=min(6, len(arp_candidates))) as pool:
                    for target_host, found_p in pool.map(_scan_one, arp_candidates):
                        # Cổng 49152 thường là RPC/AirPlay/UPnP của thiết bị không phải Android trong LAN;
                        # bỏ qua khi không đến từ mDNS ADB để tránh quét nhầm iPhone/PC/TV.
                        if found_p and found_p != 49152 and target_host not in seen_hosts:
                            lbl = self._known_labels.get(target_host, "")
                            # Không lưu vội vào wireless-hosts.json trước khi kết nối ADB thành công
                            result.append({"name": lbl or "Thiết bị LAN", "displayName": lbl or f"Thiết bị LAN (cổng {found_p})",
                                           "type": "_adb-tls-connect._tcp", "host": target_host, "port": found_p,
                                           "address": f"{target_host}:{found_p}", "model": lbl,
                                           "source": "scan", "connected": False})
                            seen_hosts.add(target_host)
        return result

    def auto_connect(self, connected: set[str]) -> None:
        """Thử `adb connect` máy LAN đã ghép nối nhưng chưa kết nối (cooldown theo địa chỉ)."""
        for service in self.discovered(connected):
            if service["connected"] or service.get("source") == "scan":
                continue
            if any(c.startswith(service["name"]) or service["name"].startswith(c) for c in connected):
                continue
            key = service["address"]
            if time.time() - self._attempts.get(key, 0) < 60:
                continue
            self._attempts[key] = time.time()
            # Chỉ tự động thử nếu cổng TCP thật sự mở (tránh gọi adb connect vào cổng ma gây treo)
            if not _tcp_port_open(service["host"], service["port"], timeout=0.15):
                continue
            try:
                out = str(self.adb.run(["connect", key], timeout=5, check=False).stdout).strip()
            except AdbError:
                continue
            if "connected to" in out and "cannot" not in out.lower() and "failed" not in out.lower():
                self.log(f"[Wi-Fi ADB] Tự kết nối {key}")
                if ":" in key:
                    h, p = key.split(":", 1)
                    if p.isdigit():
                        self.remember_port(h, int(p), service.get("model") or "")

    # ---- ghép nối QR ----------------------------------------------------
    def start_qr(self) -> dict:
        with self.lock:
            if self.session and self.session["status"] in ("WAITING", "PAIRING", "CONNECTING"):
                self.session["cancel"] = True
            name = "tiktokweb-" + secrets.token_hex(3)
            password = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(10))
            payload = f"WIFI:T:ADB;S:{name};P:{password};;"
            session = {"id": secrets.token_hex(6), "name": name, "password": password, "status": "WAITING",
                       "message": "Mở Gỡ lỗi không dây → Ghép nối thiết bị bằng mã QR rồi quét mã này.",
                       "startedAt": time.time(), "cancel": False, "address": "", "phoneIp": ""}
            self.session = session
        threading.Thread(target=self._qr_loop, args=(session,), daemon=True, name="wifi-qr").start()
        return {**self.view(), "svg": qr_svg(payload)}

    def cancel_qr(self) -> dict:
        with self.lock:
            if self.session and self.session["status"] in ("WAITING", "PAIRING", "CONNECTING"):
                self.session["cancel"] = True
                self.session["status"] = "CANCELLED"
                self.session["message"] = "Đã huỷ ghép nối QR."
        return self.view()

    def view(self) -> dict:
        with self.lock:
            session = self.session
            if not session:
                return {"status": "IDLE"}
            return {k: v for k, v in session.items() if k not in ("password", "cancel")}

    def _set(self, session: dict, status: str, message: str, **extra) -> None:
        with self.lock:
            session.update(status=status, message=message, **extra)
        self.log(f"[Wi-Fi QR] {message}")
        self.notify({"type": "wireless-qr", **self.view()})

    def _qr_loop(self, session: dict) -> None:
        deadline = time.time() + 180
        pairing = None
        while time.time() < deadline and not session["cancel"]:
            svcs = self.services(max_age=0, listen_sec=0.75)
            pairing = next((s for s in svcs if s["type"] == "_adb-tls-pairing._tcp"
                            and (s["name"] == session["name"] or s.get("rawName", "").startswith(session["name"]))), None)
            if pairing:
                break
            time.sleep(0.35)
        if session["cancel"]:
            return
        if not pairing:
            self._set(session, "FAILED", "Hết 3 phút chưa thấy điện thoại quét mã. Kiểm tra máy tính và điện thoại cùng mạng Wi-Fi/LAN rồi bấm Tạo mã mới.")
            return

        host = pairing["host"]
        self._set(session, "PAIRING", f"Đã thấy điện thoại tại {pairing['address']} — đang ghép nối…", phoneIp=host)
        try:
            res = self.adb.run(["pair", pairing["address"], session["password"]], timeout=25, check=False)
            out = f"{res.stdout}\n{res.stderr}".strip()
        except AdbError as exc:
            out = str(exc)
        if "Successfully" not in out:
            self._set(session, "FAILED", f"Ghép nối thất bại: {out or 'không có phản hồi'}", phoneIp=host)
            return

        self._set(session, "CONNECTING", f"Ghép nối thành công — đang tự động kết nối máy {host}…", phoneIp=host)
        try:
            initial_serials = {d["serial"] for d in self.adb.devices()}
        except Exception:
            initial_serials = set()

        connect_deadline = time.time() + 25
        while time.time() < connect_deadline and not session["cancel"]:
            # Lớp 1: Kiểm tra xem ADB daemon đã tự động kết nối TLS cho đúng host hoặc serial MỚI xuất hiện chưa
            try:
                cur_devices = self.adb.devices()
            except Exception:
                cur_devices = []
            for d in cur_devices:
                if d["state"] == "device":
                    ser = d["serial"]
                    if ser.startswith(f"{host}:") or (ser not in initial_serials and "._adb-tls-connect._tcp" in ser):
                        if ":" in ser and ser.split(":")[1].isdigit():
                            self.remember_port(host, int(ser.split(":")[1]), d.get("model") or "")
                        self._set(session, "CONNECTED", f"Đã kết nối {ser}. Điện thoại sẽ hiện trong danh sách thiết bị.",
                                  address=ser, phoneIp=host)
                        return

            # Lớp 2: Dò mDNS `_adb-tls-connect._tcp` qua mọi card mạng LAN cho đúng IP vừa ghép nối
            targets = [s for s in self.services(max_age=0, listen_sec=0.7)
                       if s["type"] == "_adb-tls-connect._tcp" and s["host"] == host]
            for target in targets:
                try:
                    out = str(self.adb.run(["connect", target["address"]], timeout=6, check=False).stdout).strip()
                except AdbError as exc:
                    out = str(exc)
                if "connected to" in out and "cannot" not in out.lower() and "failed" not in out.lower():
                    self.remember_port(host, target["port"], target.get("model") or "")
                    self._set(session, "CONNECTED", f"Đã kết nối {target['address']}. Điện thoại sẽ hiện trong danh sách thiết bị.",
                              address=target["address"], phoneIp=host)
                    return

            # Lớp 3: Tự động quét dải cổng Gỡ lỗi không dây đang mở trên host (30000..65535 + 5555)
            found_port = probe_wireless_port(host, timeout=0.05, known_ports=self.get_known_ports(host))
            if found_port:
                cand_addr = f"{host}:{found_port}"
                try:
                    out = str(self.adb.run(["connect", cand_addr], timeout=6, check=False).stdout).strip()
                except AdbError as exc:
                    out = str(exc)
                if "connected to" in out and "cannot" not in out.lower() and "failed" not in out.lower():
                    self.remember_port(host, found_port)
                    self._set(session, "CONNECTED", f"Đã kết nối {cand_addr}. Điện thoại sẽ hiện trong danh sách thiết bị.",
                              address=cand_addr, phoneIp=host)
                    return

            time.sleep(0.8)

        if session["cancel"]:
            return

        self._set(session, "NEEDS_PORT",
                  f"Ghép nối thành công với {host}! Nếu chưa tự kết nối, nhập cổng hiển thị ở 'Địa chỉ IP và cổng' trên điện thoại bên dưới để kết nối ngay:",
                  phoneIp=host)


# ---------------------------------------------------------------------------
# Chẩn đoán USB (Windows)
# ---------------------------------------------------------------------------
def usb_diagnose(adb_devices: list[dict]) -> dict:
    script = (
        "Get-PnpDevice -PresentOnly | Where-Object { $_.InstanceId -like 'USB*' } | "
        "ForEach-Object { '{0}|{1}|{2}|{3}' -f $_.Status, $_.Class, $_.FriendlyName, $_.InstanceId }"
    )
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True,
                             timeout=20, creationflags=CREATE_NO_WINDOW, encoding="utf-8", errors="replace").stdout
    except (OSError, subprocess.TimeoutExpired):
        out = ""
    phones = []
    for line in out.splitlines():
        parts = line.split("|", 3)
        if len(parts) < 4:
            continue
        status, cls, name, instance = parts
        match = re.search(r"VID_([0-9A-F]{4})", instance, re.I)
        vid = match.group(1).upper() if match else ""
        # Bỏ qua thiết bị Dcom/Modem USB (như ZTE Dcom 4G VID 19D2 & PID 1557/1225, card mạng NDIS...)
        if vid == "19D2" and re.search(r"PID_1557|PID_1225|NDIS|Internet Sharing|Storage|CDROM|Modem", f"{instance} {name}", re.I):
            continue
        if cls in ("Net", "CDROM", "DiskDrive") and not re.search(r"android|xiaomi|redmi|samsung|pixel|adb", name, re.I):
            continue
        if vid in ANDROID_VIDS or re.search(r"android|adb|mtp|xiaomi|redmi|samsung|pixel", f"{name} {cls}", re.I):
            phones.append({"status": status, "class": cls, "name": name, "vid": vid, "brand": ANDROID_VIDS.get(vid, ""),
                           "adbInterface": bool(re.search(r"adb|android composite", name, re.I)) or cls == "AndroidUsbDeviceClass"})
    usb_adb = [d for d in adb_devices if d.get("transport") == "usb"]
    unauthorized = [d for d in usb_adb if d.get("state") == "unauthorized"]
    hints: list[str] = []
    if any(d.get("state") == "device" for d in usb_adb):
        verdict, level = "Điện thoại USB đã kết nối ADB thành công.", "ok"
    elif unauthorized:
        verdict, level = "Đã thấy điện thoại nhưng CHƯA được cho phép gỡ lỗi.", "warn"
        hints = ["Mở khóa màn hình điện thoại, bấm “Cho phép” ở hộp thoại “Cho phép gỡ lỗi USB?” (tick “Luôn cho phép”).",
                 "Không thấy hộp thoại: vào Tùy chọn nhà phát triển → Hủy ủy quyền gỡ lỗi USB, rút cáp rồi cắm lại."]
    elif not phones:
        verdict, level = "Windows KHÔNG nhận thấy điện thoại qua USB (chỉ đang sạc).", "bad"
        hints = ["Cáp đang dùng rất có thể là cáp CHỈ SẠC — thay cáp dữ liệu (cáp zin hoặc cáp truyền được file).",
                 "Cắm thẳng vào cổng USB phía sau thân máy, không qua hub/đầu chia; thử cổng khác.",
                 "Trên điện thoại kéo thanh thông báo → chạm “Đang sạc thiết bị qua USB” → chọn “Truyền tệp”.",
                 "Hoặc dùng Wi-Fi ADB (không cần cáp): bấm “Ghép nối QR” bên trái."]
    elif not any(p["adbInterface"] for p in phones):
        verdict, level = "Windows thấy điện thoại nhưng chưa có giao diện ADB.", "warn"
        hints = ["Kiểm tra Tùy chọn nhà phát triển → Gỡ lỗi USB đang BẬT (Xiaomi: bật cả “Gỡ lỗi USB (Cài đặt bảo mật)”).",
                 "Tắt rồi bật lại Gỡ lỗi USB, rút cáp cắm lại.",
                 "Nếu Device Manager có thiết bị bị dấu “!”: cài Google USB Driver / Xiaomi USB Driver."]
    else:
        verdict, level = "Có giao diện ADB nhưng adb chưa liệt kê máy.", "warn"
        hints = ["Rút cáp cắm lại, mở khóa màn hình điện thoại.", "Bấm “Khởi động lại ADB” rồi thử lại."]
    return {"verdict": verdict, "level": level, "hints": hints, "usbDevices": phones,
            "adbUsb": [{"serial": d["serial"], "state": d["state"]} for d in usb_adb]}
