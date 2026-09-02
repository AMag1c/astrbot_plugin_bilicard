"""BiliCard 插件主入口。

监听群聊消息（私聊不自动解析），识别两类分享并回卡片，无需 @机器人或指令前缀：

- B站：BV / av / 链接 / b23 短链 / 小程序卡片 → 封面、UP主、统计、在线人数、
  热门评论、AI 字幕总结；
- 抖音：口令 / v.douyin.com 短链 / 网页地址 → 封面 + 时长 + 点赞。

出图依赖远程 t2i 服务，**渲染失败时降级为文字，但链接一定送出去**。

另可选下载视频并发到会话——默认视频消息，也可切文件上传（见 bilicard/downloader.py）；
送达后立即删除本地文件，另有启动/定时/卸载三处清扫兜底异常残留，磁盘不会累积。
"""

import asyncio
import json
import os
import re
import time
from typing import Dict, Optional, Set

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools, register

from .bilicard import douyin, login, parser, render, summarizer
from .bilicard.client import BiliClient
from .bilicard.douyin import DouyinClient
from .bilicard.config import Config
from .bilicard.credential import CredentialStore
from .bilicard.data_manager import SubscriptionStore
from .bilicard.downloader import DownloadResult, VideoDownloader, is_send_timeout


def _is_http_url(s) -> bool:
    """html_render 返回值是否为可直接发图的 http(s) URL。"""
    return isinstance(s, str) and s.startswith(("http://", "https://"))


# 以 HTTP 直链方式交给协议端上传时，删本地文件前的宽限秒数（协议端要回头来拉）
_FILE_URL_GRACE_SECONDS = 120

# 卡片就绪后再等几秒才发视频：协议端（NapCat）同时处理两条消息容易 sendMsg 超时
_SEND_STAGGER_SECONDS = 5
# 等卡片就绪的上限。远程 t2i 慢时不能让视频干等，超时就先发
_CARD_WAIT_TIMEOUT = 180


