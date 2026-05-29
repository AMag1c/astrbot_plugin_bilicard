"""打包插件为可上传 AstrBot 的 zip。

生成的 zip 顶层是 astrbot_plugin_bilicard/ 目录，排除开发脚本、预览文件、
缓存、字体 ttf（运行时只用 woff）等无关内容。
"""

import os
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # 插件目录
PARENT = os.path.dirname(ROOT)
PLUGIN = os.path.basename(ROOT)
OUT = os.path.join(PARENT, PLUGIN + ".zip")

EXCLUDE_DIRS = {"dev", "docs", "__pycache__", ".git", ".github", ".idea", ".vscode"}
EXCLUDE_FILES = {"vanfont.ttf", ".gitignore"}
EXCLUDE_EXT = {".pyc", ".zip"}
EXCLUDE_PREFIX = ("preview", "icons_preview")

if os.path.exists(OUT):
    os.remove(OUT)

included = []
with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as z:
    for dp, dns, fns in os.walk(ROOT):
        dns[:] = [d for d in dns if d not in EXCLUDE_DIRS]
        for fn in fns:
            if fn in EXCLUDE_FILES:
                continue
            if os.path.splitext(fn)[1] in EXCLUDE_EXT:
                continue
            if fn.startswith(EXCLUDE_PREFIX):
                continue
            full = os.path.join(dp, fn)
            arc = os.path.relpath(full, PARENT)  # 顶层含插件目录名
            z.write(full, arc)
            included.append(arc.replace("\\", "/"))

print(f"已生成: {OUT}")
print(f"大小: {os.path.getsize(OUT)} 字节")
print("包含文件:")
for f in sorted(included):
    print("  ", f)
