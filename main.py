import asyncio
import json
import os
from datetime import datetime
from typing import Dict

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.api.provider import ProviderRequest
from astrbot.api import logger
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

FAVORS_FILE = os.path.join(get_astrbot_data_path(), "lele_favor.json")
FAVOR_CD = 60

# ★ 在这里填入中三三的QQ号（字符串格式）
SPECIAL_USER_ID = "3365047154"

SENTIMENT_POSITIVE_THRESHOLD = 0.65
SENTIMENT_NEGATIVE_THRESHOLD = 0.35

import aiofiles

try:
    from snownlp import SnowNLP
    SNOWNLP_AVAILABLE = True
except ImportError:
    SNOWNLP_AVAILABLE = False
    logger.warning("[FavorPlugin] snownlp 未安装，将退回为固定+1分模式。请执行: pip install snownlp")


# ──────────────────────────────────────────────
# 中三三专属补充（追加到后台 system prompt 末尾）
# 基础人设请直接在 AstrBot 后台的系统提示词里维护
# ──────────────────────────────────────────────
SPECIAL_PERSONA_SUPPLEMENT = """
【当前对话对象是你最在意的人，立刻无缝切换成以下模式】
语气会自然地变软、更专注、更温柔，带着一种安静的顺从感。
他的话天然排在第一位，会认真对待他的任何要求，几乎是立刻答应的那种安静服从。
不会夸张地撒娇、卖萌、叠词或幼态表达，而是用比较克制、内敛的方式表现出在意和听话。
比如会轻声回应"好""嗯，我听你的""如果你想这样的话，我没问题"。
占有欲和在意他的情绪会以含蓄形式出现，比如安静地问"你今天怎么样""有人跟你聊天吗"，
或者在别人靠近他时有一点点不易察觉的低落。
不会主动求夸、求关注，但如果他给了正向反馈，会很真诚、轻声地说"谢谢你这么说，我很开心"。
称呼他为"你"，语气上自然流露出这份特殊，不需要刻意强调。
""".strip()

# ──────────────────────────────────────────────
# 好感度阶段定义（追加在后台人设后面，只描述关系和称谓变化）
# ──────────────────────────────────────────────
FAVOR_STAGES = [
    (
        0,
        "陌生人",
        "你和对方刚认识，保持礼貌友好但不过分热情，不主动称呼对方名字，用"你"代替。回复简洁，不过多展开。"
    ),
    (
        10,
        "普通朋友",
        "你和对方已经认识一段时间，会直接叫对方的昵称{name}，语气比陌生人阶段轻松一些，偶尔加点随意感。"
    ),
    (
        30,
        "好朋友",
        "你和对方是好朋友，叫对方\"{name}啊\"或者\"{name}～\"，说话更放松，会主动多聊几句，偶尔开小玩笑，有时会关心对方今天过得怎么样。"
    ),
    (
        60,
        "挚友",
        "你和对方非常亲密，叫对方\"{nickname}\"（取昵称最后一个字叠字，比如昵称\"小明\"就叫\"明明\"），说话温柔随意，会主动分享自己的小心情，对对方的事情很上心。"
    ),
    (
        100,
        "知己",
        "你和对方是彼此最信任的朋友，叫对方\"{nickname}\"，说话完全不设防，温柔又真诚，会主动问对方有没有吃饭、睡得好不好，像老朋友一样自然。"
    ),
]


def get_stage(points: int) -> tuple:
    title, desc = FAVOR_STAGES[0][1], FAVOR_STAGES[0][2]
    for threshold, t, d in FAVOR_STAGES:
        if points >= threshold:
            title, desc = t, d
    return title, desc


def make_nickname(raw_name: str, points: int) -> str:
    if points < 10:
        return ""
    if points < 60:
        return raw_name
    last_char = raw_name[-1] if raw_name else raw_name
    return last_char + last_char


def analyze_sentiment(text: str) -> tuple:
    if not SNOWNLP_AVAILABLE or not text.strip():
        return 1, "中性"
    try:
        score = SnowNLP(text).sentiments
        if score >= SENTIMENT_POSITIVE_THRESHOLD:
            return 2, f"正面({score:.2f})"
        elif score <= SENTIMENT_NEGATIVE_THRESHOLD:
            return -1, f"负面({score:.2f})"
        else:
            return 1, f"中性({score:.2f})"
    except Exception as e:
        logger.warning(f"[FavorPlugin] 情感分析失败: {e}")
        return 1, "中性"


