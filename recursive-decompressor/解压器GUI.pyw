#!/usr/bin/env python3
"""
递归解压器 GUI — 可视化操作界面。
双击运行, 拖拽文件, 填密码, 一键解压。

支持: ZIP / RAR / 7Z, 任意后缀, 多层嵌套, 分卷压缩, 尾部追加 ZIP
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk, simpledialog

# ── 拖拽支持 ───────────────────────────────────────────────────

try:
    from tkinterdnd2 import TkinterDnD
    _HAS_DND = True
except ImportError:
    _HAS_DND = False

def get_dropped_file() -> str | None:
    if len(sys.argv) > 1:
        path = sys.argv[1]
        if os.path.isfile(path):
            return path
    return None

def clean_dnd_path(raw: str) -> str:
    path = raw.strip()
    if path.startswith("{") and path.endswith("}"):
        path = path[1:-1]
    if path.startswith("file://"):
        path = path[7:]
    return path.strip()

# ── 7-Zip 路径探测 ──────────────────────────────────────────────

_7Z_PATHS = [
    r"C:\Program Files\7-Zip\7z.exe",
    r"C:\Program Files (x86)\7-Zip\7z.exe",
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

# ── 压缩包魔数检测 ───────────────────────────────────────────────

ARCHIVE_MAGICS = {
    "ZIP": (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"),
    "RAR": (b"Rar!\x1a\x07",),
    "7Z":  (b"7z\xbc\xaf\x27\x1c",),
}

def is_archive(filepath: str) -> bool:
    if archive_type(filepath) is not None:
        return True
    return _has_appended_zip(filepath)

def archive_type(filepath: str) -> str | None:
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

# ── 尾部 ZIP 检测 ───────────────────────────────────────────────

def _find_zip_eocd_offset(filepath: str) -> int | None:
    try:
        fsize = os.path.getsize(filepath)
        with open(filepath, "rb") as f:
            search_size = min(65536, fsize)
            f.seek(fsize - search_size)
            data = f.read(search_size)
            idx = data.rfind(b"PK\x05\x06")
            if idx >= 0:
                return fsize - search_size + idx
    except Exception:
        pass
    return None

def _has_appended_zip(filepath: str) -> bool:
    if archive_type(filepath) is not None:
        return False
    return _find_zip_eocd_offset(filepath) is not None

# ── 分卷检测 ────────────────────────────────────────────────────

_SPLIT_RE = re.compile(
    r'\.(?:0\d{2}|[1-9]\d{2,}|r\d{2}|part\d+\.rar|z\d{2})$',
    re.IGNORECASE
)

def _is_split_archive_parts(files: list[Path]) -> bool:
    if len(files) < 2:
        return False
    return any(_SPLIT_RE.search(f.name) for f in files)

# ── 解压 ─────────────────────────────────────────────────────────

def _extract_zip(filepath: str, dest: str, passwords: list[str]) -> tuple[bool, str | None]:
    exe = _find_7z()
    is_appended = _has_appended_zip(filepath)

    if is_appended:
        for pwd in [None] + passwords:
            try:
                import zipfile as zf_mod
                with zf_mod.ZipFile(filepath, "r") as zf:
                    zf.extractall(dest, pwd=pwd.encode("utf-8") if pwd else None)
                return True, pwd
            except RuntimeError as e:
                if "password" in str(e).lower():
                    continue
                return False, None
            except Exception:
                continue
        return False, None

    if exe:
        try:
            r = subprocess.run([exe, "x", filepath, f"-o{dest}", "-y"],
                               capture_output=True, text=True, timeout=120,
                               creationflags=subprocess.CREATE_NO_WINDOW)
            if r.returncode == 0:
                return True, None
        except Exception:
            pass
        for pwd in passwords:
            try:
                r = subprocess.run([exe, "x", filepath, f"-o{dest}", "-y", f"-p{pwd}"],
                                   capture_output=True, text=True, timeout=120,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
                if r.returncode == 0:
                    return True, pwd
            except Exception:
                pass
            try:
                import zipfile
                with zipfile.ZipFile(filepath, "r") as zf:
                    zf.extractall(dest, pwd=pwd.encode("utf-8"))
                return True, pwd
            except Exception:
                pass
    return False, None

def scan_for_archives(directory: str) -> list[str]:
    results = []
    for root, dirs, files in os.walk(directory):
        for fname in files:
            fpath = os.path.join(root, fname)
            if is_archive(fpath):
                results.append(fpath)
    return results


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

        self._build_ui()

        if _HAS_DND:
            self.root.drop_target_register("*")
            self.root.dnd_bind("<<Drop>>", self._on_window_drop)
            self.drop_zone.drop_target_register("*")
            self.drop_zone.dnd_bind("<<Drop>>", self._on_window_drop)

        dropped = get_dropped_file()
        if dropped:
            self.root.after(100, lambda: self._set_input_file(dropped))

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _set_input_file(self, filepath: str):
        self.file_var.set(filepath)
        parent = os.path.dirname(filepath)
        stem = Path(filepath).stem
        out_dir = os.path.join(parent, stem)
        self.out_var.set(out_dir)
        fname = os.path.basename(filepath)
        self.drop_label.configure(text=f"📄 {fname}")
        self.drop_hint.configure(text="点击更换文件 | 或继续拖入新文件")

    def _on_window_drop(self, event):
        path = clean_dnd_path(event.data)
        if os.path.isfile(path):
            self._set_input_file(path)

    # ── UI 构建 ───────────────────────────────────────────────

    def _build_ui(self):
        tk.Label(self.root, text="📦 递归解压器",
                 font=("Microsoft YaHei UI", 18, "bold"),
                 bg=BG, fg=ACCENT).pack(pady=(16, 2))
        tk.Label(self.root, text="拖入文件 → 填密码 → 一键解压到底",
                 font=("Microsoft YaHei UI", 9), bg=BG, fg=DIM).pack(pady=(0, 12))

        self.drop_zone = tk.Frame(self.root, bg=SURFACE, highlightbackground=ACCENT,
                                   highlightthickness=2, cursor="hand2")
        self.drop_zone.pack(fill="x", padx=20, pady=4, ipady=14)
        self.drop_zone.bind("<Button-1>", lambda e: self._browse_file())
        self.drop_label = tk.Label(self.drop_zone,
                                    text="📁  拖拽文件到此处 或 点击选择文件",
                                    font=("Microsoft YaHei UI", 13),
                                    bg=SURFACE, fg=ACCENT, cursor="hand2")
        self.drop_label.pack(pady=(10, 2))
        self.drop_label.bind("<Button-1>", lambda e: self._browse_file())
        self.drop_hint = tk.Label(self.drop_zone, 
                                   text="支持 zip / rar / 7z, 任意后缀, 自动识别魔数",
                                   font=("Microsoft YaHei UI", 8), bg=SURFACE, fg=DIM, cursor="hand2")
        self.drop_hint.pack(pady=(0, 8))
        self.drop_hint.bind("<Button-1>", lambda e: self._browse_file())

        self.file_var = tk.StringVar()

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
        self._add_context_menu(self.out_entry)

        pwd_frame = tk.Frame(self.root, bg=BG)
        pwd_frame.pack(fill="x", padx=20, pady=(8, 2))
        tk.Label(pwd_frame, text="🔑 密码 (每行一个, 顺序无所谓, 成功过的自动缓存)",
                 font=("Microsoft YaHei UI", 10), bg=BG, fg=FG).pack(anchor="w")
        self.pwd_text = tk.Text(pwd_frame, height=3, font=("Consolas", 10),
                                bg=SURFACE, fg=FG, insertbackground=FG,
                                relief="flat", wrap="none")
        self.pwd_text.pack(fill="x", pady=2)
        self._add_context_menu(self.pwd_text)

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

        self.progress = ttk.Progressbar(self.root, mode="indeterminate")
        self.progress.pack(fill="x", padx=20, pady=2)

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

        self.status_var = tk.StringVar(value="就绪 — 拖入文件或点击上方区域选择文件")
        tk.Label(self.root, textvariable=self.status_var,
                 font=("Microsoft YaHei UI", 8), bg="#11111b", fg=DIM,
                 anchor="w", padx=12, pady=4).pack(fill="x", side="bottom")

    # ── 日志 / 右键菜单 ────────────────────────────────────────

    def _log(self, msg: str):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", msg + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _set_status(self, msg: str):
        self.status_var.set(msg)

    def _add_context_menu(self, widget):
        menu = tk.Menu(widget, tearoff=0, bg=SURFACE, fg=FG,
                       activebackground=ACCENT, activeforeground="#1e1e2e",
                       font=("Microsoft YaHei UI", 9))
        menu.add_command(label="粘贴", command=lambda: self._paste_to(widget))
        menu.add_command(label="复制", command=lambda: self._copy_from(widget))
        menu.add_separator()
        menu.add_command(label="全选", command=lambda: self._select_all(widget))
        def show_menu(event):
            menu.tk_popup(event.x_root, event.y_root)
        widget.bind("<Button-3>", show_menu)
        widget.bind("<Button-2>", show_menu)

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

    # ── 交互 ──────────────────────────────────────────────────

    def _browse_file(self):
        path = filedialog.askopenfilename(title="选择要解压的文件")
        if path:
            self._set_input_file(path)

    def _browse_out(self):
        path = filedialog.askdirectory(title="选择输出目录")
        if path:
            self.out_var.set(path)

    def _cancel(self):
        self._cancelled = True

    def _on_close(self):
        self._cancelled = True
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
        if not os.path.isfile(filepath):
            messagebox.showerror("错误", f"文件不存在:\n{filepath}")
            return

        out_dir = self.out_var.get().strip()
        if not out_dir:
            parent = os.path.dirname(filepath)
            stem = Path(filepath).stem
            out_dir = os.path.join(parent, stem)
            self.out_var.set(out_dir)

        passwords = [p.strip() for p in self.pwd_text.get("1.0", "end").splitlines() if p.strip()]

        self._cancelled = False
        self.temp_dirs.clear()
        self.go_btn.configure(state="disabled", bg="#585b70")
        self.cancel_btn.configure(state="normal")
        self.progress.start(10)
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

        self._log(f"{'='*50}")
        self._log(f"📦 递归解压器")
        self._log(f"   输入: {filepath}")
        self._log(f"   输出: {out_dir}")
        self._log(f"   密码: {len(passwords)} 个")
        self._log(f"{'='*50}")

        threading.Thread(target=self._run, args=(filepath, out_dir, passwords), daemon=True).start()

    def _run(self, filepath: str, out_dir: str, passwords: list[str]):
        try:
            if not is_archive(filepath):
                self._ui(lambda: self._log("✗ 文件不是支持的压缩包格式"))
                return

            if _has_appended_zip(filepath):
                self._ui(lambda: self._log("🔍 检测到尾部追加 ZIP (头部是视频/图片等格式)"))

            known = list(passwords)
            layers = 0
            current = filepath
            final_output = Path(out_dir)

            while not self._cancelled:
                layers += 1
                dname = os.path.basename(current) or "(无后缀)"

                self._ui(lambda l=layers, d=dname: [
                    self._log(""),
                    self._log(f"┌─ 第 {l} 层: {d}"),
                    self._set_status(f"正在解压第 {l} 层..."),
                ])

                tmpdir = tempfile.mkdtemp(prefix=f"unzip_L{layers}_")
                self.temp_dirs.append(tmpdir)

                if known:
                    self._ui(lambda k=known: self._log(f"│ 尝试密码: {k[:4]}{'...' if len(k)>4 else ''}"))

                ok, used_pwd = _extract_zip(current, tmpdir, known)

                if not ok:
                    self._ui(lambda k=known, l=layers, d=dname: self._log(f"│ 密码不足, 已尝试: {k}"))
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
                    for _ in range(100):
                        if "pwd" in pwd_result:
                            break
                        time.sleep(0.1)
                    user_pwd = pwd_result.get("pwd")
                    if not user_pwd:
                        self._ui(lambda: self._log("│ ✗ 跳过 (无密码)"))
                        break
                    self._ui(lambda: self._log("│ 尝试用户输入密码..."))
                    ok2, p2 = _extract_zip(current, tmpdir, [user_pwd])
                    if not ok2:
                        self._ui(lambda: self._log("│ ✗ 密码错误"))
                        break
                    used_pwd = user_pwd
                    known.insert(0, user_pwd)

                if used_pwd:
                    self._ui(lambda p=used_pwd: self._log(f"│ ✓ 密码: {p}"))
                    if used_pwd not in known:
                        known.insert(0, used_pwd)
                else:
                    self._ui(lambda: self._log("│ ✓ 无密码"))

                zips = scan_for_archives(tmpdir)
                total = sum(1 for _ in Path(tmpdir).rglob("*") if _.is_file())
                self._ui(lambda t=total, z=len(zips): self._log(f"│ 解出 {t} 个文件, {z} 个是压缩包"))

                all_files = [f for f in Path(tmpdir).rglob("*") if f.is_file()]
                is_split = _is_split_archive_parts(all_files)
                if is_split:
                    self._ui(lambda: self._log("│ 🔗 检测到分卷压缩包, 继续解压"))

                if not is_split and (total >= 2 or not zips):
                    if total >= 2:
                        self._ui(lambda: self._log("│ ⏹ 文件数≥2, 停止递归 (保留内层压缩包不解压)"))

                    source_dir = Path(tmpdir)
                    while True:
                        items = list(source_dir.iterdir())
                        if len(items) == 1 and items[0].is_dir():
                            source_dir = items[0]
                        else:
                            break

                    final_output.mkdir(parents=True, exist_ok=True)
                    for item in source_dir.iterdir():
                        dest = final_output / item.name
                        if item.is_dir():
                            if dest.exists():
                                shutil.rmtree(dest)
                            shutil.copytree(item, dest)
                        else:
                            shutil.copy2(item, dest)
                    break

                if zips:
                    current = zips[0]
                elif is_split:
                    first_part = sorted(
                        [f for f in all_files if _SPLIT_RE.search(f.name)],
                        key=lambda f: f.name
                    )
                    if first_part:
                        current = str(first_part[0])
                    else:
                        self._ui(lambda: self._log("│ ✗ 无法识别分卷入口文件"))
                        break
                else:
                    break

            final_files = []
            if final_output.exists():
                final_files = sorted(
                    str(f) for f in final_output.rglob("*") if f.is_file()
                )

            self._ui(lambda l=layers, ff=final_files, fo=final_output, fp=filepath: [
                self._log(""),
                self._log(f"{'='*50}"),
                self._log(f"✅ 完成! 共 {l} 层, {len(ff)} 个最终文件"),
                self._log(f"   输出: {fo}"),
                *[self._log(f"   → {os.path.basename(f)}") for f in ff],
                self._log(f"{'='*50}"),
                self._set_status(f"完成 — {l} 层解压, {len(ff)} 个文件 → {fo}"),
                self._ask_delete_original(fp),
                self.root.after(500, self.root.destroy),
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
        self.root.after(0, fn)

    def _finish(self):
        self.progress.stop()
        self.go_btn.configure(state="normal", bg=GREEN)
        self.cancel_btn.configure(state="disabled")

    def _ask_delete_original(self, filepath: str):
        fname = os.path.basename(filepath)
        if messagebox.askyesno("解压完成", f"是否删除原文件?\n\n{fname}", parent=self.root):
            try:
                os.remove(filepath)
                self._log(f"🗑 已删除原文件: {fname}")
                self._set_status(f"完成 — 原文件已删除")
            except Exception as e:
                self._log(f"⚠ 删除原文件失败: {e}")

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    app = DecompressorGUI()
    app.run()
