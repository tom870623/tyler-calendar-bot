# Tyler Calendar Bot — 開發說明

## 這是什麼

Tyler 的 Discord Bot，每天早上 7:00（台北時間）自動推送 `/morning` 早報（借用 claude_bridge 跑本機 Claude；claude 不可用時退回推今日行事曆當備援）。另有「飛行提醒清單」功能（`/preflight` + 起飛前自動推送），詳見下方。

## 指令一覽

| 指令 | 功能 |
|------|------|
| `/schedule` | 今天到月底的航班 |
| `/preflight` | 下一趟航班的飛行提醒清單（含即時天氣） |
| `/morning` | 產生今天的早晨日報（跑本機 Claude 的 `/morning` skill） |
| `/todo` | 看今天的待辦清單（跑本機 Claude 的 `/todo` skill，讀 100_Todo 任務看板） |
| `/ask 問題` | 問 Claude 任何問題，唯讀、可追問 |
| `/reset` | 清掉本頻道的對話記憶 |

> `/today`、`/tomorrow` 已於 2026-07-14 移除（改用 `/morning` 早報 + `/todo` 待辦看板）。每日 07:00 自動推播已於 2026-07-14 由「今日行事曆」改為自動跑 `/morning` 早報（行事曆為備援）。

## Claude 橋接（morning / ask）

`cogs/claude_bridge.py` 把 Discord 接到本機的 Claude Code（headless），讓 bot 能借用「真正的 Claude 大腦」＋ MCP 工具，而不是寫死的 Python 邏輯。

- **原理**：指令觸發時，bot 在本機跑 `claude -p`（cwd = LifeOS 母資料夾 `/Users/tyler/Downloads/Tyler-agent`），把結果貼回頻道。`/morning` 送的 prompt 就是 `/morning`，直接複用 `000_Agent/skills/morning/` 那份 skill。
- **唯讀保護**：`ALLOWED_TOOLS` 只放行查詢類工具（讀信 / 讀行事曆 / 讀 Notion / 上網 / 讀檔 + `date` 與 morning 的兩支腳本）；`DISALLOWED_TOOLS` 明確擋掉寄信、改 / 刪資料、寫檔（優先於白名單）。任意 Bash 不在白名單→自動拒絕。要放寬功能改這兩個清單。
- **對話記憶**：每個頻道各維持一條 Claude session，存在 `state/claude_sessions.json`，所以可以追問；`/reset` 清掉。
- **不卡 bot**：用 asyncio 子行程跑，同頻道以 lock 串行（避免 session 互踩），跑的時候其他指令照常。逾時上限 `RUN_TIMEOUT`（300 秒）。
- **依賴**：本機要有 `claude` CLI（`/Users/tyler/.local/bin/claude`）與設定好的 MCP（讀 `~/.claude.json`）。子行程會補上 PATH 找 claude / npx。
- ⚠️ **已知風險**：morning 步驟 3、4 的 AppleScript 腳本（讀 iPhone 行事曆 / 提醒）需要 macOS「自動化」授權；由 launchd 底下的 bot 觸發時，若沒有 GUI 可按授權可能失敗，該情況下早報仍會有 Google Calendar / Gmail / Notion 的部分。

## 飛行提醒清單（preflight）

每趟航班會依「航班類型」自動組出提醒清單，並抓即時天氣。

- **航班類型自動判斷**（`cogs/preflight_data.py` 的 `classify_flight`）：
  - `outstation` 外站出發（出發地 ≠ 母基地 TPE）
  - `long_haul` 長程（排定飛行時數 ≥ 門檻，預設 6 小時）
  - `red_eye` 紅眼/清晨（起飛台北時間落在 00:00–05:00）
  - `overnight` 過夜（目的地非母基地，且下一趟從該地更晚日期起飛）
- **天氣**：來源 `aviationweather.gov` 官方 API（免費免帳號），抓起降機場的 METAR + TAF。只吃 ICAO 四碼，靠 `IATA_TO_ICAO` 對照表轉換；查不到的機場顯示原始 IATA 碼、不會壞。天氣暫時抓不到會顯示提示而非整個失敗。
  - TAF 會被 `parse_taf` 拆成一段一段（BASE / FM / BECMG / TEMPO / PROB），每段一行縮排顯示；起飛時間落在的時段用 ansi 色塊（黃底）+ ▶ 標記 highlight。起飛超出 TAF 預報範圍時會註記。
