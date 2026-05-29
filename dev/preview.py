"""本地预览脚本（开发用，不属于插件运行时）。

拉取真实 B站数据填充模板，生成 preview.html，供你在浏览器中打开检查样式。
不会启动任何浏览器，只生成 HTML 文件。

用法：
    python dev/preview.py            # 使用默认示例视频
    python dev/preview.py BV1xxxxxx  # 指定 BV 号

可选：把下方 COOKIES 填上 SESSDATA 可顺带测试真实字幕（本脚本不调用 LLM，
AI 总结区块用占位文本展示样式）。
"""

import asyncio
import os
import sys

# 让脚本能 import 到 bilicard 包
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from bilicard import render          # noqa: E402
from bilicard.client import BiliClient  # noqa: E402

DEFAULT_BVID = "BV1GJ411x7h7"

# 如需测试字幕，可填 {"SESSDATA": "xxx", "bili_jct": "xxx"}
COOKIES = {}

# AI 总结预览占位文本（实际运行时由 LLM 生成）
SUMMARY_PLACEHOLDER = (
    "本视频围绕核心主题展开，先抛出背景与问题，再逐步给出关键论据与示例，"
    "最后总结观点。整体节奏紧凑、信息密度较高，适合对该话题感兴趣的观众观看。"
    "（这是预览占位文本，实际运行时此处为 AI 根据字幕生成的真实总结。）"
)


async def main():
    bvid = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_BVID
    client = BiliClient(COOKIES)

    print(f"[1/4] 拉取视频信息: {bvid}")
    info = await client.get_video_info(bvid=bvid)
    if not info:
        print("❌ 视频信息获取失败，请检查 BV 号或网络")
        return
    print(f"      标题: {info['title']}")
    print(f"      UP主: {info['owner']['name']}  时长: {info['duration']}s")
    print(f"      统计: {info['stat']}")

    print("[2/4] 拉取实时在线人数 + 热门评论")
    online = await client.get_online(info["bvid"], info["cid"])
    comments = await client.get_hot_comments(info["aid"], 3)
    print(f"      在线: {online} 人   热评: {len(comments)} 条")
    for c in comments:
        print(f"        - {c['name']}（{c['like']}赞）: {c['message'][:30]}")

    print("[3/4] 下载封面/头像并转 base64")
    info["cover"] = await client.fetch_image_data_uri(info["cover"])
    info["owner"]["face"] = await client.fetch_image_data_uri(info["owner"]["face"])

    print("[4/4] 渲染 HTML")
    online_text = f"{online} 人在线" if online is not None else None

    try:
        from jinja2 import Template
    except ImportError:
        print("❌ 需要 jinja2 才能本地预览，请先执行: pip install jinja2")
        return
    tmpl = Template(render.load_template())

    # 1) 链接总结版：无顶部气泡 + 有 AI 总结
    data_link = render.build_template_data(
        info, online_text=online_text, comments=comments,
        summary=SUMMARY_PLACEHOLDER, show_post_bar=False,
    )
    # 2) 订阅推送版：有顶部气泡 + 无 AI 总结
    data_sub = render.build_template_data(
        info, online_text=online_text, comments=comments,
        summary=None, show_post_bar=True,
    )

    out_link = os.path.join(ROOT, "preview_链接总结.html")
    out_sub = os.path.join(ROOT, "preview_订阅推送.html")
    with open(out_link, "w", encoding="utf-8") as f:
        f.write(tmpl.render(**data_link))
    with open(out_sub, "w", encoding="utf-8") as f:
        f.write(tmpl.render(**data_sub))

    print("\n✅ 已生成两个预览文件：")
    print(f"   链接总结版（无气泡+AI总结）：{out_link}")
    print(f"   订阅推送版（有气泡+无总结）：{out_sub}")
    print("   请在浏览器中分别打开查看。")


if __name__ == "__main__":
    asyncio.run(main())
