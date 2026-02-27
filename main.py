import asyncio
import json
import os
from datetime import datetime
from typing import Dict

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.api import logger
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

# 使用 AstrBot 官方数据路径（更安全，不会乱放文件）
FAVORS_FILE = os.path.join(get_astrbot_data_path(), "lele_favor.json")
FAVOR_CD = 60  # 单位：秒

import aiofiles


class FavorPlugin(Star):
    name = "favor_star"
    description = "记录和查询用户好感度，1分钟CD，JSON持久化"

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

    async def get_favor(self, user_id: str):
        if not self._favor_cache:
            await self._init_task
        return self._favor_cache.get(user_id, {"points": 0, "last_time": 0})

    async def add_favor(self, user_id: str, now_ts: float = None):
        if not self._favor_cache:
            await self._init_task
        now_ts = now_ts or datetime.now().timestamp()
        udata = self._favor_cache.get(user_id, {"points": 0, "last_time": 0})
        last = udata.get("last_time", 0)
        if now_ts - last >= FAVOR_CD:
            udata["points"] += 1
            udata["last_time"] = now_ts
            self._favor_cache[user_id] = udata
            await self._save_favor()
            return True
        return False

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_any_msg(self, event: AstrMessageEvent):
        user_id = str(event.get_sender_id())   # ← 这里改了！原来是 event.user_id
        await self.add_favor(user_id)

    @filter.command("我的好感")                # ← 这里也改了！去掉前面的 /
    async def my_favor(self, event: AstrMessageEvent):
        user_id = str(event.get_sender_id())   # ← 这里也改了！
        favor = await self.get_favor(user_id)
        points = favor["points"]
        yield event.plain_result(f"你的好感度积分为：{points}")