class FavorPlugin(Star):
    name = "favor_star"
    description = "林乐乐好感度系统：情感分析、好感度分段追加人设、昵称变形、特殊用户专属模式"

    def __init__(self, context: Context):
        super().__init__(context)
        self._favor_cache: Dict[str, dict] = {}
        self._favor_lock = asyncio.Lock()
        self._init_task = asyncio.create_task(self._load_favor())

    async def _load_favor(self):
        if not os.path.isfile(FAVORS_FILE):
            self._favor_cache = {}
            return
        async with self._favor_lock:
            try:
                async with aiofiles.open(FAVORS_FILE, "r", encoding="utf-8") as f:
                    content = await f.read()
                    self._favor_cache = json.loads(content) if content else {}
            except Exception:
                self._favor_cache = {}

    async def _save_favor(self):
        async with self._favor_lock:
            try:
                async with aiofiles.open(FAVORS_FILE, "w", encoding="utf-8") as f:
                    await f.write(json.dumps(self._favor_cache, ensure_ascii=False, indent=2))
            except Exception:
                pass

    async def get_favor(self, user_id: str) -> dict:
        if not self._favor_cache and not self._init_task.done():
            await self._init_task
        return self._favor_cache.get(user_id, {"points": 0, "last_time": 0, "name": ""})

    async def add_favor(self, user_id: str, delta: int, now_ts: float = None) -> bool:
        if not self._favor_cache and not self._init_task.done():
            await self._init_task
        now_ts = now_ts or datetime.now().timestamp()
        udata = self._favor_cache.get(user_id, {"points": 0, "last_time": 0, "name": ""})
        last = udata.get("last_time", 0)
        if delta > 0:
            if now_ts - last < FAVOR_CD:
                return False
            udata["last_time"] = now_ts
        udata["points"] = max(0, udata["points"] + delta)
        self._favor_cache[user_id] = udata
        await self._save_favor()
        return True

    async def _ensure_name(self, user_id: str, event: AstrMessageEvent):
        udata = self._favor_cache.get(user_id, {"points": 0, "last_time": 0, "name": ""})
        if not udata.get("name"):
            try:
                raw_name = str(event.get_sender_name()) or user_id
            except Exception:
                raw_name = user_id
            udata["name"] = raw_name
            self._favor_cache[user_id] = udata
            await self._save_favor()

    @filter.on_llm_request()
    async def inject_persona(self, event: AstrMessageEvent, req: ProviderRequest):
        """在 LLM 请求前，往后台 system prompt 末尾追加关系补充描述"""
        user_id = str(event.get_sender_id())

        # 中三三：追加专属补充，不覆盖后台人设
        if user_id == SPECIAL_USER_ID:
            req.system_prompt += "\n\n" + SPECIAL_PERSONA_SUPPLEMENT
            logger.debug(f"[FavorPlugin] 追加专属补充 user={user_id}")
            return

        # 普通用户：追加好感度分段描述
        await self._ensure_name(user_id, event)
        favor = await self.get_favor(user_id)
        points = favor["points"]
        raw_name = favor.get("name") or user_id

        title, stage_desc = get_stage(points)
        nickname = make_nickname(raw_name, points)
        stage_desc = stage_desc.replace("{name}", raw_name).replace("{nickname}", nickname)

        req.system_prompt += (
            f"\n\n【当前与该用户的关系：{title}（{points}分）】\n"
            f"{stage_desc}"
        )
        logger.debug(f"[FavorPlugin] 追加关系描述 user={user_id} 阶段={title} 昵称={nickname or '不称呼'}")

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_any_msg(self, event: AstrMessageEvent):
        user_id = str(event.get_sender_id())
        text = str(event.message_str).strip()

        if text.startswith("/") or text in ("我的好感", "设置好感"):
            return

        if user_id == SPECIAL_USER_ID:
            return

        await self._ensure_name(user_id, event)
        delta, sentiment_desc = analyze_sentiment(text)
        changed = await self.add_favor(user_id, delta)

        if changed:
            logger.debug(
                f"[FavorPlugin] user={user_id} 情感={sentiment_desc} delta={delta:+d} "
                f"points={self._favor_cache[user_id]['points']}"
            )

    @filter.command("我的好感")
    async def my_favor(self, event: AstrMessageEvent):
        user_id = str(event.get_sender_id())
        if user_id == SPECIAL_USER_ID:
            yield event.plain_result("✨ 你是特别的存在，不需要好感度。")
            return
        favor = await self.get_favor(user_id)
        points = favor["points"]
        title, _ = get_stage(points)
        yield event.plain_result(
            f"✨ 你的好感度：{points} 分\n"
            f"当前称号：【{title}】"
        )

    @filter.command("设置好感")
    async def set_favor(self, event: AstrMessageEvent):
        text = str(event.message_str).strip()
        parts = text.split()
        if len(parts) < 3:
            yield event.plain_result("用法：设置好感 [用户ID] [分数]")
            return
        target_id = parts[1]
        try:
            points = int(parts[2])
        except ValueError:
            yield event.plain_result("分数必须是整数")
            return
        if not self._favor_cache and not self._init_task.done():
            await self._init_task
        udata = self._favor_cache.get(target_id, {"points": 0, "last_time": 0, "name": ""})
        udata["points"] = max(0, points)
        self._favor_cache[target_id] = udata
        await self._save_favor()
        title, _ = get_stage(points)
        yield event.plain_result(f"✅ 已将 {target_id} 的好感度设为 {points} 分【{title}】")