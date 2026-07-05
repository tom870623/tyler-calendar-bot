import discord
from discord.ext import commands, tasks
from discord import app_commands
import icalendar
import recurring_ical_events
import requests
import asyncio
import datetime
import pytz
import os
import logging
import traceback

logger = logging.getLogger(__name__)

TAIPEI_TZ = pytz.timezone('Asia/Taipei')


def get_events(date: datetime.date) -> list[dict]:
    url = os.environ['ICLOUD_ICS_URL']
    logger.info(f'正在抓取 ICS: {url[:50]}...')
    response = requests.get(url, timeout=30)
    response.raise_for_status()
    logger.info(f'ICS 抓取成功，大小: {len(response.content)} bytes')

    cal = icalendar.Calendar.from_ical(response.content)

    start = datetime.datetime.combine(date, datetime.time.min).replace(tzinfo=TAIPEI_TZ)
    end = datetime.datetime.combine(date, datetime.time.max).replace(tzinfo=TAIPEI_TZ)

    raw_events = recurring_ical_events.of(cal).between(start, end)
    logger.info(f'找到 {len(raw_events)} 個事件')

    events = []
    for vevent in raw_events:
        summary = str(vevent.get('SUMMARY', '（無標題）'))
        dtstart = vevent.get('DTSTART').dt
        dtend_prop = vevent.get('DTEND')
        dtend = dtend_prop.dt if dtend_prop else None
        events.append({'summary': summary, 'dtstart': dtstart, 'dtend': dtend})

    def sort_key(ev):
        dtstart = ev['dtstart']
        if isinstance(dtstart, datetime.datetime):
            return dtstart.astimezone(TAIPEI_TZ) if dtstart.tzinfo else dtstart.replace(tzinfo=TAIPEI_TZ)
        return datetime.datetime.combine(dtstart, datetime.time.min, tzinfo=TAIPEI_TZ)

    events.sort(key=sort_key)
    return events


def format_event(event: dict) -> str:
    dtstart = event['dtstart']
    summary = event['summary']

    if isinstance(dtstart, datetime.date) and not isinstance(dtstart, datetime.datetime):
        return f'📅 {summary}（全天）'

    if isinstance(dtstart, datetime.datetime):
        if dtstart.tzinfo:
            dtstart = dtstart.astimezone(TAIPEI_TZ)
        time_str = dtstart.strftime('%H:%M')

        dtend = event.get('dtend')
        if dtend and isinstance(dtend, datetime.datetime):
            if dtend.tzinfo:
                dtend = dtend.astimezone(TAIPEI_TZ)
            time_str += f'－{dtend.strftime("%H:%M")}'

        return f'🕐 `{time_str}` {summary}'

    return f'📅 {summary}'


def build_message(date: datetime.date, events: list[dict]) -> str:
    weekdays = ['一', '二', '三', '四', '五', '六', '日']
    weekday = weekdays[date.weekday()]
    date_str = date.strftime(f'%Y/%m/%d（週{weekday}）')

    if not events:
        return f'📆 **{date_str}**\n\n今天沒有行程，好好休息 😊'

    lines = [f'📆 **{date_str}**\n']
    for event in events:
        lines.append(format_event(event))

    return '\n'.join(lines)


class CalendarCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.daily_reminder.start()

    def cog_unload(self):
        self.daily_reminder.cancel()

    @tasks.loop(time=datetime.time(hour=23, minute=0, tzinfo=pytz.UTC))
    async def daily_reminder(self):
        channel = self.bot.get_channel(int(os.environ['DISCORD_CHANNEL_ID']))
        if not channel:
            logger.error('找不到頻道')
            return

        today = datetime.datetime.now(TAIPEI_TZ).date()
        try:
            events = await asyncio.to_thread(get_events, today)
            message = build_message(today, events)
        except Exception as e:
            logger.error(f'每日推送失敗：{e}\n{traceback.format_exc()}')
            message = '⚠️ 無法取得今日行程，請稍後再試。'

        await channel.send(message)

    @daily_reminder.before_loop
    async def before_daily_reminder(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name='today', description='查詢今日行程')
    async def cmd_today(self, interaction: discord.Interaction):
        await interaction.response.defer()
        today = datetime.datetime.now(TAIPEI_TZ).date()
        try:
            events = await asyncio.to_thread(get_events, today)
            message = build_message(today, events)
        except Exception as e:
            logger.error(f'/today 失敗：{e}\n{traceback.format_exc()}')
            message = f'⚠️ 無法取得行程：`{type(e).__name__}: {e}`'
        await interaction.followup.send(message)

    @app_commands.command(name='tomorrow', description='查詢明日行程')
    async def cmd_tomorrow(self, interaction: discord.Interaction):
        await interaction.response.defer()
        tomorrow = datetime.datetime.now(TAIPEI_TZ).date() + datetime.timedelta(days=1)
        try:
            events = await asyncio.to_thread(get_events, tomorrow)
            message = build_message(tomorrow, events)
        except Exception as e:
            logger.error(f'/tomorrow 失敗：{e}\n{traceback.format_exc()}')
            message = f'⚠️ 無法取得行程：`{type(e).__name__}: {e}`'
        await interaction.followup.send(message)


async def setup(bot: commands.Bot):
    await bot.add_cog(CalendarCog(bot))