# 元数据以 metadata.yaml 为唯一来源；此处保持与其一致，避免两份漂移。
@register(
    "astrbot_plugin_bilicard",
    "AMag1c",
    "自动识别群聊中的 B站视频链接、BV号、b23 短链与抖音口令/短链，渲染成信息卡片"
    "（B站含封面、UP主、播放/弹幕/点赞等统计、实时在线人数、热门评论、AI视频总结；"
    "抖音为封面+时长+点赞）并附上原视频链接；出图失败自动降级发链接；"
    "可选下载视频并以视频消息或文件形式发送。",
    "v0.6.0",
    "https://github.com/AMag1c/astrbot_plugin_bilicard",
)
class BiliCard(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config or {}
        self.cfg = Config(self.config)  # 配置统一入口（默认值集中、避免漂移）

        cookie = self.cfg.get("bilibili_cookie", {}) or {}
        self.cookies = {
            "SESSDATA": cookie.get("sessdata", ""),
            "bili_jct": cookie.get("bili_jct", ""),
        }
        # refresh_token（即 ac_time_value，非 Cookie，单独存）：用于自动续期登录态
        self._ac_time_value = cookie.get("ac_time_value", "")
        self._login_umo = ""  # 最近登录会话，续期失效时回该处提醒管理员
        self._last_refresh_ts = 0.0  # 上次续期检查时间戳（每日最多查一次）
        self._relogin_notified = False  # 已提醒过重新登录（避免每天重复打扰）
        self.client = BiliClient(self.cookies)
        self.douyin = DouyinClient()
        self._tmpl = render.load_template()
        self._douyin_tmpl = render.load_douyin_template()
        # 冷却记录： "{umo}:{bvid}" -> 上次解析时间戳
        self._cooldown: Dict[str, float] = {}

        try:
            data_dir = StarTools.get_data_dir("astrbot_plugin_bilicard")
        except Exception:  # noqa: BLE001
            data_dir = os.path.dirname(os.path.abspath(__file__))
        self._data_dir = str(data_dir)
        self.cred = CredentialStore(self._data_dir)
        self.store = SubscriptionStore(self.cfg)
        # 视频下载：与 self.cookies 共享同一 dict，登录/续期后自动用上新 Cookie
        self.downloader = VideoDownloader(self._data_dir, self.cookies)
        # 下载串行化：同时下多个视频会挤占带宽和磁盘，一次只跑一个
        self._download_sem = asyncio.Semaphore(1)
        self._upload_tasks: Set[asyncio.Task] = set()  # 持强引用，防止被 GC 回收
        self._upload_off_notified = False  # "未开启视频上传"只提示一次
        # 协议端读不到本地文件（与 AstrBot 分开部署）后置为 True，之后直接走 HTTP 直链
        self._prefer_url_ref = False
        self._last_video: Dict[
            str, str
        ] = {}  # umo -> 最近解析的 bvid（/下载视频 免参数）

        # 加载扫码登录持久化的凭证（优先于手填配置）
        saved = self.cred.load()
        if saved.get("SESSDATA"):
            self.cookies["SESSDATA"] = saved["SESSDATA"]
            self.cookies["bili_jct"] = saved.get("bili_jct", "")
            self.client = BiliClient(self.cookies)
        if saved.get("ac_time_value"):
            self._ac_time_value = saved["ac_time_value"]
        self._login_umo = saved.get("login_umo", "") or ""

    # ------------------------------------------------------------------ #
    # 配置便捷读取
    # ------------------------------------------------------------------ #
    def _c(self, key, default=None):
        return self.cfg.get(key, default)

    def _video_cfg(self, platform: str = "bili") -> dict:
        """取该平台的视频下载配置（两个平台各一组，互不影响）。"""
        return self.cfg.group("douyin_video" if platform == "douyin" else "bili_video")

    # ------------------------------------------------------------------ #
    # 全量消息监听：自动识别并解析
    # ------------------------------------------------------------------ #
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        # 只在群聊自动解析：私聊没有"帮群友展开链接"的场景，且会平白增加风控风险
        if event.is_private_chat():
            return

        text = event.message_str or ""
        if text.strip().startswith("/"):
            return

        if not self._session_allowed(event):
            return

        candidate = self._collect_candidate_text(event, text)
        token = parser.find_video_token(candidate)
        if not token:
            # B站没命中再看抖音（口令/短链/网页地址/小程序卡片），解析默认开启
            dy_url = douyin.find_link(candidate)
            if dy_url:
                async for r in self._handle_douyin(event, dy_url):
                    yield r
            return

        bvid, aid = await self._resolve_token(token)
        if not bvid and not aid:
            return

        info = await self.client.get_video_info(bvid=bvid, aid=aid)
        if not info or not info.get("bvid"):
            return

        if not self._check_cooldown(event.unified_msg_origin, info["bvid"]):
            return

        # 阻止这条消息继续触发 LLM 闲聊
        event.stop_event()
        # 记住本会话最近一个视频，供 /下载视频 免参数使用
        self._last_video[event.unified_msg_origin] = info["bvid"]

        logger.info("[BiliCard] B站 %s「%s」", info["bvid"], info.get("title", ""))

        # 视频下载与卡片渲染互不依赖，故在渲染之前就调度：
        # 1) 远程 t2i 渲染慢/超时/失败时（公共服务不稳定），视频照样能送达；
        # 2) 下载与渲染并行，整体更快；
        # 3) 必须早于任何 yield —— 框架是洋葱模型，yield 把控制权交回管线，若有
        #    插件在 result_decorate / respond 阶段 stop_event()（如
        #    astrbot_plugin_recall），管线不再回来，yield 之后的代码永不执行。
        card_ready = asyncio.Event()  # 卡片就绪信号，保证卡片先于视频送达
        if self._video_cfg("bili")["enabled"]:
            self._schedule_video_upload(
                event.unified_msg_origin, info, after=card_ready
            )
        elif not self._upload_off_notified:
            # 只提示一次：否则每条视频都刷一行。没有这条日志，用户会以为下载功能
            # 坏了，实际只是开关没开
            self._upload_off_notified = True
            logger.info(
                "[BiliCard] B站视频下载未启用（配置「📺 B站 · 视频下载发送」里的"
                "「自动下载并发送视频」），只发卡片。也可用 /下载视频 指令单次下载"
            )

        summary = await self._safe_summary(event, info)
        img_url = ""
        try:
            img_url = await self._render_card(
                info, summary=summary, show_post_bar=False
            )
        except asyncio.TimeoutError:
            logger.warning(
                "[BiliCard] 渲染超时（>%ss），降级为文字 bvid=%s（远程 t2i 慢/过载）",
                self.cfg.int("render_timeout"),
                info.get("bvid"),
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "[BiliCard] 渲染失败，降级为文字 bvid=%s：%s", info.get("bvid"), e
            )
        finally:
            # 出图成功与否都放行视频；必须在 yield 之前置位，yield 之后不保证执行
            card_ready.set()

        try:
            # 图文合并为一条，避免多次 yield 被其它插件中断传播
            yield event.chain_result(self._bili_chain(info, img_url, summary))
        except Exception as e:  # noqa: BLE001
            logger.error(
                "[BiliCard] 发送失败 bvid=%s: %s", info.get("bvid"), e, exc_info=True
            )

    # ------------------------------------------------------------------ #
    # 抖音：口令/短链/网页地址 → 卡片（封面+时长+点赞）+ 链接，可选下载视频
    # ------------------------------------------------------------------ #
    async def _handle_douyin(self, event: AstrMessageEvent, url: str):
        """解析抖音作品并回卡片。出图失败照发链接；视频下载走后台队列。"""
        aweme_id = await self.douyin.resolve_aweme_id(url)
        if not aweme_id:
            return
        info = await self.douyin.get_video_info(aweme_id)
        if not info:
            return
        # 冷却在拿到信息之后才记：解析失败不该占用冷却，否则用户重发要干等
        if not self._check_cooldown(event.unified_msg_origin, aweme_id):
            return

        event.stop_event()  # 别让这条消息再去触发 LLM 闲聊
        logger.info("[BiliCard] 抖音 %s「%s」", aweme_id, info.get("title", ""))

        # 与 B站同理：下载调度必须早于 yield，且不依赖卡片渲染结果
        card_ready = asyncio.Event()
        if self._video_cfg("douyin")["enabled"]:
            if info.get("video_url"):
                self._schedule_video_upload(
                    event.unified_msg_origin, info, "douyin", after=card_ready
                )
            else:
                logger.debug(
                    "[BiliCard] 抖音作品无视频直链（可能是图集），跳过下载 %s", aweme_id
                )

        img_url = ""
        try:
            img_url = await self._render_douyin_card(info)
        except asyncio.TimeoutError:
            logger.warning("[BiliCard] 抖音卡片渲染超时，降级为文字 %s", aweme_id)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "[BiliCard] 抖音卡片渲染失败，降级为文字 %s：%s", aweme_id, e
            )
        finally:
            card_ready.set()  # 同 on_message：必须在 yield 之前放行

        try:
            yield event.chain_result(
                self._build_chain(
                    info.get("title", ""),
                    info.get("share_url", ""),
                    img_url,
                    fallback_title="抖音视频",
                )
            )
        except Exception as e:  # noqa: BLE001
            logger.error("[BiliCard] 抖音发送失败 %s: %s", aweme_id, e, exc_info=True)

    async def _render_douyin_card(self, info: dict) -> str:
        """渲染抖音卡片（封面 + 时长 + 点赞）。"""
        logger.debug("[BiliCard] 开始渲染抖音卡片 %s", info.get("aweme_id", ""))
        # 封面转 data URI 规避防盗链；用抖音自己的请求头，不带 B站 Cookie
        info["cover"] = await self.douyin.fetch_image_data_uri(info.get("cover", ""))
        data = render.build_douyin_data(info)
        options = {"type": "png", "full_page": True, "scale": "device"}
        url = await asyncio.wait_for(
            self.html_render(self._douyin_tmpl, data, options=options),
            timeout=self.cfg.int("render_timeout"),
        )
        return url.strip() if isinstance(url, str) else url

    # ------------------------------------------------------------------ #
    # LLM 函数工具（AI 对话时执行订阅/登录管理；均仅管理员，函数内自鉴权）
    # ------------------------------------------------------------------ #
    @filter.llm_tool(name="subscribe_up")
    async def llm_subscribe_up(self, event: AstrMessageEvent, uid: str):
        """订阅一个 B站 UP主，有新投稿时自动推送到当前会话（仅管理员可用）。

        Args:
            uid(string): UP主的 UID（纯数字，如 486906719）
        """
        if not event.is_admin():
            yield event.plain_result("该操作需要管理员权限。")
            return
        uid = self._extract_uid(uid)
        if not uid:
            yield event.plain_result("没识别到有效的 UP主 UID。")
            return
        async for r in self._do_subscribe(event, uid):
            yield r

    @filter.llm_tool(name="unsubscribe_up")
    async def llm_unsubscribe_up(self, event: AstrMessageEvent, uid: str):
        """取消订阅一个 B站 UP主（仅管理员可用）。

        Args:
            uid(string): UP主的 UID（纯数字）
        """
        if not event.is_admin():
            yield event.plain_result("该操作需要管理员权限。")
            return
        uid = self._extract_uid(uid)
        if not uid:
            yield event.plain_result("没识别到有效的 UP主 UID。")
            return
        async for r in self._do_unsubscribe(event, uid):
            yield r

    @filter.llm_tool(name="list_subscribed_up")
    async def llm_list_subscribed_up(self, event: AstrMessageEvent):
        """查看当前会话已订阅的 B站 UP主列表（所有人可用，只读查询）。"""
        async for r in self.sublist_cmd(event):
            yield r

    @filter.llm_tool(name="bili_login")
    async def llm_bili_login(self, event: AstrMessageEvent):
        """发起 B站扫码登录，生成二维码（仅管理员可用）。订阅与 AI 总结功能需要登录。"""
        if not event.is_admin():
            yield event.plain_result("该操作需要管理员权限。")
            return
        async for r in self.login_cmd(event):
            yield r

    @filter.llm_tool(name="bili_logout")
    async def llm_bili_logout(self, event: AstrMessageEvent):
        """清除已保存的 B站登录信息（仅管理员可用）。"""
        if not event.is_admin():
            yield event.plain_result("该操作需要管理员权限。")
            return
        async for r in self.logout_cmd(event):
            yield r

    @filter.llm_tool(name="get_up_latest_video")
    async def llm_get_up_latest(self, event: AstrMessageEvent, uid: str):
        """获取某个 B站 UP主的最新一条投稿，并以视频卡片图（含链接）发送给用户（所有人可用）。

        Args:
            uid(string): UP主的 UID（纯数字，如 486906719）
        """
        async for r in self._do_latest_up(event, uid):
            yield r

    @filter.llm_tool(name="get_subscribed_latest_videos")
    async def llm_get_subs_latest(self, event: AstrMessageEvent):
        """拉取当前会话已订阅的所有 B站 UP主的最新一条投稿，逐个发送视频卡片（所有人可用，最多前 10 位）。"""
        async for r in self._do_latest_subs(event):
            yield r

    # ------------------------------------------------------------------ #
    # 内部逻辑
    # ------------------------------------------------------------------ #
    @staticmethod
    def _collect_candidate_text(event: AstrMessageEvent, text: str) -> str:
        """汇集本条消息中可能含视频标识的文本：正文 + 原始消息 + 组件 data
        （小程序卡片在 Json 组件 data 里）。引用/回复消息只取用户本次输入，避免被
        引用内容里的旧链接反复触发。返回去掉 JSON 转义斜杠（\\/ → /）的合并串。
        """
        parts = [text]
        try:
            msg_obj = getattr(event, "message_obj", None)
            if msg_obj is not None:
                comps = getattr(msg_obj, "message", None) or []
                is_reply = any(
                    (getattr(c, "type", "") or "").lower() == "reply"
                    or "reply" in type(c).__name__.lower()
                    for c in comps
                )
                if not is_reply:
                    parts.append(str(getattr(msg_obj, "raw_message", "") or ""))
                    for comp in comps:
                        data = getattr(comp, "data", None)
                        if isinstance(data, dict):
                            try:
                                parts.append(json.dumps(data, ensure_ascii=False))
                            except Exception:  # noqa: BLE001
                                parts.append(str(data))
                        elif data:
                            parts.append(str(data))
        except Exception:  # noqa: BLE001
            pass
        return " ".join(parts).replace("\\/", "/")

    async def _resolve_token(self, token):
        kind, value = token
        if kind == "b23":
            resolved = await self.client.resolve_b23(value)
            if not resolved:
                return None, None
            kind, value = resolved
        if kind == "bv":
            return value, None
        if kind == "av":
            return None, value
        return None, None

    def _check_cooldown(self, umo: str, bvid: str) -> bool:
        cd = int(self._c("cooldown_seconds", 60) or 0)
        if cd <= 0:
            return True
        key = f"{umo}:{bvid}"
        now = time.time()
        if now - self._cooldown.get(key, 0) < cd:
            logger.debug(f"[BiliCard] 冷却中，跳过 {key}")
            return False
        self._cooldown[key] = now
        return True

    def _build_chain(
        self,
        title: str,
        link: str,
        img_url: str,
        summary: Optional[str] = None,
        fallback_title: str = "视频",
    ) -> list:
        """构造要发出去的消息链（B站与抖音共用）。

        渲染成功就发卡片图（按 show_link 决定是否附链接）；渲染失败则降级成文字，
        **链接一定送出去**——远程 t2i 服务不稳，不能因为出图失败就让用户什么都收不到。
        """
        if _is_http_url(img_url):
            # 用 URL 交给框架发，别自作聪明改成 base64：实测协议端(NapCat)处理
            # base64 图片会 sendMsg 超时(retcode 1200)，同样大小走 URL 却正常
            chain = [Comp.Image.fromURL(img_url)]
            if self._c("show_link", True):
                chain.append(Comp.Plain(f"\n{link}"))
            return chain
        parts = [title or fallback_title]
        if summary:  # AI 总结已经花过 LLM 成本，降级时一并带上
            parts.append(summary)
        parts.append(link)
        return [Comp.Plain("\n\n".join(p for p in parts if p))]

    def _bili_chain(
        self, info: dict, img_url: str, summary: Optional[str] = None
    ) -> list:
        return self._build_chain(
            info.get("title", ""),
            f"https://www.bilibili.com/video/{info['bvid']}",
            img_url,
            summary,
            "B站视频",
        )

    async def _render_card(
        self, info: dict, summary: Optional[str], show_post_bar: bool
    ) -> str:
        """渲染卡片图片。summary 与 show_post_bar 区分两种模板：
        - 链接总结：show_post_bar=False，summary 有值
        - 订阅推送：show_post_bar=True，summary=None
        """
        logger.debug("[BiliCard] 开始渲染卡片 bvid=%s", info.get("bvid", ""))
        online_text = await self._build_online(info)

        comments = []
        if self._c("enable_comments", True):
            comments = await self.client.get_hot_comments(
                info["aid"], int(self._c("comment_count", 3))
            )

        # 图片转 base64，规避防盗链
        info["cover"] = await self.client.fetch_image_data_uri(info.get("cover", ""))
        info["owner"]["face"] = await self.client.fetch_image_data_uri(
            info["owner"].get("face", "")
        )

        data = render.build_template_data(
            info,
            online_text=online_text,
            comments=comments,
            summary=summary,
            show_post_bar=show_post_bar,
        )
        # PNG 无损 + 设备像素缩放，尽量保证清晰度
        options = {"type": "png", "full_page": True, "scale": "device"}
        # 限定在线渲染总耗时上限（可配 render_timeout，默认 50s），避免远程 t2i 504/挂起拖住消息处理
        url = await asyncio.wait_for(
            self.html_render(self._tmpl, data, options=options),
            timeout=self.cfg.int("render_timeout"),
        )
        # t2i 返回的 URL 可能带前后空格（如 t2i_endpoint 配置粘贴时多了空格），去掉以防 fromURL 误判
        return url.strip() if isinstance(url, str) else url

    async def _build_online(self, info: dict) -> Optional[str]:
        # 固定使用 B站视频实时在线观看人数（真实数据）
        total = await self.client.get_online(info["bvid"], info["cid"])
        return f"{total} 人在线" if total is not None else None

    async def _safe_summary(self, event: AstrMessageEvent, info: dict) -> Optional[str]:
        """取 AI 总结。它只是卡片上的一个区块，失败不该拖累整张卡片。"""
        if not self._c("enable_ai_summary", True):
            return None
        try:
            return await self._build_summary(event, info)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "[BiliCard] AI 总结失败，跳过 bvid=%s：%s", info.get("bvid"), e
            )
            return None

    async def _build_summary(
        self, event: AstrMessageEvent, info: dict
    ) -> Optional[str]:
        text = await self.client.get_subtitle_text(
            info["bvid"], info["cid"], int(self._c("summary_max_subtitle", 4000))
        )
        if not text:
            logger.debug("[BiliCard] 视频无字幕，跳过 AI 总结 bvid=%s", info["bvid"])
            return None

        provider_id = self._c("llm_provider_id", "")
        if provider_id:
            provider = self.context.get_provider_by_id(provider_id)
        else:
            provider = self.context.get_using_provider(umo=event.unified_msg_origin)
        if not provider:
            logger.warning("[BiliCard] 未找到可用 LLM Provider，跳过 AI 总结")
            return None

        logger.debug("[BiliCard] 提取到字幕，调用 LLM 生成总结 bvid=%s", info["bvid"])

        async def llm_ask(prompt: str) -> str:
            resp = await provider.text_chat(prompt=prompt, session_id="bilicard")
            return getattr(resp, "completion_text", "") or ""

        return await summarizer.summarize(
            info["title"],
            text,
            llm_ask,
            max_chars=int(self._c("summary_max_chars", 120)),
        )

    # ------------------------------------------------------------------ #
    # 视频下载与上传（下载 → 送达 → 立刻删本地文件，不留存）
    # ------------------------------------------------------------------ #
    def _schedule_video_upload(
        self,
        umo: str,
        info: dict,
        platform: str = "bili",
        after: Optional[asyncio.Event] = None,
    ) -> None:
        """把一次视频下载上传丢进后台队列。

        下载动辄几十秒到几分钟，不能占着消息处理链路；``_download_sem`` 保证同时
        只跑一个，避免群里连发多个链接时挤爆带宽和磁盘。
        ``after`` 是卡片就绪信号，用来保证卡片先于视频送达。
        """
        self._track(
            asyncio.create_task(self._deliver_video_bg(umo, info, platform, after))
        )

    async def _deliver_video_bg(
        self,
        umo: str,
        info: dict,
        platform: str = "bili",
        after: Optional[asyncio.Event] = None,
    ) -> None:
        """后台任务入口：兜住所有异常，否则 task 里的报错只会静默丢失。"""
        try:
            await self._deliver_video(umo, info, platform=platform, after=after)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.error(
                "[BiliCard] 视频上传任务异常 %s: %s",
                info.get("bvid") or info.get("aweme_id", ""),
                e,
                exc_info=True,
            )

    def _track(self, task: asyncio.Task) -> None:
        """持强引用，否则 task 可能被 GC 提前回收；完成后自动摘除。"""
        self._upload_tasks.add(task)
        task.add_done_callback(self._upload_tasks.discard)

    def _schedule_cleanup(self, path: str, delay: int) -> None:
        """延迟删除本地文件（协议端要回头来拉 URL 时用）。

        必须走后台任务：否则指令场景会被这段等待卡住，用户明明已经收到文件，
        指令却要几分钟后才结束。被取消时也照删。
        """

        async def _later() -> None:
            try:
                await asyncio.sleep(delay)
            finally:
                self.downloader.cleanup(path)

        self._track(asyncio.create_task(_later()))

    async def _deliver_video(
        self,
        umo: str,
        info: dict,
        event: Optional[AstrMessageEvent] = None,
        platform: str = "bili",
        after: Optional[asyncio.Event] = None,
    ) -> DownloadResult:
        """下载视频并送达会话。无论成功、失败还是异常，本地文件都会被删掉。

        Returns:
            下载结果。``error`` 非空即为失败，其内容可直接回给用户。
        """
        is_dy = platform == "douyin"
        key = info.get("aweme_id", "") if is_dy else info.get("bvid", "")
        vc = self._video_cfg(platform)
        quality = str(vc.get("quality", "720P"))
        max_mb = int(vc.get("max_size_mb", 100) or 100)
        timeout = int(vc.get("timeout", 300) or 300)
        logger.debug(
            "[BiliCard] 准备下载%s视频 %s 画质≤%s 体积≤%sMB",
            "抖音" if is_dy else "B站",
            key,
            quality,
            max_mb,
        )
        if self._download_sem.locked():
            logger.debug("[BiliCard] 已有视频在下载，%s 排队等待", key)
        async with self._download_sem:
            if is_dy:
                url, desc = douyin.pick_video_url(info, quality, max_mb)
                if not url:
                    return DownloadResult(
                        error=f"没有可用的视频流，或最低画质也超过 {max_mb}MB 上限"
                    )
                result = await self.downloader.fetch_direct(
                    url,
                    key,
                    info.get("title", ""),
                    referer="https://www.douyin.com/",
                    quality=desc,
                    max_size_mb=max_mb,
                    timeout=timeout,
                )
            else:
                result = await self.downloader.fetch(
                    key,
                    int(info.get("cid", 0) or 0),
                    info.get("title", ""),
                    max_size_mb=max_mb,
                    quality=quality,
                    timeout=timeout,
                )
        if not result.ok:
            logger.warning("[BiliCard] 视频未送达 %s：%s", key, result.error)
            return result

        path = result.path
        grace = 0
        try:
            # 等卡片先发：短视频下载比出图快得多，不等的话顺序会颠倒。
            # 卡片那边卡死也不能干等，故设上限；随后再错开几秒避免协议端并发超时。
            # 这段必须在 try 内：期间被取消时 CancelledError 会直达 finally，文件照删
            if after is not None:
                try:
                    await asyncio.wait_for(after.wait(), timeout=_CARD_WAIT_TIMEOUT)
                except asyncio.TimeoutError:
                    logger.debug("[BiliCard] 等卡片超时，先发视频 %s", key)
            await asyncio.sleep(_SEND_STAGGER_SECONDS)
            grace = await self._send_video(umo, result, event, platform)
        except Exception as e:  # noqa: BLE001
            logger.error("[BiliCard] 视频发送失败 %s: %s", key, e, exc_info=True)
            result.error = f"视频发送失败：{e}"
        finally:
            # 送达与否都要删：这是唯一的常规回收点，漏掉就会无限堆积
            if grace > 0:
                self._schedule_cleanup(path, grace)  # 后台延迟删，不阻塞调用方
            else:
                self.downloader.cleanup(path)
        return result

    async def _send_video(
        self,
        umo: str,
        result: DownloadResult,
        event: Optional[AstrMessageEvent],
        platform: str = "bili",
    ) -> int:
        """按该平台的配置把视频送达：视频消息（默认）或文件上传。

        Returns:
            删除本地文件前应等待的秒数。0 表示对方已取完文件、可立即删除。
        """
        if str(self._video_cfg(platform).get("send_mode", "video")) == "video":
            return await self._send_as_video(umo, result)
        # QQ(OneBot) 有专门的文件上传接口，比消息段可靠
        grace = await self._upload_via_onebot(umo, result, event)
        if grace >= 0:
            return grace
        await self.context.send_message(
            umo,
            MessageChain(
                chain=[
                    Comp.File(name=result.filename, file=os.path.abspath(result.path))
                ]
            ),
        )
        self._log_sent(result, "文件消息段")
        return 0

    @staticmethod
    def _log_sent(result: DownloadResult, how: str) -> None:
        """视频送达的唯一成功日志：一条说清画质、体积与送达方式。"""
        logger.info(
            "[BiliCard] 已发送视频 %s（%s / %.1fMB，%s）",
            result.filename,
            result.quality,
            result.size_mb,
            how,
        )

    async def _send_as_video(self, umo: str, result: DownloadResult) -> int:
        """作为视频消息发送。

        先把本地文件交给协议端（``Video.fromFileSystem`` 会生成 ``file:///`` 引用），
        协议端读不到时改用 AstrBot 文件服务的 HTTP 直链。跨机/跨容器部署时前者必然
        失败（NapCat 报 ENOENT），失败一次后本会话不再重试，直接走直链。
        """
        if not self._prefer_url_ref:
            try:
                await self.context.send_message(
                    umo,
                    MessageChain(
                        chain=[Comp.Video.fromFileSystem(os.path.abspath(result.path))]
                    ),
                )
                self._log_sent(result, "视频消息·本地文件")
                return 0
            except Exception as e:  # noqa: BLE001
                if is_send_timeout(e):
                    # 超时多半已经发出去了，改用直链重发只会让群里出现两条
                    logger.warning(
                        "[BiliCard] 发送视频超时（协议端可能已送达），不重发：%s", e
                    )
                    return 0
                self._prefer_url_ref = True
                logger.warning(
                    "[BiliCard] 协议端读不到本地视频（%s），改用 HTTP 直链，"
                    "后续不再尝试本地路径",
                    e,
                )

        url = await self._register_media_url(
            Comp.Video.fromFileSystem(os.path.abspath(result.path))
        )
        if not url:
            raise RuntimeError(
                "协议端读不到本地视频文件，且无法生成 HTTP 直链。请在 AstrBot 配置里"
                "填写 callback_api_base（协议端能访问到的 AstrBot 地址，如 "
                "http://astrbot:6185），或把插件数据目录挂给协议端容器共享"
            )
        await self.context.send_message(
            umo, MessageChain(chain=[Comp.Video.fromURL(url)])
        )
        self._log_sent(result, "视频消息·HTTP 直链")
        return _FILE_URL_GRACE_SECONDS

    async def _upload_via_onebot(
        self, umo: str, result: DownloadResult, event: Optional[AstrMessageEvent]
    ) -> int:
        """走 OneBot 的 upload_group_file / upload_private_file 上传群文件。

        仅 QQ(aiocqhttp) 适用。返回 -1 表示不适用或全部失败（调用方回退到通用文件
        消息段），否则返回删除前的等待秒数。
        """
        parts = (umo or "").split(":")
        if len(parts) < 3 or not parts[2].isdigit():
            return -1
        platform_id, msg_type, sid = parts[0], parts[1], parts[2]
        bot = self._bot_client(platform_id, event)
        if bot is None or not hasattr(bot, "call_action"):
            return -1
        is_group = "group" in msg_type.lower()
        action = "upload_group_file" if is_group else "upload_private_file"
        payload = {"group_id" if is_group else "user_id": int(sid)}

        # 先试本地绝对路径：AstrBot 与协议端同机（或共享挂载）时最快，且调用返回
        # 即表示协议端已取完文件，可以立刻删
        if not self._prefer_url_ref:
            try:
                await bot.call_action(
                    action,
                    **payload,
                    file=os.path.abspath(result.path),
                    name=result.filename,
                )
                self._log_sent(result, f"{action}·本地路径")
                return 0
            except Exception as e:  # noqa: BLE001
                if is_send_timeout(e):
                    logger.warning(
                        "[BiliCard] 上传文件超时（协议端可能已送达），不重发：%s", e
                    )
                    return 0
                self._prefer_url_ref = True
                logger.warning(
                    "[BiliCard] 协议端读不到本地文件（%s），改用 HTTP 直链，"
                    "后续不再尝试本地路径",
                    e,
                )

        # 协议端常与 AstrBot 分开部署（不同容器/Pod），读不到本地路径。若配了
        # callback_api_base，就把文件注册成 AstrBot 文件服务的 HTTP 直链再试。
        url = await self._register_media_url(
            Comp.File(name=result.filename, file=os.path.abspath(result.path))
        )
        if not url:
            return -1
        try:
            await bot.call_action(action, **payload, file=url, name=result.filename)
            self._log_sent(result, f"{action}·HTTP 直链")
            return _FILE_URL_GRACE_SECONDS
        except Exception as e:  # noqa: BLE001
            logger.warning("[BiliCard] OneBot 文件上传失败，回退文件消息段：%s", e)
            return -1

    async def _register_media_url(self, comp) -> str:
        """把本地文件注册成 AstrBot 文件服务直链（需全局配置 callback_api_base）。"""
        try:
            return await comp.register_to_file_service()
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "[BiliCard] 无法生成 HTTP 直链（AstrBot 未配置 callback_api_base？）：%s。"
                "若协议端与 AstrBot 不在同一容器，请配置它或挂载共享目录",
                e,
            )
            return ""

    def _bot_client(self, platform_id: str, event: Optional[AstrMessageEvent]):
        """取平台底层客户端（aiocqhttp 即 CQHttp 实例）。取不到返回 None。"""
        bot = getattr(event, "bot", None)
        if bot is not None:
            return bot
        try:
            inst = self.context.get_platform_inst(platform_id)
            get_client = getattr(inst, "get_client", None)
            return get_client() if callable(get_client) else None
        except Exception as e:  # noqa: BLE001
            logger.debug("[BiliCard] 获取平台客户端失败 %s: %s", platform_id, e)
            return None

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("下载视频", alias={"视频下载", "B站下载", "下载B站视频"})
    async def download_cmd(self, event: AstrMessageEvent):
        """/下载视频 [BV号/链接] —— 下载并上传视频文件（管理员）。

        不带参数时下载本会话最近一次解析过的视频。
        """
        token = parser.find_video_token(event.message_str or "")
        if token:
            bvid, aid = await self._resolve_token(token)
            if not bvid and not aid:
                # 用户明确指定了视频，解析失败就得报错，不能悄悄换成"最近那个"
                yield event.plain_result(
                    "这个链接解析不出视频，请确认是否有效（短链可能已失效）。"
                )
                return
        else:
            bvid, aid = self._last_video.get(event.unified_msg_origin, ""), None
            if not bvid:
                yield event.plain_result(
                    "没识别到视频。用法：/下载视频 BV号或链接；"
                    "本会话解析过视频后也可直接发 /下载视频 下最近那个。"
                )
                return

        info = await self.client.get_video_info(bvid=bvid, aid=aid)
        if not info or not info.get("bvid"):
            yield event.plain_result("获取视频信息失败，请确认链接有效。")
            return

        # 提示用 event.send 直接发，不用 yield：yield 会把控制权交回管线，一旦被
        # 其他插件 stop_event()，后面的下载逻辑就再也不会执行（同 on_message 注释）
        await event.send(
            MessageChain(
                chain=[
                    Comp.Plain(
                        f"正在下载「{info.get('title', '')}」，上限 "
                        f"{self._video_cfg('bili')['max_size_mb']}MB，请稍候…"
                    )
                ]
            )
        )
        result = await self._deliver_video(event.unified_msg_origin, info, event)
        if result.error:
            yield event.plain_result(f"❌ {result.error}")

    def _session_allowed(self, event: AstrMessageEvent) -> bool:
        """基于 UMO（unified_msg_origin）的会话访问控制，兼容纯群号。"""
        mode = self._c("access_mode", "all")
        if mode == "all":
            return True
        raw = self._c("session_list", []) or []
        if isinstance(raw, str):  # 兼容旧版逗号分隔字符串
            items = [x.strip() for x in raw.split(",") if x.strip()]
        else:
            items = [str(x).strip() for x in raw if str(x).strip()]
        umo = event.unified_msg_origin
        gid = str(event.get_group_id() or "")
        hit = umo in items or (gid != "" and gid in items)
        if mode == "whitelist":
            return hit
        if mode == "blacklist":
            return not hit
        return True

    # ------------------------------------------------------------------ #
    # 订阅管理指令
    # ------------------------------------------------------------------ #
    @staticmethod
    def _extract_uid(text: str) -> str:
        m = re.search(r"\d{3,}", text or "")
        return m.group(0) if m else ""

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("订阅UP")
    async def sub_cmd(self, event: AstrMessageEvent):
        """/订阅UP UID —— 订阅 UP主新投稿。"""
        uid = self._extract_uid(event.message_str)
        if not uid:
            yield event.plain_result("用法：/订阅UP UP主UID（例如 /订阅UP 486906719）")
            return
        async for r in self._do_subscribe(event, uid):
            yield r

    async def _do_subscribe(self, event: AstrMessageEvent, uid: str):
        """订阅 UP主核心逻辑（/订阅UP 指令与 LLM 工具共用）。"""
        info = await self.client.get_up_info(uid)
        # 以当前最新视频为基线，避免订阅瞬间把已有视频当作"新投稿"推送
        latest = await self.client.get_latest_videos(uid, 1)
        # UP信息与投稿都拿不到：区分"未登录"与"Cookie 失效/被风控"
        if not info and not latest:
            if not self.cookies.get("SESSDATA"):
                yield event.plain_result(
                    "订阅需要登录 B站：请先 /B站登录，或在插件配置填写 SESSDATA 后重载插件。"
                )
            else:
                yield event.plain_result(
                    "获取失败：B站接口被风控或 Cookie 已失效。请确认 SESSDATA 有效、"
                    "并在修改配置后重载了插件，稍后重试。"
                )
            return
        name = info["name"] if info else f"UID{uid}"
        last_bvid = latest[0]["bvid"] if latest else ""
        ok = self.store.add(event.unified_msg_origin, uid, name, last_bvid)
        if ok:
            yield event.plain_result(
                f"✅ 已订阅 UP主：{name}（UID {uid}）\n有新投稿会自动推送到这里。"
            )
        else:
            yield event.plain_result(f"该 UP主已在订阅列表中：{info['name']}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("取消订阅UP")
    async def unsub_cmd(self, event: AstrMessageEvent):
        """/取消订阅UP UID —— 取消订阅。"""
        uid = self._extract_uid(event.message_str)
        if not uid:
            yield event.plain_result("用法：/取消订阅UP UP主UID")
            return
        async for r in self._do_unsubscribe(event, uid):
            yield r

    async def _do_unsubscribe(self, event: AstrMessageEvent, uid: str):
        """取消订阅核心逻辑（/取消订阅UP 指令与 LLM 工具共用）。"""
        ok = self.store.remove(event.unified_msg_origin, uid)
        yield event.plain_result("✅ 已取消订阅" if ok else "未找到该订阅")

    @filter.command("订阅UP列表")
    async def sublist_cmd(self, event: AstrMessageEvent):
        """/订阅UP列表 —— 查看本会话订阅（所有人可用，只读）。"""
        subs = self.store.list(event.unified_msg_origin)
        if not subs:
            yield event.plain_result("当前没有订阅。用 /订阅UP UID 添加。")
            return
        lines = ["📋 当前订阅列表："]
        for s in subs:
            lines.append(f"· {s['name']}（UID {s['mid']}）")
        yield event.plain_result("\n".join(lines))

    # ------------------------------------------------------------------ #
    # 获取最新稿件（手动拉取，区别于订阅的自动推送）
    # ------------------------------------------------------------------ #
    async def _emit_video_card(
        self, event: AstrMessageEvent, bvid: str, with_summary: bool = False
    ):
        """渲染并发送某视频卡片（图片 + 链接）。

        with_summary=True 时附 AI 视频总结（需开启 AI 总结且视频有字幕，较慢，仅
        单个获取用；无字幕则自动省略）。失败仅记日志、不中断；以 event.chain_result
        yield 出去（0 或 1 条）。指令与 LLM 工具共用。
        """
        info = await self.client.get_video_info(bvid=bvid)
        if not info or not info.get("bvid"):
            return
        summary = await self._safe_summary(event, info) if with_summary else None
        img_url = ""
        try:
            img_url = await self._render_card(
                info, summary=summary, show_post_bar=False
            )
        except asyncio.TimeoutError:
            logger.warning("[BiliCard] 渲染超时，降级为文字 bvid=%s", bvid)
        except Exception as e:  # noqa: BLE001
            logger.warning("[BiliCard] 渲染失败，降级为文字 bvid=%s：%s", bvid, e)
        # 出图失败也要把链接送出去，不能让用户什么都收不到
        yield event.chain_result(self._bili_chain(info, img_url, summary))

    async def _do_latest_up(self, event: AstrMessageEvent, uid_text: str):
        """取某 UP主最新一稿并发卡片（/最新UP 指令与 LLM 工具共用）。"""
        uid = self._extract_uid(uid_text)
        if not uid:
            yield event.plain_result(
                "没识别到有效的 UP主 UID。用法：/最新UP UP主UID（如 /最新UP 486906719）"
            )
            return
        latest = await self.client.get_latest_videos(uid, 1)
        if not latest or not latest[0].get("bvid"):
            yield event.plain_result(
                f"没获取到 UID {uid} 的最新稿件（可能未登录/被风控，或该 UP主无投稿）。"
            )
            return
        sent = False
        async for r in self._emit_video_card(
            event, latest[0]["bvid"], with_summary=True
        ):
            sent = True
            yield r
        if not sent:
            yield event.plain_result(f"最新稿件渲染失败，请稍后再试（UID {uid}）。")

    async def _do_latest_subs(self, event: AstrMessageEvent):
        """拉取本会话所有订阅 UP主的最新稿件，逐个发卡片（指令与 LLM 工具共用）。"""
        subs = self.store.list(event.unified_msg_origin)
        targets = [
            s
            for s in subs
            if str(s.get("mid", "")).isdigit() and str(s.get("mid")) != "0"
        ]
        if not targets:
            yield event.plain_result("当前会话没有订阅。用 /订阅UP UID 添加。")
            return
        limit = 10  # 防刷屏 + 降风控：一次最多拉 10 位
        more = len(targets) > limit
        targets = targets[:limit]
        yield event.plain_result(
            f"正在拉取 {len(targets)} 位订阅 UP主的最新稿件，请稍候…"
            + ("（订阅较多，本次只取前 10 位）" if more else "")
        )
        sent = 0
        for s in targets:
            latest = await self.client.get_latest_videos(str(s["mid"]), 1)
            if latest and latest[0].get("bvid"):
                async for r in self._emit_video_card(event, latest[0]["bvid"]):
                    sent += 1
                    yield r
            await asyncio.sleep(1)  # 限速，降低风控风险
        if sent == 0:
            yield event.plain_result("没拉到任何最新稿件（可能未登录/被风控）。")

    @filter.command("最新UP", alias={"UP最新", "最新稿件"})
    async def latest_up_cmd(self, event: AstrMessageEvent):
        """/最新UP UID —— 获取某 UP主最新一稿并发卡片（所有人可用）。"""
        async for r in self._do_latest_up(event, event.message_str):
            yield r

    @filter.command("订阅最新", alias={"最新订阅", "订阅更新"})
    async def latest_subs_cmd(self, event: AstrMessageEvent):
        """/订阅最新 —— 拉取本会话所有订阅 UP主的最新稿件（所有人可用）。"""
        async for r in self._do_latest_subs(event):
            yield r

    # ------------------------------------------------------------------ #
    # B站登录（扫码 / Cookie 持久化）
    # ------------------------------------------------------------------ #
    def _save_cred(self) -> None:
        """把当前登录态（含 refresh_token / 登录会话）原子写回 credential.json。"""
        self.cred.save(
            self.cookies.get("SESSDATA", ""),
            self.cookies.get("bili_jct", ""),
            self._ac_time_value,
            self._login_umo,
        )

    def _apply_cookies(self, cookies: dict) -> None:
        self.cookies["SESSDATA"] = cookies.get("SESSDATA", "")
        self.cookies["bili_jct"] = cookies.get("bili_jct", "")
        # 新登录会带来新的 refresh_token（旧的已随旧会话作废）
        self._ac_time_value = cookies.get("refresh_token", "")
        self._relogin_notified = False  # 重新登录后重置提醒标志
        self.client = BiliClient(self.cookies)
        self._save_cred()
        logger.info(
            "[BiliCard] 登录凭证已保存，自动续期%s",
            "已开启" if self._ac_time_value else "未开启（未取到 refresh_token）",
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("B站登录")
    async def login_cmd(self, event: AstrMessageEvent):
        """/B站登录 —— 扫码登录 B站。"""
        res = await login.generate_qrcode()
        if not res:
            yield event.plain_result("二维码申请失败，请稍后重试。")
            return
        qrcode_key, url = res
        qr_path = os.path.join(self._data_dir, "qrcode_login.png")
        if not login.make_qr_image(url, qr_path):
            yield event.plain_result(
                "生成二维码图片失败，请确认已安装依赖：pip install qrcode[pil]"
            )
            return
        yield event.image_result(qr_path)
        yield event.plain_result("请用 B站手机 APP 扫码并确认登录（约 2 分钟内有效）。")
        asyncio.create_task(self._poll_login(event.unified_msg_origin, qrcode_key))

    async def _poll_login(self, umo: str, qrcode_key: str):
        for _ in range(60):  # 最多约 2 分钟
            await asyncio.sleep(2)
            code, cookies = await login.poll_qrcode(qrcode_key)
            if code == login.CODE_SUCCESS and cookies:
                self._login_umo = umo  # 记录登录会话，供续期失效时回此提醒
                self._apply_cookies(cookies)
                got_rt = bool(cookies.get("refresh_token"))
                await self.context.send_message(
                    umo,
                    MessageChain().message(
                        "✅ B站登录成功！订阅与 AI 总结功能已可用。"
                        + ("（已开启登录自动续期）" if got_rt else "")
                    ),
                )
                return
            if code == login.CODE_EXPIRED:
                await self.context.send_message(
                    umo, MessageChain().message("⚠️ 二维码已失效，请重新发送 /B站登录。")
                )
                return
        await self.context.send_message(
            umo, MessageChain().message("⚠️ 登录超时，请重新发送 /B站登录。")
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("B站登出")
    async def logout_cmd(self, event: AstrMessageEvent):
        """/B站登出 —— 清除已保存的 B站登录信息。"""
        self.cookies["SESSDATA"] = ""
        self.cookies["bili_jct"] = ""
        self._ac_time_value = ""
        self._login_umo = ""
        self.client = BiliClient(self.cookies)
        self._save_cred()
        yield event.plain_result("已登出 B站，已清除登录信息。")

    # ------------------------------------------------------------------ #
    # 订阅定时轮询与推送
    # ------------------------------------------------------------------ #
    async def initialize(self):
        # 上次运行若被强杀（容器重启/OOM），下载目录可能留有孤儿文件，先扫掉
        self.downloader.sweep(max_age_minutes=0)
        self._precheck_video_delivery()
        self._poll_task = asyncio.create_task(self._poll_loop())

    def _precheck_video_delivery(self) -> None:
        """视频送达前置条件自检，只预警不阻断。

        视频先以本地文件交给协议端；协议端与 AstrBot 分开部署（不同容器/Pod）时
        读不到，只能改用 AstrBot 文件服务的 HTTP 直链，而那需要全局配置
        callback_api_base。没有这条预警，用户要等视频下载几分钟跑完才发现发不出去。
        """
        if not (
            self._video_cfg("bili")["enabled"]
            or self._video_cfg("bili")["on_subscribe_push"]
            or self._video_cfg("douyin")["enabled"]
        ):
            return
        base, port = "", 6185
        try:
            conf = self.context.get_config()
            base = str(conf.get("callback_api_base", "") or "").strip()
            port = (conf.get("dashboard") or {}).get("port") or 6185
        except Exception as e:  # noqa: BLE001
            logger.debug("[BiliCard] 读取全局配置失败: %s", e)
        if base:
            logger.info(
                "[BiliCard] 视频送达就绪：优先本地文件，协议端读不到时回退直链 %s", base
            )
            return
        logger.warning(
            "[BiliCard] 已开启视频下载，但 AstrBot 未配置 callback_api_base。若协议端"
            "（NapCat 等）与 AstrBot 不在同一容器，它读不到本地视频文件，发送会失败"
            "（报 ENOENT）。解决其一：① AstrBot 配置里把 callback_api_base 填成协议端"
            "能访问到的 AstrBot 地址，如 http://astrbot:%s（填到端口即可，不要带 /api）；"
            "② 把 %s 挂载给协议端容器，保持路径一致。",
            port,
            self.downloader.dir,
        )

    async def _poll_loop(self):
        await asyncio.sleep(15)  # 启动后稍等，避免与初始化抢占
        while True:
            try:
                await self._maybe_refresh_credential()  # 登录态自动续期（每日最多一次）
                # 常规清理兜底：正常路径下文件发完即删，这里扫的是异常残留
                self.downloader.sweep(max_age_minutes=60)
                if self._c("enable_subscribe_push", True):
                    await self._check_subscriptions()
            except asyncio.CancelledError:
                break
            except Exception as e:  # noqa: BLE001
                logger.error(f"[BiliCard] 订阅轮询出错: {e}")
            interval = int(self._c("check_interval_minutes", 10) or 10)
            await asyncio.sleep(max(interval, 1) * 60)

    async def _maybe_refresh_credential(self) -> None:
        """每天最多一次：检查并自动续期 B站登录 Cookie（需有 refresh_token）。

        借 B站官方刷新机制（bilibili-api 实现），让登录态长期有效、免反复扫码。
        注意：只能给「仍有效」的会话续期，已彻底失效的救不回（需重新 /B站登录）。
        """
        if not self.cookies.get("SESSDATA") or not self._ac_time_value:
            return  # 未登录或没有 refresh_token（没开自动续期）→ 跳过
        now = time.time()
        if now - self._last_refresh_ts < 23 * 3600:
            return
        self._last_refresh_ts = now
        try:
            res = await self.client.refresh_cookies(self._ac_time_value)
        except Exception as e:  # noqa: BLE001
            logger.warning("[BiliCard] 自动续期异常: %s", e)
            return
        status = res.get("status")
        if status == "refreshed":
            c = res.get("cookies") or {}
            if c.get("SESSDATA"):
                self.cookies["SESSDATA"] = c["SESSDATA"]
                self.cookies["bili_jct"] = c.get("bili_jct", "")
                self._ac_time_value = c.get("ac_time_value") or self._ac_time_value
                self.client = BiliClient(self.cookies)
                self._save_cred()
                logger.info("[BiliCard] B站登录 Cookie 已自动续期")
            self._relogin_notified = False
        elif status == "noop":
            self._relogin_notified = False  # 登录态健康，重置提醒标志
        elif status == "expired":
            logger.warning("[BiliCard] B站登录态已失效，自动续期失败，请重新 /B站登录")
            if not self._relogin_notified:  # 每个失效周期只提醒一次，不每天打扰
                self._relogin_notified = True
                await self._notify_relogin()
        # status == "error"/"unavailable"：暂态或未配置，不动作

    async def _notify_relogin(self) -> None:
        """续期失败时，回最近登录会话提醒管理员重新登录（无记录则仅日志）。"""
        if not self._login_umo:
            return
        try:
            await self.context.send_message(
                self._login_umo,
                MessageChain().message(
                    "⚠️ BiliCard：B站登录态已失效、自动续期失败。"
                    "请重新发送 /B站登录 扫码登录，以恢复订阅推送与 AI 总结。"
                ),
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("[BiliCard] 提醒重新登录失败: %s", e)

    async def _check_subscriptions(self):
        # 同一 mid 本轮只查一次，减少接口请求
        cache: Dict[str, list] = {}
        for umo, sub in list(self.store.iter_items()):
            mid = str(sub["mid"])
            if not mid.isdigit() or mid == "0":
                continue  # 跳过示例项 / 非法 UID
            if mid in cache:
                latest = cache[mid]
            else:
                latest = await self.client.get_latest_videos(mid, 1)
                cache[mid] = latest
                await asyncio.sleep(1)
            if not latest or not latest[0].get("bvid"):
                continue
            newest_bvid = latest[0]["bvid"]
            if newest_bvid == sub.get("last_bvid", ""):
                continue
            # 先更新基线，避免渲染/推送异常导致刷屏
            had_baseline = bool(sub.get("last_bvid"))
            self.store.update_last_bvid(umo, mid, newest_bvid)
            if not had_baseline:
                continue  # 首次仅记录基线，不推送
            await self._push_new_video(umo, newest_bvid)

    async def _push_new_video(self, umo: str, bvid: str):
        info = await self.client.get_video_info(bvid=bvid)
        if not info:
            return
        self._last_video[umo] = bvid
        # 同 on_message：视频下载不依赖卡片渲染，渲染超时/失败也要能把视频推出去
        card_ready = asyncio.Event()
        if self._video_cfg("bili")["on_subscribe_push"]:
            self._schedule_video_upload(umo, info, after=card_ready)
        img_url = ""
        try:
            img_url = await self._render_card(info, summary=None, show_post_bar=True)
        except asyncio.TimeoutError:
            logger.warning(
                "[BiliCard] 推送渲染超时（>%ss），降级为文字 bvid=%s",
                self.cfg.int("render_timeout"),
                bvid,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("[BiliCard] 推送渲染失败，降级为文字 bvid=%s：%s", bvid, e)
        try:
            # 出图失败也照发链接，订阅推送更不能因为渲染服务抽风就静默丢掉
            await self.context.send_message(
                umo, MessageChain(chain=self._bili_chain(info, img_url))
            )
            logger.info("[BiliCard] 已推送新投稿 %s 到 %s", bvid, umo)
        except Exception as e:  # noqa: BLE001
            logger.error("[BiliCard] 推送发送失败 bvid=%s: %s", bvid, e, exc_info=True)
        finally:
            # 这条链路不走 yield，卡片确实发完了才放行视频
            card_ready.set()

    async def terminate(self):
        task = getattr(self, "_poll_task", None)
        if task:
            task.cancel()
        # 在途下载全部取消：downloader 会在取消路径上清掉自己的中间文件
        tasks = list(self._upload_tasks)
        for t in tasks:
            t.cancel()
        if tasks:
            try:
                await asyncio.wait(tasks, timeout=10)
            except Exception as e:  # noqa: BLE001
                logger.debug("[BiliCard] 等待下载任务收尾异常: %s", e)
        self.downloader.sweep(max_age_minutes=0)  # 卸载/重载时不留视频文件
