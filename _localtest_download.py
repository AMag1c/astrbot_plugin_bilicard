"""脱框架自测：downloader 选流策略与文件清理（不依赖 astrbot / 真实网络）。

放插件根目录运行：python _localtest_download.py
文件名 _localtest 前缀已被 dev/build.py 排除，不会打包。
"""

import asyncio
import os
import shutil
import sys
import tempfile
import time
import types

# downloader 在 import 时引入 aiohttp；本测不发真实请求，stub 掉即可
if "aiohttp" not in sys.modules:
    _stub = types.ModuleType("aiohttp")
    _stub.ClientTimeout = lambda **kw: kw
    _stub.ClientSession = object
    sys.modules["aiohttp"] = _stub

from bilicard import downloader  # noqa: E402
from bilicard.downloader import VideoDownloader, _StreamError  # noqa: E402

MB = 1024 * 1024

# 600 秒的视频，含 HEVC/AVC 多档位与两条音轨
DASH_DATA = {
    "timelength": 600_000,
    "dash": {
        "video": [
            {"id": 80, "codecid": 12, "bandwidth": 3_000_000, "baseUrl": "hevc1080"},
            {"id": 80, "codecid": 7, "bandwidth": 5_000_000, "baseUrl": "avc1080"},
            {"id": 64, "codecid": 7, "bandwidth": 1_000_000, "baseUrl": "avc720"},
            {"id": 120, "codecid": 7, "bandwidth": 9_000_000, "base_url": "avc4k"},
        ],
        "audio": [
            {"id": 30280, "bandwidth": 190_000, "baseUrl": "audio-192k"},
            {"id": 30232, "bandwidth": 130_000, "baseUrl": "audio-132k"},
            {"id": 30216, "bandwidth": 64_000, "baseUrl": "audio-64k"},
        ],
    },
}
# 预估体积（MB，含 132K 音轨）：4K≈684.8  1080P(avc)≈384.8  720P≈84.8


def _mk(tmp: str) -> VideoDownloader:
    dl = VideoDownloader(tmp)
    dl._playurl = lambda params: _async(DASH_DATA)  # type: ignore[assignment]
    return dl


def _async(value):
    async def _coro():
        return value

    return _coro()


def test_safe_filename():
    f = downloader.safe_filename
    assert f("正常标题", "BV1") == "正常标题_BV1.mp4"
    # 路径分隔符与保留字符必须清掉，否则会写到别处或被协议端拒收
    assert f('a/b\\c:d*e?f"g<h>i|j', "BV1") == "a_b_c_d_e_f_g_h_i_j_BV1.mp4"
    assert f("含\n换行\t制表", "BV1") == "含_换行_制表_BV1.mp4"
    assert f("", "BV1") == "BV1.mp4"
    assert f("   ", "BV1") == "BV1.mp4"
    assert f("结尾点...", "BV1") == "结尾点_BV1.mp4"  # Windows 不允许结尾为点
    assert len(f("长" * 200, "BV1")) <= 60 + len("_BV1.mp4")
    print("✓ safe_filename（非法字符/空标题/结尾点/截断）")


def test_estimate_and_url():
    est = downloader._estimate_bytes
    assert est(8_000_000, 10) == 10_000_000  # 8Mbps × 10s / 8 = 10MB
    assert est(0, 10) == 0.0
    assert est(1000, 0) == 0.0
    u = downloader._stream_url
    assert u({"baseUrl": "a"}) == "a"
    assert u({"base_url": "b"}) == "b"  # 接口两种拼写都要认
    assert u({}) == ""
    print("✓ _estimate_bytes / _stream_url")


