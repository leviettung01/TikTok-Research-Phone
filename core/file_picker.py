"""Hộp thoại chọn file Windows gọi từ backend.

Trình duyệt không trả về đường dẫn tuyệt đối, nên backend mở hộp thoại tkinter
(giống Electron dialog trong Position Lab). Chỉ một hộp thoại tại một thời điểm.
"""
from __future__ import annotations

import threading

_dialog_lock = threading.Lock()

VIDEO_TYPES = [("Video", "*.mp4 *.mov *.m4v *.webm"), ("Tất cả", "*.*")]
EXCEL_TYPES = [("Excel / CSV", "*.xlsx *.xlsm *.csv"), ("Tất cả", "*.*")]
JSON_TYPES = [("Cài vị trí TikTok", "*.json"), ("Tất cả", "*.*")]
APK_TYPES = [("Android APK", "*.apk"), ("Tất cả", "*.*")]


def _with_root(action):
    if not _dialog_lock.acquire(timeout=0.5):
        raise RuntimeError("Đang có một hộp thoại chọn file mở. Hãy đóng nó trước.")
    try:
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        root.update()
        try:
            return action(root)
        finally:
            root.destroy()
    finally:
        _dialog_lock.release()


def pick_videos() -> list[str]:
    from tkinter import filedialog
    return _with_root(lambda root: list(filedialog.askopenfilenames(parent=root, title="Chọn video", filetypes=VIDEO_TYPES)))


def pick_folder() -> str:
    from tkinter import filedialog
    return _with_root(lambda root: filedialog.askdirectory(parent=root, title="Chọn thư mục video") or "")


def pick_excel() -> str:
    from tkinter import filedialog
    return _with_root(lambda root: filedialog.askopenfilename(parent=root, title="Chọn file Excel ID sản phẩm",
                                                              filetypes=EXCEL_TYPES) or "")


def pick_json() -> str:
    from tkinter import filedialog
    return _with_root(lambda root: filedialog.askopenfilename(parent=root, title="Nhập file cài vị trí",
                                                              filetypes=JSON_TYPES) or "")


def save_json(default_name: str) -> str:
    from tkinter import filedialog
    return _with_root(lambda root: filedialog.asksaveasfilename(parent=root, title="Xuất file cài vị trí",
                                                                initialfile=default_name, defaultextension=".json",
                                                                filetypes=JSON_TYPES) or "")


def pick_apk(initial_dir: str = "") -> str:
    from tkinter import filedialog
    kwargs = {"title": "Chọn file APK cần cài đặt", "filetypes": APK_TYPES}
    if initial_dir:
        kwargs["initialdir"] = initial_dir
    return _with_root(lambda root: filedialog.askopenfilename(parent=root, **kwargs) or "")

