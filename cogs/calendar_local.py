import discord
from discord.ext import commands, tasks
from discord import app_commands
import caldav
import icalendar
import asyncio
import calendar as calendar_module
import datetime
import pytz
import os
import json
import logging
import traceback

from . import preflight_data

logger = logging.getLogger(__name__)

TAIPEI_TZ = pytz.timezone('Asia/Taipei')
SKIP_CALENDARS = {'提醒事項 ⚠️', 'Siri建議'}
EVA_CALENDAR_NAME = 'EVA Calander'
PREFLIGHT_REMINDER_HOURS = 4
REPORT_HOURS_BEFORE_TAKEOFF = 2
SCHEDULE_SYNC_WINDOW_DAYS = 45
_STATE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'state')
STATE_FILE = os.path.join(_STATE_DIR, 'schedule_state.json')
# 記錄每個每日推播（morning/todo/nightly）最後成功送出的「當地日期」，用來去重 + 補送——
# 排程當下若 bot 剛好離線（Mac 睡醒/斷網）就不會整天漏掉，等連回來、還在時段內會自動補一次。
DAILY_PUSH_FILE = os.path.join(_STATE_DIR, 'daily_pushes.json')
# 手動指定所在地（選填）：{"airport":"BKK"} 或 {"tz":"Asia/Bangkok","expires":"2026-07-20"}。
# 有設且未過期時，優先於用班表自動推斷。
LOCATION_OVERRIDE_FILE = os.path.join(_STATE_DIR, 'location_override.json')
HOME_AIRPORT = 'TPE'  # 推斷不出來時的預設所在地（母基地）
# 各推播的「當地時段」窗（當地時）：到點才送，超過就不補（避免推過時內容）。
MORNING_START_HOUR, MORNING_UNTIL_HOUR = 7, 11


def _make_client() -> caldav.DAVClient:
    return caldav.DAVClient(
        url='https://caldav.icloud.com',
        username=os.environ['ICLOUD_USERNAME'],
        password=os.environ['ICLOUD_PASSWORD']
    )


def _to_taipei(dt: datetime.datetime) -> datetime.datetime:
    return dt.astimezone(TAIPEI_TZ) if dt.tzinfo else TAIPEI_TZ.localize(dt)


def _parse_vevents(raw_items) -> list[dict]:
    events = []
    for item in raw_items:
        try:
            cal_obj = icalendar.Calendar.from_ical(item.data)
            for component in cal_obj.walk():
                if component.name != 'VEVENT':
                    continue
                dtend_prop = component.get('DTEND')
                location = component.get('LOCATION')
                events.append({
                    'uid': str(component.get('UID', '')),
                    'summary': str(component.get('SUMMARY', '（無標題）')),
                    'location': str(location) if location else None,
                    'dtstart': component.get('DTSTART').dt,
                    'dtend': dtend_prop.dt if dtend_prop else None,
                })
        except Exception:
            pass
    return events


def _sort_key(ev: dict):
    dtstart = ev['dtstart']
    if isinstance(dtstart, datetime.datetime):
        return _to_taipei(dtstart)
    return datetime.datetime.combine(dtstart, datetime.time.min, tzinfo=TAIPEI_TZ)


def _event_date(ev: dict) -> datetime.date:
    dtstart = ev['dtstart']
    if isinstance(dtstart, datetime.date) and not isinstance(dtstart, datetime.datetime):
        return dtstart
    return _to_taipei(dtstart).date()


def is_flight(event: dict) -> bool:
    return bool(event.get('location')) and '-' in event['location']


