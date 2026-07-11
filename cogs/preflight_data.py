"""飛行提醒清單（preflight）核心邏輯。

這裡都是「純函式」，不碰 Discord，方便單獨測試：
- 讀取可編輯的 checklist.json
- 判斷一趟航班的類型（長程 / 外站 / 紅眼 / 過夜）
- 抓取起降機場的天氣（METAR / TAF，來源：aviationweather.gov，免費免帳號）
- 把 TAF 拆成一段一段（每個時間段一行），並標出起飛時間落在哪一段
- 組出提醒訊息的各個區塊
"""

import datetime
import json
import logging
import os
import re
import time

import pytz
import requests

logger = logging.getLogger(__name__)

TAIPEI_TZ = pytz.timezone('Asia/Taipei')
ESC = '\x1b'  # Discord ansi 色塊用的 escape 字元

CHECKLIST_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'checklist.json'
)

# EVA 常飛航點 IATA(3碼) → ICAO(4碼)。天氣 API 只吃 ICAO。
# 查不到的機場會直接顯示原始 IATA 代碼，不會讓功能壞掉。
IATA_TO_ICAO = {
    # 台灣
    'TPE': 'RCTP', 'KHH': 'RCKH', 'RMQ': 'RCMQ', 'TSA': 'RCSS', 'HUN': 'RCYU',
    # 日本
    'NRT': 'RJAA', 'HND': 'RJTT', 'KIX': 'RJBB', 'NGO': 'RJGG', 'FUK': 'RJFF',
    'CTS': 'RJCC', 'OKA': 'ROAH', 'KOJ': 'RJFK', 'SDJ': 'RJSS', 'KMJ': 'RJFT',
    'HIJ': 'RJOA', 'TAK': 'RJOT', 'KMQ': 'RJNK', 'AXT': 'RJSK', 'AOJ': 'RJSA',
    # 韓國
    'ICN': 'RKSI', 'GMP': 'RKSS', 'PUS': 'RKPK',
    # 中國大陸
    'PVG': 'ZSPD', 'PEK': 'ZBAA', 'PKX': 'ZBAD', 'CAN': 'ZGGG', 'SZX': 'ZGSZ',
    'HGH': 'ZSHC', 'XMN': 'ZSAM', 'NKG': 'ZSNJ', 'TAO': 'ZSQD', 'WUH': 'ZHHH',
    'CTU': 'ZUUU', 'CKG': 'ZUCK', 'CGO': 'ZHCC', 'FOC': 'ZSFZ',
    # 港澳
    'HKG': 'VHHH', 'MFM': 'VMMC',
    # 東南亞
    'BKK': 'VTBS', 'DMK': 'VTBD', 'HKT': 'VTSP', 'SIN': 'WSSS', 'KUL': 'WMKK',
    'MNL': 'RPLL', 'CEB': 'RPVM', 'SGN': 'VVTS', 'HAN': 'VVNB', 'DAD': 'VVDN',
    'RGN': 'VYYY', 'PNH': 'VDPP', 'REP': 'VDSR', 'DPS': 'WADD', 'CGK': 'WIII',
    'SUB': 'WARR', 'BWN': 'WBSB', 'VTE': 'VLVT',
    # 南亞
    'DEL': 'VIDP', 'BOM': 'VABB',
    # 大洋洲
    'BNE': 'YBBN', 'SYD': 'YSSY', 'MEL': 'YMML', 'AKL': 'NZAA',
    # 北美
    'LAX': 'KLAX', 'SFO': 'KSFO', 'SEA': 'KSEA', 'ONT': 'KONT', 'IAD': 'KIAD',
    'JFK': 'KJFK', 'EWR': 'KEWR', 'ORD': 'KORD', 'YVR': 'CYVR', 'YYZ': 'CYYZ',
    'HNL': 'PHNL', 'GUM': 'PGUM', 'SJC': 'KSJC', 'DFW': 'KDFW',
    # 歐洲
    'LHR': 'EGLL', 'CDG': 'LFPG', 'VIE': 'LOWW', 'AMS': 'EHAM', 'MUC': 'EDDM',
    'MXP': 'LIMC',
}

TAG_LABELS = {
    'long_haul': '長程',
    'outstation': '外站出發',
    'red_eye': '紅眼/清晨',
    'overnight': '過夜',
}

