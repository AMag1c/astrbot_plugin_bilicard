"""脱框架自测：parser / render / summarizer / wbi 纯逻辑（不依赖 astrbot / 真实网络）。

放插件根目录运行：python _localtest.py
文件名 _localtest 前缀已被 dev/build.py 排除，不会打包。
"""

import asyncio
import sys
import time
import types

# wbi 在 import 时引入 aiohttp；本测不发真实请求，stub 掉即可
sys.modules.setdefault("aiohttp", types.ModuleType("aiohttp"))

from bilicard import parser, render, summarizer, wbi  # noqa: E402


def test_parser():
    f = parser.find_video_token
    assert f("看 https://b23.tv/abc 这个") == ("b23", "https://b23.tv/abc")
    assert f("b23.tv/xyz") == ("b23", "https://b23.tv/xyz")
    assert f("https://www.bilibili.com/video/BV1xx411c7mD") == ("bv", "BV1xx411c7mD")
    assert f("https://www.bilibili.com/video/av12345") == ("av", "12345")
    assert f("裸 BV1xx411c7mD 号") == ("bv", "BV1xx411c7mD")
    assert f("av999") == ("av", "999")
    assert f("无视频") is None
    assert f("") is None
    # 优先级：短链 > 标准链接 > 裸 BV
    assert f("BV1xx411c7mD https://b23.tv/p")[0] == "b23"
    print("✓ parser.find_video_token（短链/链接/裸BV/裸av/优先级）")


def test_render_format():
    assert render.format_count(999) == "999"
    assert render.format_count(10000) == "1万"
    assert render.format_count(12345) == "1.2万"
    assert render.format_count(123456789) == "1.2亿"
    assert render.format_count(None) == "0"
    assert render.format_count("abc") == "0"
    assert render.format_duration(65) == "01:05"
    assert render.format_duration(3725) == "01:02:05"
    assert render.format_duration(0) == "00:00"
    assert render.format_date(0) == ""
    assert len(render.format_date(1700000000)) == 10
    print("✓ render.format_count / duration / date")


def test_render_build():
    info = {
        "bvid": "BV1",
        "title": "标题",
        "desc": "简介",
        "duration": 90,
        "pubdate": 1700000000,
        "cover": "data:x",
        "owner": {"name": "UP", "face": "data:y"},
        "stat": {
            "view": 12345,
            "danmaku": 1,
            "like": 2,
            "coin": 3,
            "favorite": 4,
            "share": 5,
            "reply": 6,
        },
    }
    data = render.build_template_data(
        info,
        online_text="9 人在线",
        comments=[
            {"name": "甲", "message": "评" * 50, "like": 88},
            {"name": "乙", "message": "  ", "like": 0},  # 空白应被过滤
        ],
        summary="一段总结",
        show_post_bar=False,
    )
    assert data["bvid"] == "BV1"
    assert [s["key"] for s in data["stats"]] == [
        "view",
        "danmaku",
        "like",
        "coin",
        "favorite",
        "share",
        "reply",
    ]
    assert data["stats"][0]["value"] == "1.2万"
    assert data["show_post_bar"] is False
    assert data["summary"] == "一段总结"
    assert len(data["comments"]) == 1  # 空白评论被过滤
    assert data["comments"][0]["message"].endswith("…")  # 长评论被截断
    assert len(data["comments"][0]["message"]) <= render._COMMENT_MAX_LEN + 1
    print("✓ render.build_template_data（统计顺序/评论截断与过滤/字段）")


def test_summarizer():
    async def fake_llm(prompt):
        assert "视频标题" in prompt and "视频字幕" in prompt
        return "  “这是总结”  "

    out = asyncio.run(summarizer.summarize("标题", "字幕内容", fake_llm, max_chars=100))
    assert out == "这是总结"  # 去首尾空白与引号
    assert asyncio.run(summarizer.summarize("标题", "", fake_llm)) is None  # 空字幕

    async def boom(prompt):
        raise RuntimeError("x")

    assert asyncio.run(summarizer.summarize("标题", "字幕", boom)) is None  # 异常降级
    print("✓ summarizer.summarize（清洗/空字幕/异常降级）")


def test_wbi():
    img = "7cd084941338484aae1ad9425b84077c"
    sub = "4932caff0ff746eab6f01bf08b70ac45"
    key = wbi._get_mixin_key(img, sub)
    assert len(key) == 32
    assert wbi._get_mixin_key(img, sub) == key  # 确定性
    # 注入缓存避免触网，sign_params 应追加 wts 与 w_rid(md5)
    wbi._wbi_cache = (key, time.time())
    signed = asyncio.run(wbi.sign_params({"bvid": "BV1", "cid": 2}))
    assert "wts" in signed and len(signed["w_rid"]) == 32
    assert signed["bvid"] == "BV1"
    print("✓ wbi._get_mixin_key / sign_params（确定性 + 签名字段）")


if __name__ == "__main__":
    test_parser()
    test_render_format()
    test_render_build()
    test_summarizer()
    test_wbi()
    print("\n✅ bilicard 纯逻辑自测全部通过")