def _merge_flight_segments(events: list[dict]) -> list[dict]:
    """EVA 匯出的長程航班若跨過午夜，會被拆成兩筆同名同航線的事件
    （前段 DTEND 剛好等於後段 DTSTART）。這裡把它們接回同一趟。
    比對只在飛行事件之間進行，避免被其他行事曆的事件插隊。"""
    flights = [dict(ev) for ev in events if is_flight(ev)]
    others = [ev for ev in events if not is_flight(ev)]

    merged_flights: list[dict] = []
    for ev in flights:
        if merged_flights:
            prev = merged_flights[-1]
            if (ev['summary'] == prev['summary']
                    and ev['location'] == prev['location']
                    and isinstance(prev.get('dtend'), datetime.datetime)
                    and isinstance(ev.get('dtstart'), datetime.datetime)
                    and prev['dtend'] == ev['dtstart']):
                prev['dtend'] = ev['dtend']
                continue
        merged_flights.append(ev)

    result = merged_flights + others
    result.sort(key=_sort_key)
    return result


def get_calendar_events(cal_name: str, start: datetime.date, end: datetime.date) -> list[dict]:
    client = _make_client()
    principal = client.principal()
    fetch_start = start - datetime.timedelta(days=1)
    start_dt = datetime.datetime.combine(fetch_start, datetime.time.min, tzinfo=TAIPEI_TZ)
    end_dt = datetime.datetime.combine(end, datetime.time.max, tzinfo=TAIPEI_TZ)

    for cal in principal.calendars():
        if cal.get_display_name() != cal_name:
            continue
        raw = cal.date_search(start=start_dt, end=end_dt, expand=True)
        events = _parse_vevents(raw)
        events.sort(key=_sort_key)
        events = _merge_flight_segments(events)
        return [ev for ev in events if _event_date(ev) >= start]
    return []


def get_events_range(start: datetime.date, end: datetime.date) -> list[dict]:
    client = _make_client()
    principal = client.principal()
    calendars = principal.calendars()

    fetch_start = start - datetime.timedelta(days=1)
    start_dt = datetime.datetime.combine(fetch_start, datetime.time.min, tzinfo=TAIPEI_TZ)
    end_dt = datetime.datetime.combine(end, datetime.time.max, tzinfo=TAIPEI_TZ)

    events = []
    for cal in calendars:
        cal_name = cal.get_display_name()
        if cal_name in SKIP_CALENDARS:
            continue
        try:
            raw = cal.date_search(start=start_dt, end=end_dt, expand=True)
            events.extend(_parse_vevents(raw))
        except Exception as e:
            logger.warning(f'讀取 {cal_name} 失敗：{e}')

    events.sort(key=_sort_key)
    events = _merge_flight_segments(events)
    return [ev for ev in events if _event_date(ev) >= start]


def get_events(date: datetime.date) -> list[dict]:
    return get_events_range(date, date)


def get_upcoming_flights(days: int = SCHEDULE_SYNC_WINDOW_DAYS) -> list[dict]:
    """抓未來一段時間內的航班（依起飛時間排序），供飛行提醒使用。"""
    today = datetime.datetime.now(TAIPEI_TZ).date()
    end = today + datetime.timedelta(days=days)
    events = get_calendar_events(EVA_CALENDAR_NAME, today, end)
    flights = [ev for ev in events if is_flight(ev) and isinstance(ev.get('dtstart'), datetime.datetime)]
    flights.sort(key=_sort_key)
    return flights


def pick_next_flight(flights: list[dict]) -> dict | None:
    """挑出『下一趟』航班：優先未起飛的最近一趟，都起飛了則回最近剛飛的一趟。"""
    now = datetime.datetime.now(TAIPEI_TZ)
    future = [f for f in flights if _to_taipei(f['dtstart']) >= now]
    if future:
        return future[0]
    return flights[-1] if flights else None