SECTION_TITLES = {
    'always': '每趟',
    'long_haul': '長程',
    'outstation': '外站',
    'red_eye': '紅眼/清晨',
    'overnight': '過夜',
}

# 機場 IATA → IANA 時區名。EVA 行事曆的起降時間其實是「各機場當地時間」，
# 卻被標成 +08，所以要用這張表把時間換算成正確的絕對時間（IANA 會自動處理日光節約）。
IATA_TO_TZ = {
    # 台灣
    'TPE': 'Asia/Taipei', 'KHH': 'Asia/Taipei', 'RMQ': 'Asia/Taipei',
    'TSA': 'Asia/Taipei', 'HUN': 'Asia/Taipei',
    # 日本
    'NRT': 'Asia/Tokyo', 'HND': 'Asia/Tokyo', 'KIX': 'Asia/Tokyo', 'NGO': 'Asia/Tokyo',
    'FUK': 'Asia/Tokyo', 'CTS': 'Asia/Tokyo', 'OKA': 'Asia/Tokyo', 'KOJ': 'Asia/Tokyo',
    'SDJ': 'Asia/Tokyo', 'KMJ': 'Asia/Tokyo', 'HIJ': 'Asia/Tokyo', 'TAK': 'Asia/Tokyo',
    'KMQ': 'Asia/Tokyo', 'AXT': 'Asia/Tokyo', 'AOJ': 'Asia/Tokyo',
    # 韓國
    'ICN': 'Asia/Seoul', 'GMP': 'Asia/Seoul', 'PUS': 'Asia/Seoul',
    # 中國大陸
    'PVG': 'Asia/Shanghai', 'PEK': 'Asia/Shanghai', 'PKX': 'Asia/Shanghai',
    'CAN': 'Asia/Shanghai', 'SZX': 'Asia/Shanghai', 'HGH': 'Asia/Shanghai',
    'XMN': 'Asia/Shanghai', 'NKG': 'Asia/Shanghai', 'TAO': 'Asia/Shanghai',
    'WUH': 'Asia/Shanghai', 'CTU': 'Asia/Shanghai', 'CKG': 'Asia/Shanghai',
    'CGO': 'Asia/Shanghai', 'FOC': 'Asia/Shanghai',
    # 港澳
    'HKG': 'Asia/Hong_Kong', 'MFM': 'Asia/Macau',
    # 東南亞
    'BKK': 'Asia/Bangkok', 'DMK': 'Asia/Bangkok', 'HKT': 'Asia/Bangkok',
    'SIN': 'Asia/Singapore', 'KUL': 'Asia/Kuala_Lumpur',
    'MNL': 'Asia/Manila', 'CEB': 'Asia/Manila',
    'SGN': 'Asia/Ho_Chi_Minh', 'HAN': 'Asia/Ho_Chi_Minh', 'DAD': 'Asia/Ho_Chi_Minh',
    'RGN': 'Asia/Yangon', 'PNH': 'Asia/Phnom_Penh', 'REP': 'Asia/Phnom_Penh',
    'DPS': 'Asia/Makassar', 'CGK': 'Asia/Jakarta', 'SUB': 'Asia/Jakarta',
    'BWN': 'Asia/Brunei', 'VTE': 'Asia/Vientiane',
    # 南亞
    'DEL': 'Asia/Kolkata', 'BOM': 'Asia/Kolkata',
    # 大洋洲
    'BNE': 'Australia/Brisbane', 'SYD': 'Australia/Sydney', 'MEL': 'Australia/Melbourne',
    'AKL': 'Pacific/Auckland',
    # 北美
    'LAX': 'America/Los_Angeles', 'SFO': 'America/Los_Angeles', 'SEA': 'America/Los_Angeles',
    'ONT': 'America/Los_Angeles', 'SJC': 'America/Los_Angeles',
    'IAD': 'America/New_York', 'JFK': 'America/New_York', 'EWR': 'America/New_York',
    'ORD': 'America/Chicago', 'DFW': 'America/Chicago',
    'YVR': 'America/Vancouver', 'YYZ': 'America/Toronto',
    'HNL': 'Pacific/Honolulu', 'GUM': 'Pacific/Guam',
    # 歐洲
    'LHR': 'Europe/London', 'CDG': 'Europe/Paris', 'VIE': 'Europe/Vienna',
    'AMS': 'Europe/Amsterdam', 'MUC': 'Europe/Berlin', 'MXP': 'Europe/Rome',
}


