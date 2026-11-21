"""JSON store ghi atomic (tmp → replace) để không hỏng dữ liệu khi tắt đột ngột."""
from __future__ import annotations

import json
import os
import threading
import uuid
from pathlib import Path
from typing import Any

_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()


def lock_for(path: Path) -> threading.RLock:
    key = str(Path(path).resolve())
    with _locks_guard:
        if key not in _locks:
            _locks[key] = threading.RLock()
        return _locks[key]


def read_json(path: Path, default: Any) -> Any:
    path = Path(path)
    with lock_for(path):
        try:
            return json.loads(path.read_text("utf-8"))
        except FileNotFoundError:
            return default
        except (json.JSONDecodeError, UnicodeDecodeError):
            # Giữ lại file hỏng để kiểm tra, không ghi đè mất dữ liệu.
            backup = path.with_suffix(path.suffix + f".corrupt-{uuid.uuid4().hex[:6]}")
            try:
                path.replace(backup)
            except OSError:
                pass
            return default


def write_json(path: Path, value: Any) -> None:
    path = Path(path)
    with lock_for(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f"{path.name}.tmp-{uuid.uuid4().hex}")
        try:
            temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", "utf-8")
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                try:
                    temporary.unlink()
                except OSError:
                    pass


def update_json(path: Path, default: Any, mutate):
    """Đọc → sửa → ghi trong cùng một khóa. `mutate` trả về giá trị mới (hoặc sửa tại chỗ và trả None)."""
    with lock_for(path):
        current = read_json(path, default)
        result = mutate(current)
        value = current if result is None else result
        write_json(path, value)
        return value
