"""claude_bridge — 把 Discord 接到本機的 Claude Code（headless）。

- /morning：在本機跑 `claude -p "/morning"`，把早報 skill 的結果貼回頻道。
- /ask 問題：把你打的問題丟給 Claude 回答，像在跟 Claude 對話。

設計重點：
- 唯讀模式：只放行「查詢類」工具（讀信、讀行事曆、讀 Notion、上網、讀檔），
  寄信 / 改資料 / 刪除 / 寫檔一律擋掉（見 ALLOWED_TOOLS / DISALLOWED_TOOLS）。
- 記得上下文：每個頻道各自維持一條 Claude session，可以追問。
- 不卡住 bot：用 asyncio 子行程，跑的時候 bot 照常回應其他指令。
"""

import discord
from discord.ext import commands, tasks
from discord import app_commands
import asyncio
import datetime
import json
import os
import time
import logging

import pytz

from .calendar_local import send_long_message, push_due, _load_daily_pushes, _mark_push_sent

logger = logging.getLogger(__name__)

# 專案根目錄（LifeOS 母資料夾）——claude 要從這裡啟動，morning 的腳本才找得到。
PROJECT_ROOT = '/Users/tyler/Downloads/Tyler-agent'
CLAUDE_BIN = '/Users/tyler/.local/bin/claude'
NODE_BIN_DIR = '/Users/tyler/.local/node/bin'

# 每個頻道記住一條 session，達成「可以追問」。存檔讓 bot 重啟後仍記得。
SESSION_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'state', 'claude_sessions.json',
)

# 跑久一點的上限（早報要讀好幾個來源，約 60～120 秒）。
RUN_TIMEOUT = 300

# 「直接打字就回」的聊天頻道設定：
# - CHAT_CHANNEL_ID（.env.local，選填）：精準指定某個頻道當聊天區。
# - 或頻道名稱含下列關鍵字者，一律視為聊天頻道（免抄 ID，開個「跟分身聊天」即可）。
# 其他頻道要 @Tyler_Agent 才會回。
CHAT_CHANNEL_ID = os.environ.get('CHAT_CHANNEL_ID', '').strip()
CHAT_NAME_MARKERS = ('分身', 'bot-room', 'botroom', 'bot_room', '日記', 'journal')

APPEND_SYSTEM_PROMPT = (
    '你正在透過 Discord 回覆 Tyler。請用繁體中文、語氣自然像朋友，回覆精簡好讀，'
    '適合在聊天室顯示（避免超長）。中文與英文/數字之間加半形空格。'
    'Tyler 要求「加提醒/提醒我…」時：執行 bash 000_Agent/skills/todo/add_reminder.sh "內容" [清單] [YYYY-MM-DD HH:MM]'
    '（清單有 Life/自我保健/Swimming/To Buy (luxury)/Thought 等，預設 Life），會同步到他 iPhone。'
    '要求「加待辦」時：把任務寫進 100_Todo/🎯 任務看板/ 對應清單檔（today/short_term/long_term/ideas，格式 - [ ] 任務 (截止日: MM/DD)）。'
    '不確定用提醒還是待辦時：有明確日期時間→提醒事項；專案/規劃類→待辦看板。'
)

# ── 唯讀權限白名單 ──────────────────────────────────────────────
# 只有這些工具會被自動放行；沒列到又需要授權的工具（含任意 Bash）在無人看管
# 模式下會被自動拒絕。要放寬功能就在這裡加。
ALLOWED_TOOLS = [
    'Read', 'Grep', 'Glob', 'WebSearch', 'WebFetch',
    'Bash(date:*)',
    'Bash(bash 000_Agent/skills/morning/calendar_today.sh)',
    'Bash(bash 000_Agent/skills/morning/reminders_today.sh)',
    'Bash(bash 000_Agent/skills/morning/next_flight.sh)',
    'Bash(bash 000_Agent/skills/morning/weather.sh)',
    'Bash(python3 000_Agent/skills/morning/market.py)',
    'Bash(python3 000_Agent/skills/morning/stale_items.py)',
    'Bash(bash 000_Agent/skills/nightly/day_summary.sh)',
    # 精準的「寫入」白名單：待辦看板 + 每日日誌 + 學習日記 + 新增提醒事項腳本（其他寫入仍全鎖）
    'Write(100_Todo/**)', 'Edit(100_Todo/**)',
    'Write(000_Agent/memory/daily/**)', 'Edit(000_Agent/memory/daily/**)',
    'Write(300_Journal/**)', 'Edit(300_Journal/**)',
    'Bash(bash 000_Agent/skills/todo/add_reminder.sh:*)',
    'Bash(bash 000_Agent/skills/inbox/notes_inbox.sh:*)',
    # 整個 server 先放行讀取，破壞性操作再由下面 DISALLOWED 逐一擋掉
    'mcp__gmail', 'mcp__google-calendar', 'mcp__notion', 'mcp__firecrawl',
]

