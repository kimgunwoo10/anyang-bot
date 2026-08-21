"""서버 특화형 기능.

현재 구현:
    - 커스텀 트리거-응답 등록/삭제/목록 (관리자 전용 등록/삭제, 슬래시 커맨드)
    - 메시지 전체가 트리거 키워드와 정확히 일치하면 자동 응답(텍스트 또는 이미지)
    - 신규 멤버 입장 시 지정 채널에 환영 메시지 자동 발송 (관리자 전용 설정)

TODO (추후 구현):
    - 서버 컨셉에 맞는 "직업/역할" 스토리형 성장 시스템 (레벨업 시 역할 변경)
      -> user_levels 테이블은 utils/db.py에 이미 준비되어 있음
"""
from __future__ import annotations

import time
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from utils import db

# 같은 채널에서 같은 트리거가 이 시간(초) 안에 다시 울리면 무시한다 (도배 방지).
TRIGGER_COOLDOWN_SECONDS = 10.0

DEFAULT_WELCOME_MESSAGE = "{user}님, {server}에 오신 것을 환영합니다! 🎉"


class ServerFlavorCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        # guild_id -> [{"keyword": str, "response_type": str, "response_content": str}]
        self._trigger_cache: dict[int, list[dict]] = {}
        # (guild_id, channel_id, keyword) -> 마지막으로 반응한 시각(time.monotonic())
        self._last_fired: dict[tuple[int, int, str], float] = {}

    trigger_group = app_commands.Group(
        name="트리거", description="키워드 트리거-응답을 관리합니다."
    )
    welcome_group = app_commands.Group(
        name="환영", description="신규 멤버 환영 메시지를 관리합니다."
    )

    @staticmethod
    def _render_welcome_message(template: str, member: discord.Member) -> str:
        return (
            template.replace("{user}", member.mention)
            .replace("{username}", member.display_name)
            .replace("{server}", member.guild.name)
            .replace("{membercount}", str(member.guild.member_count))
        )

    async def _refresh_cache(self, guild_id: int) -> None:
        rows = await db.fetch_all(
            "SELECT keyword, response_type, response_content "
            "FROM triggers WHERE guild_id = ?",
            (guild_id,),
        )
        self._trigger_cache[guild_id] = [dict(row) for row in rows]

    @trigger_group.command(name="등록", description="키워드에 반응할 트리거를 등록/수정합니다.")
    @app_commands.describe(
        키워드="메시지 전체가 이 키워드와 정확히 일치할 때만 반응합니다 (예: '픽픽'은 반응, '픽픽아'는 반응 안 함).",
        응답="텍스트 내용 또는 이미지/짤 URL",
        타입="응답 형식을 선택하세요.",
    )
    @app_commands.choices(
        타입=[
            app_commands.Choice(name="텍스트", value="text"),
            app_commands.Choice(name="이미지 URL", value="image"),
        ]
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def add_trigger(
        self,
        interaction: discord.Interaction,
        키워드: str,
        응답: str,
        타입: app_commands.Choice[str],
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "이 명령어는 서버 안에서만 사용할 수 있습니다.", ephemeral=True
            )
            return

        await db.execute(
            """
            INSERT INTO triggers (guild_id, keyword, response_type, response_content, created_by)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, keyword) DO UPDATE SET
                response_type = excluded.response_type,
                response_content = excluded.response_content,
                created_by = excluded.created_by
            """,
            (interaction.guild.id, 키워드.lower(), 타입.value, 응답, interaction.user.id),
        )
        await self._refresh_cache(interaction.guild.id)

        응답형식 = "이미지" if 타입.value == "image" else "텍스트"
        await interaction.response.send_message(
            f"트리거 등록 완료: `{키워드}` → {응답형식} 응답", ephemeral=True
        )

    @trigger_group.command(name="삭제", description="등록된 트리거를 삭제합니다.")
    @app_commands.describe(키워드="삭제할 트리거 키워드")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def remove_trigger(self, interaction: discord.Interaction, 키워드: str) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "이 명령어는 서버 안에서만 사용할 수 있습니다.", ephemeral=True
            )
            return

        await db.execute(
            "DELETE FROM triggers WHERE guild_id = ? AND keyword = ?",
            (interaction.guild.id, 키워드.lower()),
        )
        await self._refresh_cache(interaction.guild.id)
        await interaction.response.send_message(f"트리거 삭제 완료: `{키워드}`", ephemeral=True)

    @trigger_group.command(name="목록", description="등록된 트리거 목록을 확인합니다.")
    async def list_triggers(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "이 명령어는 서버 안에서만 사용할 수 있습니다.", ephemeral=True
            )
            return

        rows = await db.fetch_all(
            "SELECT keyword, response_type FROM triggers WHERE guild_id = ? ORDER BY keyword",
            (interaction.guild.id,),
        )
        if not rows:
            await interaction.response.send_message("등록된 트리거가 없습니다.", ephemeral=True)
            return

        lines = [
            f"- `{row['keyword']}` ({'이미지' if row['response_type'] == 'image' else '텍스트'})"
            for row in rows
        ]
        await interaction.response.send_message(
            "**등록된 트리거 목록**\n" + "\n".join(lines), ephemeral=True
        )

    @welcome_group.command(name="설정", description="멤버가 들어올 때 환영 메시지를 보낼 채널/문구를 설정합니다.")
    @app_commands.describe(
        채널="환영 메시지를 보낼 채널",
        메시지=(
            "환영 메시지 (플레이스홀더: {user}=멘션, {username}=닉네임, "
            "{server}=서버 이름, {membercount}=멤버 수). 줄바꿈은 \\n으로 입력. "
            "비워두면 기본 문구 사용."
        ),
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def set_welcome(
        self,
        interaction: discord.Interaction,
        채널: discord.TextChannel,
        메시지: Optional[str] = None,
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "이 명령어는 서버 안에서만 사용할 수 있습니다.", ephemeral=True
            )
            return

        content = 메시지 if 메시지 else DEFAULT_WELCOME_MESSAGE
        # 슬래시 커맨드 입력창은 한 줄짜리라 실제 줄바꿈을 못 넣으므로,
        # 사용자가 "\n"이라고 입력한 부분을 실제 줄바꿈으로 변환해준다.
        content = content.replace("\\n", "\n")
        await db.execute(
            """
            INSERT INTO guild_config (guild_id, welcome_channel_id, welcome_message) VALUES (?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                welcome_channel_id = excluded.welcome_channel_id,
                welcome_message = excluded.welcome_message
            """,
            (interaction.guild.id, 채널.id, content),
        )
        await interaction.response.send_message(
            f"환영 메시지 채널이 {채널.mention}로 설정되었습니다.\n미리보기:\n{content}",
            ephemeral=True,
        )

    @welcome_group.command(name="해제", description="환영 메시지 자동 발송을 끕니다.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def unset_welcome(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "이 명령어는 서버 안에서만 사용할 수 있습니다.", ephemeral=True
            )
            return

        await db.execute(
            "UPDATE guild_config SET welcome_channel_id = NULL WHERE guild_id = ?",
            (interaction.guild.id,),
        )
        await interaction.response.send_message("환영 메시지 발송이 해제되었습니다.", ephemeral=True)

    @welcome_group.command(name="테스트", description="현재 설정된 환영 메시지가 어떻게 보일지 미리 확인합니다.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def test_welcome(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "이 명령어는 서버 안에서만 사용할 수 있습니다.", ephemeral=True
            )
            return

        row = await db.fetch_one(
            "SELECT welcome_message FROM guild_config WHERE guild_id = ?",
            (interaction.guild.id,),
        )
        template = row["welcome_message"] if row and row["welcome_message"] else DEFAULT_WELCOME_MESSAGE
        preview = self._render_welcome_message(template, interaction.user)
        await interaction.response.send_message(f"미리보기:\n{preview}", ephemeral=True)

    @add_trigger.error
    @remove_trigger.error
    @set_welcome.error
    @unset_welcome.error
    @test_welcome.error
    async def _permission_error_handler(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message(
                "이 명령어를 사용하려면 '서버 관리' 권한이 필요합니다.", ephemeral=True
            )
        else:
            raise error

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None:
            return

        if message.guild.id not in self._trigger_cache:
            await self._refresh_cache(message.guild.id)

        triggers = self._trigger_cache.get(message.guild.id, [])
        if not triggers:
            return

        content_exact = message.content.strip().lower()
        for trigger in triggers:
            if trigger["keyword"] == content_exact:
                cooldown_key = (message.guild.id, message.channel.id, trigger["keyword"])
                now = time.monotonic()
                if now - self._last_fired.get(cooldown_key, 0.0) < TRIGGER_COOLDOWN_SECONDS:
                    return  # 쿨다운 중이면 도배 방지를 위해 무시
                self._last_fired[cooldown_key] = now

                if trigger["response_type"] == "image":
                    embed = discord.Embed()
                    embed.set_image(url=trigger["response_content"])
                    await message.channel.send(embed=embed)
                else:
                    await message.channel.send(trigger["response_content"])
                break  # 한 메시지당 하나의 트리거만 반응

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        row = await db.fetch_one(
            "SELECT welcome_channel_id, welcome_message FROM guild_config WHERE guild_id = ?",
            (member.guild.id,),
        )
        if not row or not row["welcome_channel_id"]:
            return

        channel = member.guild.get_channel(row["welcome_channel_id"])
        if channel is None:
            return

        template = row["welcome_message"] or DEFAULT_WELCOME_MESSAGE
        await channel.send(self._render_welcome_message(template, member))


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ServerFlavorCog(bot))
