"""평판/기여도 시스템 (뼈대 - 추후 구현 예정).

계획:
    - 특정 리액션(예: 🙏)을 감사 표시로 집계
    - `/평판랭킹` 같은 명령어로 서버 내 순위를 보여줌
    - reputation 테이블(utils/db.py에 이미 생성됨)을 사용

on_raw_reaction_add 이벤트에서 지정된 이모지를 감지해
reputation.thanks_count를 누적하는 방식으로 구현할 예정.
"""
from discord.ext import commands


class ReputationCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ReputationCog(bot))
