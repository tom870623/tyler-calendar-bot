"""lifeos_push — 每小時把 vault 快照推上 LifeOS 儀表板（Cloudflare Worker）。

打包內容：今日待辦（含勾選狀態）、三天內到期、掛太久的事、今日行程、下一趟航班。
Worker 端存進 D1 的 LifeosSnapshot 表，手機打開 https://lifeos.tylerthepilot.workers.dev
就看得到（Mac 關機時資料會停留在最後一次推送）。

設定（.env.local）：LIFEOS_PUSH_URL、LIFEOS_PUSH_KEY。沒設就整個安靜停用。
"""

import asyncio
import datetime
import json
import logging
import os
import re
import subprocess

import aiohttp
import pytz
from discord.ext import commands, tasks

from .calendar_local import (
    format_event,
    get_events,
    get_upcoming_flights,
    pick_next_flight,
    _to_taipei,
)
from .todo_reminder import collect_due_tasks

logger = logging.getLogger(__name__)

TAIPEI_TZ = pytz.timezone('Asia/Taipei')
PROJECT_ROOT = '/Users/tyler/Downloads/Tyler-agent'
TODAY_BOARD = os.path.join(PROJECT_ROOT, '100_Todo', '🎯 任務看板', '🔴 today.md')

TASK_LINE = re.compile(r'\s*- \[([ x])\]\s*(.+)')


def _strip_md(text: str) -> str:
    """拿掉 Discord 用的 markdown 記號，網頁上顯示乾淨文字。"""
    return re.sub(r'[*`]|~~', '', text)


def _today_tasks() -> list[dict]:
    items = []
    try:
        with open(TODAY_BOARD, encoding='utf-8') as f:
            for line in f:
                m = TASK_LINE.match(line)
                if m:
                    text = _strip_md(m.group(2).strip().split('　')[0])
                    items.append({'text': text[:90], 'done': m.group(1) == 'x'})
    except OSError:
        pass
    return items


def _stale_items() -> list[str]:
    try:
        out = subprocess.run(
            ['python3', '000_Agent/skills/morning/stale_items.py'],
            capture_output=True, text=True, cwd=PROJECT_ROOT, timeout=60,
        ).stdout.strip()
    except Exception:
        return []
    lines = [l for l in out.splitlines() if l.startswith('- ')]
    return lines[:8]


def _flight_line(f: dict) -> str:
    dep = _to_taipei(f['dtstart'])
    days = (dep.date() - datetime.datetime.now(TAIPEI_TZ).date()).days
    when = '今天' if days == 0 else ('明天' if days == 1 else f'{days} 天後')
    route = (f.get('location') or '').replace('-', '→')
    return f"{f['summary']} {route} · {dep.month}/{dep.day} · {when}"


BORN = datetime.date(2026, 6, 29)  # LifeOS 誕生日
MEMORY_DIR = os.path.expanduser(
    '~/.claude/projects/-Users-tyler-Downloads-Tyler-agent/memory'
)


def _public_stats() -> dict:
    """給 Tyrone 公開頁的統計數。⚠️ 只放無害的「數量」，不放任何內容。"""
    def count(fn):
        try:
            return fn()
        except Exception:
            return None

    skills = count(lambda: sum(
        1 for d in os.listdir(os.path.join(PROJECT_ROOT, '000_Agent', 'skills'))
        if os.path.isfile(os.path.join(PROJECT_ROOT, '000_Agent', 'skills', d, 'SKILL.md'))
    ))
    memories = count(lambda: sum(
        1 for f in os.listdir(MEMORY_DIR)
        if f.endswith('.md') and f != 'MEMORY.md'
    ))
    domains = count(lambda: sum(
        1 for d in os.listdir(PROJECT_ROOT)
        if re.match(r'\d{3}_', d) and os.path.isdir(os.path.join(PROJECT_ROOT, d))
    ))
    mcp = count(lambda: len(
        json.load(open(os.path.expanduser('~/.claude.json'))).get('mcpServers', {})
    ))
    return {
        'skills': skills,
        'memories': memories,
        'domains': domains,
        'mcp': mcp,
        'days_alive': (datetime.datetime.now(TAIPEI_TZ).date() - BORN).days,
        'apps_live': 3,  # Discord bot、Finance OS、LifeOS 儀表板
    }


def build_snapshot() -> dict:
    now = datetime.datetime.now(TAIPEI_TZ)
    today = now.date()

    events = [_strip_md(format_event(e)) for e in get_events(today)]
    flights = get_upcoming_flights()
    nf = pick_next_flight(flights)

    due = collect_due_tasks(today)
    due_soon = due['tomorrow'] + due['soon']

    return {
        'generated_at': now.strftime('%m/%d %H:%M'),
        'events': events[:10],
        'next_flight': _flight_line(nf) if nf else None,
        'today_tasks': _today_tasks(),
        'due_soon': due_soon[:6],
        'stale': _stale_items(),
        'public_stats': _public_stats(),
    }


class LifeosPushCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.push_snapshot.start()

    def cog_unload(self):
        self.push_snapshot.cancel()

    @tasks.loop(hours=1)  # 啟動先跑一次，之後每小時
    async def push_snapshot(self):
        url = os.environ.get('LIFEOS_PUSH_URL')
        key = os.environ.get('LIFEOS_PUSH_KEY')
        if not url or not key:
            return
        try:
            snapshot = await asyncio.to_thread(build_snapshot)
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url,
                    data=json.dumps(snapshot, ensure_ascii=False),
                    headers={'X-Push-Key': key, 'Content-Type': 'application/json'},
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    if resp.status == 200:
                        logger.info('LifeOS 快照已推送')
                    else:
                        logger.error(f'LifeOS 快照推送失敗：HTTP {resp.status}')
        except Exception:
            logger.exception('LifeOS 快照推送出錯')

    @push_snapshot.before_loop
    async def _wait_ready(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(LifeosPushCog(bot))
