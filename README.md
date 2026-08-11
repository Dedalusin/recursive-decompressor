# 递归解压器 (Recursive Decompressor)

自动穿透多层嵌套/伪装压缩包的可视化解压工具。

## 功能

- **魔数识别**: 读取文件头判断真实格式，无视后缀名（`.jpg` `.png` `.bin` 无后缀均可）
- **多格式支持**: ZIP / RAR / 7Z
- **递归解压**: 自动穿透任意层嵌套，直到最终内容
- **密码缓存**: 成功过的密码自动重试后续层
- **分卷支持**: 自动识别 `.001` `.002` `.r00` 等分卷压缩
- **尾部 ZIP**: 支持 MP4/PNG 等文件末尾追加的 ZIP 数据
- **右键菜单**: 可注册到 Windows 右键菜单（见下文）

## 使用方法

### GUI（推荐）

```bash
# 安装依赖
pip install tkinterdnd2

# 启动
python 解压器GUI.pyw [文件路径]
```

- 拖拽文件到窗口，或点击选择
- 密码每行一个，顺序无所谓
- 点击「开始解压」
- 解压完成后可选择删除原文件

### 命令行

```bash
python recursive-decompressor.py <文件路径> [-o 输出目录] [-p 密码1 密码2 ...]
```

## 依赖

- Python 3.8+
- 7-Zip（https://7-zip.org/，用于 RAR/7Z 解密）
- tkinterdnd2（可选，用于拖拽支持）

## 安装右键菜单

1. 编辑 `安装右键菜单.reg`，将 `{PYTHON_PATH}` 和 `{GUI_PATH}` 替换为实际路径
2. 双击导入注册表
3. 右键任意文件 → 📦 递归解压

或使用 PowerShell:
```powershell
.\安装右键菜单.ps1 -PythonPath "C:\path\to\pythonw.exe" -GuiPath "C:\path\to\解压器GUI.pyw"
```

## 目录结构

```
recursive-decompressor/
├── 解压器GUI.pyw              # GUI 主程序
├── recursive-decompressor.py  # 命令行版本
├── 启动GUI.bat               # Windows 快捷启动
├── 安装右键菜单.reg           # 右键菜单注册表
├── 卸载右键菜单.reg           # 卸载右键菜单
└── README.md
```
