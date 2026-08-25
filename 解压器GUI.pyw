#!/usr/bin/env python3
"""
递归解压器 GUI v2 — 拖拽文件 / 一键解压。
- 支持从资源管理器拖拽文件到窗口
- 输出目录默认: 输入文件同目录下, 以输入文件名(去后缀)命名的子目录
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk, simpledialog

# ── 密码缓存 ─────────────────────────────────────────────────────

_PWD_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "password_cache.json")

def _load_password_cache() -> list[str]:
    """加载历史密码 (按最近使用降序)"""
    try:
        with open(_PWD_CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        entries = data.get("passwords", [])
        entries.sort(key=lambda e: e.get("last_used", 0), reverse=True)
        return [e["pwd"] for e in entries]
    except Exception:
        return []

def _save_password_to_cache(pwd: str):
    """记录成功密码到历史缓存"""
    try:
        with open(_PWD_CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {"passwords": []}
    entries = data.get("passwords", [])
    for e in entries:
        if e["pwd"] == pwd:
            e["count"] = e.get("count", 1) + 1
            e["last_used"] = time.time()
            break
    else:
        entries.append({"pwd": pwd, "count": 1, "last_used": time.time()})
    data["passwords"] = entries[:50]  # 最多保留 50 个
    try:
        with open(_PWD_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def _add_manual_password(pwd: str):
    """手动添加密码到缓存"""
    if not pwd:
        return
    _save_password_to_cache(pwd)

# ── 拖拽支持 (tkinterdnd2) ─────────────────────────────────────

try:
    from tkinterdnd2 import TkinterDnD
    _HAS_DND = True
except ImportError:
    _HAS_DND = False

def get_dropped_file() -> str | None:
    """检查是否有通过 Explorer 拖拽传入的文件路径"""
    if len(sys.argv) > 1:
        path = sys.argv[1]
        if os.path.isfile(path):
            return path
    return None

def clean_dnd_path(raw: str) -> str:
    """清理拖拽传入的路径 (tkinterdnd2 可能用花括号包裹)"""
    path = raw.strip()
    # Windows 路径如果含空格会被 {...} 包裹
    if path.startswith("{") and path.endswith("}"):
        path = path[1:-1]
    # 可能带 file:// 前缀
    if path.startswith("file://"):
        path = path[7:]
    return path.strip()

# ── 7-Zip 路径探测 ──────────────────────────────────────────────

_7Z_PATHS = [
    r"C:\Program Files\7-Zip\7z.exe",
    r"C:\Program Files (x86)\7-Zip\7z.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\7-Zip\7z.exe"),
    os.path.expanduser(r"~/scoop/apps/7zip/current/7z.exe"),
    "7z", "7za", "7zz",
]

def _find_7z() -> str | None:
    for p in _7Z_PATHS:
        try:
            r = subprocess.run([p, "--help"], capture_output=True, timeout=5,
                               creationflags=subprocess.CREATE_NO_WINDOW)
            if r.returncode == 0:
                return p
        except Exception:
            continue
    return None

# ── LZ4 工具路径探测 ────────────────────────────────────────────

_LZ4_PATHS = [
    # 工具同目录 (推荐: 把 lz4.exe 复制到工具目录)
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "lz4.exe"),
    os.environ.get("LZ4_EXE", ""),
    r"C:\Program Files\lz4\lz4.exe",
    r"C:\Program Files (x86)\lz4\lz4.exe",
]

def _find_lz4() -> str | None:
    for p in _LZ4_PATHS:
        if not p:
            continue
        try:
            r = subprocess.run([p, "--version"], capture_output=True, timeout=5,
                               creationflags=subprocess.CREATE_NO_WINDOW)
            if r.returncode == 0:
                return p
        except Exception:
            continue
    return None

# ── 压缩包魔数检测 ───────────────────────────────────────────────

ARCHIVE_MAGICS = {
    "ZIP":  (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"),
    "RAR":  (b"Rar!\x1a\x07",),
    "7Z":   (b"7z\xbc\xaf\x27\x1c",),
    "GZIP": (b"\x1f\x8b",),               # .gz .tgz
    "BZ2":  (b"BZh",),                     # .bz2
    "LZ4":  (b"\x04\x22\x4d\x18",),        # LZ4 frame
}

def _is_tar_file(filepath: str) -> bool:
    """检测 tar 文件 (通过 header checksum 验证, tar 无固定魔数)"""
    try:
        with open(filepath, "rb") as f:
            header = f.read(512)
        if len(header) < 512:
            return False
        # 读取 checksum 字段 (offset 148-155, 8字节八进制ASCII)
        chksum_str = header[148:156]
        # 必须全是八进制数字或 null/空格
        for b in chksum_str:
            if b == 0 or b == 32:  # null or space
                break
            if b < 0x30 or b > 0x37:  # '0'-'7'
                return False
        if chksum_str[0] == 0:
            return False  # checksum 全零 = 空 tar
        return True
    except Exception:
        return False

def is_archive(filepath: str, use_7z_fallback: bool = False) -> bool:
    """检测是否为压缩包。use_7z_fallback 仅用于用户选择的初始文件"""
    if archive_type(filepath) is not None:
        return True
    if _has_appended_zip(filepath):
        return True
    if _is_tar_file(filepath):
        return True
    # 7z 启发式回退 — 慢, 仅用于初始文件
    if use_7z_fallback and _try_7z_detect(filepath):
        return True
    return False

_7z_detect_cache: dict[str, bool] = {}

def _try_7z_detect(filepath: str) -> bool:
    """让 7z 尝试识别文件 (处理魔数检测无法覆盖的混合格式)"""
    if filepath in _7z_detect_cache:
        return _7z_detect_cache[filepath]
    exe = _find_7z()
    if not exe:
        return False
    try:
        r = subprocess.run([exe, "l", filepath, "-slt"],
                           input="\n", capture_output=True, text=True, timeout=10,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        output = r.stdout + r.stderr
        is_ok = "Type = " in output or "Cannot open encrypted archive" in output
        _7z_detect_cache[filepath] = is_ok
        return is_ok
    except Exception:
        return False

def archive_type(filepath: str) -> str | None:
    """返回压缩包类型: 'ZIP' / 'RAR' / '7Z' / None"""
    try:
        with open(filepath, "rb") as f:
            header = f.read(8)
        for atype, magics in ARCHIVE_MAGICS.items():
            for m in magics:
                if header.startswith(m):
                    return atype
        return None
    except Exception:
        return None

# ── 尾部 ZIP 检测 (MP4+ZIP 混合文件等) ─────────────────────────
# 某些文件在开头是其他格式(MP4/PNG等), ZIP 数据追加在末尾

def _find_zip_eocd_offset(filepath: str) -> int | None:
    """在文件末尾 64KB 范围内搜索 ZIP EOCD 签名 (PK\x05\x06),
    返回 EOCD 在文件中的绝对偏移, 没有则返回 None"""
    try:
        fsize = os.path.getsize(filepath)
        with open(filepath, "rb") as f:
            search_size = min(65536, fsize)
            f.seek(fsize - search_size)
            data = f.read(search_size)
            # 从后往前搜 PK\x05\x06
            idx = data.rfind(b"PK\x05\x06")
            if idx >= 0:
                return fsize - search_size + idx
    except Exception:
        pass
    return None

def _has_appended_zip(filepath: str) -> bool:
    """检测文件是否有追加的 ZIP 数据 (头部不是 ZIP 但尾部有)"""
    if archive_type(filepath) is not None:
        return False  # 头就是压缩包, 不是追加模式
    return _find_zip_eocd_offset(filepath) is not None

# ── 解压 ─────────────────────────────────────────────────────────

import re

# 分卷压缩包特征: .001 .002 ... / .r00 .r01 ... / .part1.rar .part2.rar ...
_SPLIT_RE = re.compile(
    r'\.(?:0\d{2}|[1-9]\d{2,}|r\d{2}|part\d+\.rar|z\d{2})$',
    re.IGNORECASE
)

def _is_split_archive_parts(files: list[Path]) -> bool:
    """检测文件列表是否全为分卷压缩包的一部分"""
    if len(files) < 2:
        return False
    # 如果有 .001 或 .r00 存在, 就是分卷
    return any(_SPLIT_RE.search(f.name) for f in files)

def _extract_appended_zip(filepath: str, dest: str, password: str | None,
                         progress_cb=None, cancel_check=None, proc_holder=None) -> bool:
    """提取尾部追加 ZIP: 切出 ZIP 数据用 7z (快速) 或回退 zipfile"""
    exe = _find_7z()
    if not exe:
        return False

    import struct
    try:
        fsize = os.path.getsize(filepath)
        import zipfile as zf_mod
        with zf_mod.ZipFile(filepath, "r") as zf:
            infos = zf.infolist()
            if not infos:
                return False
            first_offset = infos[0].header_offset

        zip_tmp = os.path.join(os.path.dirname(dest), f"_zip_slice_{os.getpid()}.zip")
        with open(filepath, "rb") as src, open(zip_tmp, "wb") as dst:
            src.seek(first_offset)
            remaining = fsize - first_offset
            chunk = 8 * 1024 * 1024
            while remaining > 0:
                data = src.read(min(chunk, remaining))
                if not data:
                    break
                dst.write(data)
                remaining -= len(data)

        cmd = [exe, "x", zip_tmp, f"-o{dest}", "-y", "-bsp1"]
        if password:
            cmd.append(f"-p{password}")
        ok = _run_7z_with_progress(cmd, progress_cb, cancel_check=cancel_check,
                                   proc_holder=proc_holder)
        os.unlink(zip_tmp)
        return ok
    except Exception:
        return False

def _extract_lz4(filepath: str, dest: str, progress_cb=None,
                 cancel_check=None, proc_holder=None) -> bool:
    """LZ4 解压: lz4.exe -d input output"""
    exe = _find_lz4()
    if not exe:
        return False
    try:
        # 输出文件名: 去掉 .lz4 后缀, 没有则加 .out
        base = filepath
        if base.lower().endswith(".lz4"):
            base = base[:-4]
        else:
            base = base + ".out"
        out_path = os.path.join(dest, os.path.basename(base))
        # 检查同名输出是否已存在 (lz4 拒绝覆盖)
        if os.path.exists(out_path):
            os.unlink(out_path)
        proc = subprocess.Popen(
            [exe, "-d", filepath, out_path],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if proc_holder is not None:
            proc_holder(proc)
        # 读取输出 (lz4 有进度)
        last_pct = -1
        remainder = b""
        while True:
            if cancel_check and cancel_check():
                proc.kill()
                proc.wait()
                return False
            chunk = proc.stdout.read(8192)
            if not chunk:
                break
            data = remainder + chunk
            *lines, remainder = data.replace(b"\r", b"\n").split(b"\n")
            for line_bytes in lines:
                line = line_bytes.decode("utf-8", errors="replace")
                m = re.search(r'(\d{1,3})\s*MiB', line)
                if m and progress_cb:
                    pct = int(m.group(1))
                    if pct != last_pct:
                        progress_cb(min(pct, 100))
                        last_pct = pct
        proc.wait(timeout=600)
        return proc.returncode == 0 and os.path.isfile(out_path)
    except Exception:
        return False

def _extract_zip(filepath: str, dest: str, passwords: list[str],
                 progress_cb=None, cancel_check=None, proc_holder=None) -> tuple[bool, str | None]:
    """(成功, 使用的密码或None=无密码). progress_cb(percent: int) 可选"""
    exe = _find_7z()
    is_appended = _has_appended_zip(filepath)

    # LZ4 → 专用工具
    if archive_type(filepath) == "LZ4":
        ok = _extract_lz4(filepath, dest, progress_cb, cancel_check, proc_holder)
        return (ok, None) if ok else (False, None)

    # 尾部追加 ZIP → 切出数据用 7z
    if is_appended:
        for pwd in [None] + passwords:
            if _extract_appended_zip(filepath, dest, pwd, progress_cb, cancel_check, proc_holder):
                return True, pwd
        return False, None

    # 普通压缩包 → 用 7z 流式读取进度
    if exe:
        for pwd in [None] + passwords:
            cmd = [exe, "x", filepath, f"-o{dest}", "-y", "-bsp1"]
            if pwd:
                cmd.append(f"-p{pwd}")
            try:
                ok = _run_7z_with_progress(cmd, progress_cb,
                                           input_data="\n" if not pwd else None,
                                           cancel_check=cancel_check,
                                           proc_holder=proc_holder)
                if ok:
                    return True, pwd
            except Exception:
                pass
    return False, None

def _run_7z_with_progress(cmd: list, progress_cb, input_data=None,
                          cancel_check=None, proc_holder=None) -> bool:
    """运行 7z 并解析进度, 返回是否成功"""
    import re as _re
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        stdin=subprocess.PIPE if input_data else None,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if proc_holder is not None:
        proc_holder(proc)
    if input_data:
        proc.stdin.write(input_data.encode("utf-8", errors="replace"))
        proc.stdin.close()

    last_pct = -1
    remainder = b""
    while True:
        if cancel_check and cancel_check():
            proc.kill()
            proc.wait()
            return False
        chunk = proc.stdout.read(8192)
        if not chunk:
            break
        data = remainder + chunk
        *lines, remainder = data.replace(b"\r", b"\n").split(b"\n")
        for line_bytes in lines:
            line = line_bytes.decode("utf-8", errors="replace")
            m = _re.search(r'(\d{1,3})\s*%', line)
            if m and progress_cb:
                pct = int(m.group(1))
                if pct != last_pct:
                    progress_cb(pct)
                    last_pct = pct
    if remainder:
        line = remainder.decode("utf-8", errors="replace")
        m = _re.search(r'(\d{1,3})\s*%', line)
        if m and progress_cb:
            pct = int(m.group(1))
            if pct != last_pct:
                progress_cb(pct)
    proc.wait(timeout=600)
    return proc.returncode in (0, 1)


# ═══════════════════════════════════════════════════════════════════
# GUI
# ═══════════════════════════════════════════════════════════════════

BG      = "#1e1e2e"
FG      = "#cdd6f4"
ACCENT  = "#89b4fa"
SURFACE = "#313244"
BTN_BG  = "#45475a"
GREEN   = "#a6e3a1"
RED     = "#f38ba8"
DIM     = "#6c7086"


class DecompressorGUI:
    def __init__(self):
        if _HAS_DND:
            self.root = TkinterDnD.Tk()
        else:
            self.root = tk.Tk()
        self.root.title("📦 递归解压器")
        self.root.geometry("680x620")
        self.root.minsize(500, 480)
        self.root.configure(bg=BG)

        self._cancelled = False
        self.known_passwords: list[str] = []
        self.temp_dirs: list[str] = []
        self._current_proc = None  # 当前 7z 子进程, 用于取消
        self._progress_determinate = False  # 进度条是否已切为确定模式

        self._build_ui()

        # 拖拽支持 — tkinterdnd2 窗口内拖入
        if _HAS_DND:
            self.root.drop_target_register("*")
            self.root.dnd_bind("<<Drop>>", self._on_window_drop)
            self.drop_zone.drop_target_register("*")
            self.drop_zone.dnd_bind("<<Drop>>", self._on_window_drop)

        # 检查是否通过 Explorer 拖拽启动
        dropped = get_dropped_file()
        if dropped:
            self.root.after(100, lambda: self._set_input_file(dropped))

        # 关闭时清理
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _derive_output_dir(self, filepath: str) -> str:
        """推导输出目录: 输入文件同目录 + 文件名(去后缀)。
        无扩展名或与输入文件同名时加 _解压 后缀"""
        parent = os.path.dirname(filepath)
        stem = Path(filepath).stem
        out_dir = os.path.join(parent, stem)
        if os.path.isfile(out_dir):
            out_dir = os.path.join(parent, stem + "_解压")
        return out_dir

    def _set_input_file(self, filepath: str):
        """设置输入文件并自动推导输出目录"""
        self.file_var.set(filepath)
        self.out_var.set(self._derive_output_dir(filepath))
        # 更新拖拽区域显示
        fname = os.path.basename(filepath)
        self.drop_label.configure(text=f"📄 {fname}")
        self.drop_hint.configure(text="点击更换文件 | 或继续拖入新文件")

    def _on_window_drop(self, event):
        """窗口内拖拽文件事件"""
        path = clean_dnd_path(event.data)
        if os.path.isfile(path):
            self._set_input_file(path)

    # ── UI 构建 ───────────────────────────────────────────────

    def _build_ui(self):
        # 标题
        tk.Label(self.root, text="📦 递归解压器",
                 font=("Microsoft YaHei UI", 18, "bold"),
                 bg=BG, fg=ACCENT).pack(pady=(16, 2))
        tk.Label(self.root, text="拖入文件 → 填密码 → 一键解压到底",
                 font=("Microsoft YaHei UI", 9), bg=BG, fg=DIM).pack(pady=(0, 12))

        # ── 拖拽区域 ──
        self.drop_zone = tk.Frame(self.root, bg=SURFACE, highlightbackground=ACCENT,
                                   highlightthickness=2, cursor="hand2")
        self.drop_zone.pack(fill="x", padx=20, pady=4, ipady=14)
        self.drop_zone.bind("<Button-1>", lambda e: self._browse_file())
        # 让内部 label 也响应点击
        self.drop_label = tk.Label(self.drop_zone,
                                    text="📁  拖拽文件到此处 或 点击选择文件",
                                    font=("Microsoft YaHei UI", 13),
                                    bg=SURFACE, fg=ACCENT, cursor="hand2")
        self.drop_label.pack(pady=(10, 2))
        self.drop_label.bind("<Button-1>", lambda e: self._browse_file())
        self.drop_hint = tk.Label(self.drop_zone, text="支持 zip / rar / 7z, 任意后缀 (.jpg .png .bin 无后缀等), 自动识别魔数",
                                   font=("Microsoft YaHei UI", 8), bg=SURFACE, fg=DIM, cursor="hand2")
        self.drop_hint.pack(pady=(0, 8))
        self.drop_hint.bind("<Button-1>", lambda e: self._browse_file())

        # 隐藏的输入文件变量
        self.file_var = tk.StringVar()

        # ── 输出目录 ──
        out_frame = tk.Frame(self.root, bg=BG)
        out_frame.pack(fill="x", padx=20, pady=(8, 2))
        tk.Label(out_frame, text="📂 输出目录", font=("Microsoft YaHei UI", 10),
                 bg=BG, fg=FG).pack(anchor="w")
        out_row = tk.Frame(out_frame, bg=BG)
        out_row.pack(fill="x", pady=2)
        self.out_var = tk.StringVar()
        self.out_entry = tk.Entry(out_row, textvariable=self.out_var,
                                   font=("Consolas", 9), bg=SURFACE, fg=FG,
                                   insertbackground=FG, relief="flat")
        self.out_entry.pack(side="left", fill="x", expand=True, ipady=3)
        tk.Button(out_row, text="更改...", command=self._browse_out,
                  bg=BTN_BG, fg=FG, relief="flat", cursor="hand2",
                  font=("Microsoft YaHei UI", 9)).pack(side="left", padx=(6, 0))

        # ── 密码 ──
        pwd_frame = tk.Frame(self.root, bg=BG)
        pwd_frame.pack(fill="x", padx=20, pady=(8, 2))
        tk.Label(pwd_frame, text="🔑 密码 (每行一个, 顺序无所谓, 成功过的自动缓存)",
                 font=("Microsoft YaHei UI", 10), bg=BG, fg=FG).pack(anchor="w")
        self.pwd_text = tk.Text(pwd_frame, height=3, font=("Consolas", 10),
                                bg=SURFACE, fg=FG, insertbackground=FG,
                                relief="flat", wrap="none")
        self.pwd_text.pack(fill="x", pady=2)
        # 右键菜单
        self._add_context_menu(self.pwd_text)
        self._add_context_menu(self.out_entry)

        # ── 历史密码行 ──
        hist_frame = tk.Frame(self.root, bg=BG)
        hist_frame.pack(fill="x", padx=20, pady=(2, 4))
        self.history_passwords = _load_password_cache()
        self.history_var = tk.StringVar()
        self.history_combo = ttk.Combobox(hist_frame, textvariable=self.history_var,
                                           values=self.history_passwords,
                                           font=("Consolas", 9), state="readonly",
                                           width=28)
        self.history_combo.pack(side="left")
        self.history_combo.bind("<<ComboboxSelected>>", self._history_selected)
        tk.Button(hist_frame, text="加入历史", command=self._add_current_pwd,
                  bg=BTN_BG, fg=FG, relief="flat", cursor="hand2",
                  font=("Microsoft YaHei UI", 9)).pack(side="left", padx=(6, 0))
        self.use_history_var = tk.BooleanVar(value=True)
        tk.Checkbutton(hist_frame, text="使用历史密码", variable=self.use_history_var,
                       bg=BG, fg=FG, selectcolor=SURFACE, activebackground=BG,
                       activeforeground=FG, font=("Microsoft YaHei UI", 9),
                       cursor="hand2").pack(side="left", padx=(8, 0))

        # ── 按钮 ──
        btn_frame = tk.Frame(self.root, bg=BG)
        btn_frame.pack(fill="x", padx=20, pady=8)
        self.go_btn = tk.Button(btn_frame, text="▶  开始解压", command=self._start,
                                bg=GREEN, fg="#1e1e2e",
                                font=("Microsoft YaHei UI", 12, "bold"),
                                relief="flat", cursor="hand2", padx=28, pady=6)
        self.go_btn.pack(side="left")
        self.cancel_btn = tk.Button(btn_frame, text="✕ 取消", command=self._cancel,
                                    bg=BTN_BG, fg=FG, font=("Microsoft YaHei UI", 10),
                                    relief="flat", cursor="hand2", padx=16, pady=6,
                                    state="disabled")
        self.cancel_btn.pack(side="left", padx=8)

        # 进度条
        self.progress = ttk.Progressbar(self.root, mode="indeterminate")
        self.progress.pack(fill="x", padx=20, pady=2)

        # ── 日志 ──
        log_frame = tk.Frame(self.root, bg=BG)
        log_frame.pack(fill="both", expand=True, padx=20, pady=(4, 12))
        tk.Label(log_frame, text="📋 运行日志", font=("Microsoft YaHei UI", 10),
                 bg=BG, fg=FG).pack(anchor="w")
        self.log_text = tk.Text(log_frame, font=("Consolas", 9),
                                bg="#11111b", fg="#a6adc8",
                                insertbackground=FG, relief="flat",
                                state="disabled", wrap="word")
        self.log_text.pack(fill="both", expand=True, pady=2, side="left")
        scrollbar = tk.Scrollbar(log_frame, command=self.log_text.yview)
        scrollbar.pack(side="right", fill="y")
        self.log_text.configure(yscrollcommand=scrollbar.set)
        self._add_context_menu(self.log_text)

        # 状态栏
        self.status_var = tk.StringVar(value="就绪 — 拖入文件或点击上方区域选择文件")
        tk.Label(self.root, textvariable=self.status_var,
                 font=("Microsoft YaHei UI", 8), bg="#11111b", fg=DIM,
                 anchor="w", padx=12, pady=4).pack(fill="x", side="bottom")

    # ── 日志 / 状态 ───────────────────────────────────────────

    def _log(self, msg: str):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", msg + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _set_status(self, msg: str):
        self.status_var.set(msg)

    # ── 交互 ──────────────────────────────────────────────────

    def _history_selected(self, event=None):
        """选中历史密码 → 填入密码框"""
        pwd = self.history_var.get().strip()
        if pwd:
            self.pwd_text.insert("end", pwd + "\n")

    def _add_current_pwd(self):
        """把密码框当前内容加入历史"""
        pwds = [p.strip() for p in self.pwd_text.get("1.0", "end").splitlines() if p.strip()]
        for p in pwds:
            _add_manual_password(p)
        self.history_passwords = _load_password_cache()
        self.history_combo.configure(values=self.history_passwords)
        self._log(f"🔑 已加入历史: {len(pwds)} 个密码")

    def _browse_file(self):
        path = filedialog.askopenfilename(title="选择要解压的文件")
        if path:
            self._set_input_file(path)

    def _browse_out(self):
        path = filedialog.askdirectory(title="选择输出目录")
        if path:
            self.out_var.set(path)

    def _add_context_menu(self, widget):
        """为输入框添加右键菜单 (粘贴/复制/剪切)"""
        menu = tk.Menu(widget, tearoff=0, bg=SURFACE, fg=FG,
                       activebackground=ACCENT, activeforeground="#1e1e2e",
                       font=("Microsoft YaHei UI", 9))
        menu.add_command(label="粘贴", command=lambda: self._paste_to(widget))
        menu.add_command(label="复制", command=lambda: self._copy_from(widget))
        menu.add_separator()
        menu.add_command(label="全选", command=lambda: self._select_all(widget))

        def show_menu(event):
            menu.tk_popup(event.x_root, event.y_root)
        widget.bind("<Button-3>", show_menu)  # Windows 右键
        widget.bind("<Button-2>", show_menu)  # macOS/Linux 中键

    def _paste_to(self, widget):
        try:
            text = widget.clipboard_get()
            if isinstance(widget, tk.Text):
                widget.insert("insert", text)
            else:
                widget.insert("insert", text)
        except Exception:
            pass

    def _copy_from(self, widget):
        try:
            if isinstance(widget, tk.Text):
                sel = widget.get("sel.first", "sel.last")
            else:
                sel = widget.selection_get()
            widget.clipboard_clear()
            widget.clipboard_append(sel)
        except Exception:
            pass

    def _select_all(self, widget):
        if isinstance(widget, tk.Text):
            widget.tag_add("sel", "1.0", "end")
        else:
            widget.select_range(0, "end")

    def _cancel(self):
        self._cancelled = True
        self._log("⚠ 正在取消...")
        self.progress.stop()
        self.progress.configure(mode="indeterminate")
        if self._current_proc:
            try:
                self._current_proc.kill()
            except Exception:
                pass

    def _on_close(self):
        self._cancelled = True
        if self._current_proc:
            try:
                self._current_proc.kill()
            except Exception:
                pass
        for d in self.temp_dirs:
            try:
                shutil.rmtree(d, ignore_errors=True)
            except Exception:
                pass
        self.root.destroy()

    # ── 主流程 ────────────────────────────────────────────────

    def _start(self):
        filepath = self.file_var.get().strip()
        if not filepath:
            messagebox.showwarning("提示", "请先拖入或选择一个文件")
            return

        # 支持目录
        if os.path.isdir(filepath):
            items = [os.path.join(filepath, f) for f in os.listdir(filepath)
                     if os.path.isfile(os.path.join(filepath, f))]
            if not items:
                messagebox.showinfo("提示", f"目录为空:\n{filepath}")
                return

            # 规则1: 只有1个文件 → 直接解压它
            if len(items) == 1:
                self.out_var.set(self._derive_output_dir(items[0]))
                self.file_var.set(items[0])
                # 递归: 单文件流程
                filepath = items[0]
                # fall through to single-file path below
            else:
                # 规则2: 检测分卷 (.001 .002) → 用 001 作为入口
                from pathlib import Path as _Path
                all_files = [_Path(f) for f in items]
                if _is_split_archive_parts(all_files):
                    first = sorted(
                        [f for f in all_files if _SPLIT_RE.search(f.name)],
                        key=lambda f: f.name
                    )[0]
                    self.out_var.set(self._derive_output_dir(str(first)))
                    self.file_var.set(str(first))
                    filepath = str(first)
                    # fall through to single-file path
                else:
                    messagebox.showinfo("提示",
                        f"目录含 {len(items)} 个文件，非分卷格式，无法自动确定目标。\n"
                        f"请直接右键点击具体文件。")
                    return

        if not os.path.isfile(filepath):
            messagebox.showerror("错误", f"文件不存在:\n{filepath}")
            return

        out_dir = self.out_var.get().strip()
        if not out_dir:
            out_dir = self._derive_output_dir(filepath)
            self.out_var.set(out_dir)

        # 清理该输出目录的历史残留临时目录 (上次强杀/崩溃留下的)
        out_parent = os.path.dirname(out_dir)
        out_base = os.path.basename(out_dir)
        if os.path.isdir(out_parent):
            for d in os.listdir(out_parent):
                if d.startswith(out_base + "_temp_L"):
                    shutil.rmtree(os.path.join(out_parent, d), ignore_errors=True)

        passwords = [p.strip() for p in self.pwd_text.get("1.0", "end").splitlines() if p.strip()]
        # 勾选"使用历史密码"时追加历史 (去重, 用户输入优先)
        if self.use_history_var.get():
            for p in self.history_passwords:
                if p not in passwords:
                    passwords.append(p)

        self._cancelled = False
        self.temp_dirs.clear()
        self.go_btn.configure(state="disabled", bg="#585b70")
        self.cancel_btn.configure(state="normal")
        self.progress.start(10)
        # 清日志
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

        self._log(f"{'='*50}")
        self._log(f"📦 递归解压器")
        self._log(f"   输入: {filepath}")
        self._log(f"   输出: {out_dir}")
        self._log(f"   密码: {len(passwords)} 个" + (" (含历史)" if self.use_history_var.get() else ""))
        self._log(f"{'='*50}")

        threading.Thread(target=self._run, args=(filepath, out_dir, passwords), daemon=True).start()

    def _run(self, filepath: str, out_dir: str, passwords: list[str]):
        try:
            if not is_archive(filepath, use_7z_fallback=True):
                self._ui(lambda: self._log("✗ 文件不是支持的压缩包格式"))
                return

            # 提示尾部追加 ZIP 等特殊格式
            if _has_appended_zip(filepath):
                fsize_mb = os.path.getsize(filepath) / 1024 / 1024
                self._ui(lambda s=fsize_mb: (
                    self._log(f"🔍 检测到尾部追加 ZIP ({s:.0f} MB, 头部是视频/图片)"),
                    self._log("⏳ 大文件解密提取较慢, 请耐心等待...") if s > 500 else None,
                ))

            known = list(passwords)
            layers = 0
            current = filepath
            final_output = Path(out_dir)
            success = False  # 只有正常终止才为 True

            # 进度回调 (线程安全, 更新进度条和日志)
            def _progress(pct: int):
                def _update(p=pct, l=layers):
                    self._set_status(f"正在解压第 {l} 层... {p}%")
                    self._set_progress(p)
                    # 每 20% 在日志中也输出一次
                    if p % 20 == 0:
                        self._log(f"│ 进度: {p}%")
                self._ui(_update)

            while not self._cancelled:
                if self._cancelled:
                    break
                layers += 1
                dname = os.path.basename(current) or "(无后缀)"

                self._ui(lambda l=layers, d=dname, c=current: [
                    self._log(""),
                    self._log(f"┌─ 第 {l} 层: {d} ({os.path.getsize(c)/1024/1024:.0f} MB)"),
                    self._set_status(f"正在解压第 {l} 层..."),
                ])

                tmpdir = str(final_output) + f"_temp_L{layers}"
                # 清理上次残留的同名目录 (强杀/崩溃时 finally 未执行)
                if os.path.exists(tmpdir):
                    shutil.rmtree(tmpdir, ignore_errors=True)
                os.makedirs(tmpdir, exist_ok=True)
                self.temp_dirs.append(tmpdir)

                if known:
                    self._ui(lambda k=known: self._log(f"│ 尝试密码: {k[:4]}{'...' if len(k)>4 else ''}"))

                ok, used_pwd = _extract_zip(current, tmpdir, known, _progress,
                                           cancel_check=lambda: self._cancelled,
                                           proc_holder=lambda p: setattr(self, '_current_proc', p))

                if not ok:
                    if self._cancelled:
                        break
                    self._ui(lambda k=known, l=layers, d=dname: self._log(f"│ 密码不足, 已尝试: {k}"))
                    # 弹窗问密码
                    pwd_result = {}
                    _layers = layers
                    _dname = dname
                    def _ask():
                        p = simpledialog.askstring(
                            f"第 {_layers} 层需要密码",
                            f"文件: {_dname}\n\n已尝试: {known[:5]}\n请输入密码 (取消则停止):",
                            parent=self.root
                        )
                        pwd_result["pwd"] = p
                    self._ui(_ask)
                    # 等用户响应 (无超时, simpledialog 是阻塞的)
                    import time
                    while "pwd" not in pwd_result:
                        time.sleep(0.1)
                    user_pwd = (pwd_result.get("pwd") or "").strip()
                    if not user_pwd:
                        self._ui(lambda: self._log("│ ✗ 跳过"))
                        break
                    self._ui(lambda: self._log(f"│ 尝试用户输入密码..."))
                    ok2, p2 = _extract_zip(current, tmpdir, [user_pwd], _progress,
                                           cancel_check=lambda: self._cancelled,
                                           proc_holder=lambda p: setattr(self, '_current_proc', p))
                    if not ok2:
                        self._ui(lambda: self._log("│ ✗ 密码错误"))
                        break
                    used_pwd = user_pwd
                    known.insert(0, user_pwd)

                if used_pwd:
                    self._ui(lambda p=used_pwd: self._log(f"│ ✓ 密码: {p}"))
                    if used_pwd not in known:
                        known.insert(0, used_pwd)
                    # 记录到历史缓存 (局部性: 内层大概率同密码)
                    _save_password_to_cache(used_pwd)
                else:
                    self._ui(lambda: self._log("│ ✓ 无密码"))

                # ── 决策: 继续解压还是停止 ──────────────────────
                # 先扫描顶层
                top_items = list(Path(tmpdir).iterdir())
                top_files = [f for f in top_items if f.is_file()]
                top_dirs = [d for d in top_items if d.is_dir()]
                zips = [f for f in top_files if is_archive(str(f))]
                total = len(top_items)
                self._ui(lambda t=total, z=len(zips): self._log(f"│ 解出 {t} 项 ({len(top_files)} 文件, {len(top_dirs)} 目录), {len(zips)} 个压缩包"))

                # 1. 如果只有 1 个目录 → 穿透进去看里面的内容
                inner_dir = None
                if len(top_items) == 1 and top_dirs:
                    inner_dir = top_dirs[0]
                    top_items = list(inner_dir.iterdir())
                    top_files = [f for f in top_items if f.is_file()]
                    top_dirs = [d for d in top_items if d.is_dir()]
                    zips = [f for f in top_files if is_archive(str(f))]
                    total = len(top_items)
                    self._ui(lambda t=total, z=len(zips): self._log(f"│ ↳ 穿透目录, 内共 {t} 项 ({len(top_files)} 文件, {len(top_dirs)} 目录), {len(zips)} 个压缩包"))

                # 2. 分卷检测
                is_split = _is_split_archive_parts(top_files)
                if is_split:
                    self._ui(lambda: self._log("│ 🔗 检测到分卷压缩包, 继续解压"))

                # 3. 终止判断:
                #    - 有子目录 → 真实内容, 停
                #    - ≥2 个普通文件 → 真实内容, 停
                #    - 无压缩包 → 到底了, 停
                #    - 1 个压缩包 → 继续解压
                should_stop = False
                stop_reason = ""
                if is_split:
                    should_stop = False
                elif top_dirs:
                    should_stop = True
                    stop_reason = "包含子目录"
                elif total >= 2 and not zips:
                    should_stop = True
                    stop_reason = f"{total} 个文件均非压缩包"
                elif total >= 2 and zips:
                    # 有多个文件, 其中有些是压缩包 — 但仍应停止 (混合内容 = 真实内容)
                    should_stop = True
                    stop_reason = f"{total} 个文件 (含 {len(zips)} 个压缩包), 混合内容视为最终"
                elif total == 1 and not zips:
                    should_stop = True
                    stop_reason = "单文件非压缩包"
                elif not zips and total == 0:
                    should_stop = True
                    stop_reason = "空目录"
                # else: 1 file that IS a zip → continue

                if should_stop:
                    self._ui(lambda r=stop_reason: self._log(f"│ ⏹ {r}, 停止递归"))

                    # 展平中间包裹层
                    source_dir = inner_dir if inner_dir else Path(tmpdir)
                    # 展平: 穿透单层目录包裹
                    while True:
                        items = list(source_dir.iterdir())
                        if len(items) == 1 and items[0].is_dir():
                            source_dir = items[0]
                        else:
                            break  # 0项(空目录) 或 ≥2项 或 单文件 → 停止

                    self._ui(lambda: self._log("│ 📁 正在复制最终文件..."))
                    final_output.mkdir(parents=True, exist_ok=True)
                    for item in source_dir.iterdir():
                        dest = final_output / item.name
                        if item.is_dir():
                            if dest.exists():
                                shutil.rmtree(dest)
                            self._ui(lambda n=item.name: self._log(f"│   → {n}/"))
                            shutil.copytree(item, dest)
                        else:
                            self._ui(lambda n=item.name: self._log(f"│   → {n}"))
                            shutil.copy2(item, dest)
                    self._ui(lambda: self._log("│ 📁 复制完成"))
                    success = True
                    break

                # 选下一个要解压的文件
                if zips:
                    current = str(zips[0])
                elif is_split:
                    first_part = sorted(
                        [f for f in top_files if _SPLIT_RE.search(f.name)],
                        key=lambda f: f.name
                    )
                    if first_part:
                        current = str(first_part[0])
                    else:
                        self._ui(lambda: self._log("│ ✗ 无法识别分卷入口文件"))
                        break
                else:
                    break

                # 清理上一层的临时目录, 释放磁盘空间
                if len(self.temp_dirs) > 1:
                    old = self.temp_dirs.pop(0)
                    try:
                        shutil.rmtree(old, ignore_errors=True)
                    except Exception:
                        pass

            # 结果: 只统计不列举
            if success:
                final_files = []
                if final_output.exists():
                    final_files = [f for f in final_output.iterdir()]

                self._ui(lambda l=layers, ff=final_files, fo=final_output, fp=filepath: [
                    self._log(""),
                    self._log(f"{'='*50}"),
                    self._log(f"✅ 完成! 共 {l} 层, {len(ff)} 个最终文件/目录"),
                    self._log(f"   输出: {fo}"),
                    self._log(f"{'='*50}"),
                    self._set_status(f"完成 — {l} 层解压 → {fo}"),
                    self._ask_delete_original(fp),
                    self.root.after(500, self.root.destroy),
                ])
            else:
                # 失败时保留当前层文件到输出目录
                if current and os.path.isfile(current):
                    final_output.mkdir(parents=True, exist_ok=True)
                    preserved = os.path.join(str(final_output), os.path.basename(current))
                    try:
                        shutil.copy2(current, preserved)
                        self._ui(lambda p=preserved: self._log(f"📁 失败层文件已保留: {os.path.basename(p)}"))
                    except Exception:
                        pass
                self._ui(lambda: [
                    self._set_status("解压中断 — 窗口保持, 请查看日志"),
                ])

        except Exception as e:
            self._ui(lambda e=e: [
                self._log(f"✗ 异常: {e}"),
                self._set_status("解压失败"),
                messagebox.showerror("解压失败", f"解压过程中出现错误:\n\n{e}"),
            ])
        finally:
            self._ui(self._finish)
            for d in self.temp_dirs:
                try:
                    shutil.rmtree(d, ignore_errors=True)
                except Exception:
                    pass

    def _ui(self, fn):
        """在线程安全地在 UI 线程执行"""
        self.root.after(0, fn)

    def _finish(self):
        self.progress.stop()
        self.progress.configure(mode="indeterminate")
        self._progress_determinate = False
        self.go_btn.configure(state="normal", bg=GREEN)
        self.cancel_btn.configure(state="disabled")

    def _set_progress(self, pct: int):
        """更新进度条为确定百分比模式"""
        if not self._progress_determinate:
            self.progress.stop()
            self.progress.configure(mode="determinate", maximum=100)
            self._progress_determinate = True
        self.progress.configure(value=pct)

    def _ask_delete_original(self, filepath: str):
        """解压成功后询问是否删除原文件 (分卷时删除整个分卷家族)"""
        fname = os.path.basename(filepath)
        if not messagebox.askyesno("解压完成", f"是否删除原文件?\n\n{fname}", parent=self.root):
            return
        targets = [filepath]
        # 分卷家族: xxx.001/002, xxx.part1.rar/part2.rar, xxx.r00/r01, xxx.7z.001
        if _SPLIT_RE.search(fname):
            base = _SPLIT_RE.sub("", fname)
            d = os.path.dirname(filepath)
            for f in os.listdir(d):
                if f.startswith(base) and _SPLIT_RE.search(f):
                    targets.append(os.path.join(d, f))
        for t in targets:
            try:
                os.remove(t)
                self._log(f"🗑 已删除: {os.path.basename(t)}")
            except Exception as e:
                self._log(f"⚠ 删除失败: {os.path.basename(t)} ({e})")

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    app = DecompressorGUI()
    app.run()
