"""레벨링(경험치) 시스템 - MEE6 표준 방식.

경험치 규칙 (대부분의 레벨링 봇이 쓰는 MEE6 방식 그대로):
    - [채팅] 메시지 1개당 15~25 XP를 랜덤으로 지급
    - [채팅] 같은 유저는 60초에 한 번만 XP 획득 (도배로 레벨 올리기 방지)
    - [음성] 음성 채널 접속 1분마다 5~10 XP를 랜덤으로 지급 (채팅보다 적게)
    - [음성] 잠수 상태면 1분마다 1~2 XP만 지급 (아예 안 주진 않고 아주 조금만).
      잠수로 간주하는 조건:
        · 헤드셋을 끈 상태(스피커 음소거, 자기가 껐든 서버가 껐든) — 안 듣고 있으면 잠수
        · 채널에 혼자 있을 때 (봇 제외 사람이 2명 미만)
        · 서버의 "잠수(AFK) 채널"에 있을 때
      마이크만 끈 상태(뮤트)는 듣고는 있는 것이므로 정상 XP를 받는다.
    - 다음 레벨까지 필요한 XP = 5 * (레벨^2) + 50 * 레벨 + 100
      (레벨 0→1: 100 XP, 1→2: 155 XP, 2→3: 220 XP, ... 점점 많이 필요해짐)
    - 채팅/음성 XP는 같은 레벨 풀에 합산된다 (레벨이 따로 있지 않음)

포인트 (웹 상점 화폐, XP와 별개로 적립):
    - 채팅 1분당 70P, 음성 1분당 80P, 음성 잠수 1분당 30P
    - /출석체크: 하루 1회 1,000P (KST 자정 기준으로 날짜가 바뀜)
    - user_points 테이블에 저장, 웹 상점(cogs/web_shop.py)에서 소비

슬래시 커맨드:
    - /레벨 확인 [유저]: 자신(또는 지정 유저)의 레벨/경험치/서버 순위 확인
    - /레벨 랭킹: 서버 상위 10명 리더보드
    - /레벨 채널설정 채널:#...: 레벨업 축하 메시지를 보낼 채널 지정 (관리자 전용)
    - /레벨 채널해제: 지정 해제 → 레벨업한 그 채널에 바로 축하 메시지 (기본값, 관리자 전용)

데이터는 user_levels 테이블(guild_id, user_id, level, xp)에 저장되며,
xp 컬럼에는 "현재 레벨에서 모은 XP"를 저장한다 (누적 총 XP가 아님).
레벨업 알림 채널은 guild_config.levelup_channel_id에 저장된다.

TODO (추후 개선 가능):
    - 레벨업 시 역할 자동 부여 (직업/스토리형 성장 시스템과 연계)
    - 특정 채널 XP 제외 설정
"""
from __future__ import annotations

import logging
import random
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils import db

logger = logging.getLogger("anyang.leveling")

# MEE6 표준: 메시지당 15~25 XP, 유저당 60초 쿨다운
XP_MIN = 15
XP_MAX = 25
XP_COOLDOWN_SECONDS = 60.0

# 음성 채널: 1분마다 5~10 XP (잠수가 많으므로 채팅의 절반 이하로 낮게 책정)
VOICE_XP_MIN = 5
VOICE_XP_MAX = 10

# 음성 잠수 상태(헤드셋 끔 / 혼자 있음 / AFK 채널): 1분마다 1~2 XP만 지급
VOICE_AFK_XP_MIN = 1
VOICE_AFK_XP_MAX = 2

# 포인트(웹 상점 화폐) 적립량 — XP와 별개로 같이 쌓인다
CHAT_POINTS = 70       # 채팅 1분당 (XP와 같은 60초 쿨다운에 묶여 지급)
VOICE_POINTS = 80      # 음성 정상 참여 1분당
VOICE_AFK_POINTS = 30  # 음성 잠수 1분당
DAILY_POINTS = 1000    # /출석체크 하루 1회

