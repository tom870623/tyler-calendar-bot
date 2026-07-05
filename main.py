import discord
from discord.ext import commands
import os
import asyncio
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

intents = discord.Intents.default()
intents.message_content = True

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
        await bot.load_extension('cogs.calendar')
        await bot.start(os.environ['DISCORD_TOKEN'])


asyncio.run(main())
