import discord
from discord.ext import commands
import os
import asyncio
import logging
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '.env.local'))

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

intents = discord.Intents.default()
bot = commands.Bot(command_prefix='!', intents=intents)


@bot.event
async def on_ready():
    logger.info(f'Bot 已上線：{bot.user} (ID: {bot.user.id})')
    try:
        # 對 bot 所在的每個伺服器「即時」同步指令（幾秒生效，不用等全域慢慢傳）
        for guild in bot.guilds:
            bot.tree.copy_global_to(guild=guild)
            synced = await bot.tree.sync(guild=guild)
            logger.info(f'已同步 {len(synced)} 個指令到伺服器「{guild.name}」')
        if not bot.guilds:
            logger.warning('bot 不在任何伺服器，無法即時同步指令')
        # 清掉「全域」殘留（例如舊的 today/tomorrow）——Discord 端最多 1 小時內移除
        bot.tree.clear_commands(guild=None)
        await bot.tree.sync()
    except Exception as e:
        logger.error(f'指令同步失敗：{e}')


async def main():
    async with bot:
        await bot.load_extension('cogs.calendar_local')
        await bot.load_extension('cogs.claude_bridge')
        await bot.load_extension('cogs.todo_reminder')
        await bot.start(os.environ['DISCORD_TOKEN'])


asyncio.run(main())