# 한국 시간(KST, UTC+9) — 출석체크 날짜 판정용. 한국은 서머타임이 없어 고정 오프셋으로 충분.
KST = timezone(timedelta(hours=9))


def xp_needed_for_next_level(level: int) -> int:
    """현재 레벨에서 다음 레벨로 가는 데 필요한 XP (MEE6 공식)."""
    return 5 * (level**2) + 50 * level + 100


class LevelingCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        # (guild_id, user_id) -> 마지막으로 XP를 받은 시각(time.monotonic())
        self._last_xp_at: dict[tuple[int, int], float] = {}

    level_group = app_commands.Group(
        name="레벨", description="레벨/경험치 시스템을 관리합니다."
    )

    async def cog_load(self) -> None:
        self.voice_xp_loop.start()

    async def cog_unload(self) -> None:
        self.voice_xp_loop.cancel()

    # ------------------------------------------------------------------ XP 지급

    async def _add_xp(self, guild_id: int, user_id: int, gained: int) -> int | None:
        """XP를 지급하고 DB에 반영한다. 레벨업했으면 새 레벨을, 아니면 None을 반환."""
        row = await db.fetch_one(
            "SELECT level, xp FROM user_levels WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )
        level = row["level"] if row else 0
        xp = (row["xp"] if row else 0) + gained

        leveled_up = False
        while xp >= xp_needed_for_next_level(level):
            xp -= xp_needed_for_next_level(level)
            level += 1
            leveled_up = True

        await db.execute(
            """
            INSERT INTO user_levels (guild_id, user_id, level, xp) VALUES (?, ?, ?, ?)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET
                level = excluded.level,
                xp = excluded.xp
            """,
            (guild_id, user_id, level, xp),
        )
        return level if leveled_up else None

    async def _add_points(self, guild_id: int, user_id: int, amount: int) -> None:
        """웹 상점 화폐(포인트)를 적립한다."""
        await db.execute(
            """
            INSERT INTO user_points (guild_id, user_id, points) VALUES (?, ?, ?)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET
                points = points + excluded.points
            """,
            (guild_id, user_id, amount),
        )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None:
            return
        # 슬래시 커맨드가 아닌 일반 채팅만 집계 (내용이 아예 없는 시스템 메시지 제외)
        if message.type not in (discord.MessageType.default, discord.MessageType.reply):
            return

        key = (message.guild.id, message.author.id)
        now = time.monotonic()
        if now - self._last_xp_at.get(key, -XP_COOLDOWN_SECONDS) < XP_COOLDOWN_SECONDS:
            return
        self._last_xp_at[key] = now

        new_level = await self._add_xp(
            message.guild.id, message.author.id, random.randint(XP_MIN, XP_MAX)
        )
        await self._add_points(message.guild.id, message.author.id, CHAT_POINTS)
        if new_level is not None:
            await self._announce_level_up(
                message.guild, message.author, message.channel, new_level
            )

    @tasks.loop(seconds=60.0)
    async def voice_xp_loop(self) -> None:
        """1분마다 음성 채널을 돌면서 XP를 지급한다 (잠수 상태면 소량만)."""
        for guild in self.bot.guilds:
            for voice_channel in guild.voice_channels:
                humans = [m for m in voice_channel.members if not m.bot]
                if not humans:
                    continue

                # 잠수 판정 1) 서버의 잠수(AFK) 채널  2) 혼자 있음(봇 제외 2명 미만)
                channel_is_afk = (
                    guild.afk_channel is not None
                    and voice_channel.id == guild.afk_channel.id
                ) or len(humans) < 2

                for member in humans:
                    state = member.voice
                    if state is None:
                        continue
                    # 잠수 판정 3) 헤드셋 끔(스피커 음소거) = 안 듣고 있는 상태.
                    # 마이크만 끈 뮤트는 듣고 있는 것이므로 정상 지급한다.
                    is_afk = channel_is_afk or state.self_deaf or state.deaf

                    if is_afk:
                        gained = random.randint(VOICE_AFK_XP_MIN, VOICE_AFK_XP_MAX)
                        pts = VOICE_AFK_POINTS
                    else:
                        gained = random.randint(VOICE_XP_MIN, VOICE_XP_MAX)
                        pts = VOICE_POINTS

                    await self._add_points(guild.id, member.id, pts)
                    new_level = await self._add_xp(guild.id, member.id, gained)
                    if new_level is not None:
                        # 음성 채널에도 텍스트 채팅이 있으므로 기본값으로 사용 가능
                        await self._announce_level_up(
                            guild, member, voice_channel, new_level
                        )

    @voice_xp_loop.before_loop
    async def _wait_for_ready(self) -> None:
        await self.bot.wait_until_ready()

    async def _announce_level_up(
        self,
        guild: discord.Guild,
        member: discord.abc.User,
        fallback_channel: discord.abc.Messageable,
        new_level: int,
    ) -> None:
        """레벨업 축하 메시지 발송. 지정 채널이 있으면 거기로, 없으면 fallback_channel로."""
        row = await db.fetch_one(
            "SELECT levelup_channel_id FROM guild_config WHERE guild_id = ?",
            (guild.id,),
        )
        channel = fallback_channel
        if row and row["levelup_channel_id"]:
            configured = guild.get_channel(row["levelup_channel_id"])
            if configured is not None:
                channel = configured

        try:
            await channel.send(
                f"🎉 {member.mention}님이 **레벨 {new_level}**(으)로 올라갔어요! 축하합니다!"
            )
        except discord.Forbidden:
            logger.warning(
                f"레벨업 메시지 발송 실패(권한 없음): guild={guild.id}"
            )

    # ------------------------------------------------------------ 조회 커맨드

    @level_group.command(name="확인", description="자신 또는 다른 유저의 레벨과 경험치를 확인합니다.")
    @app_commands.describe(유저="확인할 유저 (비워두면 자기 자신)")
    async def check_level(
        self, interaction: discord.Interaction, 유저: Optional[discord.Member] = None
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "이 명령어는 서버 안에서만 사용할 수 있습니다.", ephemeral=True
            )
            return

        target = 유저 or interaction.user
        row = await db.fetch_one(
            "SELECT level, xp FROM user_levels WHERE guild_id = ? AND user_id = ?",
            (interaction.guild.id, target.id),
        )
        level = row["level"] if row else 0
        xp = row["xp"] if row else 0
        needed = xp_needed_for_next_level(level)

        # 서버 내 순위: 나보다 (레벨, xp)가 높은 사람 수 + 1
        rank_row = await db.fetch_one(
            """
            SELECT COUNT(*) + 1 AS rank FROM user_levels
            WHERE guild_id = ? AND (level > ? OR (level = ? AND xp > ?))
            """,
            (interaction.guild.id, level, level, xp),
        )
        rank = rank_row["rank"] if rank_row else 1

        # 10칸짜리 진행 바 (예: ▰▰▰▱▱▱▱▱▱▱)
        filled = min(10, int(xp / needed * 10))
        bar = "▰" * filled + "▱" * (10 - filled)

        prow = await db.fetch_one(
            "SELECT points FROM user_points WHERE guild_id = ? AND user_id = ?",
            (interaction.guild.id, target.id),
        )
        points = prow["points"] if prow else 0

        embed = discord.Embed(
            title=f"{target.display_name}님의 레벨",
            description=(
                f"**레벨 {level}** (서버 {rank}위)\n"
                f"{bar}  {xp} / {needed} XP\n"
                f"✨ 보유 포인트 **{points:,}P**"
            ),
            color=discord.Color.blurple(),
        )
        embed.set_thumbnail(url=target.display_avatar.url)
        await interaction.response.send_message(embed=embed)

    @level_group.command(name="랭킹", description="서버 레벨 상위 10명을 확인합니다.")
    async def leaderboard(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "이 명령어는 서버 안에서만 사용할 수 있습니다.", ephemeral=True
            )
            return

        rows = await db.fetch_all(
            """
            SELECT user_id, level, xp FROM user_levels
            WHERE guild_id = ? ORDER BY level DESC, xp DESC LIMIT 10
            """,
            (interaction.guild.id,),
        )
        if not rows:
            await interaction.response.send_message(
                "아직 아무도 경험치를 얻지 않았어요. 채팅을 시작해보세요!", ephemeral=True
            )
            return

        medals = ["🥇", "🥈", "🥉"]
        lines = []
        for i, row in enumerate(rows):
            member = interaction.guild.get_member(row["user_id"])
            name = member.display_name if member else f"(나간 유저 {row['user_id']})"
            prefix = medals[i] if i < len(medals) else f"{i + 1}."
            lines.append(f"{prefix} **{name}** — 레벨 {row['level']} ({row['xp']} XP)")

        embed = discord.Embed(
            title=f"🏆 {interaction.guild.name} 레벨 랭킹",
            description="\n".join(lines),
            color=discord.Color.gold(),
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="출석체크", description="하루 한 번 출석하고 1,000포인트를 받습니다.")
    async def daily_check(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "이 명령어는 서버 안에서만 사용할 수 있습니다.", ephemeral=True
            )
            return

        today = datetime.now(KST).strftime("%Y-%m-%d")
        row = await db.fetch_one(
            "SELECT points, last_daily FROM user_points WHERE guild_id = ? AND user_id = ?",
            (interaction.guild.id, interaction.user.id),
        )
        if row and row["last_daily"] == today:
            await interaction.response.send_message(
                f"오늘은 이미 출석했어요! 내일 다시 와주세요. (보유 {row['points']:,}P)",
                ephemeral=True,
            )
            return

        await db.execute(
            """
            INSERT INTO user_points (guild_id, user_id, points, last_daily) VALUES (?, ?, ?, ?)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET
                points = points + excluded.points,
                last_daily = excluded.last_daily
            """,
            (interaction.guild.id, interaction.user.id, DAILY_POINTS, today),
        )
        total = (row["points"] if row else 0) + DAILY_POINTS
        await interaction.response.send_message(
            f"✅ {interaction.user.mention}님 출석 완료! **+{DAILY_POINTS:,}P** (보유 {total:,}P)"
        )

    # ------------------------------------------------------------ 관리자 설정

    @level_group.command(name="채널설정", description="레벨업 축하 메시지를 보낼 채널을 지정합니다.")
    @app_commands.describe(채널="레벨업 축하 메시지를 모아서 보낼 채널")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def set_levelup_channel(
        self, interaction: discord.Interaction, 채널: discord.TextChannel
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "이 명령어는 서버 안에서만 사용할 수 있습니다.", ephemeral=True
            )
            return

        await db.execute(
            """
            INSERT INTO guild_config (guild_id, levelup_channel_id) VALUES (?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                levelup_channel_id = excluded.levelup_channel_id
            """,
            (interaction.guild.id, 채널.id),
        )
        await interaction.response.send_message(
            f"레벨업 축하 메시지가 이제 {채널.mention} 채널로 발송됩니다.", ephemeral=True
        )

    @level_group.command(name="채널해제", description="레벨업 채널 지정을 해제합니다 (레벨업한 채널에 바로 발송).")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def unset_levelup_channel(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "이 명령어는 서버 안에서만 사용할 수 있습니다.", ephemeral=True
            )
            return

        await db.execute(
            "UPDATE guild_config SET levelup_channel_id = NULL WHERE guild_id = ?",
            (interaction.guild.id,),
        )
        await interaction.response.send_message(
            "레벨업 채널 지정이 해제되었습니다. 이제 레벨업한 채널에 바로 축하 메시지를 보냅니다.",
            ephemeral=True,
        )

    @set_levelup_channel.error
    @unset_levelup_channel.error
    async def _permission_error_handler(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message(
                "이 명령어를 사용하려면 '서버 관리' 권한이 필요합니다.", ephemeral=True
            )
        else:
            raise error


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(LevelingCog(bot))
