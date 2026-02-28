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

# 使用 AstrBot 官方数据路径
FAVORS_FILE = os.path.join(get_astrbot_data_path(), "lele_favor.json")
FAVOR_CD = 60  # 单位：秒

# SnowNLP 情感阈值
SENTIMENT_POSITIVE_THRESHOLD = 0.65  # 高于此值视为正面，+2分
SENTIMENT_NEGATIVE_THRESHOLD = 0.35  # 低于此值视为负面，-1分
# 中间区间 (0.35 ~ 0.65) 视为中性，+1分

# 好感度称号 & 对应人设 prompt（可自行修改）
# 格式：(最低分数, 称号, 人设描述)
FAVOR_STAGES = [
    (
        0,
        "陌生人",
        "你刚认识这位用户，语气礼貌正式，保持适当距离，不会主动分享自己的想法，回答简洁。"
    ),
    (
        10,
        "普通朋友",
        "你和这位用户已经认识一段时间了，语气平和友好，偶尔会加一点轻松的语气，但整体还是比较正经。"
    ),
    (
        30,
        "好朋友",
        "你和这位用户是好朋友，说话轻松自然，可以用昵称称呼对方，偶尔开个无害的小玩笑，会主动关心对方状态。"
    ),
    (
        60,
        "挚友",
        "你和这位用户是多年挚友，说话亲密随意，会撒娇、开玩笑，对对方的事情非常上心，说话带着明显的亲近感。"
    ),
    (
        100,
        "知己",
        "你和这位用户是彼此最信任的知己，说话毫无保留，极其亲密，会主动分享心情，对对方的一切都感兴趣，语气温柔而真诚。"
    ),
]

import aiofiles

try:
    from snownlp import SnowNLP
    SNOWNLP_AVAILABLE = True
except ImportError:
    SNOWNLP_AVAILABLE = False
    logger.warning("[FavorPlugin] snownlp 未安装，将退回为固定+1分模式。请执行: pip install snownlp")


def get_stage(points: int) -> tuple[str, str]:
    """根据积分返回 (称号, 人设描述)"""
    title, persona = FAVOR_STAGES[0][1], FAVOR_STAGES[0][2]
    for threshold, t, p in FAVOR_STAGES:
        if points >= threshold:
            title, persona = t, p
    return title, persona


def analyze_sentiment(text: str) -> tuple[int, str]:
    """
    分析文本情感，返回 (分数变化, 描述)
    正面 → +2，中性 → +1，负面 → -1
    """
    if not SNOWNLP_AVAILABLE or not text.strip():
        return 1, "中性"

    try:
        sentiment_score = SnowNLP(text).sentiments  # 0.0 ~ 1.0
        if sentiment_score >= SENTIMENT_POSITIVE_THRESHOLD:
            return 2, f"正面({sentiment_score:.2f})"
        elif sentiment_score <= SENTIMENT_NEGATIVE_THRESHOLD:
            return -1, f"负面({sentiment_score:.2f})"
        else:
            return 1, f"中性({sentiment_score:.2f})"
    except Exception as e:
        logger.warning(f"[FavorPlugin] 情感分析失败: {e}")
        return 1, "中性"


class FavorPlugin(Star):
    name = "favor_star"
    description = "记录和查询用户好感度，1分钟CD，SnowNLP情感分析，好感度影响说话风格，JSON持久化"

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
        return self._favor_cache.get(user_id, {"points": 0, "last_time": 0})

    async def add_favor(self, user_id: str, delta: int, now_ts: float = None) -> bool:
        """
        尝试修改好感度。
        delta 可以为正（加分）或负（扣分）。
        CD 仅对加分生效，扣分不受 CD 限制。
        返回 True 表示本次操作生效。
        """
        if not self._favor_cache and not self._init_task.done():
            await self._init_task
        now_ts = now_ts or datetime.now().timestamp()
        udata = self._favor_cache.get(user_id, {"points": 0, "last_time": 0})
        last = udata.get("last_time", 0)

        if delta > 0:
            # 加分受 CD 限制
            if now_ts - last < FAVOR_CD:
                return False
            udata["last_time"] = now_ts

        # 扣分时保底 0，不进入负数
        udata["points"] = max(0, udata["points"] + delta)
        self._favor_cache[user_id] = udata
        await self._save_favor()
        return True

    @filter.on_llm_request()
    async def inject_persona(self, event: AstrMessageEvent, req: ProviderRequest):
        """在 LLM 请求前，根据发送者好感度动态注入人设 prompt"""
        user_id = str(event.get_sender_id())
        favor = await self.get_favor(user_id)
        points = favor["points"]
        title, persona = get_stage(points)

        inject = (
            f"\n\n【好感度系统】当前与用户（{user_id}）的关系阶段：{title}（{points}分）。"
            f"请严格按照以下描述的风格与对方交流：{persona}"
        )
        req.system_prompt += inject
        logger.debug(f"[FavorPlugin] 注入人设 user={user_id} 阶段={title} points={points}")

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_any_msg(self, event: AstrMessageEvent):
        user_id = str(event.get_sender_id())
        text = str(event.message_str).strip()

        # 跳过指令消息，避免和 my_favor 指令重复处理
        if text.startswith("/") or text == "我的好感":
            return

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
        favor = await self.get_favor(user_id)
        points = favor["points"]
        title, _ = get_stage(points)
        yield event.plain_result(
            f"✨ 你的好感度：{points} 分\n"
            f"当前称号：【{title}】"
        )

    @filter.command("设置好感")
    async def set_favor(self, event: AstrMessageEvent):
        # 用法：设置好感 @某人 50
        text = str(event.message_str).strip()
        parts = text.split()
        
        # 解析参数，格式：设置好感 [user_id] [分数]
        # 群里可以直接用QQ号，比如：设置好感 123456789 50
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

        udata = self._favor_cache.get(target_id, {"points": 0, "last_time": 0})
        udata["points"] = max(0, points)
        self._favor_cache[target_id] = udata
        await self._save_favor()

        title, _ = get_stage(points)
        yield event.plain_result(f"✅ 已将 {target_id} 的好感度设为 {points} 分【{title}】")
