"""视频下载：取流 → 下载 →（必要时）ffmpeg 合并 → 交付本地文件路径。

两个入口：

- :meth:`VideoDownloader.fetch` —— B站，需要选流。两条路线按环境自动选择：
  **DASH + ffmpeg 合并**（画质可到 1080P/4K；B站把音轨与视轨拆成两个 m4s，
  不合并就是无声视频）；没有 ffmpeg 时退回 **durl 单文件**（``platform=html5``
  让 B站直接返回封装好音轨的 MP4，代价是画质通常止步 720P）。
- :meth:`VideoDownloader.fetch_direct` —— 抖音等已知直链的平台，省掉选流。

失败语义：所有对外方法失败时返回带 ``error`` 的 :class:`DownloadResult`，不向上抛
异常（与 client.py 一致）；中途失败或被取消都会清掉已落盘的中间产物，不留垃圾。

不依赖 AstrBot，可脱框架单测。
"""

import asyncio
import os
import re
import shutil
import time
from dataclasses import dataclass
from typing import Optional, Tuple

import aiohttp

from .log import logger

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

API_PLAYURL = "https://api.bilibili.com/x/player/playurl"

# 配置里的画质名 → B站 qn 档位值
QUALITY_QN = {"360P": 16, "480P": 32, "720P": 64, "1080P": 80, "4K": 120}
# qn → 展示名（B站档位比配置项细，如 116=1080P60）
QN_NAME = {
    6: "240P",
    16: "360P",
    32: "480P",
    64: "720P",
    74: "720P60",
    80: "1080P",
    112: "1080P+",
    116: "1080P60",
    120: "4K",
    125: "HDR",
    126: "杜比视界",
    127: "8K",
}
# durl 路线的降级阶梯（从高到低逐档重试，直到体积达标）
_DURL_LADDER = [80, 64, 32, 16]

# H.264/AVC 的 codecid。B站现在大量视频默认给 HEVC(12)/AV1(13)，
# 但 QQ 客户端解不了这两种，播放会黑屏/失败，故选流时优先 AVC。
_CODEC_AVC = 7

_UNSAFE_NAME_RE = re.compile(r'[\\/:*?"<>|\r\n\t]+')

# 下载进度日志间隔（秒）。大视频要下几分钟，没有心跳日志会像卡死
_PROGRESS_LOG_SECONDS = 15


@dataclass
class DownloadResult:
    """下载结果。``ok`` 为 False 时 ``error`` 必有一句可直接展示给用户的原因。"""

    path: str = ""  # 落盘的绝对路径（成功时）
    filename: str = ""  # 建议的展示文件名（含扩展名，可含中文标题）
    size_mb: float = 0.0
    quality: str = ""  # 实际画质名，如 "1080P"
    error: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.path)


class _StreamError(Exception):
    """取流阶段失败。``retryable`` 表示能否回退到另一条取流路线。

    体积超限属于不可回退（换路线画质只会更低但时长不变，仍然超）。
    """

    def __init__(self, msg: str, retryable: bool = True):
        super().__init__(msg)
        self.msg = msg
        self.retryable = retryable


def _unlink(path: str) -> None:
    """尽力删除单个文件，永不抛异常。"""
    if not path:
        return
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except Exception as e:  # noqa: BLE001
        logger.warning("[BiliCard] 删除临时文件失败 %s: %s", path, e)


def safe_filename(title: str, bvid: str, ext: str = ".mp4") -> str:
    """把视频标题转成各平台都能接受的文件名（用于上传时的展示名）。"""
    name = _UNSAFE_NAME_RE.sub("_", (title or "").strip())
    name = name[:60].strip(" .")  # 截断 + 去掉首尾空格/点（Windows 不允许结尾点）
    return f"{name}_{bvid}{ext}" if name else f"{bvid}{ext}"


