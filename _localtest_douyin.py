"""脱框架自测：抖音口令/链接提取与分享页数据解析（不依赖 astrbot / 真实网络）。

放插件根目录运行：python _localtest_douyin.py
文件名 _localtest 前缀已被 dev/build.py 排除，不会打包。
"""

import json
import sys
import types

if "aiohttp" not in sys.modules:
    _stub = types.ModuleType("aiohttp")
    _stub.ClientTimeout = lambda **kw: kw
    _stub.ClientSession = object
    sys.modules["aiohttp"] = _stub

from bilicard import douyin, render  # noqa: E402
from bilicard.douyin import DouyinClient  # noqa: E402

# 抖音分享到 QQ 的真实形态：前面一串识别码 + 中文话术 + 短链
KOULING = (
    "7.86 gJb:/ 复制打开抖音，看看【某某某的作品】这个高压锅真的会爆炸吗 "
    "https://v.douyin.com/iRNBho6G/ 复制此链接，打开Dou音搜索，直接观看视频！"
)


def test_find_link():
    f = douyin.find_link
    # 口令：要能从一堆噪声里把短链摘出来
    assert f(KOULING) == "https://v.douyin.com/iRNBho6G/"
    # 无协议头也要补全
    assert f("看看 v.douyin.com/abc123/ 这个") == "https://v.douyin.com/abc123/"
    # 网页地址 → 归一化成分享页
    assert f("https://www.douyin.com/video/7412345678901234567") == (
        "https://www.iesdouyin.com/share/video/7412345678901234567"
    )
    assert f("https://www.douyin.com/discover?modal_id=7412345678901234567") == (
        "https://www.iesdouyin.com/share/video/7412345678901234567"
    )
    # 不是抖音的不能误判
    assert f("https://www.bilibili.com/video/BV1xx411c7mD") is None
    assert f("普通聊天内容") is None
    assert f("") is None
    print("✓ find_link（口令/短链/网页/发现页/不误判）")


def test_strip_watermark():
    f = douyin.strip_watermark
    assert f("https://aweme.snssdk.com/aweme/v1/playwm/?video_id=v123") == (
        "https://aweme.snssdk.com/aweme/v1/play/?video_id=v123"
    )
    assert f("https://x/aweme/v1/play/?video_id=v1") == (
        "https://x/aweme/v1/play/?video_id=v1"
    )  # 已无水印，保持不变
    assert f("") == ""
    print("✓ strip_watermark")


def _page(payload: dict) -> str:
    return (
        "<html><script>window._ROUTER_DATA = "
        + json.dumps(payload, ensure_ascii=False)
        + ";</script></html>"
    )


def test_extract_router_data():
    ex = DouyinClient._extract_router_data
    assert ex(_page({"a": 1})) == {"a": 1}
    # 页面里没有该变量
    assert ex("<html>nothing</html>") is None
    # 后面还跟着别的语句：靠括号配平定位边界，不依赖 </script> 收尾
    html = (
        '<script>window._ROUTER_DATA = {"a": {"b": 2}};'
        'window.OTHER = {"c": 3};</script>'
    )
    assert ex(html) == {"a": {"b": 2}}
    # ⚠ 文案里带花括号：单纯数括号会提前收尾，必须跳过字符串内部
    tricky = {"loaderData": {"p": {"desc": "配方是 {糖:2} 和 {盐:1} }}} 哈哈"}}}
    assert ex(_page(tricky)) == tricky
    # 文案里带转义引号
    esc = {"a": {"desc": '他说\\"好的\\" }'}}
    assert ex(_page(esc)) == esc
    # 括号不配平（页面被截断）时不能瞎解析
    assert ex('<script>window._ROUTER_DATA = {"a": {"b": 2}') is None
    print("✓ _extract_router_data（配平/字符串内花括号/转义引号/截断）")


