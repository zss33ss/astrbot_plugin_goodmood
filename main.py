import asyncio
import json
import os
from datetime import datetime
from typing import Dict

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Star, register

FAVORS_FILE = "lele_favor.json"
FAVOR_LOCK = asyncio.Lock()
FAVOR_CD = 60  # 单位：秒

_favor_cache: Dict[str, dict] = {}

# 依赖 aiofiles，如未自动导入请自行 pip install aiofiles
import aiofiles

async def _load_favor():
    global _favor_cache
    if not os.path.isfile(FAVORS_FILE):
        _favor_cache = {}
        return
    async with FAVOR_LOCK:
        try:
            async with aiofiles.open(FAVORS_FILE, "r", encoding="utf-8") as f:
                content = await f.read()
                _favor_cache = json.loads(content) if content else {}
        except Exception:
            _favor_cache = {}

async def _save_favor():
    async with FAVOR_LOCK:
        try:
            async with aiofiles.open(FAVORS_FILE, "w", encoding="utf-8") as f:
                await f.write(json.dumps(_favor_cache, ensure_ascii=False, indent=2))
        except Exception:
            pass

async def get_favor(user_id: str):
    if not _favor_cache:
        await _load_favor()
    return _favor_cache.get(user_id, {"points": 0, "last_time": 0})

async def add_favor(user_id: str, now_ts: float = None):
    if not _favor_cache:
        await _load_favor()
    now_ts = now_ts or datetime.now().timestamp()
    udata = _favor_cache.get(user_id, {"points": 0, "last_time": 0})
    last = udata.get("last_time", 0)
    if now_ts - last >= FAVOR_CD:
        udata["points"] += 1
        udata["last_time"] = now_ts
        _favor_cache[user_id] = udata
        await _save_favor()
        return True
    return False

class FavorStar(Star):
    # 插件信息可根据你的需求补充
    name = "favor_star"
    description = "记录和查询用户好感度，1分钟CD，JSON持久化"

    @filter.on_event(AstrMessageEvent)
    async def on_any_msg(self, event: AstrMessageEvent):
        user_id = str(event.user_id)
        await add_favor(user_id)

    @Star.on_command("/my mood")
    async def my_mood(self, event: AstrMessageEvent):
        user_id = str(event.user_id)
        favor = await get_favor(user_id)
        points = favor["points"]
        yield event.plain_result(f"你的好感度积分为：{points}")

    @staticmethod
    def get_user_favor_points(user_id: str) -> int:
        """
        用于外部：获取用户好感积分，方便后续个性化机器人回复
        """
        loop = asyncio.get_event_loop()
        favor = loop.run_until_complete(get_favor(user_id))
        return favor["points"]

# 注册Star插件
register(FavorStar)