# ── 明確封鎖清單（優先於白名單）────────────────────────────────
# 這是「唯讀」的保險：就算上面整個 server 被放行，這些會寫入 / 寄出 / 刪除的
# 工具仍然打不動。
DISALLOWED_TOOLS = [
    # Write/Edit 不整個封鎖：白名單只放行 100_Todo/**，其他路徑沒被允許＝自動拒絕。
    'NotebookEdit',
    # Gmail：寄信、草稿、刪信、改標籤、過濾器
    'mcp__gmail__send_email', 'mcp__gmail__draft_email',
    'mcp__gmail__delete_email', 'mcp__gmail__batch_delete_emails',
    'mcp__gmail__modify_email', 'mcp__gmail__batch_modify_emails',
    'mcp__gmail__create_label', 'mcp__gmail__update_label',
    'mcp__gmail__delete_label', 'mcp__gmail__get_or_create_label',
    'mcp__gmail__create_filter', 'mcp__gmail__create_filter_from_template',
    'mcp__gmail__delete_filter',
    # Google Calendar：新增 / 修改 / 刪除行程
    'mcp__google-calendar__create-event',
    'mcp__google-calendar__update-event',
    'mcp__google-calendar__delete-event',
    # Notion：建立 / 修改 / 刪除頁面、區塊、資料庫、評論
    'mcp__notion__API-post-page', 'mcp__notion__API-patch-page',
    'mcp__notion__API-update-a-block', 'mcp__notion__API-delete-a-block',
    'mcp__notion__API-patch-block-children',
    'mcp__notion__API-create-a-comment',
    'mcp__notion__API-update-page-markdown',
    'mcp__notion__API-create-a-database',
    'mcp__notion__API-create-a-data-source',
    'mcp__notion__API-update-a-data-source',
    'mcp__notion__API-move-page', 'mcp__notion__API-duplicate-page',
    # Firecrawl：會改動帳號設定的 monitor 類
    'mcp__firecrawl__firecrawl_monitor_create',
    'mcp__firecrawl__firecrawl_monitor_update',
    'mcp__firecrawl__firecrawl_monitor_delete',
]


