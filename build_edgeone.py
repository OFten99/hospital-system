"""
医院门诊管理系统 —— EdgeOne Pages 部署包生成脚本

背景
----
腾讯云 EdgeOne Pages（EdgeOne Makers）的云函数只会打包仓库里的 cloud-functions/
目录，而且要求入口文件自身包含 `app = Flask(...)` 这一「入口标识」，
平台靠它识别出这是 WSGI 框架模式的函数。

为了让仓库里只维护一份源码（web_app/），本脚本负责把应用同步过去：

    web_app/app.py            ->  cloud-functions/[[default]].py   (站点根路径全匹配)
    web_app/db.py             ->  cloud-functions/db.py
    web_app/templates/*.html  ->  cloud-functions/templates/
    web_app/static/*          ->  cloud-functions/static/
    (内置)                     ->  cloud-functions/requirements.txt
    (内置)                     ->  cloud-functions/README.md

用法
----
    python build_edgeone.py

注意
----
cloud-functions/ 整个目录都是自动生成的，不要手动修改里面的文件。
要改功能请改 web_app/ 下的源码，然后重新运行本脚本。
"""

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "web_app"
DST = ROOT / "cloud-functions"

ENTRY_NAME = "[[default]].py"

# 云端入口文件开头插入的引导代码：确保同目录下的 db.py 能被 import。
# EdgeOne 加载函数时不保证把函数目录加进 sys.path。
ENTRY_PROLOGUE = '''# -*- coding: utf-8 -*-
# ============================================================================
# 本文件由 build_edgeone.py 从 web_app/app.py 自动生成，请勿直接修改。
# 需要改动请编辑 web_app/app.py，然后运行： python build_edgeone.py
# ============================================================================
import os as _os
import sys as _sys

_HERE = _os.path.dirname(_os.path.abspath(__file__))
if _HERE not in _sys.path:
    _sys.path.insert(0, _HERE)


'''

REQUIREMENTS = """# EdgeOne Pages 云函数依赖（本文件由 build_edgeone.py 生成）
# 与 web_app/requirements.txt 保持一致
Flask>=3.0.0
mysql-connector-python>=8.3.0
"""

README = """# cloud-functions —— EdgeOne Pages 云函数目录（自动生成）

**目录内所有文件均由仓库根目录的 `build_edgeone.py` 生成，请勿手动修改。**

- `[[default]].py` —— 站点根路径全匹配的 Flask 入口（源码来自 `web_app/app.py`）
- `db.py` —— 数据库连接工具（源码来自 `web_app/db.py`）
- `templates/`、`static/` —— 页面模板与样式（源码来自 `web_app/`）
- `requirements.txt` —— 云函数运行时依赖

要改功能，请编辑 `web_app/` 下的源码，然后重新运行：

```bash
python build_edgeone.py
```

部署方式见 `docs/部署到公网_EdgeOne.md`。
"""


def dump(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    print(f"  写入 {path.relative_to(ROOT)}  ({len(text)} 字节)")


def copy_tree(src: Path, dst: Path, pattern: str = "*"):
    if not src.exists():
        print(f"  [警告] 源目录不存在，跳过：{src}")
        return 0
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True, exist_ok=True)
    count = 0
    for item in sorted(src.glob(pattern)):
        if item.is_file():
            shutil.copy2(item, dst / item.name)
            count += 1
    print(f"  复制 {src.relative_to(ROOT)} -> {dst.relative_to(ROOT)}（{count} 个文件）")
    return count



def dump_templates_data():
    """把 templates/ 与 static/ 内容打包为 templates_data.py（内存字典）。

    EdgeOne Makers 的 PythonFunctionBuilder 只把 .py 文件与依赖装进函数运行时，
    子目录 templates/、static/ 不会随函数代码上传；这里把它们内嵌为
    cloud-functions/templates_data.py，入口在云端找不到磁盘模板时解包使用。
    """
    items = []
    tpl = SRC / "templates"
    if tpl.exists():
        for html in sorted(tpl.glob("*.html")):
            items.append((f"templates/{html.name}", html.read_text(encoding="utf-8")))
    st = SRC / "static"
    if st.exists():
        for f in sorted(st.rglob("*")):
            if f.is_file():
                rel = f.relative_to(st).as_posix()
                items.append((f"static/{rel}", f.read_text(encoding="utf-8")))
    lines = [
        "# -*- coding: utf-8 -*-",
        "# 本文件由 build_edgeone.py 自动生成：模板与静态资源内存包。",
        "# 用途：EdgeOne 云函数打包只含 .py 文件，云端磁盘无 templates/ 时由",
        "#       入口文件解包本字典到临时目录供 Flask 渲染。",
        "# 键：templates/xxx.html 或 static/xxx.css",
        "TEMPLATES = {",
    ]
    for name, content in items:
        esc = (content.replace("\\", "\\\\").replace('"', '\\"')
                    .replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t"))
        lines.append(f'    "{name}": "{esc}",')
    lines.append("}")
    dump(DST / "templates_data.py", "\n".join(lines))
    print(f"  模板/静态资源已内嵌 templates_data.py（{len(items)} 个文件）")


def main():
    app_py = SRC / "app.py"
    db_py = SRC / "db.py"
    if not app_py.exists() or not db_py.exists():
        print(f"[错误] 找不到源码：{app_py} / {db_py}")
        sys.exit(1)

    print("=" * 64)
    print("生成 EdgeOne Pages 云函数目录 cloud-functions/")
    print("=" * 64)

    DST.mkdir(parents=True, exist_ok=True)

    # 1. Flask 入口（加引导代码，保证能 import 同目录的 db）
    source = app_py.read_text(encoding="utf-8")
    if "app = Flask(" not in source:
        print("[错误] web_app/app.py 中没有找到 `app = Flask(` 入口标识，"
              "EdgeOne 无法识别为框架模式函数。")
        sys.exit(1)
    dump(DST / ENTRY_NAME, ENTRY_PROLOGUE + source)

    # 2. 数据库工具（加一行来源说明即可，本身不依赖同级模块）
    dump(DST / "db.py",
         "# 本文件由 build_edgeone.py 从 web_app/db.py 自动生成，请勿直接修改。\n"
         + db_py.read_text(encoding="utf-8"))

    # 3. 模板与静态资源
    copy_tree(SRC / "templates", DST / "templates", "*.html")
    copy_tree(SRC / "static", DST / "static")

    # 3.5 模板与静态资源的内存兜底包（.py 文件才会被 EdgeOne 打包进函数运行时）
    dump_templates_data()

    # 4. 依赖与说明
    dump(DST / "requirements.txt", REQUIREMENTS)
    dump(DST / "README.md", README)

    # 5. 清理 Python 缓存，避免被打进部署包
    cache = DST / "__pycache__"
    if cache.exists():
        shutil.rmtree(cache)

    print("-" * 64)
    files = [p for p in DST.rglob("*") if p.is_file()]
    total = sum(p.stat().st_size for p in files)
    print(f"完成：共 {len(files)} 个文件，合计 {total / 1024:.1f} KB")
    print("接下来：提交并推送到 GitHub，然后在 EdgeOne Pages 控制台导入本仓库即可。")
    print("详细步骤见 docs/部署到公网_EdgeOne.md")


if __name__ == "__main__":
    main()
