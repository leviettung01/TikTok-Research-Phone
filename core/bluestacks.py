"""Quản lý BlueStacks: đọc bluestacks.conf, bật instance, adb connect.

Port từ tool cũ tiktok_bluestacks_auto.py, giữ nguyên hành vi.
"""
from __future__ import annotations

import os
import re
import socket
import subprocess

import config


from concurrent.futures import ThreadPoolExecutor, as_completed


def port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.12) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def check_ports(ports: set[int] | list[int], host: str = "127.0.0.1", timeout: float = 0.12) -> dict[int, bool]:
    unique = set(ports)
    if not unique:
        return {}
    with ThreadPoolExecutor(max_workers=min(32, len(unique))) as pool:
        futures = {pool.submit(port_open, p, host, timeout): p for p in unique}
        return {futures[f]: f.result() for f in as_completed(futures)}


def _conf_file() -> str | None:
    for candidate in config.BLUESTACKS_CONF_PATHS:
        if os.path.isfile(candidate):
            return candidate
    return None


def list_instances() -> list[dict]:
    conf = _conf_file()
    if not conf:
        return []
    try:
        with open(conf, "r", encoding="utf-8", errors="ignore") as handle:
            lines = handle.readlines()
    except OSError:
        return []
    instances: dict[str, dict] = {}
    for line in lines:
        match = re.match(r'^bst\.instance\.([^.]+)\.([^=]+)="?(.*?)"?$', line.strip())
        if match:
            name, key, value = match.groups()
            instances.setdefault(name, {})[key] = value

    # Thu thập tất cả các port cần kiểm tra và quét song song chỉ mất ~0.05s
    all_ports: set[int] = set()
    for instance_id, data in instances.items():
        for key in ("status.adb_port", "adb_port"):
            val = data.get(key, "")
            if val.isdigit() and int(val) > 0:
                all_ports.add(int(val))

    port_status = check_ports(all_ports)

    result = []
    for instance_id, data in instances.items():
        candidate_ports = []
        for key in ("status.adb_port", "adb_port"):
            val = data.get(key, "")
            if val.isdigit() and int(val) > 0:
                p = int(val)
                if p not in candidate_ports:
                    candidate_ports.append(p)
        if not candidate_ports:
            continue
        
        # Chọn port đang mở (ưu tiên status.adb_port nếu mở)
        chosen_port = next((p for p in candidate_ports if port_status.get(p)), None)
        port = chosen_port or candidate_ports[0]
        online = bool(port_status.get(port, False))

        lowered = instance_id.lower()
        if lowered.startswith("rvc"):
            android = "11"
        elif lowered.startswith("pie"):
            android = "9"
        elif lowered.startswith("nougat"):
            android = "7"
        else:
            android = "11" if "11" in data.get("os_version", "") else "9"
        result.append({
            "id": instance_id,
            "name": data.get("display_name") or instance_id,
            "port": port,
            "serial": f"127.0.0.1:{port}",
            "androidVersion": android,
            "online": online,
        })
    result.sort(key=lambda item: (not item["online"], item["name"].lower()))
    return result


def launch_instance(instance_id: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", instance_id or ""):
        raise ValueError("Mã instance BlueStacks không hợp lệ.")
    for player in config.HD_PLAYER_PATHS:
        if os.path.isfile(player):
            try:
                subprocess.Popen([player, "--instance", instance_id],
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                return
            except OSError as exc:
                # Nếu yêu cầu quyền admin (WinError 740), gọi qua ShellExecute runas
                if getattr(exc, "winerror", None) == 740 or "elevation" in str(exc).lower():
                    import ctypes
                    ret = ctypes.windll.shell32.ShellExecuteW(None, "runas", player, f"--instance {instance_id}", None, 1)
                    if ret > 32:
                        return
                    # Fallback cmd start
                    ret_cmd = subprocess.run(["cmd", "/c", "start", "", player, "--instance", instance_id],
                                             capture_output=True)
                    if ret_cmd.returncode == 0:
                        return
                raise
    raise FileNotFoundError("Không tìm thấy HD-Player.exe của BlueStacks.")
