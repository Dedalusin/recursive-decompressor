#!/usr/bin/env python3
"""
递归解压器 — 智能解压嵌套/伪装 zip 压缩包。

场景：zip 压缩 → 改后缀(.jpg/.png/无后缀) → 再压缩 → 再改后缀 ……
本工具自动识别 zip 魔数(不管后缀名), 递归解压到最内层,
密码自动缓存复用, 无需手动反复输入。

用法:
  python recursive-decompressor.py <输入文件> [-o 输出目录] [-p 密码1 密码2 ...]

特性:
  - 魔数检测: 读取文件头 PK\x03\x04, 不依赖后缀名
  - 密码缓存: 成功过的密码自动重试下一层
  - 7z 优先: 支持 AES-256 加密 (7-Zip/WinRAR 常用)
  - zipfile 回退: 7z 不可用时用 Python 标准库
  - 递归清理: 中间文件自动清理, 只保留最终结果
"""

import argparse
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

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
            result = subprocess.run([p, "--help"], capture_output=True, timeout=5,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
            if result.returncode == 0:
                return p
        except Exception:
            continue
    return None

# ── 压缩包魔数检测 ───────────────────────────────────────────────

ARCHIVE_MAGICS = {
    "ZIP": (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"),
    "RAR": (b"Rar!\x1a\x07",),           # RAR 4.x & 5.x
    "7Z":  (b"7z\xbc\xaf\x27\x1c",),
}
MAX_MAGIC_LEN = 8

def is_archive(filepath: str | Path) -> bool:
    """读取文件头魔数判断是否为压缩包 (zip/rar/7z), 不依赖后缀名."""
    try:
        with open(filepath, "rb") as f:
            header = f.read(MAX_MAGIC_LEN)
        return any(
            header.startswith(m)
            for magics in ARCHIVE_MAGICS.values()
            for m in magics
        )
    except (OSError, PermissionError):
        return False


# ── 解压逻辑 ─────────────────────────────────────────────────────

import re

# 分卷压缩包特征: .001 .002 ... / .r00 .r01 ... / .part1.rar .part2.rar ...
_SPLIT_RE = re.compile(
    r'\.(?:0\d{2}|[1-9]\d{2,}|r\d{2}|part\d+\.rar|z\d{2})$',
    re.IGNORECASE
)

def _is_split_archive_parts(files: list) -> bool:
    if len(files) < 2:
        return False
    return any(_SPLIT_RE.search(str(f)) for f in files)

def _extract_with_7z(filepath: str, dest: str, password: str | None) -> bool:
    """用 7z 解压, 支持 AES-256 加密."""
    exe = _find_7z()
    if not exe:
        return False
    cmd = [exe, "x", filepath, f"-o{dest}", "-y"]
    if password:
        cmd.append(f"-p{password}")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True,
                                timeout=120,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode == 0:
            return True
        stderr = result.stderr.lower()
        # 密码错误 / 需要密码
        if "wrong password" in stderr or "cannot open encrypted archive" in stderr:
            return False
        # 返回码非0但不一定是密码问题（如文件损坏）
        # 如果是密码问题会返回 2 (fatal error) 或 7 (command line error)
        return False
    except Exception:
        return False


def _extract_with_zipfile(filepath: str, dest: str, password: str | None) -> bool:
    """用 Python zipfile 解压 (仅支持传统 ZipCrypto)."""
    try:
        pwd = password.encode("utf-8") if password else None
        with zipfile.ZipFile(filepath, "r") as zf:
            # 先检查是否加密
            encrypted = any(fi.flag_bits & 0x1 for fi in zf.infolist())
            if encrypted and not pwd:
                return False  # 需要密码但没给
            zf.extractall(dest, pwd=pwd)
        return True
    except RuntimeError as e:
        if "password" in str(e).lower():
            return False
        # AES 加密 zipfile 处理不了, 静默返回 False 让 7z 兜底
        if "encryption" in str(e).lower():
            return False
        return False
    except zipfile.BadZipFile:
        return False
    except Exception:
        return False


def extract_zip(filepath: str, dest: str, passwords: list[str]) -> str | None:
    """
    尝试用给定密码列表解压 zip 文件。
    优先 7z (支持 AES), 回退 zipfile。

    返回: 成功的密码 (None 表示无密码), 或 None 表示全部失败。
    """
    # 先尝试无密码
    if _extract_with_7z(filepath, dest, None):
        return None
    if _extract_with_zipfile(filepath, dest, None):
        return None

    # 尝试每个密码
    for pwd in passwords:
        if _extract_with_7z(filepath, dest, pwd):
            return pwd
        if _extract_with_zipfile(filepath, dest, pwd):
            return pwd

    return None  # 全部失败


def scan_for_archives(directory: str) -> list[str]:
    """扫描目录下所有压缩包文件 (按魔数, 不按后缀)."""
    results = []
    for root, dirs, files in os.walk(directory):
        for fname in files:
            fpath = os.path.join(root, fname)
            if is_archive(fpath):
                results.append(fpath)
    return results


# ── 递归解压主循环 ───────────────────────────────────────────────

def recursive_decompress(
    input_file: str,
    output_dir: str,
    passwords: list[str],
    verbose: bool = True,
) -> dict:
    """
    递归解压入口。

    返回:
      {
        "layers": 3,           # 解压层数
        "final_files": [...],  # 最终非 zip 文件列表
        "passwords_used": [],  # 每层用的密码 (None=无密码)
        "errors": [],          # 错误信息
      }
    """
    input_path = Path(input_file).resolve()
    output_path = Path(output_dir).resolve()
    output_path.mkdir(parents=True, exist_ok=True)

    if not is_archive(input_path):
        return {
            "layers": 0,
            "final_files": [str(input_path)],
            "passwords_used": [],
            "errors": [f"文件不是支持的压缩包: {input_path}"],
        }

    known_passwords = list(passwords)  # 可变副本, 成功密码会追加
    layers = 0
    passwords_used: list[str | None] = []
    errors: list[str] = []
    current_file = str(input_path)
    temp_dirs: list[str] = []
    final_output = output_path

    if verbose:
        print(f"\n{'='*60}")
        print(f"  递归解压器")
        print(f"  输入: {input_path}")
        print(f"  输出: {final_output}")
        print(f"{'='*60}\n")

    try:
        while True:
            layers += 1
            display_name = Path(current_file).name or "(无后缀)"

            # 创建当前层的临时解压目录
            tmpdir = tempfile.mkdtemp(prefix=f"unzip_L{layers}_")
            temp_dirs.append(tmpdir)

            if verbose:
                print(f"┌─ 第 {layers} 层 ─────────────────────────────")
                print(f"│ 文件: {display_name}")
                if known_passwords:
                    print(f"│ 尝试密码: [{', '.join(known_passwords[:4])}{'...' if len(known_passwords) > 4 else ''}]")

            # 尝试解压
            password = extract_zip(current_file, tmpdir, known_passwords)
            if password is not None:
                # 有密码: 成功了
                passwords_used.append(password)
                if password not in known_passwords:
                    known_passwords.insert(0, password)  # 放到最前面优先尝试
                if verbose:
                    print(f"│ 密码: {password} ✓")
            elif password is None and any(
                _extract_with_7z(current_file, tmpdir, p) for p in known_passwords
            ):
                # 这个分支不应到达 (extract_zip 已处理), 但保留
                pass
            else:
                # 密码列表全失败 — 手动询问
                if not sys.stdin.isatty():
                    errors.append(
                        f"第 {layers} 层需要密码但非交互模式无法输入: {display_name}\n"
                        f"  已尝试密码: {known_passwords}\n"
                        f"  提示: 用 -p 参数预置密码, 或在实际终端中运行"
                    )
                    if verbose:
                        print(f"│ ✗ 需要密码但非交互模式无法输入")
                        print(f"│   已尝试: {known_passwords}")
                        print(f"│   提示: 用 -p 参数预置密码")
                    layers -= 1
                    break

                if verbose:
                    print(f"│ 需要密码! (已尝试: {known_passwords})")
                try:
                    user_pwd = input(f"│ 请输入第 {layers} 层密码 (回车跳过): ").strip()
                except EOFError:
                    user_pwd = ""
                if user_pwd:
                    # 用新密码再试
                    extracted = False
                    if _extract_with_7z(current_file, tmpdir, user_pwd):
                        extracted = True
                    elif _extract_with_zipfile(current_file, tmpdir, user_pwd):
                        extracted = True

                    if extracted:
                        passwords_used.append(user_pwd)
                        known_passwords.insert(0, user_pwd)
                        if verbose:
                            print(f"│ 密码: {user_pwd} ✓")
                    else:
                        errors.append(f"第 {layers} 层密码错误: {display_name}")
                        if verbose:
                            print(f"│ ✗ 密码错误或解压失败")
                        layers -= 1
                        break
                else:
                    errors.append(f"第 {layers} 层无密码跳过: {display_name}")
                    if verbose:
                        print(f"│ ✗ 跳过 (无密码)")
                    layers -= 1
                    break

            # 扫描当前层的压缩包文件
            zip_files = scan_for_archives(tmpdir)
            if verbose:
                non_zips = sum(1 for _ in Path(tmpdir).rglob("*") if _.is_file())
                total_files = non_zips
                print(f"│ 解出: {total_files} 个文件, {len(zip_files)} 个是压缩包")
                print(f"└──────────────────────────────────────────")

            # 终止条件: 不是分卷 + (文件数≥2 或 无更多压缩包)
            all_files_list = list(Path(tmpdir).rglob("*"))
            all_files_only = [f for f in all_files_list if f.is_file()]
            is_split = _is_split_archive_parts(all_files_only)
            if is_split and verbose:
                print(f"│ 🔗 检测到分卷压缩包, 继续解压")

            if not is_split and (total_files >= 2 or not zip_files):
                if total_files >= 2:
                    if verbose:
                        print(f"│ ⏹ 文件数≥2, 停止递归 (保留内层压缩包不解压)")

                # 展平中间包裹层
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

            # 选下一个要解压的文件
            if zip_files:
                current_file = zip_files[0]
            elif is_split:
                first_part = sorted(
                    [f for f in all_files_only if _SPLIT_RE.search(f.name)],
                    key=lambda f: f.name
                )
                if first_part:
                    current_file = str(first_part[0])
                else:
                    errors.append(f"无法识别分卷入口文件")
                    break
            else:
                break

        # 收集最终文件列表
        final_files = []
        if final_output.exists():
            for f in sorted(final_output.rglob("*")):
                if f.is_file():
                    final_files.append(str(f))

        result = {
            "layers": layers,
            "final_files": final_files,
            "passwords_used": passwords_used,
            "errors": errors,
        }

        if verbose:
            print(f"\n{'='*60}")
            print(f"  完成! 共 {layers} 层, {len(final_files)} 个最终文件")
            if passwords_used:
                print(f"  使用密码: {passwords_used}")
            if errors:
                print(f"  错误: {errors}")
            print(f"  输出: {final_output}")
            for f in final_files:
                print(f"    → {Path(f).name}")
            print(f"{'='*60}\n")

        return result

    finally:
        # 清理临时目录
        for d in temp_dirs:
            try:
                shutil.rmtree(d, ignore_errors=True)
            except Exception:
                pass


# ── CLI 入口 ─────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="递归解压器 — 自动穿透多层嵌套/伪装 zip 压缩包",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  %(prog)s secret.jpg                          # 自动识别魔数, 交互输密码
  %(prog)s data.bin -o ./out                   # 指定输出目录
  %(prog)s archive.png -p pass123 pass456      # 预置密码列表
  %(prog)s nested.zip -p hunter2 -o ./result -q # 安静模式
        """,
    )
    parser.add_argument("input", help="输入文件 (任意后缀, 自动检测魔数)")
    parser.add_argument("-o", "--output", default="./decompressed",
                        help="输出目录 (默认: ./decompressed)")
    parser.add_argument("-p", "--passwords", nargs="*", default=[],
                        help="预置密码列表 (多个用空格分隔)")
    parser.add_argument("-q", "--quiet", action="store_true",
                        help="安静模式, 不显示详细信息")

    args = parser.parse_args()

    if not os.path.isfile(args.input):
        print(f"错误: 文件不存在 — {args.input}", file=sys.stderr)
        sys.exit(1)

    result = recursive_decompress(
        input_file=args.input,
        output_dir=args.output,
        passwords=args.passwords,
        verbose=not args.quiet,
    )

    if result["errors"]:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
