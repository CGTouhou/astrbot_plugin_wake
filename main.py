"""叫醒小鲸鱼 —— 关键词唤醒 + 窗口内小模型判断。

设计原则：
- 低耦合：只写 event.is_at_or_wake_command 一个标志，不碰 prompt / contexts / request_llm。
- 高内聚：配置、窗口、判断、提示词各自独立成文件。
- 强扩展：换提示词改 prompts.py，换判断逻辑改 judge.py，窗口策略改 state.py。

两个钩子：
  on_group_message   → 决定「要不要让她开口」
  on_llm_response    → 她真的生成了回复 → 开窗/续窗
"""

from __future__ import annotations

import asyncio
import random
import re
import time
from collections import deque

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, Image, Plain
from astrbot.api.provider import LLMResponse
from astrbot.api.star import Context, Star
from astrbot.core.agent.message import TextPart
from astrbot.core.platform.message_type import MessageType

from .config import WakeConfig
from .judge import judge as run_judge
from .state import WindowManager

# qq_tools 之类的插件会往 event.message_str 尾部追加「 [MSG_ID:123]」，
# 那是给它自己的工具用的。留着会污染判断输入（小模型看到一串数字 ID），
# 也会被合并进 merged 再写回消息。这里统一剥掉。
_MSG_ID_RE = re.compile(r"\s*\[MSG_ID:\d+\]\s*")


def _strip_msg_id(text: str) -> str:
    """剥掉消息尾部的 [MSG_ID:xxx] 标记。"""
    return _MSG_ID_RE.sub("", text or "").strip()

MAX_HISTORY = 20          # 每个群保留多少条历史给判断用
HISTORY_FOR_PROMPT = 8    # 实际塞进 prompt 的条数
PENDING_IMAGE_TTL = 300   # 缓存的图超过这么久没人取就丢掉（秒）
HISTORY_MERGE_WINDOW = 60 # 同一人连续发言超过这么久就不再合并（秒）


class WakePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.window = WindowManager()
        self._history: dict[str, deque[tuple[str, str, float]]] = {}
        # 防抖：umo:sender → 判断任务 token（只有这个人最新的那个能通过）。
        # key 里带 sender 是必须的：只用 umo 时，A、B 在 1.5 秒内先后叫她，
        # B 的 token 会把 A 的顶掉，A 那条直接 return，不唤醒也不回复。
        self._latest_token: dict[str, int] = {}
        # 防抖：umo → (发送者ID, 该发送者最后一条消息)。醒来判断时优先用它
        self._latest_event: dict[str, tuple[str, AstrMessageEvent]] = {}
        # 私聊防抖：umo → 本次窗口内收到的文本，最后一条醒来时合并成一句
        self._private_pending: dict[str, list[str]] = {}
        # 防抖 token 的自增序号。不能用 time.monotonic()：Windows 上精度约
        # 15.6ms，连发的消息会取到同一个值，导致先到的反而抢到放行权。
        self._debounce_seq: int = 0
        # 防抖轮次的起始时刻：token_key → monotonic 时间。用于「最大等待」上限，
        # 从这一轮第一条算起，防止一直被新消息刷新而永远不判断。
        self._debounce_start: dict[str, float] = {}
        # 图片：umo → 待消费的本地图片路径（图/文字分开发时用）
        self._pending_images: dict[str, list[str]] = {}
        # 图片缓存的时间戳，超过 PENDING_IMAGE_TTL 秒没人来取就丢掉，防止长期占内存
        self._pending_images_ts: dict[str, float] = {}
        logger.info(
            "[wake] 叫醒小鲸鱼 v1.1.8 已加载（history_count=%s, 合并连续消息=开）",
            self._cfg().history_count,
        )

    # ================= 配置 =================

    def _cfg(self) -> WakeConfig:
        try:
            raw = dict(self.config)
        except Exception:
            raw = {}
        return WakeConfig.from_raw(raw)

    # ================= 钩子 1：收到群消息 =================

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=900)
    async def on_group_message(self, event: AstrMessageEvent):
        cfg = self._cfg()
        if not cfg.enabled:
            return

        umo = event.unified_msg_origin
        group_id = event.get_group_id()
        if not cfg.group_allowed(group_id):
            return

        # 顺手清掉过期没人取的图片缓存
        self._gc_pending_images()

        # 先记历史（不论后面判断结果如何，聊天记录都要留着）
        self._remember(umo, event)

        # 图片消息：只把图缓存下来，不触发判断（等用户叫她的那条消息带描述）
        if await self._capture_images(umo, event, cfg):
            return

        if self._should_skip(event, cfg):
            return

        # 记下「这个人最后一条消息」，防抖醒来时优先用它判断（能接上后半句）
        self._latest_event[umo] = (self._sender_id(event), event)

        # ① 被 @ 了 → 直接唤醒，不用小模型
        if event.is_at_or_wake_command:
            self._log(cfg, umo, "mention", "被@，直接唤醒")
            self._wake(event, "mention")
            return

        # ② 窗口内 → 每条都问小模型（走防抖：等对方把话打完）
        if self.window.is_active(umo):
            await self._debounced_judge(
                event, cfg, umo, "window", [], cfg.debounce_for("window")
            )
            return

        # ③ 窗口外 → 关键词命中才问
        keywords = self._effective_keywords(cfg, event)
        hits = self._hit_keywords(event.message_str, keywords)
        if not hits:
            return

        self._log(cfg, umo, "keyword", f"命中关键词 {hits}")
        await self._debounced_judge(
            event, cfg, umo, "keyword", hits, cfg.debounce_for("keyword")
        )


    # ================= 钩子 1b：收到私聊消息 =================

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE, priority=900)
    async def on_private_message(self, event: AstrMessageEvent):
        """私聊防抖：等对方把话说完，再合并成一句交给她。

        群里由 wake 判断「要不要醒」，私聊不需要判断 —— AstrBot 默认私聊
        全部唤醒（waking_check 里 is_private_chat 分支）。这里只解决「连发
        三句被当成三轮对话、回三次」：第一条之后每来一条就重新计时，只有
        最后一条放行，前面的 stop_event 掉（既不进 LLM 也不发消息），并把
        它们的文字合并进最后这条。
        """
        cfg = self._cfg()
        if not cfg.enabled:
            return
        delay = cfg.debounce_for("private")
        if delay <= 0:
            # 开关刚关掉时，之前攒下的文本没人会来取了，顺手清掉
            self._private_pending.pop(event.unified_msg_origin, None)
            self._debounce_start.pop(event.unified_msg_origin, None)
            return

        umo = event.unified_msg_origin
        text = _strip_msg_id((event.message_str or "").strip())
        if text.startswith("/"):
            # 命令直接放行，别让 /wake 这类操作白等 1.5 秒
            return

        if text:
            self._private_pending.setdefault(umo, []).append(text)

        self._debounce_seq += 1
        token = self._debounce_seq
        self._latest_token[umo] = token
        delay = self._limit_wait(cfg, umo, "private", delay)
        if delay > 0:
            await asyncio.sleep(delay)
        if self._latest_token.get(umo) != token:
            # 后面还有新消息，让它代表这一段；自己既不进 LLM 也不发消息
            event.stop_event()
            return
        self._debounce_start.pop(umo, None)

        parts = self._private_pending.pop(umo, [])
        if not parts:
            return
        merged = "".join(parts).strip()
        if merged and merged != _strip_msg_id(event.message_str or ""):
            event.message_str = merged
            self._log(cfg, umo, "private", f"合并连续消息为一句：{merged}")

    # ================= 钩子 2：她生成了回复 → 开窗 =================

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, resp: LLMResponse):
        try:
            if event.get_message_type() != MessageType.GROUP_MESSAGE:
                return
            if event.get_extra("handlers_parsed_params"):
                return  # 指令回复不开窗

            cfg = self._cfg()
            if not cfg.enabled:
                return

            umo = event.unified_msg_origin
            if not cfg.group_allowed(event.get_group_id()):
                return

            # 把她的回复也记进历史（判断「是不是在接她的话」需要）
            text = (getattr(resp, "completion_text", "") or "").strip()
            if text:
                self._append(umo, "你", text[:200])

            self.window.touch(umo, cfg.window_seconds, source="bot_replied")
            if cfg.debug_log:
                logger.info(
                    f"[wake] {umo} 开窗/续窗 {cfg.window_seconds}s "
                    f"剩余 {self.window.remaining(umo)}s"
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[wake] on_llm_response 出错: {exc}")

    @filter.on_llm_request(priority=200)
    async def on_llm_request_inject_images(self, event: AstrMessageEvent, req) -> None:
        """把缓存下来的图片转成文字，塞进这轮请求。

        场景：用户先发一张图（单独一条消息），隔几秒再发文字叫她。
        图那条消息的 message_str 是空的，进不了 LLM 请求；文字那条又没有图。
        所以这里把图交给视觉模型描述，再以额外内容块的形式追加进去。
        """
        cfg = self._cfg()
        if not cfg.enabled or not cfg.enable_image:
            return
        try:
            if event.get_message_type() != MessageType.GROUP_MESSAGE:
                return
        except Exception:  # noqa: BLE001
            return

        umo = event.unified_msg_origin
        pending = self._pending_images.pop(umo, [])
        if not pending:
            return

        try:
            descs = []
            for path in pending[:3]:
                desc = await self._describe_image(cfg, path)
                if desc:
                    descs.append(desc)
            if not descs:
                self._log(cfg, umo, "image", f"{len(pending)} 张图转述失败")
                return
            text = "\n".join(f"[图片内容] {d}" for d in descs)
            req.extra_user_content_parts.append(TextPart(text=text))
            self._log(cfg, umo, "image", f"已注入 {len(descs)} 张图片描述")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[wake] 图片注入失败: {exc}")

    # ================= 判断 & 唤醒 =================

    async def _judge_and_maybe_wake(
        self,
        event: AstrMessageEvent,
        cfg: WakeConfig,
        umo: str,
        *,
        scenario: str,
        hit_keywords: list[str] | None = None,
        wake_event: AstrMessageEvent | None = None,
    ):
        provider = await self._get_provider(cfg, umo)
        if provider is None:
            self._log(cfg, umo, scenario, "没有可用 provider，跳过")
            return

        self_name, persona_line = self._self_identity(event, umo, cfg)
        sender_name = event.get_sender_name() or "群友"
        history, message = self._split_history_tail(
            umo, sender_name, event, cfg.history_count
        )
        if message != _strip_msg_id(event.message_str):
            self._log(cfg, umo, scenario, f"合并连续消息为一句：{message[:60]}")

        result = await run_judge(
            provider=provider,
            scenario=scenario,
            self_name=self_name,
            persona_line=persona_line,
            history=history,
            sender_name=sender_name,
            message=message,
            hit_keywords=hit_keywords or [],
            custom_prompt=(
                cfg.prompt_keyword if scenario == "keyword" else cfg.prompt_window
            ),
            timeout=cfg.judge_timeout,
            fail_open=cfg.fail_open,
        )

        self._log(cfg, umo, scenario, f"小模型: {result.action} ({result.reason})")

        if not result.should_reply:
            return

        if random.random() >= cfg.reply_probability:
            self._log(cfg, umo, scenario, f"判断要回，但概率 {cfg.reply_probability} 未命中")
            return

        # 防抖可能把判断对象换成了更新的那条消息，而那条消息的 pipeline
        # 早已走完 —— 事后置 is_at_or_wake_command 不会生效。真正还在等
        # 的是发起本次防抖判断的那条 event，唤醒必须打在它身上。
        self._wake(wake_event or event, scenario, message)

    def _wake(self, event: AstrMessageEvent, source: str, merged: str = ""):
        event.is_at_or_wake_command = True
        event.set_extra("wake_source", source)
        # 把「合并后的完整一句」写回这条消息。
        # 唤醒只标记当前这一条，被合并进来的第 2~N 条不会进 pipeline，
        # 不写回的话它们的内容在小模型判断时看到了、回复时却看不到。
        if merged and merged != _strip_msg_id(event.message_str):
            event.message_str = merged
            event.set_extra("wake_merged", merged)
        logger.info(
            "[wake] 已唤醒 source=%s umo=%s stopped=%s at_or_wake=%s "
            "send_oper=%s call_llm=%s has_result=%s msg=%r",
            source,
            event.unified_msg_origin,
            event.is_stopped(),
            event.is_at_or_wake_command,
            getattr(event, "_has_send_oper", "?"),
            getattr(event, "call_llm", "?"),
            bool(event.get_result()),
            (event.message_str or "")[:40],
        )

    # ================= 防抖 =================

    def _limit_wait(self, cfg: WakeConfig, key: str, scenario: str, delay: float) -> float:
        """把本次要等的时间裁到「最大等待」的剩余额度内。

        防抖是「等到没人说话」，但如果对方一直在说，这个条件永远不成立，
        窗口就被无限刷新，她永远等不到判断。所以从这一轮第一条消息开始记
        一个起点，最多等 max_wait 秒；到点返回 0（不再等），立刻判断一次。
        """
        max_wait = cfg.max_wait_for(scenario) if cfg else 0.0
        if max_wait <= 0:
            return delay
        now = time.monotonic()
        start = self._debounce_start.get(key)
        # 起点超过一整轮还没被消费，说明上一轮是异常收尾（判断抛错、
        # handler 被取消之类，没走到放行那句的清理）。这种情况直接重置，
        # 否则这次会算出负数额度、立刻判断，防抖等于没生效。
        if start is None or now - start > max_wait * 2:
            start = now
            self._debounce_start[key] = start
        remain = max_wait - (now - start)
        if remain <= 0:
            return 0.0
        return min(delay, remain)

    async def _debounced_judge(
        self,
        event: AstrMessageEvent,
        cfg: WakeConfig,
        umo: str,
        scenario: str,
        hit_keywords: list[str],
        delay: float,
    ):
        """等对方把话说完再判断。

        做法：每条消息都在自己的 handler 里睡 `delay` 秒，睡醒后检查
        「我是不是这个人最新的那个判断任务」——
        是就判断，不是就让更新的那个接管（说明对方还在敲）。

        判断时优先用「同一个人的最后一条消息」：
          「大肥鱼」→ 睡 → 「你在干嘛呢」→ 醒来时用后一句去判断，
        这样接的是完整的一句话，而不是只看到「大肥鱼」三个字。
        """
        if delay > 0:
            # 按「会话 + 发送者」独立防抖：只合并同一个人连发的话，
            # 别人同时开口不能把这条挤掉。
            token_key = f"{umo}:{self._sender_id(event)}"
            self._debounce_seq += 1
            token = self._debounce_seq
            self._latest_token[token_key] = token
            delay = self._limit_wait(cfg, token_key, scenario, delay)
            if delay > 0:
                await asyncio.sleep(delay)
            if self._latest_token.get(token_key) != token:
                self._log(cfg, umo, scenario, "防抖：有更新的判断任务接管，本条放弃")
                return
            self._debounce_start.pop(token_key, None)

        target = event
        latest = self._latest_event.get(umo)
        if latest is not None:
            sender_id, latest_event = latest
            # 只有「同一个人继续补充」才把判断对象换成最新那条。
            # 换人了必须仍用触发的那条：否则别人的话会被当成新消息去判断，
            # 合并出来的是别人的发言，最后还会把别人的话写回自己这条的
            # message_str（窗口内别人 @ 她时就会踩到）。
            if sender_id == self._sender_id(event):
                target = latest_event
        if target is not event:
            self._log(cfg, umo, scenario, "防抖：改用最新的那条消息判断")

        await self._judge_and_maybe_wake(
            target,
            cfg,
            umo,
            scenario=scenario,
            hit_keywords=hit_keywords,
            wake_event=event,
        )

    @staticmethod
    def _sender_id(event: AstrMessageEvent) -> str:
        try:
            return str(event.get_sender_id() or "")
        except Exception:  # noqa: BLE001
            return ""

    async def _get_provider(self, cfg: WakeConfig, umo: str):
        try:
            if cfg.llm_provider:
                provider = self.context.get_provider_by_id(cfg.llm_provider)
                if provider is not None:
                    return provider
            try:
                return await self.context.get_using_provider_async(umo)
            except TypeError:
                return await self.context.get_using_provider_async()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[wake] 取 provider 失败: {exc}")
            return None

    # ================= 过滤 =================

    def _should_skip(self, event: AstrMessageEvent, cfg: WakeConfig) -> bool:
        # 没文字（纯图/纯表情）
        text = _strip_msg_id(event.message_str)
        if not text:
            return True
        # 指令
        if text[:1] in ("/", "#", "!", "！"):
            return True
        # @ 了别人（不是她）
        try:
            self_id = str(event.get_self_id() or "")
        except Exception:  # noqa: BLE001
            self_id = ""
        ats = [
            str(comp.qq)
            for comp in event.get_messages()
            if isinstance(comp, At) and str(comp.qq) not in ("all", "")
        ]
        if ats and self_id and self_id not in ats:
            return True
        return False

    # ================= 关键词 =================

    def _effective_keywords(self, cfg: WakeConfig, event: AstrMessageEvent) -> list[str]:
        # auto_keywords 打开时，她自己的每一个名字（含别名）都自动当关键词
        auto = cfg.alias_names() if cfg.auto_keywords else []
        return cfg.all_keywords(auto)

    @staticmethod
    def _hit_keywords(text: str, keywords: list[str]) -> list[str]:
        if not text:
            return []
        low = text.lower()
        return [kw for kw in keywords if kw and kw.lower() in low]

    # ================= 历史缓冲 =================

    async def _capture_images(
        self, umo: str, event: AstrMessageEvent, cfg: WakeConfig
    ) -> bool:
        """把消息里的图片落地成本地路径并缓存。

        返回 True 表示「这是纯图片消息」——调用方应直接 return，
        不要再走关键词/窗口判断（图本身不自带文字，判了也是白判）。
        """
        if not cfg.enable_image:
            return False
        try:
            comps = event.get_messages()
        except Exception:  # noqa: BLE001
            return False
        paths: list[str] = []
        for comp in comps:
            if not isinstance(comp, Image):
                continue
            try:
                path = await comp.convert_to_file_path()
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[wake] 图片落地失败: {exc}")
                continue
            if path:
                paths.append(str(path))
        if not paths:
            return False
        buf = self._pending_images.setdefault(umo, [])
        buf.extend(paths)
        del buf[:-3]  # 最多留 3 张
        self._pending_images_ts[umo] = time.time()
        self._log(cfg, umo, "image", f"缓存 {len(paths)} 张图，等文字消息带走")
        return not _strip_msg_id(event.message_str)

    def _gc_pending_images(self, now: float | None = None) -> None:
        """清掉长时间没人来取的图片缓存，防止纯图消息后一直占着内存。"""
        if not self._pending_images_ts:
            return
        now = now or time.time()
        for umo, ts in list(self._pending_images_ts.items()):
            if now - ts > PENDING_IMAGE_TTL:
                self._pending_images_ts.pop(umo, None)
                self._pending_images.pop(umo, None)

    async def _describe_image(self, cfg: WakeConfig, path: str) -> str:
        """调视觉模型把一张图描述成一句话。拿不到就返回空串。

        模型和提示词都优先跟随 AstrBot 全局配置（provider_settings），
        这样插件转述和 AstrBot 自带群聊转述的风格一致；插件配置只做覆盖。
        """
        prov_id = (cfg.image_caption_provider or "").strip()
        prompt = ""
        try:
            psettings = self.context.get_config(umo=None).get("provider_settings") or {}
            if not prov_id:
                prov_id = str(
                    psettings.get("default_image_caption_provider_id") or ""
                ).strip()
            prompt = str(psettings.get("image_caption_prompt") or "").strip()
        except Exception:  # noqa: BLE001
            pass
        if not prov_id:
            return ""
        if not prompt:
            prompt = "用一句中文说清楚这张图片的内容，不要客套话。"
        prov = self.context.get_provider_by_id(prov_id)
        if prov is None:
            logger.warning(f"[wake] 找不到视觉模型 {prov_id}")
            return ""
        resp = await prov.text_chat(prompt=prompt, image_urls=[path])
        return (getattr(resp, "completion_text", "") or "").strip()

    def _remember(self, umo: str, event: AstrMessageEvent):
        text = _strip_msg_id((event.message_str or "").strip())
        if not text:
            # 纯图/表情/空消息：记一个占位，让小模型知道「这个人刚发了张图」，
            # 但不带图片内容（内容由 AstrBot 的群聊上下文负责）。
            if self._has_image(event):
                text = "[图片]"
            else:
                return
        self._append(umo, event.get_sender_name() or "群友", text[:200])

    def _has_image(self, event: AstrMessageEvent) -> bool:
        """这条消息里有没有图片组件。"""
        try:
            return any(isinstance(c, Image) for c in event.get_messages())
        except Exception:  # noqa: BLE001
            return False

    def _append(self, umo: str, speaker: str, text: str):
        buf = self._history.get(umo)
        if buf is None:
            buf = deque(maxlen=MAX_HISTORY)
            self._history[umo] = buf
        buf.append((speaker, text, time.monotonic()))

    def _split_history_tail(
        self,
        umo: str,
        sender_name: str,
        event: AstrMessageEvent,
        count: int = HISTORY_FOR_PROMPT,
    ) -> tuple[list[tuple[str, str]], str]:
        """把历史尾部「这个人连续说的几句」摘出来，合并成一条待判消息。

        对付「一个字一个字敲」：
            你 / 在 / 干 / 什 / 么
        五条会被拼成「你在干什么」，当成一条新消息交给小模型，
        历史里也不再重复出现这五条，模型不会看到「新消息：么」这种怪东西。

        返回 (剩下的历史, 合并后的消息)。
        """
        raw = list(self._history.get(umo, []))
        parts: list[str] = []
        now = time.monotonic()
        # 从尾部往前收同一人的连续发言，最多收到 deque 上限，防止刷屏合并成长文
        while raw and raw[-1][0] == sender_name and len(parts) < MAX_HISTORY:
            item = raw[-1]
            # 隔太久的旧发言不算「同一次说话」。否则上一轮没成功回复时，
            # 「你/…」不会打断连续性，旧发言一直留着，下一轮再发同样的
            # 内容就会被合并成「大肥鱼你在吗大肥鱼你在吗」。
            ts = item[2] if len(item) > 2 else now
            if now - ts > HISTORY_MERGE_WINDOW:
                break
            parts.insert(0, raw.pop()[1])
        # [图片] 占位后面补个空格，避免和后面的文字粘成一坨（"你/在/干"这种不加）
        fixed = [p + " " if p.startswith("[图片]") else p for p in parts]
        merged = "".join(fixed).strip()
        if not merged:
            merged = _strip_msg_id(event.message_str)
        # judge.py 那边是严格解包 (speaker, text)，时间戳只在本函数内用
        return [(item[0], item[1]) for item in raw[-max(1, count):]], merged

    # ================= 自述 / 日志 =================

    def _self_identity(self, event: AstrMessageEvent, umo: str, cfg: WakeConfig) -> tuple[str, str]:
        """返回 (她的名字, 人格描述行)。名字支持多个别名，人格描述暂时留空（prompt 会兜底）。"""
        names = cfg.alias_names()
        if len(names) <= 1:
            return names[0], ""
        return f"{names[0]}（也叫{'、'.join(names[1:])}）", ""

    @staticmethod
    def _log(cfg: WakeConfig, umo: str, scenario: str, msg: str):
        if cfg.debug_log:
            logger.info(f"[wake] {umo} [{scenario}] {msg}")

    # ================= 命令 =================

    @filter.command("wake")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def wake_command(self, event: AstrMessageEvent):
        """叫醒小鲸鱼：wake on|off|status|test|kw add/del/list|sec N|p N"""
        args = self._parse_args(event.message_str)
        if not args:
            yield event.plain_result(self._help_text())
            return

        sub = args[0].lower()
        new_config_changed = False

        if sub in ("on", "off"):
            self.config["enabled"] = sub == "on"
            new_config_changed = True
            yield event.plain_result(f"叫醒小鲸鱼：{'已开启' if sub == 'on' else '已关闭'}")

        elif sub == "status":
            yield event.plain_result(self._status_text(event))

        elif sub == "sec" and len(args) >= 2:
            try:
                sec = int(args[1])
            except ValueError:
                yield event.plain_result("秒数要是整数，比如 /wake sec 120")
                return
            self.config["window_seconds"] = max(0, min(3600, sec))
            new_config_changed = True
            yield event.plain_result(f"窗口时长已设为 {self.config['window_seconds']} 秒")

        elif sub in ("p", "prob") and len(args) >= 2:
            try:
                p = float(args[1])
            except ValueError:
                yield event.plain_result("概率要是小数，比如 /wake p 0.8")
                return
            self.config["reply_probability"] = max(0.0, min(1.0, p))
            new_config_changed = True
            yield event.plain_result(f"回复概率已设为 {self.config['reply_probability']}")

        elif sub == "kw" and len(args) >= 2:
            action = args[1].lower()
            kws = list(self.config.get("keywords") or [])
            if action == "list":
                yield event.plain_result(
                    "关键词：" + ("、".join(kws) if kws else "（空）")
                )
                return
            if action == "add" and len(args) >= 3:
                kw = " ".join(args[2:]).strip()
                if kw and kw not in kws:
                    kws.append(kw)
                    self.config["keywords"] = kws
                    new_config_changed = True
                yield event.plain_result(f"已添加关键词：{kw}")
                return
            if action == "del" and len(args) >= 3:
                kw = " ".join(args[2:]).strip()
                if kw in kws:
                    kws.remove(kw)
                    self.config["keywords"] = kws
                    new_config_changed = True
                yield event.plain_result(f"已删除关键词：{kw}")
                return
            yield event.plain_result("用法：/wake kw add 关键词 / del 关键词 / list")

        elif sub == "test" and len(args) >= 2:
            msg = " ".join(args[1:])
            cfg = self._cfg()
            hits = self._hit_keywords(msg, self._effective_keywords(cfg, event))
            yield event.plain_result(
                f"测试「{msg}」→ 关键词命中：{hits or '无'}；窗口："
                f"{'开着' if self.window.is_active(event.unified_msg_origin) else '关着'}"
            )

        else:
            yield event.plain_result(self._help_text())

        if new_config_changed:
            try:
                self.config.save_config()
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[wake] 保存配置失败: {exc}")

    @staticmethod
    def _parse_args(text: str) -> list[str]:
        parts = (text or "").strip().split()
        if not parts:
            return []
        if parts[0].lstrip("/").lower() == "wake":
            return parts[1:]
        return parts

    def _help_text(self) -> str:
        return (
            "叫醒小鲸鱼命令：\n"
            "/wake on|off        开关\n"
            "/wake status        看状态\n"
            "/wake sec 120       窗口时长（秒）\n"
            "/wake p 0.8         回复概率\n"
            "/wake kw add 词     加关键词\n"
            "/wake kw del 词     删关键词\n"
            "/wake kw list       看关键词\n"
            "/wake test 某句话   测试匹配"
        )

    def _status_text(self, event: AstrMessageEvent) -> str:
        cfg = self._cfg()
        umo = event.unified_msg_origin
        if self.window.is_active(umo):
            win_desc = f"开着，还剩 {self.window.remaining(umo)}s"
        else:
            win_desc = "关着"
        return (
            f"开关：{'开' if cfg.enabled else '关'}\n"
            f"窗口：{win_desc}\n"
            f"窗口时长：{cfg.window_seconds}s　回复概率：{cfg.reply_probability}\n"
            f"关键词：{'、'.join(self._effective_keywords(cfg, event)) or '（空）'}\n"
            f"白名单：{'、'.join(cfg.whitelist_groups) or '（空=所有群）'}"
        )

    # ================= 生命周期 =================

    async def terminate(self):
        self.window.clear()
        self._history.clear()
        self._latest_token.clear()
        self._latest_event.clear()
        self._pending_images.clear()
        self._pending_images_ts.clear()
        self._private_pending.clear()
        self._debounce_start.clear()
