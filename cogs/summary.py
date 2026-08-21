"""채널 대화 요약 기능 (뼈대 - 추후 구현 예정).

계획:
    - `/요약 [개수]` 슬래시 커맨드로 최근 N개 메시지를 가져와
      Gemini API로 요약해서 보여줌
    - ai_qa.py의 Gemini 클라이언트 초기화 패턴을 재사용
"""
from discord.ext import commands


class SummaryCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(SummaryCog(bot))
