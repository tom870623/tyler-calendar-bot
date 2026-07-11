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
        synced = await bot.tree.sync()
        logger.info(f'已同步 {len(synced)} 個指令')
    except Exception as e:
        logger.error(f'指令同步失敗：{e}')


async def main():
    async with bot:
        await bot.load_extension('cogs.calendar_local')
        await bot.load_extension('cogs.claude_bridge')
        await bot.start(os.environ['DISCORD_TOKEN'])


asyncio.run(main())