def to_icao(iata: str) -> str | None:
    """把 IATA 三碼轉成 ICAO 四碼；查不到回 None。"""
    return IATA_TO_ICAO.get(iata.strip().upper())


def airport_tz(iata: str):
    """回傳機場的 IANA 時區物件；查不到回 None。"""
    name = IATA_TO_TZ.get((iata or '').strip().upper())
    return pytz.timezone(name) if name else None


def _resolve_local(naive: datetime.datetime, iata: str) -> datetime.datetime:
    """把『當地牆上時鐘時間』（naive）依機場時區換算成有時區的絕對時間。
    查不到時區的機場就退回台北時間（等同舊行為，不會壞）。"""
    tz = airport_tz(iata) or TAIPEI_TZ
    return tz.localize(naive)


def flight_times(flight: dict, report_hours_before: int = 2) -> dict:
    """把一趟航班的起降時間（行事曆存的是各機場當地時間、卻標成 +08）
    換算成正確的絕對時間，回傳各種顯示用字串與判斷用數值。"""
    res = {
        'origin': '', 'dest': '',
        'report_local': None, 'report_taipei': None,
        'dep_local': None, 'dep_taipei': None, 'dep_z': None,
        'arr_local': None, 'arr_taipei': None, 'arr_z': None,
        'total': None, 'total_hours': None,
        'dep_hour_local': None, 'dep_utc': None,
        'dep_same_as_taipei': True, 'arr_same_as_taipei': True,
    }
    origin, dest = _route(flight)
    res['origin'], res['dest'] = origin, dest
    dtstart = flight.get('dtstart')
    dtend = flight.get('dtend')
    if not isinstance(dtstart, datetime.datetime):
        return res

    naive_dep = dtstart.replace(tzinfo=None)
    dep_aware = _resolve_local(naive_dep, origin)
    dep_utc = dep_aware.astimezone(pytz.utc)
    dep_taipei = dep_aware.astimezone(TAIPEI_TZ)
    report_naive = naive_dep - datetime.timedelta(hours=report_hours_before)
    report_taipei = (dep_aware - datetime.timedelta(hours=report_hours_before)).astimezone(TAIPEI_TZ)

    res['dep_local'] = naive_dep.strftime('%H:%M')
    res['dep_taipei'] = dep_taipei.strftime('%H:%M')
    res['dep_z'] = dep_utc.strftime('%H:%MZ')
    res['report_local'] = report_naive.strftime('%H:%M')
    res['report_taipei'] = report_taipei.strftime('%H:%M')
    res['dep_hour_local'] = naive_dep.hour
    res['dep_utc'] = dep_utc
    res['dep_same_as_taipei'] = (res['dep_local'] == res['dep_taipei'])

    if isinstance(dtend, datetime.datetime):
        naive_arr = dtend.replace(tzinfo=None)
        arr_aware = _resolve_local(naive_arr, dest)
        arr_utc = arr_aware.astimezone(pytz.utc)
        arr_taipei = arr_aware.astimezone(TAIPEI_TZ)
        res['arr_local'] = naive_arr.strftime('%H:%M')
        res['arr_taipei'] = arr_taipei.strftime('%H:%M')
        res['arr_z'] = arr_utc.strftime('%H:%MZ')
        res['arr_same_as_taipei'] = (res['arr_local'] == res['arr_taipei'])
        delta = arr_utc - dep_utc
        res['total'] = _fmt_duration(delta)
        res['total_hours'] = delta.total_seconds() / 3600

    return res