def format_event(event: dict) -> str:
    dtstart = event['dtstart']
    summary = event['summary']
    location = event.get('location')

    if isinstance(dtstart, datetime.date) and not isinstance(dtstart, datetime.datetime):
        return f'📅 {summary}（全天）'

    dtend = event.get('dtend')

    if is_flight(event):
        t = preflight_data.flight_times(event, REPORT_HOURS_BEFORE_TAKEOFF)
        origin, _, dest = location.partition('-')
        line = f'✈️ **{summary}** {origin}→{dest}\n'
        # 起飛時間以出發地當地時間為主；若與台北不同再補台北時間
        dep = f'`{t["dep_local"]}`（{origin}當地）'
        if not t.get('dep_same_as_taipei'):
            dep += f' / `{t["dep_taipei"]}`（台北）'
        line += f'　報到 `{t["report_local"]}`　起飛 {dep} · `{t["dep_z"]}`'
        if t.get('total'):
            line += f'　總時長 {t["total"]}'
        return line

    start_taipei = _to_taipei(dtstart)
    time_str = start_taipei.strftime('%H:%M')
    if isinstance(dtend, datetime.datetime):
        end_taipei = _to_taipei(dtend)
        time_str += f'－{end_taipei.strftime("%H:%M")}'
    return f'🕐 `{time_str}` {summary}'


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


def build_range_message(start: datetime.date, end: datetime.date, events: list[dict]) -> str:
    weekdays = ['一', '二', '三', '四', '五', '六', '日']

    by_day: dict[datetime.date, list[dict]] = {}
    for ev in events:
        dtstart = ev['dtstart']
        d = dtstart if isinstance(dtstart, datetime.date) and not isinstance(dtstart, datetime.datetime) else dtstart.date()
        by_day.setdefault(d, []).append(ev)

    lines = [f'📆 **{start.strftime("%Y/%m/%d")} ～ {end.strftime("%Y/%m/%d")}**']
    if not by_day:
        lines.append('\n這段期間沒有航班 😊')
        return '\n'.join(lines)

    d = start
    while d <= end:
        day_events = by_day.get(d)
        if day_events:
            weekday = weekdays[d.weekday()]
            lines.append(f'\n**{d.strftime("%m/%d")}（週{weekday}）**')
            for ev in day_events:
                lines.append(format_event(ev))
        d += datetime.timedelta(days=1)
    return '\n'.join(lines)


async def send_long_message(sender, text: str, limit: int = 1900):
    if len(text) <= limit:
        await sender(text)
        return
    chunk = ''
    for line in text.split('\n'):
        if chunk and len(chunk) + len(line) + 1 > limit:
            await sender(chunk)
            chunk = line
        else:
            chunk = f'{chunk}\n{line}' if chunk else line
    if chunk:
        await sender(chunk)


class CategorySelect(discord.ui.Select):
    """單一分類（如「每趟」）的勾選下拉；只管自己這段的項目。"""

    def __init__(self, parent, category, entries):
        self._parent = parent
        self._idxs = [gi for gi, _ in entries]
        options = [
            discord.SelectOption(label=text[:100], value=str(gi),
                                 default=gi in parent.checked)
            for gi, text in entries[:25]
        ]
        super().__init__(
            placeholder=f'〔{category}〕勾選已準備好的…（可多選）',
            min_values=0, max_values=len(options), options=options,
        )

    async def callback(self, interaction):
        selected = {int(v) for v in self.values}
        for gi in self._idxs:
            self._parent.checked.discard(gi)
        self._parent.checked |= selected
        for opt in self.options:
            opt.default = int(opt.value) in self._parent.checked
        await interaction.response.edit_message(embed=self._parent.build_embed(), view=self._parent)