- **單一 Embed 卡片**：整個 preflight（航班資訊 + 天氣 + NOTAM + 清單）組成**一則 Embed 訊息**（只有一個頭貼），由 `PreflightChecklistView.build_embed()` 產生。天氣放 embed 欄位（各機場一欄）；欄位值上限 1024、整卡上限 6000，`_truncate` 會保護。
- **時區處理（重要）**：EVA 行事曆的起降時間其實是「各機場當地時間」卻被標成 +08。`IATA_TO_TZ`（IANA 名）+ `flight_times()` 會把起飛/報到/抵達換算成正確絕對時間、算出真實總時長（自動處理 DST）。查不到時區的機場退回台北時間。起飛時間顯示「當地 + 台北（若不同）+ Zulu」。此換算同時用於 `/today`、`/tomorrow`、`/schedule`（`format_event`）與 `classify_flight`（紅眼用當地時、長程用真實時數）。新增機場時記得同時補 `IATA_TO_ICAO` 與 `IATA_TO_TZ`。
- **天氣抓取重試**：`fetch_weather` 對 METAR/TAF 各重試 3 次（timeout 15s），因 aviationweather.gov 偶爾回 502。
- **NOTAM**：附 FAA DINS 查詢連結 + 該航線 ICAO 碼（先不自動抓，資料源不穩）。
- **觸發時機**：起飛前 4 小時自動推送（沿用 `_fire_reminder`，用 embed 的 author 顯示提醒字樣），也可用 `/preflight` 隨時手動叫。
- **提醒清單**：無 emoji，用 ☐/☑ 呈現在 embed 欄位 + Discord 下拉多選（`PreflightChecklistView`）點選打勾，勾選後即時更新整張卡片與進度。勾選狀態存記憶體，bot 重啟後重置。內容改 `checklist.json` 即可增減，不用改程式（下拉最多 25 項）。

### 相關檔案

- `checklist.json` — 可編輯的提醒清單內容 + 判斷參數（母基地、長程門檻、紅眼時段）
- `cogs/preflight_data.py` — 純邏輯：機場對照、航班分類、天氣抓取、訊息組裝（可單獨測試）
- `cogs/calendar_local.py` — `/preflight` 指令、起飛前推送掛勾

## 架構

```
discord-calendar-bot/
├── main.py              # Railway 版入口（ICS 方式）
├── main_local.py        # Mac 本機版入口（CalDAV 方式，讀所有行事曆）
├── cogs/
│   ├── calendar.py      # Railway 用 cog（fetch ICS URL）
│   └── calendar_local.py# Mac 本機用 cog（CalDAV，讀全部行事曆）
├── .env.local           # 本機環境變數（不能 commit）
├── requirements.txt     # Railway 用套件
└── bot.log              # 本機 bot 執行 log
```

## 兩個執行模式

### 模式 A：Mac 本機版（目前主要使用）

- 用 CalDAV 讀取所有行事曆（EVA Calander、日常事項、咖米包等）
- 透過 `launchd` 開機自動啟動
- 憑證存在 `.env.local`

**重啟（載入新程式碼）：**
```bash
kill $(pgrep -f main_local.py)                  # launchd KeepAlive 會自動用新版重生並接管
tail -f ~/Library/Logs/tyler-calendarbot.log   # 看 log（2026-07-14 起 log 搬到這，不再是 bot.log）
```
確認 launchd 有接管：`launchctl list | grep calendarbot`（第一欄是 PID 即正常）。

> **關於 78 EX_CONFIG（2026-07-14 已根治）**：之前 launchd 重啟常回 78「不 spawn」，根因是 **log 檔 `bot.log` 在 `~/Downloads`（受 macOS TCC 隱私保護）**，launchd 在 exec 前要開這個 log 寫 stdio 被系統擋掉 → 還沒跑到 Python 就 EX_CONFIG。解法是把 plist 的 `StandardOutPath/StandardErrorPath` 搬到 **`~/Library/Logs/tyler-calendarbot.log`**（非保護區）。修好後 `launchctl bootout` + `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.tyler.calendarbot.plist` 重載也正常了。`WorkingDirectory` 留在 Downloads 沒問題（程式起來後自己讀得到）。

### 模式 B：Railway 雲端版（備用）

- 只讀 EVA Calander（透過公開 ICS URL）
- 部署在 Railway，24 小時在線
- 需要把 Mac bot 停掉才不會衝突

## 環境變數

| 變數 | 說明 | 在哪裡 |
|------|------|--------|
| `DISCORD_TOKEN` | Bot token | `.env.local` / Railway |
| `DISCORD_CHANNEL_ID` | 推送的頻道 ID | `.env.local` / Railway |
| `ICLOUD_USERNAME` | Apple ID | `.env.local` / Railway |
| `ICLOUD_PASSWORD` | App-Specific Password | `.env.local` / Railway |
| `ICLOUD_ICS_URL` | 公開行事曆 ICS 連結（Railway 專用）| Railway only |

## 已知限制

- **Apple CalDAV 封鎖雲端 IP**：Railway 的美國/亞洲伺服器都被 Apple 封鎖，所以雲端版只能用公開 ICS URL
- **本機版依賴 Mac 開著**：Mac 關機 bot 就下線

## iCloud 行事曆清單（CalDAV 讀到的）

- EVA Calander（長榮班表）
- 日常事項
- 朋友局
- 咖米包
- 小陳泰美勒
- 臺鐵訂票行事曆

排除：提醒事項 ⚠️、Siri建議

## 新增功能的方式

在 `cogs/calendar_local.py`（或 `cogs/calendar.py`）裡加新的 `@app_commands.command`：

```python
@app_commands.command(name='week', description='查詢本週行程')
async def cmd_week(self, interaction: discord.Interaction):
    await interaction.response.defer()
    # 你的邏輯
    await interaction.followup.send(message)
```

加完後本機版自動重啟（launchd KeepAlive），Railway 版需要 push 到 GitHub。

## GitHub

`https://github.com/tom870623/tyler-calendar-bot`（private）

## 每日推送時間

台北時間 07:00 = UTC 23:00
