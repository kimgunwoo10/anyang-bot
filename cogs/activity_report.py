"""서버 활동 데이터 분석 / 리포트 기능 (뼈대 - 추후 구현 예정).

계획:
    - on_message에서 message_activity 테이블에 (guild_id, channel_id, user_id) 기록
    - `/활동리포트` 명령어 또는 주기적 스케줄 작업으로
      사용자별 "이번 주 활동 리포트"를 DM으로 전송
"""
from discord.ext import commands


class ActivityReportCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ActivityReportCog(bot))
