"""AI 활용 기능 - Google Gemini API(무료 티어) 기반 자연어 질의응답.

현재 구현:
    - `/질문` 슬래시 커맨드: 사용자가 직접 질문 -> Gemini 응답
    - `/faq설정`: 서버별 FAQ/지식베이스 텍스트 등록 (관리자 전용)
    - `/qa채널설정`: 지정한 채널에서는 슬래시 커맨드 없이 자연어로 질문하면 자동 응답 (관리자 전용)
    - `/qa채널해제`: 설정된 AI 자동 응답 채널을 해제 (관리자 전용)
    - `/성격설정`: 아냥이의 말투/성격을 서버별로 커스터마이징 (관리자 전용)
    - `/성격초기화`: 커스터마이징한 말투/성격을 기본값으로 되돌림 (관리자 전용)
    - `/대화초기화`: 나와 아냥이의 대화 기억을 초기화 (누구나 자신의 기록만 초기화 가능)

FAQ 지식베이스는 guild_faq 테이블에 서버별로 저장되며, 질문할 때마다
시스템 프롬프트에 컨텍스트로 함께 전달된다.

또한 conversation_history 테이블에 (서버, 유저)별로 최근 대화를 저장해두고,
다음 질문을 할 때 이전 대화 맥락으로 함께 전달해서 "이전 대화를 기억"하도록 한다.
너무 길어지지 않도록 최근 MAX_HISTORY_MESSAGES개까지만 유지한다.

Gemini API 키는 https://aistudio.google.com/apikey 에서 무료로 발급받을 수 있다
(무료 티어는 분당/일일 요청 수 제한이 있음).

TODO (추후 구현):
    - 채널 요약 기능(summary.py로 분리 예정)
    - 부적절한 발언 감지 후 모더레이터 채널 알림(모더레이션 보조 기능)
"""
from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from config import GEMINI_API_KEY, GEMINI_MODEL
from utils import db

logger = logging.getLogger("anyang.ai_qa")

try:
    from google import genai
    from google.genai import types as genai_types
    from google.genai.errors import APIError as GoogleAPIError
except ImportError:  # requirements.txt를 아직 설치하지 않은 경우를 대비
    genai = None  # type: ignore[assignment]
    genai_types = None  # type: ignore[assignment]
    GoogleAPIError = Exception  # type: ignore[assignment,misc]

MAX_FAQ_CHARS = 8000
MAX_PERSONA_CHARS = 2000
MAX_DISCORD_MESSAGE = 1900  # 디스코드 메시지 길이 제한(2000자) 안전 마진
MAX_HISTORY_MESSAGES = 6  # 유저별로 기억할 최근 대화 메시지 수 (user+model 합산, 약 3턴)

DEFAULT_PERSONA = "친근하고 간결한 한국어로, 예의 바르게 답변해."

SYSTEM_PROMPT_TEMPLATE = """너는 디스코드 서버 "{guild_name}"의 도우미 봇 "아냥"이야.

[말투/성격]
{persona}

아래는 이 서버의 FAQ/지식베이스야. 답변할 때 이 내용을 최우선으로 참고하고,
FAQ에 없는 내용이면 너의 일반 지식으로 최대한 도움이 되도록 답해.
확실하지 않은 내용은 추측해서 단정하지 말고 모른다고 솔직하게 말해줘.

[서버 FAQ / 지식베이스]
{faq_content}
"""


class AiQaCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.client: "genai.Client | None" = None
        if genai is not None and GEMINI_API_KEY:
            self.client = genai.Client(api_key=GEMINI_API_KEY)
        else:
            logger.warning("Gemini 클라이언트를 초기화하지 못했습니다. AI 질의응답이 비활성화됩니다.")

    async def _get_faq(self, guild_id: int) -> str:
        row = await db.fetch_one("SELECT content FROM guild_faq WHERE guild_id = ?", (guild_id,))
        return row["content"] if row else ""

    async def _get_qa_channel_id(self, guild_id: int) -> int | None:
        row = await db.fetch_one(
            "SELECT ai_qa_channel_id FROM guild_config WHERE guild_id = ?", (guild_id,)
        )
        return row["ai_qa_channel_id"] if row and row["ai_qa_channel_id"] else None

    async def _get_persona(self, guild_id: int) -> str:
        row = await db.fetch_one(
            "SELECT persona FROM guild_config WHERE guild_id = ?", (guild_id,)
        )
        return row["persona"] if row and row["persona"] else DEFAULT_PERSONA

    async def _get_history(self, guild_id: int, user_id: int) -> list[dict]:
        rows = await db.fetch_all(
            """
            SELECT role, content FROM conversation_history
            WHERE guild_id = ? AND user_id = ?
            ORDER BY id DESC LIMIT ?
            """,
            (guild_id, user_id, MAX_HISTORY_MESSAGES),
        )
        # DESC로 가져왔으니 시간순(오래된 것 -> 최신)으로 뒤집는다
        return [
            {"role": row["role"], "parts": [{"text": row["content"]}]}
            for row in reversed(rows)
        ]

    async def _save_turn(self, guild_id: int, user_id: int, question: str, answer: str) -> None:
        await db.execute(
            "INSERT INTO conversation_history (guild_id, user_id, role, content) VALUES (?, ?, 'user', ?)",
            (guild_id, user_id, question),
        )
        await db.execute(
            "INSERT INTO conversation_history (guild_id, user_id, role, content) VALUES (?, ?, 'model', ?)",
            (guild_id, user_id, answer),
        )
        # 오래된 기록 정리 (유저당 최근 MAX_HISTORY_MESSAGES개만 유지)
        await db.execute(
            """
            DELETE FROM conversation_history
            WHERE guild_id = ? AND user_id = ? AND id NOT IN (
                SELECT id FROM conversation_history
                WHERE guild_id = ? AND user_id = ?
                ORDER BY id DESC LIMIT ?
            )
            """,
            (guild_id, user_id, guild_id, user_id, MAX_HISTORY_MESSAGES),
        )

    async def _ask_gemini(self, guild: discord.Guild, user_id: int, question: str) -> str:
        if self.client is None:
            return "Gemini API가 설정되지 않아 답변할 수 없습니다. 관리자에게 문의해주세요."

        faq_content = await self._get_faq(guild.id)
        persona = await self._get_persona(guild.id)
        system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
            guild_name=guild.name,
            persona=persona,
            faq_content=faq_content or "(등록된 FAQ가 없습니다)",
        )

        history = await self._get_history(guild.id, user_id)
        contents = [*history, {"role": "user", "parts": [{"text": question}]}]

        try:
            response = await self.client.aio.models.generate_content(
                model=GEMINI_MODEL,
                contents=contents,
                config=genai_types.GenerateContentConfig(system_instruction=system_prompt),
            )
        except GoogleAPIError as e:
            logger.error(f"Gemini API 오류: {e}")
            return "AI 응답을 생성하는 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요."

        answer = response.text or "답변을 생성하지 못했습니다. (안전 필터에 의해 차단되었을 수 있습니다)"
        await self._save_turn(guild.id, user_id, question, answer)
        return answer

    @staticmethod
    def _truncate(text: str) -> str:
        if len(text) > MAX_DISCORD_MESSAGE:
            return text[:MAX_DISCORD_MESSAGE] + "\n...(응답이 길어 일부만 표시됩니다)"
        return text

    @app_commands.command(name="질문", description="아냥에게 질문합니다. (Gemini AI 기반)")
    @app_commands.describe(내용="궁금한 내용을 입력해주세요.")
    async def ask(self, interaction: discord.Interaction, 내용: str) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "이 명령어는 서버 안에서만 사용할 수 있습니다.", ephemeral=True
            )
            return

        await interaction.response.defer(thinking=True)
        answer = await self._ask_gemini(interaction.guild, interaction.user.id, 내용)
        await interaction.followup.send(self._truncate(answer))

    @app_commands.command(name="faq설정", description="서버 FAQ/지식베이스 내용을 등록/교체합니다.")
    @app_commands.describe(내용="FAQ로 사용할 텍스트 (기존 내용을 전부 대체합니다)")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def set_faq(self, interaction: discord.Interaction, 내용: str) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "이 명령어는 서버 안에서만 사용할 수 있습니다.", ephemeral=True
            )
            return

        content = 내용[:MAX_FAQ_CHARS]
        await db.execute(
            """
            INSERT INTO guild_faq (guild_id, content) VALUES (?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET content = excluded.content
            """,
            (interaction.guild.id, content),
        )
        await interaction.response.send_message(
            f"FAQ가 저장되었습니다. ({len(content)}자)", ephemeral=True
        )

    @app_commands.command(
        name="qa채널설정",
        description="지정한 채널에서는 슬래시 커맨드 없이 자연어 질문에 자동으로 답변합니다.",
    )
    @app_commands.describe(채널="AI 자동 응답을 사용할 채널")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def set_qa_channel(
        self, interaction: discord.Interaction, 채널: discord.TextChannel
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "이 명령어는 서버 안에서만 사용할 수 있습니다.", ephemeral=True
            )
            return

        await db.execute(
            """
            INSERT INTO guild_config (guild_id, ai_qa_channel_id) VALUES (?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET ai_qa_channel_id = excluded.ai_qa_channel_id
            """,
            (interaction.guild.id, 채널.id),
        )
        await interaction.response.send_message(
            f"AI 질의응답 채널이 {채널.mention}로 설정되었습니다.", ephemeral=True
        )

    @app_commands.command(
        name="qa채널해제",
        description="설정된 AI 자동 응답 채널을 해제합니다.",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def unset_qa_channel(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "이 명령어는 서버 안에서만 사용할 수 있습니다.", ephemeral=True
            )
            return

        await db.execute(
            "UPDATE guild_config SET ai_qa_channel_id = NULL WHERE guild_id = ?",
            (interaction.guild.id,),
        )
        await interaction.response.send_message(
            "AI 질의응답 채널 설정이 해제되었습니다.", ephemeral=True
        )

    @app_commands.command(
        name="성격설정",
        description="아냥이의 말투/성격을 이 서버에 맞게 설정합니다.",
    )
    @app_commands.describe(내용="예: '반말로 장난스럽고 이모지 많이 써서 답변해'")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def set_persona(self, interaction: discord.Interaction, 내용: str) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "이 명령어는 서버 안에서만 사용할 수 있습니다.", ephemeral=True
            )
            return

        content = 내용[:MAX_PERSONA_CHARS]
        await db.execute(
            """
            INSERT INTO guild_config (guild_id, persona) VALUES (?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET persona = excluded.persona
            """,
            (interaction.guild.id, content),
        )
        await interaction.response.send_message(
            f"말투/성격이 설정되었습니다: {content}", ephemeral=True
        )

    @app_commands.command(
        name="성격초기화",
        description="아냥이의 말투/성격을 기본값으로 되돌립니다.",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def reset_persona(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "이 명령어는 서버 안에서만 사용할 수 있습니다.", ephemeral=True
            )
            return

        await db.execute(
            "UPDATE guild_config SET persona = NULL WHERE guild_id = ?",
            (interaction.guild.id,),
        )
        await interaction.response.send_message(
            f"말투/성격이 기본값으로 초기화되었습니다: {DEFAULT_PERSONA}", ephemeral=True
        )

    @app_commands.command(
        name="대화초기화",
        description="나와 아냥이의 대화 기억을 초기화합니다.",
    )
    async def reset_conversation(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "이 명령어는 서버 안에서만 사용할 수 있습니다.", ephemeral=True
            )
            return

        await db.execute(
            "DELETE FROM conversation_history WHERE guild_id = ? AND user_id = ?",
            (interaction.guild.id, interaction.user.id),
        )
        await interaction.response.send_message(
            "대화 기억이 초기화되었습니다. 다음 질문부터는 새 대화로 시작해요.", ephemeral=True
        )

    @set_faq.error
    @set_qa_channel.error
    @unset_qa_channel.error
    @set_persona.error
    @reset_persona.error
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
        if not message.content.strip():
            return

        qa_channel_id = await self._get_qa_channel_id(message.guild.id)
        if qa_channel_id is None or message.channel.id != qa_channel_id:
            return

        async with message.channel.typing():
            answer = await self._ask_gemini(message.guild, message.author.id, message.content)
        await message.reply(self._truncate(answer), mention_author=False)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AiQaCog(bot))