class PreflightChecklistView(discord.ui.View):
    """飛行提醒卡片（單一 Embed）+ 每個分類各一個下拉勾選清單。
    航班資訊、天氣、NOTAM、提醒清單全在同一則訊息，只會有一個頭貼。
    勾選狀態存記憶體，bot 持續執行期間有效（重啟後會重置）。"""

    FIELD_LIMIT = 1024
    MAX_SELECTS = 5  # Discord 一個訊息最多 5 排元件

    def __init__(self, data, note=None):
        super().__init__(timeout=None)
        self.data = data
        self.note = note
        self.items = data.get('items', [])
        self.checked = set()

        # 依分類分組（保留順序）
        self.cats = {}
        for idx, it in enumerate(self.items):
            self.cats.setdefault(it['category'], []).append((idx, it['text']))

        for category, entries in list(self.cats.items())[:self.MAX_SELECTS]:
            self.add_item(CategorySelect(self, category, entries))

    def _cat_progress(self, entries):
        done = sum(1 for gi, _ in entries if gi in self.checked)
        return done, len(entries)

    def _checklist_chunks(self):
        """把 ☐/☑ 清單依分類、且不超過 embed 欄位長度上限，切成幾塊。"""
        chunks, cur = [], ''
        for category, entries in self.cats.items():
            done, total = self._cat_progress(entries)
            body = f'**〔{category}〕{done}/{total}**\n' + ''.join(
                f'{"☑" if gi in self.checked else "☐"} {text}\n' for gi, text in entries
            )
            if cur and len(cur) + len(body) > self.FIELD_LIMIT:
                chunks.append(cur.rstrip())
                cur = body
            else:
                cur += body
        if cur:
            chunks.append(cur.rstrip())
        return chunks or ['（無）']

    def build_embed(self):
        embed = discord.Embed(
            title=self.data.get('title', '飛行提醒'),
            description=self.data.get('tags_line') or None,
            color=0x2b6cb0,
        )
        if self.note:
            embed.set_author(name=self.note)

        # 時間：報到 / 起飛 / 抵達 / 總時長，做成一格一格好觀看。
        # 大字為「當地時間」；若與台北不同，第二行補上台北時間 + Zulu。
        t = self.data.get('times', {})
        origin = t.get('origin') or ''
        dest = t.get('dest') or ''
        if t.get('report_local'):
            sub = '' if t.get('dep_same_as_taipei') else f'台北 {t.get("report_taipei", "")}'
            embed.add_field(name=f'🛫 報到（{origin}）',
                            value=f'## {t["report_local"]}' + (f'\n{sub}' if sub else ''),
                            inline=True)
        if t.get('dep_local'):
            extra = t.get('dep_z', '')
            if not t.get('dep_same_as_taipei'):
                extra = f'台北 {t.get("dep_taipei", "")} · {extra}'
            embed.add_field(name=f'✈️ 起飛（{origin}）',
                            value=f'## {t["dep_local"]}\n{extra}', inline=True)
        if t.get('arr_local'):
            extra = t.get('arr_z', '')
            if not t.get('arr_same_as_taipei'):
                extra = f'台北 {t.get("arr_taipei", "")} · {extra}'
            embed.add_field(name=f'🛬 抵達（{dest}）',
                            value=f'## {t["arr_local"]}\n{extra}', inline=True)
        if t.get('total'):
            embed.add_field(name='⏱️ 總時長', value=f'## {t["total"]}', inline=True)

        for name, value in self.data.get('weather', []):
            embed.add_field(name=name, value=value, inline=False)
        notam = self.data.get('notam')
        if notam:
            embed.add_field(name=notam[0], value=notam[1], inline=False)

        chunks = self._checklist_chunks()
        title = f'✅ 提醒清單（下方各段選單勾選）\u3000{len(self.checked)}/{len(self.items)}'
        for i, chunk in enumerate(chunks):
            embed.add_field(name=(title if i == 0 else '\u200b'), value=chunk, inline=False)

        footer = '▶／黃底＝起飛所在時段 · 勾選狀態 bot 重啟後會重置'
        if len(self.cats) > self.MAX_SELECTS:
            footer += f' · 分類超過 {self.MAX_SELECTS} 段，只有前 {self.MAX_SELECTS} 段可勾選'
        embed.set_footer(text=footer)
        return embed