def load_checklist() -> dict:
    """讀取可編輯的 checklist.json；壞掉時回一份安全的預設值。"""
    try:
        with open(CHECKLIST_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        logger.error('讀取 checklist.json 失敗，使用預設值', exc_info=True)
        return {
            'home_base': 'TPE',
            'long_haul_min_hours': 6,
            'red_eye_start_hour': 0,
            'red_eye_end_hour': 5,
            'checklist': {'always': ['護照、組員證、派遣文件']},
        }


def _route(flight: dict) -> tuple[str, str]:
    """從 location（如 'TPE-BKK'）拆出出發地、目的地 IATA 代碼。"""
    loc = flight.get('location') or ''
    origin, _, dest = loc.partition('-')
    return origin.strip().upper(), dest.strip().upper()


def classify_flight(flight: dict, all_flights: list[dict], config: dict) -> list[str]:
    """判斷這趟航班符合哪些提醒類型，回傳 tag 清單（always 一定包含）。"""
    tags = ['always']
    origin, dest = _route(flight)

    home_base = str(config.get('home_base', 'TPE')).upper()
    long_haul_min = float(config.get('long_haul_min_hours', 6))
    red_start = int(config.get('red_eye_start_hour', 0))
    red_end = int(config.get('red_eye_end_hour', 5))

    if origin and origin != home_base:
        tags.append('outstation')

    dtstart = flight.get('dtstart')
    dtend = flight.get('dtend')
    timing = flight_times(flight)

    # 長程：用換算過的『真實飛行時數』判斷（避免外站時區造成的偏誤）
    if timing['total_hours'] is not None and timing['total_hours'] >= long_haul_min:
        tags.append('long_haul')

    # 紅眼：用出發地『當地時間』的小時判斷（半夜/清晨報到起飛）
    if timing['dep_hour_local'] is not None and red_start <= timing['dep_hour_local'] < red_end:
        tags.append('red_eye')

    if isinstance(dtstart, datetime.datetime) and dest and dest != home_base:
        later = [
            f for f in all_flights
            if isinstance(f.get('dtstart'), datetime.datetime) and f['dtstart'] > dtstart
        ]
        later.sort(key=lambda f: f['dtstart'])
        if later:
            nxt = later[0]
            nxt_origin, _ = _route(nxt)
            arrive_date = (dtend or dtstart).astimezone(TAIPEI_TZ).date()
            nxt_date = nxt['dtstart'].astimezone(TAIPEI_TZ).date()
            if nxt_origin == dest and nxt_date > arrive_date:
                tags.append('overnight')

    return tags


def fetch_weather(icao_codes: list[str], timeout: int = 15, retries: int = 3) -> dict[str, dict]:
    """抓取多個機場的 METAR + TAF。回傳 {ICAO: {'metar':..., 'taf':...}}。
    aviationweather.gov 偶爾會回 502／逾時，所以每種各重試幾次；
    最後仍失敗就回空/部分結果，不丟例外，讓提醒不會整個壞掉。"""
    result: dict[str, dict] = {code: {} for code in icao_codes}
    if not icao_codes:
        return result
    ids = ','.join(icao_codes)
    base = 'https://aviationweather.gov/api/data'
    for kind, key in (('metar', 'metar'), ('taf', 'taf')):
        for attempt in range(1, retries + 1):
            try:
                r = requests.get(
                    f'{base}/{kind}', params={'ids': ids, 'format': 'json'}, timeout=timeout
                )
                r.raise_for_status()
                for item in r.json():
                    code = item.get('icaoId')
                    if code not in result:
                        continue
                    raw = item.get('rawOb') or item.get('rawTAF') or ''
                    result[code][key] = raw.strip()
                break  # 成功就跳出重試迴圈
            except Exception:
                logger.warning('抓取 %s 失敗（第 %d/%d 次）', kind, attempt, retries, exc_info=True)
                if attempt < retries:
                    time.sleep(1.5)
    return result


# ── TAF 分段解析 ────────────────────────────────────────────────

def _ddhh_to_utc(ddhh: str, issue_day: int, anchor: datetime.datetime) -> datetime.datetime | None:
    """把 TAF 的 DDHH（日+時，Zulu）轉成實際 UTC 時間。
    TAF 時間都在發布時間之後，所以日期若比發布日小，就是跨到下個月。
    小時可能是 24（代表隔天 00 時）。"""
    if not ddhh or len(ddhh) < 4:
        return None
    try:
        dd = int(ddhh[0:2])
        hh = int(ddhh[2:4])
    except ValueError:
        return None
    extra_day = 0
    if hh >= 24:
        hh -= 24
        extra_day = 1
    year, month = anchor.year, anchor.month
    if dd < issue_day:
        month += 1
        if month > 12:
            month = 1
            year += 1
    try:
        dt = datetime.datetime(year, month, dd, hh, tzinfo=pytz.utc)
    except ValueError:
        return None
    return dt + datetime.timedelta(days=extra_day)


def _is_change_indicator(tok: str) -> bool:
    return (bool(re.fullmatch(r'FM\d{6}', tok))
            or tok in ('BECMG', 'TEMPO', 'INTER')
            or bool(re.fullmatch(r'PROB\d{2}', tok)))


def parse_taf(raw: str, anchor: datetime.datetime) -> list[dict]:
    """把一份 TAF 拆成多個時間段。每段回 {'text', 'start', 'end'}（start/end 為 UTC）。
    BASE 與 FM 是『主要時段』，彼此接續；BECMG/TEMPO/PROB 各有自己的時間窗。"""
    tokens = raw.split()
    i = 0
    if i < len(tokens) and tokens[i] == 'TAF':
        i += 1
    while i < len(tokens) and tokens[i] in ('AMD', 'COR'):
        i += 1
    if i < len(tokens):  # ICAO
        i += 1
    issue_day = anchor.day
    if i < len(tokens) and re.fullmatch(r'\d{6}Z', tokens[i]):
        issue_day = int(tokens[i][0:2])
        i += 1
    vs = ve = None
    if i < len(tokens) and re.fullmatch(r'\d{4}/\d{4}', tokens[i]):
        vs, ve = tokens[i].split('/')
        i += 1
    ve_dt = _ddhh_to_utc(ve, issue_day, anchor) if ve else None

    base = []
    while i < len(tokens) and not _is_change_indicator(tokens[i]):
        base.append(tokens[i])
        i += 1

    raw_segs: list[tuple] = []  # (kind, start_ddhh, end_ddhh, text)
    if vs:
        raw_segs.append(('BASE', vs, None, (f'{vs}/{ve} ' + ' '.join(base)).strip()))

    while i < len(tokens):
        tok = tokens[i]
        m = re.fullmatch(r'FM(\d{6})', tok)
        if m:
            ddhh = m.group(1)[0:4]
            grp = [tok]
            i += 1
            while i < len(tokens) and not _is_change_indicator(tokens[i]):
                grp.append(tokens[i])
                i += 1
            raw_segs.append(('FM', ddhh, None, ' '.join(grp)))
        elif tok in ('BECMG', 'TEMPO', 'INTER') or re.fullmatch(r'PROB\d{2}', tok):
            grp = [tok]
            i += 1
            if tok.startswith('PROB') and i < len(tokens) and tokens[i] in ('TEMPO', 'INTER'):
                grp.append(tokens[i])
                i += 1
            win_s = win_e = None
            if i < len(tokens) and re.fullmatch(r'\d{4}/\d{4}', tokens[i]):
                win_s, win_e = tokens[i].split('/')
                grp.append(tokens[i])
                i += 1
            while i < len(tokens) and not _is_change_indicator(tokens[i]):
                grp.append(tokens[i])
                i += 1
            kind = 'PROB' if tok.startswith('PROB') else tok
            raw_segs.append((kind, win_s, win_e, ' '.join(grp)))
        else:
            i += 1

    # 主要時段（BASE/FM）的起點，用來算彼此的結束時間
    prevailing_starts = [
        _ddhh_to_utc(s[1], issue_day, anchor)
        for s in raw_segs if s[0] in ('BASE', 'FM')
    ]
    prevailing_starts = [d for d in prevailing_starts if d is not None]

    segments = []
    for kind, sd, ed, text in raw_segs:
        if kind in ('BASE', 'FM'):
            start_dt = _ddhh_to_utc(sd, issue_day, anchor) if sd else None
            end_dt = ve_dt
            if start_dt is not None:
                future = [d for d in prevailing_starts if d > start_dt]
                if future:
                    end_dt = min(future)
            segments.append({'text': text, 'start': start_dt, 'end': end_dt})
        else:
            segments.append({
                'text': text,
                'start': _ddhh_to_utc(sd, issue_day, anchor) if sd else None,
                'end': _ddhh_to_utc(ed, issue_day, anchor) if ed else None,
            })
    return segments


def _truncate(value: str, limit: int = 1024) -> str:
    """確保放進 embed 欄位不超過長度上限（超過就截斷並補上省略號）。"""
    if len(value) <= limit:
        return value
    return value[:limit - 20].rstrip() + '\n… （內容過長已截斷）'


def build_weather_field(iata: str, icao: str | None, entry: dict,
                        takeoff_utc: datetime.datetime | None,
                        anchor: datetime.datetime) -> tuple[str, str]:
    """組出單一機場的天氣，回傳 (欄位名稱, 欄位內容)。
    METAR + 分段縮排的 TAF，並把起飛落在的 TAF 時段以 ansi 黃底 + ▶ 標記。"""
    if not icao:
        return (f'{iata}', '查無對照代碼，請自行查詢天氣')

    metar = entry.get('metar')
    taf = entry.get('taf')

    lines = [f'METAR｜{metar}' if metar else 'METAR｜暫時抓不到，稍後再試']
    note = ''
    if taf:
        lines.append('TAF｜')
        segs = parse_taf(taf, anchor)
        matched = False
        for seg in segs:
            hit = (takeoff_utc is not None and seg['start'] is not None
                   and seg['end'] is not None and seg['start'] <= takeoff_utc < seg['end'])
            if hit:
                matched = True
                lines.append(f'{ESC}[1;30;43m▶ {seg["text"]}{ESC}[0m')
            else:
                lines.append(f'    {seg["text"]}')
        if takeoff_utc is not None and not matched:
            note = '\n（起飛時段未落在此 TAF 預報範圍內）'
    else:
        lines.append('TAF｜暫時抓不到，稍後再試')

    value = '```ansi\n' + '\n'.join(lines) + '\n```' + note
    return (f'{iata}（{icao}）', _truncate(value))


def get_checklist_items(flight: dict, all_flights: list[dict], config: dict,
                        tags: list[str] | None = None) -> list[dict]:
    """依航班類型挑出提醒項目，回傳扁平清單 [{'category', 'text'}, ...]。"""
    if tags is None:
        tags = classify_flight(flight, all_flights, config)
    checklist_cfg = config.get('checklist', {})
    items = []
    for tag in tags:
        for it in (checklist_cfg.get(tag) or []):
            items.append({'category': SECTION_TITLES.get(tag, tag), 'text': str(it)})
    return items


def _fmt_duration(td: datetime.timedelta) -> str:
    """把時間長度格式化成『Xh YYm』。"""
    total_min = int(td.total_seconds() // 60)
    h, m = divmod(total_min, 60)
    return f'{h}h {m:02d}m'


def build_preflight(flight: dict, all_flights: list[dict], config: dict,
                    report_hours_before: int = 2) -> dict:
    """組出一趟航班飛行提醒的結構化資料（會實際連網抓天氣），給 Embed 卡片用。

    回傳 dict：
      title     — 卡片標題（航班 + 航線）
      tags_line — 標籤文字（放描述區），可能為空字串
      times     — dict：report / takeoff / takeoff_z / arrival / arrival_z / total（皆為字串，缺值為 None）
      weather   — list[(欄位名, 欄位值)]，每個起降機場一欄
      notam     — (欄位名, 欄位值)
      items     — list[{'category','text'}]，給下拉勾選清單用
    """
    summary = flight.get('summary', '（航班）')
    origin, dest = _route(flight)
    tags = classify_flight(flight, all_flights, config)
    anchor = datetime.datetime.now(pytz.utc)

    title = f'🧭 飛行提醒｜{summary}　{origin}→{dest}'

    times = flight_times(flight, report_hours_before)
    takeoff_utc = times.get('dep_utc')  # 正確的起飛 UTC，供天氣 highlight 用

    tags_line = ''
    shown_tags = [TAG_LABELS[t] for t in tags if t in TAG_LABELS]
    if shown_tags:
        tags_line = f'🏷️ {"・".join(shown_tags)}'

    icao_map = {}
    for iata in (origin, dest):
        if iata:
            icao_map[iata] = to_icao(iata)
    codes = [c for c in icao_map.values() if c]
    weather = fetch_weather(codes) if codes else {}

    weather_fields = []
    for iata, icao in icao_map.items():
        entry = weather.get(icao, {}) if icao else {}
        weather_fields.append(build_weather_field(iata, icao, entry, takeoff_utc, anchor))

    notam_codes = ' '.join(codes) or f'{origin} {dest}'
    notam = ('📋 NOTAM',
             f'[FAA DINS 查詢](https://www.notams.faa.gov/dinsQueryWeb/) → 貼入 `{notam_codes}`')

    items = get_checklist_items(flight, all_flights, config, tags=tags)

    return {'title': title, 'tags_line': tags_line, 'times': times,
            'weather': weather_fields, 'notam': notam, 'items': items}
