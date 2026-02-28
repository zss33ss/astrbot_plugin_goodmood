import asyncio
import json
import os
from datetime import datetime
from typing import Dict

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.api import logger
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

# 使用 AstrBot 官方数据路径
FAVORS_FILE = os.path.join(get_astrbot_data_path(), "lele_favor.json")
FAVOR_CD = 0  # 单位：秒

# SnowNLP 情感阈值
SENTIMENT_POSITIVE_THRESHOLD = 0.65  # 高于此值视为正面，+2分
SENTIMENT_NEGATIVE_THRESHOLD = 0.35  # 低于此值视为负面，-1分
# 中间区间 (0.35 ~ 0.65) 视为中性，+1分

# 好感度称号（可自行扩展）
FAVOR_TITLES = [
    (0,   "陌生人"),
    (10,  "普通朋友"),
    (30,  "好朋友"),
    (60,  "挚友"),
    (100, "知己"),
]

import aiofiles

try:
    from snownlp import SnowNLP
    SNOWNLP_AVAILABLE = True
except ImportError:
    SNOWNLP_AVAILABLE = False
    logger.warning("[FavorPlugin] snownlp 未安装，将退回为固定+1分模式。请执行: pip install snownlp")


def get_favor_title(points: int) -> str:
    """根据积分返回称号"""
    title = FAVOR_TITLES[0][1]
    for threshold, name in FAVOR_TITLES:
        if points >= threshold:
            title = name
    return title


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
    description = "记录和查询用户好感度，1分钟CD，SnowNLP情感分析，JSON持久化"

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

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_any_msg(self, event: AstrMessageEvent):
        user_id = str(event.get_sender_id())
        text = str(event.message_str).strip()

        # 跳过指令消息，避免和 my_favor 指令重复处理
        if text.startswith("/") or text == "我的好感":
            return

        delta, sentiment_desc = analyze_sentiment(text)

        # 【调试日志】查看情感判断结果，排查问题用，稳定后可改回 debug 级别
        logger.info(f"[FavorPlugin] user={user_id} 文本='{text[:30]}' 情感={sentiment_desc} delta={delta:+d}")

        changed = await self.add_favor(user_id, delta)

        logger.info(
            f"[FavorPlugin] add_favor结果: changed={changed} "
            f"当前积分={self._favor_cache.get(user_id, {}).get('points', '?')}"
        )

    @filter.command("我的好感")
    async def my_favor(self, event: AstrMessageEvent):
        user_id = str(event.get_sender_id())
        favor = await self.get_favor(user_id)
        points = favor["points"]
        title = get_favor_title(points)
        yield event.plain_result(
            f"✨ 你的好感度：{points} 分\n"
            f"当前称号：【{title}】"
        )