def _load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {'snapshot': {}, 'reminded': [], 'initialized': False}
    try:
        with open(STATE_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        logger.error(traceback.format_exc())
        return {'snapshot': {}, 'reminded': [], 'initialized': False}


def _save_state(state: dict):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def _load_daily_pushes() -> dict:
    try:
        with open(DAILY_PUSH_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def _mark_push_sent(key: str, local_date_iso: str):
    data = _load_daily_pushes()
    data[key] = local_date_iso
    os.makedirs(_STATE_DIR, exist_ok=True)
    with open(DAILY_PUSH_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _load_location_override() -> dict | None:
    """讀手動指定的所在地；過期或讀不到回 None。"""
    try:
        with open(LOCATION_OVERRIDE_FILE, 'r', encoding='utf-8') as f:
            d = json.load(f)
    except Exception:
        return None
    exp = d.get('expires')
    if exp and datetime.datetime.now(TAIPEI_TZ).date().isoformat() > str(exp):
        return None
    return d


# 位置推斷有快取，避免每次檢查都打 CalDAV（位置變動慢，快取 30 分鐘）。
_LOCATION_CACHE = {'ts': None, 'airport': None}


def _detect_location_airport() -> str:
    """從班表推斷『現在人在哪個機場』：最後一班已起飛航班的目的地；推不出來回母基地。"""
    try:
        today = datetime.datetime.now(TAIPEI_TZ).date()
        events = get_calendar_events(
            EVA_CALENDAR_NAME, today - datetime.timedelta(days=3), today + datetime.timedelta(days=2)
        )
    except Exception:
        logger.warning('推斷位置時讀班表失敗，退回母基地', exc_info=True)
        return HOME_AIRPORT
    now = datetime.datetime.now(pytz.utc)
    legs = []
    for ev in events:
        if not is_flight(ev) or not isinstance(ev.get('dtstart'), datetime.datetime):
            continue
        t = preflight_data.flight_times(ev)
        if t.get('dep_utc') is None:
            continue
        legs.append((t['dep_utc'], t.get('dest') or HOME_AIRPORT))
    legs.sort(key=lambda x: x[0])
    loc = HOME_AIRPORT
    for dep_utc, dest in legs:
        if dep_utc <= now:
            loc = dest  # 這班已起飛→人在（或正前往）目的地
        else:
            break       # 之後的航班還沒發生
    return loc or HOME_AIRPORT


def current_location():
    """回傳 (tz, 機場代碼, 來源)。優先用手動指定，其次班表推斷（快取），最後母基地台北。"""
    override = _load_location_override()
    if override:
        tzname = override.get('tz')
        airport = override.get('airport')
        if not tzname and airport:
            tz = preflight_data.airport_tz(airport)
            if tz:
                return tz, airport, 'override'
        if tzname:
            try:
                return pytz.timezone(tzname), airport or '?', 'override'
            except Exception:
                pass
    now = datetime.datetime.now(pytz.utc)
    cached = _LOCATION_CACHE['ts']
    if cached is None or (now - cached).total_seconds() > 1800:
        _LOCATION_CACHE['airport'] = _detect_location_airport()
        _LOCATION_CACHE['ts'] = now
    airport = _LOCATION_CACHE['airport']
    tz = preflight_data.airport_tz(airport) or TAIPEI_TZ
    return tz, airport, 'schedule'


def push_due(key: str, start_hour: int, until_hour: int, tz=None):
    """判斷某個每日推播現在該不該送。tz=None 代表用『目前所在地時區』（隨班表跑）。
    回傳 (該送?, tz, 地點代碼)。"""
    place = None
    if tz is None:
        tz, place, _ = current_location()
    now_local = datetime.datetime.now(tz)
    due = (start_hour <= now_local.hour < until_hour
           and _load_daily_pushes().get(key) != now_local.date().isoformat())
    return due, tz, place


class CalendarLocalCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._pending_reminders: set[str] = set()
        self._morning_lock = asyncio.Lock()  # 避免同時送出而重複
        self.morning_check.start()
        self.schedule_sync.start()

    def cog_unload(self):
        self.morning_check.cancel()
        self.schedule_sync.cancel()

    def _morning_channel(self):
        # 早報送「早晨推播」頻道（MORNING_CHANNEL_ID），沒設就退回 DISCORD_CHANNEL_ID。
        return self.bot.get_channel(int(os.environ.get('MORNING_CHANNEL_ID') or os.environ['DISCORD_CHANNEL_ID']))

    async def _do_morning(self, channel) -> bool:
        """實際送出早報（跑 /morning，失敗退回今日行事曆備援）。有成功送出回 True。"""
        bridge = self.bot.get_cog('ClaudeBridgeCog')
        if bridge is not None:
            try:
                result, is_error = await bridge._run_claude('/morning', str(channel.id))
                if not is_error:
                    await send_long_message(channel.send, result)
                    return True
                logger.error(f'每日 /morning 回報錯誤，改推行事曆備援：{result[:200]}')
            except Exception as e:
                logger.error(f'每日 /morning 失敗，改推行事曆備援：{e}\n{traceback.format_exc()}')
        # 備援：claude 不可用時，退回原本的今日行事曆推播。
        today = datetime.datetime.now(TAIPEI_TZ).date()
        try:
            events = await asyncio.to_thread(get_events, today)
            message = build_message(today, events)
        except Exception as e:
            logger.error(f'每日推送失敗：{e}\n{traceback.format_exc()}')
            message = '⚠️ 無法取得今日行程。'
        await channel.send(message)
        return True

    @tasks.loop(minutes=10)
    async def morning_check(self):
        """每 10 分檢查一次：若『你目前所在地』的當地時間已過 07:00（到 MORNING_UNTIL_HOUR
        之間）、而今天還沒送早報，就送一次。位置由班表推斷，所以到外站會自動用當地時間；
        排程當下離線也會在連回來後補送。一天只送一次。"""
        due, tz, place = push_due('morning', MORNING_START_HOUR, MORNING_UNTIL_HOUR)
        if not due:
            return
        async with self._morning_lock:
            # 進鎖後再確認一次（避免兩個 tick 同時送）
            if _load_daily_pushes().get('morning') == datetime.datetime.now(tz).date().isoformat():
                return
            channel = self._morning_channel()
            if not channel:
                return  # 還沒連上，下個週期再補
            if await self._do_morning(channel):
                _mark_push_sent('morning', datetime.datetime.now(tz).date().isoformat())
                logger.info(f'早報已送（所在地 {place} / {tz}）')

    @morning_check.before_loop
    async def before_morning_check(self):
        await self.bot.wait_until_ready()

    @tasks.loop(minutes=30)
    async def schedule_sync(self):
        channel = self.bot.get_channel(int(os.environ['DISCORD_CHANNEL_ID']))
        today = datetime.datetime.now(TAIPEI_TZ).date()
        end = today + datetime.timedelta(days=SCHEDULE_SYNC_WINDOW_DAYS)

        try:
            events = await asyncio.to_thread(get_calendar_events, EVA_CALENDAR_NAME, today, end)
        except Exception as e:
            logger.error(f'班表同步失敗：{e}\n{traceback.format_exc()}')
            return

        state = _load_state()
        old_snapshot = state.get('snapshot', {})
        first_run = not state.get('initialized', False)
        new_snapshot = {}
        diffs = []

        for ev in events:
            uid = ev['uid']
            entry = {
                'summary': ev['summary'],
                'location': ev['location'],
                'dtstart': ev['dtstart'].isoformat(),
                'dtend': ev['dtend'].isoformat() if ev['dtend'] else None,
            }
            new_snapshot[uid] = entry

            if not first_run:
                old = old_snapshot.get(uid)
                if old is None:
                    diffs.append(f'🆕 新增班表\n{format_event(ev)}')
                elif old != entry:
                    diffs.append(f'♻️ 班表異動\n舊：{old.get("summary")}　{old.get("dtstart")}\n新：{format_event(ev)}')

        if not first_run:
            today_iso = today.isoformat()
            for uid, old in old_snapshot.items():
                if uid not in new_snapshot and old.get('dtstart', '') >= today_iso:
                    diffs.append(f'❌ 取消班表\n{old.get("summary")}（原訂 {old.get("dtstart")}）')

        if diffs and channel:
            await send_long_message(channel.send, '📋 **班表更新通知**\n\n' + '\n\n'.join(diffs))

        reminded = set(state.get('reminded', []))
        now = datetime.datetime.now(TAIPEI_TZ)
        for ev in events:
            if not is_flight(ev):
                continue
            dtstart = ev.get('dtstart')
            if not isinstance(dtstart, datetime.datetime):
                continue
            uid = ev['uid']
            if uid in reminded or uid in self._pending_reminders:
                continue
            takeoff = _to_taipei(dtstart)
            reminder_time = takeoff - datetime.timedelta(hours=PREFLIGHT_REMINDER_HOURS)
            if reminder_time <= now:
                continue
            self._pending_reminders.add(uid)
            asyncio.create_task(self._fire_reminder(uid, ev, reminder_time, channel))

        latest_state = _load_state()
        latest_state['snapshot'] = new_snapshot
        latest_state['initialized'] = True
        _save_state(latest_state)

    @schedule_sync.before_loop
    async def before_schedule_sync(self):
        await self.bot.wait_until_ready()

    async def _fire_reminder(self, uid: str, ev: dict, reminder_time: datetime.datetime, channel):
        wait_seconds = (reminder_time - datetime.datetime.now(TAIPEI_TZ)).total_seconds()
        if wait_seconds > 0:
            await asyncio.sleep(wait_seconds)
        if channel:
            try:
                await self._send_preflight(
                    channel.send, ev,
                    note=f'⏰ 起飛前 {PREFLIGHT_REMINDER_HOURS} 小時提醒',
                )
            except Exception:
                logger.error(traceback.format_exc())

        state = _load_state()
        reminded = set(state.get('reminded', []))
        reminded.add(uid)
        state['reminded'] = list(reminded)
        _save_state(state)
        self._pending_reminders.discard(uid)

    # /today、/tomorrow 已移除（2026-07-14，改用 /morning 早報 + /todo 待辦看板）

    @app_commands.command(name='schedule', description='查詢從今天到月底的航班')
    async def cmd_schedule(self, interaction: discord.Interaction):
        await interaction.response.defer()
        today = datetime.datetime.now(TAIPEI_TZ).date()
        last_day = calendar_module.monthrange(today.year, today.month)[1]
        end_date = today.replace(day=last_day)
        try:
            events = await asyncio.to_thread(get_calendar_events, EVA_CALENDAR_NAME, today, end_date)
            flights = [ev for ev in events if is_flight(ev)]
            message = build_range_message(today, end_date, flights)
        except Exception as e:
            logger.error(traceback.format_exc())
            message = f'⚠️ 錯誤：{e}'
            await interaction.followup.send(message)
            return
        await send_long_message(interaction.followup.send, message)

    async def _send_preflight(self, sender, flight: dict,
                              flights: list[dict] | None = None, note: str | None = None):
        """組出並送出一趟航班的飛行提醒——整合成一則 Embed 卡片（只有一個頭貼），
        附下拉多選勾選清單。天氣抓取在背景執行緒，不卡住 bot。"""
        if flights is None:
            flights = await asyncio.to_thread(get_upcoming_flights)
        config = preflight_data.load_checklist()
        data = await asyncio.to_thread(
            preflight_data.build_preflight,
            flight, flights, config, REPORT_HOURS_BEFORE_TAKEOFF,
        )
        view = PreflightChecklistView(data, note=note)
        await sender(embed=view.build_embed(), view=view)

    @app_commands.command(name='preflight', description='顯示下一趟航班的飛行提醒清單（含天氣）')
    async def cmd_preflight(self, interaction: discord.Interaction):
        await interaction.response.defer()
        try:
            flights = await asyncio.to_thread(get_upcoming_flights)
            flight = pick_next_flight(flights)
            if not flight:
                await interaction.followup.send('未來 45 天內沒有航班 😊')
                return
            await self._send_preflight(interaction.followup.send, flight, flights)
        except Exception as e:
            logger.error(traceback.format_exc())
            await interaction.followup.send(f'⚠️ 錯誤：{e}')


async def setup(bot: commands.Bot):
    await bot.add_cog(CalendarLocalCog(bot))
