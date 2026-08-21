"""게임/이벤트 기능 (뼈대 - 추후 구현 예정).

계획:
    - 정기 이벤트 참가자를 모아 자동으로 파티(그룹)를 구성하는 명령어
    - 간단한 텍스트 기반 협동 RPG 세션 진행 골격 (전투/선택지 분기)
"""
from discord.ext import commands


class EventMatchingCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(EventMatchingCog(bot))