def test_dash_pick():
    tmp = tempfile.mkdtemp()
    try:
        dl = _mk(tmp)
        run = asyncio.run

        # 1080P 上限、400MB 空间：选 AVC 而非体积更小的 HEVC（QQ 解不了 HEVC）
        v, a, q = run(dl._dash_streams("BV1", 1, 80, 400 * MB))
        assert (v, q) == ("avc1080", "1080P"), (v, q)
        assert a == "audio-132k"  # 音轨优先 132K，把体积预算留给画质

        # 放开到 4K，但 4K 约 682MB 超限 → 自动降到 1080P
        v, _, q = run(dl._dash_streams("BV1", 1, 120, 400 * MB))
        assert (v, q) == ("avc1080", "1080P"), (v, q)

        # 空间收紧到 100MB → 继续降到 720P
        v, _, q = run(dl._dash_streams("BV1", 1, 80, 100 * MB))
        assert (v, q) == ("avc720", "720P"), (v, q)

        # 画质上限 720P 时不得挑更高档
        v, _, q = run(dl._dash_streams("BV1", 1, 64, 400 * MB))
        assert (v, q) == ("avc720", "720P"), (v, q)

        # 最低档仍超限 → 不可回退的失败（换路线画质更低但时长不变，照样超）
        try:
            run(dl._dash_streams("BV1", 1, 80, 10 * MB))
            raise AssertionError("应抛 _StreamError")
        except _StreamError as e:
            assert e.retryable is False
            assert "超出上限" in e.msg

        # 无 DASH 流 → 可回退到 durl 路线
        dl._playurl = lambda params: _async({"dash": {}})  # type: ignore[assignment]
        try:
            run(dl._dash_streams("BV1", 1, 80, 400 * MB))
            raise AssertionError("应抛 _StreamError")
        except _StreamError as e:
            assert e.retryable is True
        print("✓ _dash_streams（AVC 优先 / 档位上限 / 超限降档 / 失败可回退性）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_durl_ladder():
    tmp = tempfile.mkdtemp()
    try:
        dl = VideoDownloader(tmp)
        calls = []
        sizes = {80: 300 * MB, 64: 90 * MB, 32: 40 * MB, 16: 20 * MB}

        def fake(params):
            calls.append(params["qn"])
            # durl 路线的关键参数：html5 平台才会返回已封装音轨的单文件 MP4
            assert params["platform"] == "html5"
            qn = params["qn"]
            return _async(
                {"quality": qn, "durl": [{"url": f"u{qn}", "size": sizes[qn]}]}
            )

        dl._playurl = fake  # type: ignore[assignment]

        # 1080P 约 300MB 超限 → 逐档降到 720P
        url, q = asyncio.run(dl._durl_stream("BV1", 1, 80, 100 * MB))
        assert (url, q) == ("u64", "720P"), (url, q)
        assert calls == [80, 64], calls

        # 上限本就是 480P：不该去试更高档位
        calls.clear()
        url, q = asyncio.run(dl._durl_stream("BV1", 1, 32, 100 * MB))
        assert (url, q) == ("u32", "480P")
        assert calls == [32, 16] or calls == [32], calls

        # 全部超限
        calls.clear()
        try:
            asyncio.run(dl._durl_stream("BV1", 1, 80, 1 * MB))
            raise AssertionError("应抛 _StreamError")
        except _StreamError as e:
            assert e.retryable is False and "超出上限" in e.msg
        print("✓ _durl_stream（html5 参数 / 超限逐档降级 / 全超限失败）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_playurl_error_semantics():
    tmp = tempfile.mkdtemp()
    try:
        dl = VideoDownloader(tmp)
        dl._playurl = VideoDownloader._playurl.__get__(dl)  # 还原真实实现

        # 直接测 code!=0 的分支：大会员/地区限制换路线也没用，不可回退
        async def check():
            class _Resp:
                status = 200

                async def json(self):
                    return {"code": -404, "message": "啥都木有"}

                async def __aenter__(self):
                    return self

                async def __aexit__(self, *a):
                    return False

            class _Session:
                def get(self, *a, **kw):
                    return _Resp()

                async def __aenter__(self):
                    return self

                async def __aexit__(self, *a):
                    return False

            downloader.aiohttp.ClientSession = lambda **kw: _Session()
            return await dl._playurl({"bvid": "BV1"})

        try:
            asyncio.run(check())
            raise AssertionError("应抛 _StreamError")
        except _StreamError as e:
            assert e.retryable is False and "啥都木有" in e.msg
        print("✓ _playurl（B站拒绝提供地址 → 不可回退）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_cleanup_and_sweep():
    tmp = tempfile.mkdtemp()
    try:
        dl = VideoDownloader(tmp)
        os.makedirs(dl.dir, exist_ok=True)

        # 一次失败下载可能留下的全部中间产物都要清掉
        stem = os.path.join(dl.dir, "BV1_1")
        out = stem + ".mp4"
        leftovers = [
            out,
            out + ".part",
            stem + ".video.m4s",
            stem + ".video.m4s.part",
            stem + ".audio.m4s",
            stem + ".audio.m4s.part",
        ]
        for p in leftovers:
            with open(p, "wb") as f:
                f.write(b"x")
        dl._cleanup_stem(stem, out)
        assert not any(os.path.exists(p) for p in leftovers)

        # sweep：只清超龄的，在途文件不能误删
        old = os.path.join(dl.dir, "old.mp4")
        new = os.path.join(dl.dir, "new.mp4")
        for p in (old, new):
            with open(p, "wb") as f:
                f.write(b"x")
        os.utime(old, (time.time() - 7200, time.time() - 7200))
        assert dl.sweep(60) == 1
        assert not os.path.exists(old) and os.path.exists(new)

        # max_age_minutes=0 → 清空（插件启动/卸载时用）
        assert dl.sweep(0) == 1
        assert os.listdir(dl.dir) == []

        # 目录不存在也不能炸
        shutil.rmtree(dl.dir)
        assert dl.sweep(0) == 0
        print("✓ _cleanup_stem / sweep（中间产物清理 / 超龄清理 / 不误删在途）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_fetch_guard():
    tmp = tempfile.mkdtemp()
    try:
        dl = VideoDownloader(tmp)
        # 缺少标识时应返回带 error 的结果，而不是抛异常
        r = asyncio.run(dl.fetch("", 0))
        assert not r.ok and r.error
        print("✓ fetch（缺参数返回错误结果而非抛异常）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    test_safe_filename()
    test_estimate_and_url()
    test_dash_pick()
    test_durl_ladder()
    test_playurl_error_semantics()
    test_cleanup_and_sweep()
    test_fetch_guard()
    print("\n✅ downloader 纯逻辑自测全部通过")