class VideoDownloader:
    """B站视频下载器。

    Args:
        data_dir: 插件数据目录；视频落在其下的 ``downloads/`` 子目录。
        cookies: 登录 Cookie 字典。传入 main 持有的同一个 dict 对象即可，
            登录/续期后原地更新的新 Cookie 会自动生效，无需重建下载器。
        api_timeout: playurl 等 API 请求的超时（秒）。
    """

    def __init__(
        self,
        data_dir: str,
        cookies: Optional[dict] = None,
        api_timeout: int = 20,
    ):
        self.dir = os.path.join(data_dir, "downloads")
        self.cookies = cookies if cookies is not None else {}
        self.api_timeout = aiohttp.ClientTimeout(total=api_timeout)
        self._ffmpeg: Optional[str] = None
        self._ffmpeg_probed = False

    # ------------------------------------------------------------------ #
    # 环境探测
    # ------------------------------------------------------------------ #
    def ffmpeg_path(self) -> str:
        """探测 ffmpeg 可执行文件（结果缓存）。没有则返回空串。"""
        if not self._ffmpeg_probed:
            self._ffmpeg_probed = True
            self._ffmpeg = shutil.which("ffmpeg") or ""
            if self._ffmpeg:
                logger.debug("[BiliCard] 检测到 ffmpeg：%s", self._ffmpeg)
            else:
                logger.warning(
                    "[BiliCard] 未检测到 ffmpeg，B站视频只能走单文件流（最高约 720P）"
                )
        return self._ffmpeg or ""

    def _headers(self) -> dict:
        """B站 CDN 有防盗链，下载视频流必须带 Referer，否则 403。"""
        h = {"User-Agent": _UA, "Referer": "https://www.bilibili.com/"}
        if self.cookies:
            h["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items() if v)
        return h

    @staticmethod
    def _plain_headers(referer: str = "") -> dict:
        """第三方站点用的请求头。

        **绝不带 Cookie**——那是 B站的登录凭证，发给别家 CDN 等于泄露。
        """
        h = {"User-Agent": _UA}
        if referer:
            h["Referer"] = referer
        return h

    # ------------------------------------------------------------------ #
    # 对外主流程
    # ------------------------------------------------------------------ #
    async def fetch(
        self,
        bvid: str,
        cid: int,
        title: str = "",
        *,
        max_size_mb: int = 100,
        quality: str = "720P",
        timeout: int = 300,
    ) -> DownloadResult:
        """下载单个视频（多分 P 只取传入的 cid，即首 P）。

        Args:
            bvid: 视频 BV 号。
            cid: 分 P 的 cid（取自 view 接口）。
            title: 视频标题，仅用于生成展示文件名。
            max_size_mb: 体积上限，超限直接放弃（下载前按预估拦截，下载中按实际字节兜底）。
            quality: 画质上限，见 :data:`QUALITY_QN`。
            timeout: 下载 + 合并的总超时（秒）。

        Returns:
            :class:`DownloadResult`。失败时 ``path`` 为空且 ``error`` 有原因。
        """
        if not bvid or not cid:
            return DownloadResult(error="缺少视频标识，无法下载")

        try:
            os.makedirs(self.dir, exist_ok=True)
        except Exception as e:  # noqa: BLE001
            logger.error("[BiliCard] 创建下载目录失败 %s: %s", self.dir, e)
            return DownloadResult(error="创建下载目录失败")

        max_bytes = max(int(max_size_mb), 0) * 1024 * 1024
        max_qn = QUALITY_QN.get(quality, 64)
        stem = os.path.join(self.dir, f"{bvid}_{int(time.time())}")
        out = stem + ".mp4"
        display = safe_filename(title, bvid)

        try:
            coro = self._fetch_inner(bvid, cid, max_qn, max_bytes, stem, out)
            size, qname, used_ffmpeg = await asyncio.wait_for(coro, timeout=timeout)
        except asyncio.TimeoutError:
            self._cleanup_stem(stem, out)
            logger.warning("[BiliCard] 视频下载超时（>%ss）bvid=%s", timeout, bvid)
            return DownloadResult(error=f"视频下载超时（超过 {timeout} 秒）")
        except asyncio.CancelledError:
            self._cleanup_stem(stem, out)
            raise
        except _StreamError as e:
            self._cleanup_stem(stem, out)
            logger.warning("[BiliCard] 取流失败 bvid=%s: %s", bvid, e.msg)
            return DownloadResult(error=e.msg)
        except Exception as e:  # noqa: BLE001
            self._cleanup_stem(stem, out)
            logger.error("[BiliCard] 视频下载失败 bvid=%s: %s", bvid, e, exc_info=True)
            return DownloadResult(error=f"视频下载失败：{e}")

        size_mb = size / 1024 / 1024
        logger.debug(
            "[BiliCard] 视频下载完成 bvid=%s 画质=%s 大小=%.1fMB 合并=%s",
            bvid,
            qname,
            size_mb,
            "是" if used_ffmpeg else "否",
        )
        return DownloadResult(
            path=out, filename=display, size_mb=round(size_mb, 1), quality=qname
        )

    async def _fetch_inner(
        self, bvid: str, cid: int, max_qn: int, max_bytes: int, stem: str, out: str
    ) -> Tuple[int, str, bool]:
        """取流并下载，返回 (字节数, 画质名, 是否用了 ffmpeg)。失败抛异常。"""
        # 路线一：DASH + ffmpeg 合并（画质最高）
        if self.ffmpeg_path():
            try:
                v_url, a_url, qname = await self._dash_streams(
                    bvid, cid, max_qn, max_bytes
                )
            except _StreamError as e:
                if not e.retryable:
                    raise
                logger.debug(
                    "[BiliCard] DASH 取流不可用（%s），回退 durl 单文件", e.msg
                )
            else:
                logger.debug("[BiliCard] 取流路线：DASH + ffmpeg 合并，画质 %s", qname)
                return await self._dash_download(
                    v_url, a_url, qname, max_bytes, stem, out
                )

        # 路线二：durl 单文件（无需 ffmpeg）
        url, qname = await self._durl_stream(bvid, cid, max_qn, max_bytes)
        logger.debug("[BiliCard] 取流路线：durl 单文件（无需合并），画质 %s", qname)
        size = await self._download_stream(url, out, max_bytes)
        return size, qname, False

    async def _dash_download(
        self,
        v_url: str,
        a_url: str,
        qname: str,
        max_bytes: int,
        stem: str,
        out: str,
    ) -> Tuple[int, str, bool]:
        """下载 DASH 音视频两条流并合并。任何失败都清掉中间文件。"""
        v_tmp = stem + ".video.m4s"
        a_tmp = stem + ".audio.m4s"
        try:
            v_size = await self._download_stream(v_url, v_tmp, max_bytes, "视频流")
            # 音轨额度扣掉视频已用的，否则两条流各自达标、合计却超限。
            # 下限取 1 而非 0：0 在 _download_stream 里表示"不限制"。
            a_limit = max(max_bytes - v_size, 1) if max_bytes else 0
            a_size = await self._download_stream(a_url, a_tmp, a_limit, "音频流")
            merge_start = time.time()
            await self._merge(v_tmp, a_tmp, out)
            logger.debug(
                "[BiliCard] ffmpeg 合并完成，耗时 %.1fs", time.time() - merge_start
            )
        except BaseException:
            _unlink(out)
            raise
        finally:
            _unlink(v_tmp)
            _unlink(a_tmp)

        try:
            size = os.path.getsize(out)
        except OSError:
            size = v_size + a_size
        return size, qname, True

    # ------------------------------------------------------------------ #
    # 取流
    # ------------------------------------------------------------------ #
    async def _playurl(self, params: dict) -> dict:
        """请求 playurl 接口，返回 data 段。失败抛 :class:`_StreamError`。"""
        try:
            async with aiohttp.ClientSession(timeout=self.api_timeout) as session:
                async with session.get(
                    API_PLAYURL, params=params, headers=self._headers()
                ) as resp:
                    if resp.status != 200:
                        raise _StreamError(f"播放地址接口返回 HTTP {resp.status}")
                    data = await resp.json()
        except _StreamError:
            raise
        except Exception as e:  # noqa: BLE001
            raise _StreamError(f"请求播放地址失败：{e}") from e

        if data.get("code") != 0:
            msg = data.get("message") or "未知错误"
            # -404/87008 等多为大会员/付费/地区限制，换路线也拿不到
            raise _StreamError(f"B站拒绝提供播放地址：{msg}", retryable=False)
        return data.get("data") or {}

    async def _dash_streams(
        self, bvid: str, cid: int, max_qn: int, max_bytes: int
    ) -> Tuple[str, str, str]:
        """选 DASH 音视频流，返回 (视频直链, 音频直链, 画质名)。

        一次请求即可拿到全部可用档位，故降级在本地挑选完成，无需重复请求接口。
        """
        data = await self._playurl(
            {
                "bvid": bvid,
                "cid": cid,
                "qn": max_qn,
                "fnval": 16,  # DASH
                "fnver": 0,
                "fourk": 1,
            }
        )
        dash = data.get("dash") or {}
        videos = [v for v in (dash.get("video") or []) if _stream_url(v)]
        audios = [a for a in (dash.get("audio") or []) if _stream_url(a)]
        if not videos or not audios:
            raise _StreamError("该视频没有可用的 DASH 音视频流")

        # 音轨不必最高音质：群里看视频 132K 足够，省下的体积留给画质。
        # 长视频里高码率音轨能吃掉几十 MB 预算，逼得视频被迫降档
        audios.sort(key=_audio_rank)
        audio = audios[0]
        a_bandwidth = int(audio.get("bandwidth", 0) or 0)
        seconds = float(data.get("timelength", 0) or 0) / 1000

        # 优先 AVC(H.264)：HEVC/AV1 体积更小但 QQ 客户端解不了
        avc = [v for v in videos if int(v.get("codecid", 0) or 0) == _CODEC_AVC]
        pool = avc or videos
        if not avc:
            logger.warning(
                "[BiliCard] 该视频无 AVC 编码流，改用原编码（QQ 客户端可能播不了）"
            )
        # 按档位、码率从高到低，挑第一个预估体积达标的
        pool.sort(
            key=lambda v: (int(v.get("id", 0) or 0), int(v.get("bandwidth", 0) or 0)),
            reverse=True,
        )

        min_est = 0.0  # 候选里最小的预估体积，仅用于超限时的报错文案
        skipped = []  # 因超限被跳过的档位，用于说明"为什么没给你要的画质"
        for v in pool:
            qn = int(v.get("id", 0) or 0)
            if qn > max_qn:
                continue
            est = _estimate_bytes(
                int(v.get("bandwidth", 0) or 0) + a_bandwidth, seconds
            )
            if est and (not min_est or est < min_est):
                min_est = est
            if max_bytes and est and est > max_bytes:
                skipped.append(f"{QN_NAME.get(qn, qn)}≈{est / 1024 / 1024:.0f}MB")
                continue
            if skipped:
                logger.info(
                    "[BiliCard] %s 超出 %sMB 上限，降至 %s",
                    "、".join(skipped),
                    max_bytes // 1024 // 1024,
                    QN_NAME.get(qn, qn),
                )
            if not est and max_bytes:
                # 接口没给 bandwidth/timelength，预估失效；仍可下载，但只能靠
                # 下载中的字节校验拦截，可能下到一半才中止
                logger.warning(
                    "[BiliCard] 无法预估视频体积（接口缺 bandwidth/timelength），"
                    "超限只能在下载中途拦截"
                )
            return _stream_url(v), _stream_url(audio), QN_NAME.get(qn, f"qn{qn}")

        hint = f"（最低画质仍约 {min_est / 1024 / 1024:.0f}MB）" if min_est else ""
        raise _StreamError(f"视频体积超出上限{hint}", retryable=False)

    async def _durl_stream(
        self, bvid: str, cid: int, max_qn: int, max_bytes: int
    ) -> Tuple[str, str]:
        """选 durl 单文件流，返回 (直链, 画质名)。

        ``platform=html5`` 是关键：B站据此返回已封装音轨的 Progressive MP4，
        免去 DASH 合并。durl 一次只给一个档位，故超限时逐档降级重试。
        """
        last_size = 0
        for qn in [q for q in _DURL_LADDER if q <= max_qn] or [16]:
            data = await self._playurl(
                {
                    "bvid": bvid,
                    "cid": cid,
                    "qn": qn,
                    "fnval": 3,
                    "fnver": 0,
                    "player": 3,
                    "otype": "json",
                    "platform": "html5",
                    "high_quality": 1,
                }
            )
            durl = data.get("durl") or []
            if not durl:
                continue
            first = durl[0]
            url = first.get("url") or ""
            if not url:
                continue
            size = int(first.get("size", 0) or 0)
            last_size = size or last_size
            if max_bytes and size and size > max_bytes:
                logger.debug(
                    "[BiliCard] qn=%s 体积 %.0fMB 超限，降档重试",
                    qn,
                    size / 1024 / 1024,
                )
                continue
            # B站实际给的档位可能低于请求值，以响应里的 quality 为准
            actual = int(data.get("quality", qn) or qn)
            return url, QN_NAME.get(actual, f"qn{actual}")

        if last_size:
            raise _StreamError(
                f"视频体积超出上限（最低画质仍约 {last_size / 1024 / 1024:.0f}MB）",
                retryable=False,
            )
        raise _StreamError("没拿到可下载的视频地址（可能是付费/大会员/地区限制视频）")

    # ------------------------------------------------------------------ #
    # 下载与合并
    # ------------------------------------------------------------------ #
    async def fetch_direct(
        self,
        url: str,
        key: str,
        title: str = "",
        *,
        referer: str = "",
        quality: str = "原画",
        max_size_mb: int = 100,
        timeout: int = 300,
    ) -> DownloadResult:
        """下载一个已知的视频直链（抖音等不需要选流的平台用）。

        与 :meth:`fetch` 共用同一套落盘、限额与清理机制，区别只是省掉取流选档。

        Args:
            url: 视频直链。
            key: 生成落盘文件名用的标识（如 aweme_id）。
            title: 作品标题，仅用于展示文件名。
            referer: 对方 CDN 要求的 Referer。请求**不带 B站 Cookie**。
            quality: 已由调用方选定的画质描述，仅用于回填结果与日志。
            max_size_mb: 体积上限，超限放弃。
            timeout: 下载总超时（秒）。
        """
        if not url:
            return DownloadResult(error="没有可下载的视频地址")
        try:
            os.makedirs(self.dir, exist_ok=True)
        except Exception as e:  # noqa: BLE001
            logger.error("[BiliCard] 创建下载目录失败 %s: %s", self.dir, e)
            return DownloadResult(error="创建下载目录失败")

        max_bytes = max(int(max_size_mb), 0) * 1024 * 1024
        out = os.path.join(self.dir, f"{key}_{int(time.time())}.mp4")
        headers = self._plain_headers(referer)
        try:
            size = await asyncio.wait_for(
                self._download_stream(url, out, max_bytes, "视频", headers),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            _unlink(out)
            _unlink(out + ".part")
            logger.warning("[BiliCard] 视频下载超时（>%ss）key=%s", timeout, key)
            return DownloadResult(error=f"视频下载超时（超过 {timeout} 秒）")
        except asyncio.CancelledError:
            _unlink(out)
            _unlink(out + ".part")
            raise
        except _StreamError as e:
            _unlink(out)
            _unlink(out + ".part")
            logger.warning("[BiliCard] 视频下载中止 key=%s: %s", key, e.msg)
            return DownloadResult(error=e.msg)
        except Exception as e:  # noqa: BLE001
            _unlink(out)
            _unlink(out + ".part")
            logger.error("[BiliCard] 视频下载失败 key=%s: %s", key, e, exc_info=True)
            return DownloadResult(error=f"视频下载失败：{e}")

        size_mb = size / 1024 / 1024
        logger.debug("[BiliCard] 视频下载完成 key=%s 大小=%.1fMB", key, size_mb)
        return DownloadResult(
            path=out,
            filename=safe_filename(title, key),
            size_mb=round(size_mb, 1),
            quality=quality,
        )

    async def _download_stream(
        self,
        url: str,
        dest: str,
        max_bytes: int,
        label: str = "视频",
        headers: Optional[dict] = None,
    ) -> int:
        """流式下载到 dest，返回字节数。

        先写 ``.part`` 再 :func:`os.replace`，避免半截文件被当成成品；
        边下边累计校验上限，防止 Content-Length 缺失或不实导致撑爆磁盘。
        大文件耗时长，按固定间隔打进度日志，避免看起来像卡死。
        """
        part = dest + ".part"
        written = 0
        last_log = time.time()
        try:
            # 下载不设 total 超时（大文件慢），由 fetch 的总超时统一兜底；
            # 但设 sock_read 防止连接挂死不返回数据。
            timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=60)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, headers=headers or self._headers()) as resp:
                    if resp.status != 200:
                        raise RuntimeError(f"下载返回 HTTP {resp.status}")
                    clen = resp.headers.get("Content-Length")
                    total = int(clen) if clen and clen.isdigit() else 0
                    if max_bytes and total > max_bytes:
                        raise _StreamError(
                            f"视频体积 {total / 1024 / 1024:.0f}MB 超出上限",
                            retryable=False,
                        )
                    logger.debug(
                        "[BiliCard] 开始下载%s（%s）",
                        label,
                        f"{total / 1024 / 1024:.1f}MB" if total else "大小未知",
                    )
                    with open(part, "wb") as f:
                        async for chunk in resp.content.iter_chunked(1024 * 1024):
                            await asyncio.to_thread(f.write, chunk)
                            written += len(chunk)
                            if max_bytes and written > max_bytes:
                                raise _StreamError(
                                    "视频体积超出上限（下载中止）", retryable=False
                                )
                            now = time.time()
                            if now - last_log >= _PROGRESS_LOG_SECONDS:
                                last_log = now
                                done = written / 1024 / 1024
                                pct = f" ({written * 100 // total}%)" if total else ""
                                logger.debug(
                                    "[BiliCard] %s下载中 %.1fMB%s", label, done, pct
                                )
            os.replace(part, dest)
            return written
        except BaseException:
            _unlink(part)
            raise

    async def _merge(self, video: str, audio: str, out: str) -> None:
        """用 ffmpeg 把音视频流封装成 MP4（``-c copy`` 不转码，秒级完成）。

        ``+faststart`` 把 moov 前置，QQ 等客户端可边下边播。
        """
        cmd = [
            self.ffmpeg_path(),
            "-y",
            "-loglevel",
            "error",
            "-i",
            video,
            "-i",
            audio,
            "-c",
            "copy",
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-movflags",
            "+faststart",
            out,
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await proc.communicate()
        except BaseException:
            # 被取消/超时：确保子进程不残留
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
            raise
        if proc.returncode != 0:
            detail = (stderr or b"").decode(errors="ignore").strip()[:200]
            raise RuntimeError(
                f"ffmpeg 合并失败：{detail or f'退出码 {proc.returncode}'}"
            )

    # ------------------------------------------------------------------ #
    # 清理
    # ------------------------------------------------------------------ #
    def _cleanup_stem(self, stem: str, out: str) -> None:
        """清掉一次下载可能产生的全部中间文件。"""
        for p in (
            out,
            out + ".part",
            stem + ".video.m4s",
            stem + ".video.m4s.part",
            stem + ".audio.m4s",
            stem + ".audio.m4s.part",
        ):
            _unlink(p)

    def cleanup(self, path: str) -> None:
        """删除一个已交付的视频文件（发送完成后调用）。"""
        _unlink(path)

    def sweep(self, max_age_minutes: int = 30) -> int:
        """清掉下载目录里的超龄残留，返回删除数量。

        正常路径下文件发完即删；这里兜的是进程被强杀（容器重启/OOM）留下的孤儿
        文件——没有它，磁盘占用会随重启次数无上限增长。

        ``max_age_minutes=0`` 表示清空整个目录（插件启动时用，此刻不可能有在途下载）。
        """
        if not os.path.isdir(self.dir):
            return 0
        deadline = time.time() - max(max_age_minutes, 0) * 60
        removed = 0
        try:
            entries = os.listdir(self.dir)
        except Exception as e:  # noqa: BLE001
            logger.warning("[BiliCard] 扫描下载目录失败: %s", e)
            return 0
        for name in entries:
            p = os.path.join(self.dir, name)
            try:
                if not os.path.isfile(p) or os.path.getmtime(p) > deadline:
                    continue
            except OSError:
                continue
            _unlink(p)
            removed += 1
        if removed:
            logger.info("[BiliCard] 已清理 %s 个残留视频文件", removed)
        return removed


def is_send_timeout(e: BaseException) -> bool:
    """协议端返回的是"超时"还是真失败。

    OneBot 的 retcode 1200 只表示没在时限内收到回执，消息往往**已经发出去了**。
    这种情况绝不能换个方式重发——群里会出现两条一模一样的视频。
    """
    if getattr(e, "retcode", None) == 1200:
        return True
    return "timeout" in str(e).lower()


def _audio_rank(a: dict) -> tuple:
    """音轨挑选优先级：132K 够用且省，其次 64K；Hi-Res/杜比放最后。"""
    order = {30232: 0, 30216: 1, 30280: 2}  # 132K / 64K / 192K
    aid = int(a.get("id", 0) or 0)
    return (order.get(aid, 3), int(a.get("bandwidth", 0) or 0))


def _stream_url(stream: dict) -> str:
    """DASH 流的直链字段在不同接口版本下有两种拼写。"""
    return stream.get("baseUrl") or stream.get("base_url") or ""


def _estimate_bytes(bandwidth: int, seconds: float) -> float:
    """按码率与时长预估体积：bandwidth(bps) × 秒 / 8。

    比 HTTP HEAD 更可靠（B站 CDN 常不返回 Content-Length），且下载前就能判断。
    """
    if bandwidth <= 0 or seconds <= 0:
        return 0.0
    return bandwidth * seconds / 8
