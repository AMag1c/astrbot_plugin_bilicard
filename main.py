"""BiliCard 插件主入口。

监听所有消息，自动识别 B站视频（BV / av / 链接 / b23 短链 / QQ 小程序卡片），
抓取视频信息、实时在线人数、热门评论、AI 字幕总结，渲染成卡片图片回复。
无需 @机器人、无需唤醒词、无需命令前缀。
"""

import asyncio
import json
import os
import re
import time
from typing import Dict, Optional

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools, register

from .bilicard import login, parser, render, summarizer
from .bilicard.client import BiliClient
from .bilicard.config import Config
from .bilicard.credential import CredentialStore
from .bilicard.data_manager import SubscriptionStore


def _is_http_url(s) -> bool:
    """html_render 返回值是否为可直接发图的 http(s) URL。"""
    return isinstance(s, str) and s.startswith(("http://", "https://"))


# 元数据以 metadata.yaml 为唯一来源；此处保持与其一致，避免两份漂移。
@register(
    "astrbot_plugin_bilicard",
    "AMag1c",
    "自动识别群聊/私聊中的 B站视频链接、BV号、b23 短链，渲染成精美信息卡片"
    "（封面、UP主、播放/弹幕/点赞等统计、实时在线人数、热门评论、AI视频总结），并附上视频链接。",
    "v0.2.1",
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
        self.client = BiliClient(self.cookies)
        self._tmpl = render.load_template()
        # 冷却记录： "{umo}:{bvid}" -> 上次解析时间戳
        self._cooldown: Dict[str, float] = {}

        try:
            data_dir = StarTools.get_data_dir("astrbot_plugin_bilicard")
        except Exception:  # noqa: BLE001
            data_dir = os.path.dirname(os.path.abspath(__file__))
        self._data_dir = str(data_dir)
        self.cred = CredentialStore(self._data_dir)
        self.store = SubscriptionStore(self.cfg)

        # 加载扫码登录持久化的 Cookie（优先于手填配置）
        saved = self.cred.load()
        if saved.get("SESSDATA"):
            self.cookies["SESSDATA"] = saved["SESSDATA"]
            self.cookies["bili_jct"] = saved.get("bili_jct", "")
            self.client = BiliClient(self.cookies)

    # ------------------------------------------------------------------ #
    # 配置便捷读取
    # ------------------------------------------------------------------ #
    def _c(self, key, default=None):
        return self.cfg.get(key, default)

    # ------------------------------------------------------------------ #
    # 全量消息监听：自动识别并解析
    # ------------------------------------------------------------------ #
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        is_private = event.is_private_chat()
        mode = self._c("trigger_mode", "all")
        if mode == "group_only" and is_private:
            return
        if mode == "private_only" and not is_private:
            return

        text = event.message_str or ""
        if text.strip().startswith("/"):
            return

        # 会话黑/白名单（基于 UMO，兼容群号）
        if not self._session_allowed(event):
            return

        token = parser.find_video_token(self._collect_candidate_text(event, text))
        if not token:
            return

        # 归一化为 bvid / aid
        bvid, aid = await self._resolve_token(token)
        if not bvid and not aid:
            return

        info = await self.client.get_video_info(bvid=bvid, aid=aid)
        if not info or not info.get("bvid"):
            return

        # 冷却防刷屏
        if not self._check_cooldown(event.unified_msg_origin, info["bvid"]):
            return

        # 阻止这条消息继续触发 LLM 闲聊
        event.stop_event()

        logger.info(
            "[BiliCard] 识别到视频 %s「%s」，开始处理",
            info["bvid"],
            info.get("title", ""),
        )
        try:
            summary = None
            if self._c("enable_ai_summary", True):
                summary = await self._build_summary(event, info)
            img_url = await self._render_card(
                info, summary=summary, show_post_bar=False
            )
        except asyncio.TimeoutError:
            logger.warning(
                "[BiliCard] 渲染超时（>%ss），跳过 bvid=%s（远程 t2i 服务慢/过载）",
                self.cfg.int("render_timeout"),
                info.get("bvid"),
            )
            return
        except Exception as e:  # noqa: BLE001
            logger.warning("[BiliCard] 渲染失败，跳过 bvid=%s：%s", info.get("bvid"), e)
            return

        if not _is_http_url(img_url):
            logger.warning(
                "[BiliCard] 渲染图片 URL 无效，跳过 bvid=%s：url=%r",
                info.get("bvid"),
                img_url,
            )
            return

        logger.info("[BiliCard] 卡片渲染成功 bvid=%s，准备发送", info["bvid"])
        try:
            # 图片 + 链接合并为一条消息，避免多次 yield 被其他插件中断传播
            chain = [Comp.Image.fromURL(img_url)]
            if self._c("show_link", True):
                chain.append(
                    Comp.Plain(f"\nhttps://www.bilibili.com/video/{info['bvid']}")
                )
            yield event.chain_result(chain)
            logger.info("[BiliCard] 已发送视频卡片 bvid=%s", info["bvid"])
        except Exception as e:  # noqa: BLE001
            logger.error(
                "[BiliCard] 发送失败 bvid=%s: %s", info.get("bvid"), e, exc_info=True
            )

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

    async def _render_card(
        self, info: dict, summary: Optional[str], show_post_bar: bool
    ) -> str:
        """渲染卡片图片。summary 与 show_post_bar 区分两种模板：
        - 链接总结：show_post_bar=False，summary 有值
        - 订阅推送：show_post_bar=True，summary=None
        """
        logger.info("[BiliCard] 开始渲染卡片 bvid=%s", info.get("bvid", ""))
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

    async def _build_summary(
        self, event: AstrMessageEvent, info: dict
    ) -> Optional[str]:
        text = await self.client.get_subtitle_text(
            info["bvid"], info["cid"], int(self._c("summary_max_subtitle", 4000))
        )
        if not text:
            logger.info("[BiliCard] 视频无字幕，跳过 AI 总结 bvid=%s", info["bvid"])
            return None

        provider_id = self._c("llm_provider_id", "")
        if provider_id:
            provider = self.context.get_provider_by_id(provider_id)
        else:
            provider = self.context.get_using_provider(umo=event.unified_msg_origin)
        if not provider:
            logger.warning("[BiliCard] 未找到可用 LLM Provider，跳过 AI 总结")
            return None

        logger.info("[BiliCard] 提取字幕成功，调用 LLM 生成总结 bvid=%s", info["bvid"])

        async def llm_ask(prompt: str) -> str:
            resp = await provider.text_chat(prompt=prompt, session_id="bilicard")
            return getattr(resp, "completion_text", "") or ""

        result = await summarizer.summarize(
            info["title"],
            text,
            llm_ask,
            max_chars=int(self._c("summary_max_chars", 120)),
        )
        logger.info("[BiliCard] AI 总结完成 bvid=%s", info["bvid"])
        return result

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
    # B站登录（扫码 / Cookie 持久化）
    # ------------------------------------------------------------------ #
    def _apply_cookies(self, cookies: dict) -> None:
        self.cookies["SESSDATA"] = cookies.get("SESSDATA", "")
        self.cookies["bili_jct"] = cookies.get("bili_jct", "")
        self.client = BiliClient(self.cookies)
        self.cred.save(self.cookies["SESSDATA"], self.cookies["bili_jct"])

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
                self._apply_cookies(cookies)
                await self.context.send_message(
                    umo,
                    MessageChain().message(
                        "✅ B站登录成功！订阅与 AI 总结功能已可用。"
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
        self.client = BiliClient(self.cookies)
        self.cred.save("", "")
        yield event.plain_result("已登出 B站，已清除登录信息。")

    # ------------------------------------------------------------------ #
    # 订阅定时轮询与推送
    # ------------------------------------------------------------------ #
    async def initialize(self):
        self._poll_task = asyncio.create_task(self._poll_loop())

    async def _poll_loop(self):
        await asyncio.sleep(15)  # 启动后稍等，避免与初始化抢占
        while True:
            try:
                if self._c("enable_subscribe_push", True):
                    await self._check_subscriptions()
            except asyncio.CancelledError:
                break
            except Exception as e:  # noqa: BLE001
                logger.error(f"[BiliCard] 订阅轮询出错: {e}")
            interval = int(self._c("check_interval_minutes", 10) or 10)
            await asyncio.sleep(max(interval, 1) * 60)

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
        try:
            img_url = await self._render_card(info, summary=None, show_post_bar=True)
        except asyncio.TimeoutError:
            logger.warning(
                "[BiliCard] 推送渲染超时（>%ss），跳过 bvid=%s",
                self.cfg.int("render_timeout"),
                bvid,
            )
            return
        except Exception as e:  # noqa: BLE001
            logger.warning("[BiliCard] 推送渲染失败，跳过 bvid=%s：%s", bvid, e)
            return
        if not _is_http_url(img_url):
            logger.warning(
                "[BiliCard] 推送图片 URL 无效，跳过 bvid=%s：url=%r", bvid, img_url
            )
            return
        try:
            chain = MessageChain(chain=[Comp.Image.fromURL(img_url)])
            if self._c("show_link", True):
                chain.chain.append(
                    Comp.Plain(f"\nhttps://www.bilibili.com/video/{info['bvid']}")
                )
            await self.context.send_message(umo, chain)
            logger.info("[BiliCard] 已推送新投稿 %s 到 %s", bvid, umo)
        except Exception as e:  # noqa: BLE001
            logger.error("[BiliCard] 推送发送失败 bvid=%s: %s", bvid, e, exc_info=True)

    async def terminate(self):
        task = getattr(self, "_poll_task", None)
        if task:
            task.cancel()