def test_find_item_and_normalize():
    item = {
        "aweme_id": "7412345678901234567",
        "desc": "  这个高压锅真的会爆炸吗  ",
        "duration": 65000,  # 毫秒
        "video": {
            "play_addr": {
                "url_list": ["https://aweme.snssdk.com/aweme/v1/playwm/?video_id=v1"]
            },
            "cover": {"url_list": ["https://p3.douyinpic.com/cover.jpeg"]},
        },
        "statistics": {"digg_count": 12345},
        "author": {
            "nickname": "某某某",
            "avatar_thumb": {"url_list": ["https://a.jpg"]},
        },
    }
    # 真实页面里作品埋在 loaderData 的动态 key 下
    data = {"loaderData": {"video_(id)/page": {"videoInfoRes": {"item_list": [item]}}}}
    found = DouyinClient._find_item(data)
    assert found is item, found

    info = DouyinClient._normalize(found, "fallback")
    assert info["aweme_id"] == "7412345678901234567"
    assert info["title"] == "这个高压锅真的会爆炸吗"  # 首尾空白已清
    assert info["duration"] == 65  # 毫秒 → 秒
    assert info["like"] == 12345
    assert info["cover"] == "https://p3.douyinpic.com/cover.jpeg"
    assert info["video_url"].endswith("/play/?video_id=v1")  # 已去水印
    assert info["share_url"] == ("https://www.douyin.com/video/7412345678901234567")
    print("✓ _find_item / _normalize（动态 key 定位 / 字段映射 / 毫秒转秒 / 去水印）")


def test_find_item_variants():
    f = DouyinClient._find_item
    item = {"aweme_id": "1", "desc": "x"}
    # key 名怎么变都能找到：按特征匹配而非固定路径
    assert f({"loaderData": {"p": {"aweme_detail": item}}}) is item
    assert f({"loaderData": {"p": [{"item_list": [item]}]}}) is item
    assert f({"随便什么键": {"更深": {"再深": [{"x": item}]}}}) is item
    # 缺 aweme_id 的不算作品，避免误认
    assert f({"a": {"desc": "x", "video": {}}}) is None
    assert f({"loaderData": {"p": {"foo": "bar"}}}) is None
    assert f({}) is None
    print("✓ _find_item（特征匹配 / 任意深度 / 不误认）")


def test_filter_reason():
    f = DouyinClient._find_filter_reason
    # 作品受限时 item_list 为空，真正原因在 filter_list 里
    data = {
        "loaderData": {
            "video_(id)/page": {
                "videoInfoRes": {
                    "item_list": [],
                    "filter_list": [{"aweme_id": "1", "detail_msg": "当前视频不见了"}],
                }
            }
        }
    }
    assert DouyinClient._find_item(data) is None  # 空 item_list 不能误当作品
    assert f(data) == "当前视频不见了"
    assert f({"a": {"filter_list": [{"filter_reason": "需要验证"}]}}) == "需要验证"
    assert f({"a": {"filter_list": []}}) == ""
    assert f({}) == ""
    print("✓ _find_filter_reason（受限原因/空列表/无该字段）")


def test_salvage():
    """JSON 结构大变时的兜底：直接从页面文本里捞地址。"""
    html = (
        "<html><head><title>世上道理千千万 - 抖音</title></head>"
        '<script>var x = {"play_addr":{"url_list":'
        '["https:\\u002F\\u002Fv3.douyinvod.com\\u002Fabc\\u002Fvideo.mp4"]},'
        '"cover":{"url_list":["https:\\/\\/p3.douyinpic.com\\/cover.jpeg"]}};'
        "</script></html>"
    )
    info = DouyinClient._salvage(html, "999")
    assert info is not None
    assert info["video_url"] == "https://v3.douyinvod.com/abc/video.mp4"
    assert info["cover"] == "https://p3.douyinpic.com/cover.jpeg"
    assert "世上道理千千万" in info["title"]
    assert info["aweme_id"] == "999"
    assert info["share_url"].endswith("/999")
    # 页面里没有任何视频地址时不能瞎编
    assert DouyinClient._salvage("<html>nothing</html>", "999") is None
    print("✓ _salvage（转义还原/播放地址/封面/标题/找不到则放弃）")


