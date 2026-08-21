"""디스코드 봇 "아냥" 실행 진입점.

discord.py의 app_commands(슬래시 커맨드) 기반으로 동작하며,
cogs/ 폴더의 각 기능 모듈을 로드한다.
"""
from __future__ import annotations

import asyncio
import logging

import discord
from discord.ext import commands

from config import DISCORD_TOKEN
from utils.db import init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("anyang")

# 메시지 내용을 읽어야 하는 기능(트리거-응답, AI Q&A, 모더레이션)이 있으므로
# message_content 인텐트가 필요하다. Discord 개발자 포털에서 해당 인텐트를 켜야 한다.
INTENTS = discord.Intents.default()
INTENTS.message_content = True
INTENTS.members = True

# 우선 구현된 코그 + 앞으로 채워질 코그(뼈대)를 함께 로드 시도한다.
INITIAL_COGS = [
    "cogs.server_flavor",     # 트리거-응답, (추후) 직업/역할 성장 시스템
    "cogs.ai_qa",             # Gemini 기반 AI 질의응답
    "cogs.tts",               # 음성 채널 TTS 읽어주기
    "cogs.leveling",          # 레벨링(경험치) + 포인트 시스템
    "cogs.web_shop",          # 웹 레벨 상점 서버
    "cogs.reputation",        # (추후) 평판/기여도 시스템
    "cogs.summary",           # (추후) 채널 대화 요약
    "cogs.activity_report",   # (추후) 활동 리포트
    "cogs.event_matching",    # (추후) 이벤트 파티 매칭 / 협동 RPG
]


class AnyangBot(commands.Bot):
    def __init__(self) -> None:
        super().__init__(command_prefix="!", intents=INTENTS, help_command=None)

    async def setup_hook(self) -> None:
        await init_db()

        for cog in INITIAL_COGS:
            try:
                await self.load_extension(cog)
                logger.info(f"코그 로드 완료: {cog}")
            except Exception:
                logger.exception(f"코그 로드 실패: {cog}")

        synced = await self.tree.sync()
        logger.info(f"슬래시 커맨드 {len(synced)}개 동기화 완료")

    async def on_ready(self) -> None:
        logger.info(f"{self.user} (ID: {self.user.id}) 로그인 완료")
        logger.info("아냥 봇이 준비되었습니다.")


async def main() -> None:
    bot = AnyangBot()
    async with bot:
        await bot.start(DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