def _load_sessions() -> dict:
    if not os.path.exists(SESSION_FILE):
        return {}
    try:
        with open(SESSION_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def _save_sessions(data: dict):
    os.makedirs(os.path.dirname(SESSION_FILE), exist_ok=True)
    with open(SESSION_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


class ClaudeBridgeCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._sessions = _load_sessions()  # channel_id(str) -> session_id
        # 同一頻道同時只跑一個 claude，避免 session 互相踩到。
        self._locks: dict[str, asyncio.Lock] = {}
        self.nightly_journal.start()

    def cog_unload(self):
        self.nightly_journal.cancel()

    # 每 15 分檢查：台北 22:00～23:59 之間、今天還沒送過就自動彙整今日日誌（含斷線補送）。
    @tasks.loop(minutes=15)
    async def nightly_journal(self):
        import datetime as _dt
        taipei = pytz.timezone('Asia/Taipei')
        due, _tz, _place = push_due('nightly', 22, 24, tz=taipei)
        if not due:
            return
        # 晚間日誌送「晚間回顧」頻道（NIGHTLY_CHANNEL_ID），沒設就退回 DISCORD_CHANNEL_ID。
        channel = self.bot.get_channel(int(os.environ.get('NIGHTLY_CHANNEL_ID') or os.environ['DISCORD_CHANNEL_ID']))
        if not channel:
            return
        channel_id = str(channel.id)
        today_iso = _dt.datetime.now(taipei).date().isoformat()
        async with self._lock_for(channel_id):
            if _load_daily_pushes().get('nightly') == today_iso:
                return
            result, is_error = await self._run_claude('/nightly', channel_id)
            if is_error:
                logger.error(f'每晚 /nightly 失敗：{result[:200]}')
                return  # 不標記，下個週期再試
            await send_long_message(channel.send, '🌙 **今日收尾**\n\n' + result)
            _mark_push_sent('nightly', today_iso)

    @nightly_journal.before_loop
    async def _wait_ready(self):
        await self.bot.wait_until_ready()

    def _lock_for(self, channel_id: str) -> asyncio.Lock:
        if channel_id not in self._locks:
            self._locks[channel_id] = asyncio.Lock()
        return self._locks[channel_id]

    def _is_chat_channel(self, channel) -> bool:
        """判斷是不是「直接打字就回」的聊天頻道。"""
        if CHAT_CHANNEL_ID and str(getattr(channel, 'id', '')) == CHAT_CHANNEL_ID:
            return True
        name = getattr(channel, 'name', '') or ''
        return any(marker in name for marker in CHAT_NAME_MARKERS)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """讓你不用打 /ask：在聊天頻道直接打字、或在別的頻道 @Tyler_Agent，
        就把訊息接進現有的 Claude 大腦回你。"""
        # 忽略 bot 自己與其他機器人、私訊、空訊息、傳統 ! 指令
        if message.author.bot or message.guild is None:
            return
        content = (message.content or '').strip()
        if not content or content.startswith('!'):
            return

        mentioned = self.bot.user in message.mentions
        if not (mentioned or self._is_chat_channel(message.channel)):
            return

        # 去掉 @機器人 的標記，只留下真正的問題
        if mentioned:
            for tag in (f'<@{self.bot.user.id}>', f'<@!{self.bot.user.id}>'):
                content = content.replace(tag, '')
            content = content.strip()
        if not content:
            content = '哈囉'

        channel_id = str(message.channel.id)
        async with self._lock_for(channel_id):
            try:
                async with message.channel.typing():
                    result, _ = await self._run_claude(content, channel_id)
            except Exception as e:
                logger.error(f'on_message 對話失敗：{e}')
                result = f'⚠️ 出了點狀況：{e}'
        await send_long_message(message.channel.send, result)

    async def _run_claude(self, prompt: str, channel_id: str) -> tuple[str, bool]:
        """在本機跑一次 claude -p，回傳 (文字結果, 是否錯誤)。
        會沿用該頻道上一條 session 以保留上下文。"""
        args = [
            CLAUDE_BIN, '-p',
            '--output-format', 'json',
            '--append-system-prompt', APPEND_SYSTEM_PROMPT,
            '--add-dir', PROJECT_ROOT,
            '--allowedTools', *ALLOWED_TOOLS,
            '--disallowedTools', *DISALLOWED_TOOLS,
        ]
        prev = self._sessions.get(channel_id)
        if prev:
            args += ['--resume', prev]

        # 確保子行程找得到 claude 與 npx（launchd 的 PATH 通常很精簡）。
        env = dict(os.environ)
        env['PATH'] = f'{os.path.dirname(CLAUDE_BIN)}:{NODE_BIN_DIR}:' + env.get('PATH', '')

        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=PROJECT_ROOT,
                env=env,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=prompt.encode('utf-8')),
                timeout=RUN_TIMEOUT,
            )
        except asyncio.TimeoutError:
            return ('⚠️ 這題想太久（超過 5 分鐘）就先停了，可以換個問法或稍後再試。', True)
        except Exception as e:
            logger.error(f'claude 子行程啟動失敗：{e}')
            return (f'⚠️ 無法啟動 Claude：{e}', True)

        if proc.returncode != 0:
            err = stderr.decode('utf-8', 'replace').strip()[-500:]
            logger.error(f'claude 回傳非零：{proc.returncode}\n{err}')
            return (f'⚠️ Claude 執行出錯（code {proc.returncode}）：\n```\n{err}\n```', True)

        try:
            data = json.loads(stdout.decode('utf-8', 'replace'))
        except json.JSONDecodeError:
            raw = stdout.decode('utf-8', 'replace').strip()
            return (raw or '⚠️ Claude 沒有回傳內容。', bool(not raw))

        # 記住新的 session id，之後才能在同頻道追問。
        sid = data.get('session_id')
        if sid:
            self._sessions[channel_id] = sid
            _save_sessions(self._sessions)

        result = data.get('result') or ''
        is_error = bool(data.get('is_error'))
        if not result:
            result = '⚠️ Claude 沒有回傳內容。'
            is_error = True
        return (result, is_error)

    @app_commands.command(name='morning', description='產生今天的早晨日報（Google/Gmail/Notion + iPhone 行事曆與提醒）')
    async def cmd_morning(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)
        channel_id = str(interaction.channel_id)
        async with self._lock_for(channel_id):
            started = time.monotonic()
            result, _ = await self._run_claude('/morning', channel_id)
        elapsed = int(time.monotonic() - started)
        await send_long_message(interaction.followup.send, result)
        logger.info(f'/morning 完成，用時 {elapsed}s')

    @app_commands.command(name='todo', description='看今天的待辦清單（讀 100_Todo 任務看板）')
    async def cmd_todo(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)
        channel_id = str(interaction.channel_id)
        async with self._lock_for(channel_id):
            started = time.monotonic()
            result, _ = await self._run_claude('/todo', channel_id)
        elapsed = int(time.monotonic() - started)
        await send_long_message(interaction.followup.send, result)
        logger.info(f'/todo 完成，用時 {elapsed}s')

    @app_commands.command(name='nightly', description='立刻彙整今天的日誌草稿（平常每晚 22:00 自動跑）')
    async def cmd_nightly(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)
        channel_id = str(interaction.channel_id)
        async with self._lock_for(channel_id):
            started = time.monotonic()
            result, _ = await self._run_claude('/nightly', channel_id)
        elapsed = int(time.monotonic() - started)
        await send_long_message(interaction.followup.send, '🌙 **今日收尾**\n\n' + result)
        logger.info(f'/nightly 完成，用時 {elapsed}s')

    @app_commands.command(name='journal', description='寫今天的學習日記（一問一答，直接在本頻道回覆即可）')
    async def cmd_journal(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)
        channel_id = str(interaction.channel_id)
        # Discord 版日記：叮嚀 Claude 一次只問一題純文字、問完停下等下一則訊息，不要用互動選單。
        journal_prompt = (
            '/journal\n\n'
            '（重要：你正在透過 Discord 幫 Tyler 寫日記，這是聊天室不是 CLI。'
            '請務必「一次只問一題、用純文字問、問完就停下來等我下一則訊息」，'
            '不要用 AskUserQuestion，也不要一次把五題問完。'
            '五題問完後生成日記給我確認；我回「OK/好」後存到 300_Journal/YYYY-MM/YYYY-MM-DD.md、'
            '並把當中的任務同步進 100_Todo 任務看板即可。Discord 版不用 git push，存檔就好。）'
        )
        async with self._lock_for(channel_id):
            result, _ = await self._run_claude(journal_prompt, channel_id)
        await send_long_message(interaction.followup.send, result)
        logger.info('/journal 啟動（Discord 互動模式）')

    @app_commands.command(name='ask', description='問 Claude：查資料、加待辦/提醒事項（可追問）')
    @app_commands.describe(問題='想問 Claude 的內容')
    async def cmd_ask(self, interaction: discord.Interaction, 問題: str):
        await interaction.response.defer(thinking=True)
        channel_id = str(interaction.channel_id)
        async with self._lock_for(channel_id):
            result, _ = await self._run_claude(問題, channel_id)
        # 把問題也帶上，頻道裡看起來像一問一答。
        header = f'**❓ {問題}**\n\n'
        await send_long_message(interaction.followup.send, header + result)

    @app_commands.command(name='reset', description='清掉本頻道的對話記憶，下次從頭開始')
    async def cmd_reset(self, interaction: discord.Interaction):
        channel_id = str(interaction.channel_id)
        if self._sessions.pop(channel_id, None):
            _save_sessions(self._sessions)
            await interaction.response.send_message('🧹 好，這個頻道的對話記憶清掉了，下次會從頭開始。')
        else:
            await interaction.response.send_message('這個頻道本來就沒有對話記憶 😊')


async def setup(bot: commands.Bot):
    await bot.add_cog(ClaudeBridgeCog(bot))