def test_images_and_missing_fields():
    # 图集作品没有 play_addr：video_url 为空，上层据此跳过下载
    item = {
        "aweme_id": "2",
        "desc": "图集",
        "images": [{"url_list": ["https://p/1.jpg"]}],
    }
    info = DouyinClient._normalize(item, "2")
    assert info["video_url"] == ""
    # 字段大面积缺失也不能抛
    bare = DouyinClient._normalize({}, "3")
    assert bare["aweme_id"] == "3" and bare["duration"] == 0 and bare["like"] == 0
    print("✓ _normalize（图集无直链 / 字段缺失不抛异常）")


def test_real_kouling_samples():
    """两条真实分享口令（含短链以连字符开头、URL 后紧跟其它口令片段）。"""
    a = (
        "分享口令:6.46 OXZ:/ 04/24 F@U.Yz :9pm 我在地球上最致命的五个地方死里逃生：中 "
        "# 野兽先生挑战  https://v.douyin.com/-cBGfFJ-h0k/ 复制此链接，打开Dou音搜索，直接观看视频！"
    )
    b = (
        "分享口令:1.25 复制打开抖音，看看【端木不疑的作品】世上道理千千万，唯有强者说了算"
        "# 天行九歌# 抖音... https://v.douyin.com/nZs-4CNYGFU/ p@Q.kc :3pm 07/11 qEU:/ "
    )
    # 短链 ID 以 - 开头也要完整取到，且不能把后面的口令片段吞进来
    assert douyin.find_link(a) == "https://v.douyin.com/-cBGfFJ-h0k/"
    assert douyin.find_link(b) == "https://v.douyin.com/nZs-4CNYGFU/"
    print("✓ find_link（两条真实口令：连字符开头 / URL 后接口令片段）")


MB = 1024 * 1024


def _item_with_bitrates():
    def addr(url, w, h, size):
        return {"url_list": [url], "width": w, "height": h, "data_size": size}

    return {
        "aweme_id": "1",
        "desc": "x",
        "video": {
            "play_addr": addr("https://x/playwm/default", 720, 1280, 0),
            "bit_rate": [
                {
                    "gear_name": "normal_720",
                    "bit_rate": 1_000_000,
                    "play_addr": addr("https://x/playwm/720", 720, 1280, 20 * MB),
                },
                {
                    "gear_name": "adapt_1080",
                    "bit_rate": 3_000_000,
                    "play_addr": addr("https://x/playwm/1080", 1080, 1920, 60 * MB),
                },
                {
                    "gear_name": "low_540",
                    "bit_rate": 500_000,
                    "play_addr": addr("https://x/playwm/540", 540, 960, 8 * MB),
                },
            ],
        },
    }


