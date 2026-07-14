"""todo_reminder — 待辦到期分級提醒。

每晚 20:00（台北）掃 100_Todo/🎯 任務看板 的未完成任務，
依截止日分級推播：🔴 已逾期/今天、🟠 明天、🟡 三天內。
沒有到期項目就整晚安靜（寧少勿多，避免通知疲勞）。
"""

import datetime
import logging
import os
import re

import discord
import pytz
from discord.ext import commands, tasks

logger = logging.getLogger(__name__)

TAIPEI_TZ = pytz.timezone('Asia/Taipei')
BOARD_DIR = '/Users/tyler/Downloads/Tyler-agent/100_Todo/🎯 任務看板'
BOARD_FILES = ['🔴 today.md', '🟠 short_term.md', '🟢 long_term.md']

# 截止日格式：(截止日: 2026-07-15) / （截止日: 07/15，備註）／YYYY-MM-DD 或 M/D
DUE_RE = re.compile(r'截止日[:：]\s*(\d{4}-\d{1,2}-\d{1,2}|\d{1,2}/\d{1,2})')
TASK_RE = re.compile(r'\s*- \[ \]\s*(.+)')


def _parse_due(raw: str, today: datetime.date) -> datetime.date | None:
    try:
        if '-' in raw:
            return datetime.date.fromisoformat(
                '-'.join(p.zfill(2) for p in raw.split('-'))
            )
        m, d = raw.split('/')
        due = datetime.date(today.year, int(m), int(d))
        # 12 月看到 1 月的截止日＝明年
        if (today - due).days > 300:
            due = due.replace(year=today.year + 1)
        return due
    except ValueError:
        return None


def _display(text: str) -> str:
    """截掉註記尾巴，讓提醒一行乾淨好讀。"""
    out = text.split('　')[0]
    out = re.sub(r'\s*[（(]截止日[^）)]*[）)]\s*', '', out)
    return (out[:70] + '…') if len(out) > 70 else out


def collect_due_tasks(today: datetime.date) -> dict[str, list[str]]:
    """回傳 {'overdue': [...], 'today': [...], 'tomorrow': [...], 'soon': [...]}"""
    buckets: dict[str, list[str]] = {'overdue': [], 'today': [], 'tomorrow': [], 'soon': []}
    for fn in BOARD_FILES:
        path = os.path.join(BOARD_DIR, fn)
        try:
            with open(path, encoding='utf-8') as f:
                lines = f.readlines()
        except OSError:
            continue
        for line in lines:
            m = TASK_RE.match(line)
            if not m:
                continue
            text = m.group(1).strip()
            dm = DUE_RE.search(text)
            if not dm:
                continue
            due = _parse_due(dm.group(1), today)
            if due is None:
                continue
            days = (due - today).days
            label = f'{_display(text)}（{due.month}/{due.day}）'
            if days < 0:
                buckets['overdue'].append(f'{_display(text)}（{due.month}/{due.day}，已過 {-days} 天）')
            elif days == 0:
                buckets['today'].append(label)
            elif days == 1:
                buckets['tomorrow'].append(label)
            elif days <= 3:
                buckets['soon'].append(label)
    return buckets


def build_embed(buckets: dict[str, list[str]]) -> discord.Embed | None:
    if not any(buckets.values()):
        return None
    embed = discord.Embed(
        title='⏰ 待辦到期提醒',
        description='今晚盤點：這些事快到期了',
        color=0xE74C3C if (buckets['overdue'] or buckets['today']) else 0xE67E22,
    )
    sections = [
        ('overdue', '🔴 已逾期'),
        ('today', '🔴 今天到期（還沒打勾）'),
        ('tomorrow', '🟠 明天到期'),
        ('soon', '🟡 三天內'),
    ]
    for key, title in sections:
        if buckets[key]:
            value = '\n'.join(f'• {t}' for t in buckets[key])[:1024]
            embed.add_field(name=title, value=value, inline=False)
    return embed


class TodoReminderCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.due_check.start()

    def cog_unload(self):
        self.due_check.cancel()

    # 每晚 20:00 台北 ＝ UTC 12:00
    @tasks.loop(time=datetime.time(hour=12, minute=0, tzinfo=pytz.UTC))
    async def due_check(self):
        channel = self.bot.get_channel(int(os.environ['DISCORD_CHANNEL_ID']))
        if not channel:
            return
        today = datetime.datetime.now(TAIPEI_TZ).date()
        try:
            embed = build_embed(collect_due_tasks(today))
        except Exception:
            logger.exception('待辦到期掃描失敗')
            return
        if embed:
            await channel.send(embed=embed)
            logger.info('已推播待辦到期提醒')
        else:
            logger.info('今晚沒有到期待辦，不推播')

    @due_check.before_loop
    async def _wait_ready(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(TodoReminderCog(bot))
