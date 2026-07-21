"""archive_completed — 每 2 小時掃任務看板，把打勾 [x] 的任務自動搬進 📂 archive.md。

跑 000_Agent/skills/todo/archive_completed.py（同一支腳本 Tyler 也能手動跑）。
沒有新完成的項目就整個安靜，不推播（寧靜優先，同 todo_reminder 的作法）。
"""

import asyncio
import logging
import os
import subprocess

from discord.ext import commands, tasks

logger = logging.getLogger(__name__)

PROJECT_ROOT = '/Users/tyler/Downloads/Tyler-agent'
SCRIPT = '000_Agent/skills/todo/archive_completed.py'


def _run_archive() -> str:
    out = subprocess.run(
        ['python3', SCRIPT],
        capture_output=True, text=True, cwd=PROJECT_ROOT, timeout=60,
    )
    return out.stdout.strip()


class ArchiveCompletedCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.archive_sweep.start()

    def cog_unload(self):
        self.archive_sweep.cancel()

    @tasks.loop(hours=2)  # 啟動先跑一次，之後每 2 小時
    async def archive_sweep(self):
        try:
            output = await asyncio.to_thread(_run_archive)
        except Exception:
            logger.exception('封存掃描失敗')
            return
        if output.startswith('（沒有'):
            logger.info('封存掃描：沒有新完成的項目')
            return
        logger.info(f'封存掃描：{output}')
        channel_id = os.environ.get('MORNING_CHANNEL_ID') or os.environ.get('DISCORD_CHANNEL_ID')
        channel = self.bot.get_channel(int(channel_id)) if channel_id else None
        if channel:
            await channel.send(f'📥 自動封存完成任務：\n{output}')

    @archive_sweep.before_loop
    async def _wait_ready(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(ArchiveCompletedCog(bot))