def test_collect_streams_and_pick():
    info = DouyinClient._normalize(_item_with_bitrates(), "1")
    streams = info["streams"]
    # 按短边分辨率降序：1080 → 720 → 540
    assert [min(s["width"], s["height"]) for s in streams] == [1080, 720, 540]
    assert all(s["url"].find("playwm") < 0 for s in streams)  # 都已去水印

    pick = douyin.pick_video_url
    # 空间充足 → 取不超过该档的最高画质
    url, desc = pick(info, "1080P", 100)
    assert url.endswith("/1080") and "1080P" in desc
    # 体积卡住 → 自动降到装得下的最高档
    assert pick(info, "1080P", 30)[0].endswith("/720")
    assert pick(info, "1080P", 10)[0].endswith("/540")
    # 指定画质上限，不取更高的
    assert pick(info, "720P", 100)[0].endswith("/720")
    assert pick(info, "540P", 100)[0].endswith("/540")
    # 最低档也超限 → 明确放弃，不做无谓下载
    assert pick(info, "1080P", 5) == ("", "")
    # 不限体积
    assert pick(info, "1080P", 0)[0].endswith("/1080")
    # 没有多档信息时退回默认地址
    bare = DouyinClient._normalize({"aweme_id": "2", "desc": "y"}, "2")
    assert bare["streams"] == []
    assert pick(bare, "720P", 100) == ("", "默认")

    # 无多档、靠改写 ratio 的分支：受支持的档位要如实报画质，不能都叫"默认"
    single = {
        "video_url": "https://x/aweme/v1/play/?ratio=720p&video_id=a",
        "streams": [],
    }
    assert pick(single, "720P", 100)[1] == "720P"  # 与原值相同也算生效
    assert pick(single, "540P", 100) == (
        "https://x/aweme/v1/play/?ratio=540p&video_id=a",
        "540P",
    )
    # 地址里没有 ratio 参数（CDN 直链）→ 如实报"默认"，不谎报画质
    cdn = {"video_url": "https://v3.douyinvod.com/a/b.mp4", "streams": []}
    assert pick(cdn, "540P", 100) == (cdn["video_url"], "默认")
    print("✓ _collect_streams / pick_video_url（排序/按体积降档/指定画质/全超限）")


def test_apply_ratio():
    """分享页只给一档时，靠改写地址里的 ratio 参数换清晰度。"""
    f = douyin.apply_ratio
    url = (
        "https://aweme.snssdk.com/aweme/v1/play/?line=0"
        "&logo_name=aweme_diversion_search&ratio=720p&video_id=v0d00fg10000abc"
    )
    # 只支持这三档（480p/360p 抖音不认且会返回更大的文件，已移除）
    for name, expect in (("1080P", "1080p"), ("720P", "720p"), ("540P", "540p")):
        assert f"ratio={expect}" in f(url, name), name
    assert f(url, "360P") == url  # 已移除的档位不改写，避免适得其反（92MB 那个坑）
    # 改写后其余参数不能被破坏，也不能残留第二个 ratio
    got = f(url, "540P")
    assert "video_id=v0d00fg10000abc" in got and "line=0" in got
    assert got.count("ratio=") == 1
    # 未知档位 / 地址里没有该参数 → 原样返回
    assert f(url, "auto") == url
    assert f("https://v3.douyinvod.com/a/b.mp4", "720P") == (
        "https://v3.douyinvod.com/a/b.mp4"
    )
    assert f("", "720P") == ""
    print("✓ apply_ratio（改写 ratio / 不破坏其它参数 / auto 与无参数时不动）")


def test_collect_streams_bad_shape():
    """分享页的 bit_rate 常是空值或非数组，不能让它把解析搞崩。"""
    c = DouyinClient._collect_streams
    assert c({"bit_rate": None}) == []
    assert c({"bit_rate": 12345}) == []  # 直接遍历会抛 TypeError
    assert c({"bit_rate": {"a": 1}}) == []
    assert c({"bit_rate": []}) == []
    assert c({}) == []
    print("✓ _collect_streams（bit_rate 为空/整数/字典时不崩）")


def test_render_data():
    d = render.build_douyin_data(
        {"cover": "data:image/jpeg;base64,xx", "duration": 65, "like": 12345}
    )
    assert d["duration"] == "01:05"
    assert d["like"] == "1.2万"
    assert d["cover"].startswith("data:")
    print("✓ render.build_douyin_data（时长/点赞格式化）")


if __name__ == "__main__":
    test_find_link()
    test_strip_watermark()
    test_extract_router_data()
    test_find_item_and_normalize()
    test_find_item_variants()
    test_filter_reason()
    test_salvage()
    test_images_and_missing_fields()
    test_real_kouling_samples()
    test_collect_streams_and_pick()
    test_apply_ratio()
    test_collect_streams_bad_shape()
    test_render_data()
    print("\n✅ 抖音解析纯逻辑自测全部通过")
