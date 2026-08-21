"""TTS(문자 읽어주기) 기능 - 카미봇/연홍봇 스타일.

현재 구현:
    - `/tts입장`: 명령어를 실행한 유저가 있는 음성 채널에 아냥이가 들어감
    - `/tts퇴장`: 음성 채널에서 나감
    - `/tts채널설정`: 이 채널에 올라오는 메시지를 음성으로 읽어주도록 지정 (관리자 전용)
    - `/tts채널해제`: 위 설정 해제 (관리자 전용)
    - `/tts목소리`: 유저 개인이 Google Cloud Standard 목소리(A/B/C/D)를 선택 (GOOGLE_TTS_API_KEY 필요)
    - `/tts프리미엄목소리`: 유저 개인이 ElevenLabs 프리미엄 목소리를 검색해서 선택 (ELEVENLABS_API_KEY 필요)

TTS 엔진은 세 가지를 지원한다:
    1. edge-tts - 마이크로소프트 엣지 읽어주기 기능을 그대로 이용하는 무료 라이브러리, API 키 불필요.
       아무도 목소리를 설정하지 않았을 때 쓰이는 기본값(안전망) 용도로만 남겨둠.
    2. Google Cloud Text-to-Speech (Standard A/B/C/D) - REST API(text:synthesize)를 API 키로 직접 호출.
       GOOGLE_TTS_API_KEY가 .env에 설정된 경우에만 활성화된다.
    3. ElevenLabs - 고품질 프리미엄 TTS. ELEVENLABS_API_KEY가 .env에 설정된 경우에만 활성화되며,
       무료 플랜(매달 일정 글자 수 무료)으로도 사용 가능하나 키 발급은 사용자가 직접 해야 한다.

유저가 어떤 목소리를 선택했는지는 user_tts_voice 테이블에 provider('edge'|'google'|'elevenlabs')와 함께
저장되고, 실제 메시지를 읽을 때 해당 provider에 맞는 엔진으로 라우팅된다.

오디오 재생에는 ffmpeg가 필요한데, imageio-ffmpeg 패키지가 내장 ffmpeg 실행 파일을 제공하므로
별도 설치 없이 동작한다. 음성 채널 재생을 위해 discord.py voice 기능(PyNaCl, davey)이 필요하다.

TODO (추후 개선 가능):
    - 길드별 기본 목소리 지정
    - 읽어줄 메시지 길이 제한 조정 UI
"""
from __future__ import annotations

import asyncio
import base64
import logging
import re
import tempfile
from pathlib import Path

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from config import ELEVENLABS_API_KEY, GOOGLE_TTS_API_KEY
from utils import db

logger = logging.getLogger("anyang.tts")

try:
    import edge_tts
except ImportError:  # requirements.txt를 아직 설치하지 않은 경우를 대비
    edge_tts = None  # type: ignore[assignment]

try:
    from elevenlabs.client import AsyncElevenLabs
except ImportError:
    AsyncElevenLabs = None  # type: ignore[assignment]

try:
    import imageio_ffmpeg

    FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()
except ImportError:
    FFMPEG_PATH = "ffmpeg"  # 시스템 PATH에 ffmpeg가 있다고 가정

MAX_TTS_CHARS = 200
# 아무도 목소리를 설정하지 않았을 때 쓰는 최후의 안전망(edge-tts, 항상 키 없이 동작).
DEFAULT_PROVIDER = "edge"
DEFAULT_VOICE = "ko-KR-SunHiNeural"
ELEVENLABS_MODEL_ID = "eleven_multilingual_v2"  # 한국어 등 다국어 지원 모델
GOOGLE_TTS_ENDPOINT = "https://texttospeech.googleapis.com/v1/text:synthesize"

# Google Cloud Text-to-Speech 한국어(ko-KR) Standard 목소리 4종.
VOICE_CHOICES = [
    app_commands.Choice(name="A (여성) - Google Cloud Standard", value="ko-KR-Standard-A"),
    app_commands.Choice(name="B (여성) - Google Cloud Standard", value="ko-KR-Standard-B"),
    app_commands.Choice(name="C (남성) - Google Cloud Standard", value="ko-KR-Standard-C"),
    app_commands.Choice(name="D (남성) - Google Cloud Standard", value="ko-KR-Standard-D"),
]

_MENTION_PATTERN = re.compile(r"<@!?(\d+)>")
_ROLE_PATTERN = re.compile(r"<@&\d+>")
_CHANNEL_PATTERN = re.compile(r"<#\d+>")
_CUSTOM_EMOJI_PATTERN = re.compile(r"<a?:(\w+):\d+>")
_URL_PATTERN = re.compile(r"https?://\S+")


class TtsCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._queues: dict[int, asyncio.Queue[tuple[str, str, str]]] = {}
        self._workers: dict[int, asyncio.Task] = {}

        self.elevenlabs_client: "AsyncElevenLabs | None" = None
        if AsyncElevenLabs is not None and ELEVENLABS_API_KEY:
            self.elevenlabs_client = AsyncElevenLabs(api_key=ELEVENLABS_API_KEY)

        if edge_tts is None:
            logger.warning("edge-tts가 설치되지 않아 기본 안전망 목소리가 비활성화됩니다.")
        if not GOOGLE_TTS_API_KEY:
            logger.info(
                "GOOGLE_TTS_API_KEY가 없어 Google Cloud Standard 목소리가 비활성화됩니다 "
                "(설정 전까지 `/tts목소리`는 사용할 수 없습니다)."
            )
        if self.elevenlabs_client is None:
            logger.info(
                "ELEVENLABS_API_KEY가 없어 ElevenLabs 프리미엄 목소리가 비활성화됩니다."
            )

    @property
    def _tts_available(self) -> bool:
        return edge_tts is not None or self.elevenlabs_client is not None

    # ---------- 헬퍼 ----------

    async def _get_tts_channel_id(self, guild_id: int) -> int | None:
        row = await db.fetch_one(
            "SELECT tts_channel_id FROM guild_config WHERE guild_id = ?", (guild_id,)
        )
        return row["tts_channel_id"] if row and row["tts_channel_id"] else None

    async def _get_user_voice(self, user_id: int) -> tuple[str, str]:
        """(provider, voice) 튜플을 반환. 설정된 적 없으면 기본 edge-tts 목소리."""
        row = await db.fetch_one(
            "SELECT provider, voice FROM user_tts_voice WHERE user_id = ?", (user_id,)
        )
        if row and row["voice"]:
            return (row["provider"] or DEFAULT_PROVIDER), row["voice"]
        return DEFAULT_PROVIDER, DEFAULT_VOICE

    def _clean_text_for_tts(self, message: discord.Message) -> str:
        text = message.content

        def _mention_to_name(match: re.Match) -> str:
            member = message.guild.get_member(int(match.group(1))) if message.guild else None
            return member.display_name if member else "누군가"

        text = _MENTION_PATTERN.sub(_mention_to_name, text)
        text = _ROLE_PATTERN.sub("역할", text)
        text = _CHANNEL_PATTERN.sub("채널", text)
        text = _CUSTOM_EMOJI_PATTERN.sub(lambda m: m.group(1), text)
        text = _URL_PATTERN.sub("링크", text)
        return text.strip()

    def _ensure_worker(self, guild: discord.Guild) -> None:
        if guild.id not in self._workers or self._workers[guild.id].done():
            self._queues.setdefault(guild.id, asyncio.Queue())
            self._workers[guild.id] = asyncio.create_task(self._worker_loop(guild))

    async def _worker_loop(self, guild: discord.Guild) -> None:
        """큐를 순서대로 처리하되, 현재 메시지가 재생되는 동안 다음 메시지의 음성
        변환(API 호출)을 미리 시작해둬서 대기 시간을 줄인다. 실제 재생은 항상 하나씩만
        나가며(겹치지 않음), 미리 준비해두는 건 소리 없는 "변환" 단계뿐이다."""
        queue = self._queues[guild.id]
        prefetch_task: asyncio.Task[str | None] | None = None

        while True:
            if prefetch_task is not None:
                audio_path = await prefetch_task
                prefetch_task = None
            else:
                text, provider, voice = await queue.get()
                audio_path = await self._generate_audio(text, provider, voice)
                queue.task_done()

            # 지금 메시지를 재생하기 직전, 큐에 이미 대기 중인 다음 메시지가 있으면
            # 그 음성 변환을 백그라운드로 미리 시작해둔다.
            if not queue.empty():
                next_text, next_provider, next_voice = queue.get_nowait()
                prefetch_task = asyncio.create_task(
                    self._generate_audio(next_text, next_provider, next_voice)
                )
                queue.task_done()

            if audio_path is None:
                continue

            try:
                await self._play_and_wait(guild, audio_path)
            except Exception:
                logger.exception("TTS 재생 중 오류가 발생했습니다.")

    async def _generate_audio(self, text: str, provider: str, voice: str) -> str | None:
        if provider == "elevenlabs":
            return await self._generate_elevenlabs_audio(text, voice)
        if provider == "google":
            return await self._generate_google_audio(text, voice)
        return await self._generate_edge_audio(text, voice)

    async def _generate_edge_audio(self, text: str, voice: str) -> str | None:
        if edge_tts is None:
            return None
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            temp_path = f.name
        try:
            communicate = edge_tts.Communicate(text, voice)
            await communicate.save(temp_path)
            return temp_path
        except Exception as e:
            logger.error(f"edge-tts 음성 생성 오류: {e}")
            Path(temp_path).unlink(missing_ok=True)
            return None

    async def _generate_google_audio(self, text: str, voice_name: str) -> str | None:
        if not GOOGLE_TTS_API_KEY:
            return None
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            temp_path = f.name
        try:
            payload = {
                "input": {"text": text},
                "voice": {"languageCode": "ko-KR", "name": voice_name},
                "audioConfig": {"audioEncoding": "MP3"},
            }
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{GOOGLE_TTS_ENDPOINT}?key={GOOGLE_TTS_API_KEY}", json=payload
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        logger.error(f"Google TTS API 오류 ({resp.status}): {body}")
                        Path(temp_path).unlink(missing_ok=True)
                        return None
                    data = await resp.json()

            audio_b64 = data.get("audioContent")
            if not audio_b64:
                Path(temp_path).unlink(missing_ok=True)
                return None

            with open(temp_path, "wb") as out:
                out.write(base64.b64decode(audio_b64))
            return temp_path
        except Exception as e:
            logger.error(f"Google TTS 음성 생성 오류: {e}")
            Path(temp_path).unlink(missing_ok=True)
            return None

    async def _generate_elevenlabs_audio(self, text: str, voice_id: str) -> str | None:
        if self.elevenlabs_client is None:
            return None
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            temp_path = f.name
        try:
            audio_stream = self.elevenlabs_client.text_to_speech.convert(
                voice_id=voice_id,
                text=text,
                model_id=ELEVENLABS_MODEL_ID,
                output_format="mp3_44100_128",
            )
            with open(temp_path, "wb") as out:
                async for chunk in audio_stream:
                    out.write(chunk)
            return temp_path
        except Exception as e:
            logger.error(f"ElevenLabs 음성 생성 오류: {e}")
            Path(temp_path).unlink(missing_ok=True)
            return None

    async def _play_and_wait(self, guild: discord.Guild, temp_path: str) -> None:
        voice_client = guild.voice_client
        if voice_client is None or not voice_client.is_connected():
            Path(temp_path).unlink(missing_ok=True)
            return

        try:
            done = asyncio.Event()

            def _after(error: Exception | None) -> None:
                if error:
                    logger.error(f"TTS 오디오 재생 오류: {error}")
                self.bot.loop.call_soon_threadsafe(done.set)

            voice_client.play(
                discord.FFmpegPCMAudio(temp_path, executable=FFMPEG_PATH), after=_after
            )
            await done.wait()
        finally:
            Path(temp_path).unlink(missing_ok=True)

    # ---------- 슬래시 커맨드 ----------

    @app_commands.command(name="tts입장", description="내가 있는 음성 채널에 아냥이가 들어옵니다.")
    async def join_voice(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "이 명령어는 서버 안에서만 사용할 수 있습니다.", ephemeral=True
            )
            return

        if not self._tts_available:
            await interaction.response.send_message(
                "TTS 기능을 사용할 수 없습니다 (엔진 미설치). 관리자에게 문의해주세요.",
                ephemeral=True,
            )
            return

        voice_state = interaction.user.voice
        if voice_state is None or voice_state.channel is None:
            await interaction.response.send_message(
                "먼저 음성 채널에 들어가주세요.", ephemeral=True
            )
            return

        # 음성 채널 연결은 3초 안에 안 끝날 수 있으므로 먼저 defer로 응답 시간을 확보한다.
        await interaction.response.defer(ephemeral=True)

        channel = voice_state.channel
        try:
            if interaction.guild.voice_client is None:
                await channel.connect()
            else:
                await interaction.guild.voice_client.move_to(channel)
        except Exception as e:
            logger.error(f"음성 채널 연결 실패: {e}")
            await interaction.followup.send(
                "음성 채널 연결에 실패했습니다. 잠시 후 다시 시도해주세요.", ephemeral=True
            )
            return

        self._ensure_worker(interaction.guild)
        await interaction.followup.send(f"{channel.mention}에 들어왔습니다! 🎙️", ephemeral=True)

    @app_commands.command(name="tts퇴장", description="아냥이가 음성 채널에서 나갑니다.")
    async def leave_voice(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "이 명령어는 서버 안에서만 사용할 수 있습니다.", ephemeral=True
            )
            return

        if interaction.guild.voice_client is None:
            await interaction.response.send_message(
                "현재 음성 채널에 들어가 있지 않습니다.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        await interaction.guild.voice_client.disconnect(force=True)
        await interaction.followup.send("음성 채널에서 나갔습니다.", ephemeral=True)

    @app_commands.command(name="tts채널설정", description="이 채널의 메시지를 음성으로 읽어주도록 설정합니다.")
    @app_commands.describe(채널="TTS로 읽어줄 텍스트 채널")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def set_tts_channel(
        self, interaction: discord.Interaction, 채널: discord.TextChannel
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "이 명령어는 서버 안에서만 사용할 수 있습니다.", ephemeral=True
            )
            return

        await db.execute(
            """
            INSERT INTO guild_config (guild_id, tts_channel_id) VALUES (?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET tts_channel_id = excluded.tts_channel_id
            """,
            (interaction.guild.id, 채널.id),
        )
        await interaction.response.send_message(
            f"TTS 채널이 {채널.mention}로 설정되었습니다. 이제 `/tts입장`으로 음성 채널에 들어온 뒤 "
            f"이 채널에 메시지를 쓰면 읽어줍니다.",
            ephemeral=True,
        )

    @app_commands.command(name="tts채널해제", description="TTS 채널 설정을 해제합니다.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def unset_tts_channel(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "이 명령어는 서버 안에서만 사용할 수 있습니다.", ephemeral=True
            )
            return

        await db.execute(
            "UPDATE guild_config SET tts_channel_id = NULL WHERE guild_id = ?",
            (interaction.guild.id,),
        )
        await interaction.response.send_message("TTS 채널 설정이 해제되었습니다.", ephemeral=True)

    @app_commands.command(
        name="tts목소리",
        description="내 메시지를 읽어줄 Google Cloud Standard TTS 목소리를 선택합니다 (API 키 설정 필요).",
    )
    @app_commands.describe(목소리="원하는 목소리를 선택하세요.")
    @app_commands.choices(목소리=VOICE_CHOICES)
    async def set_voice(
        self, interaction: discord.Interaction, 목소리: app_commands.Choice[str]
    ) -> None:
        if not GOOGLE_TTS_API_KEY:
            await interaction.response.send_message(
                "Google Cloud TTS API 키가 설정되지 않아 이 목소리를 사용할 수 없습니다. "
                "관리자에게 문의해주세요.",
                ephemeral=True,
            )
            return

        await db.execute(
            """
            INSERT INTO user_tts_voice (user_id, voice, provider) VALUES (?, ?, 'google')
            ON CONFLICT(user_id) DO UPDATE SET voice = excluded.voice, provider = 'google'
            """,
            (interaction.user.id, 목소리.value),
        )
        await interaction.response.send_message(
            f"내 TTS 목소리가 '{목소리.name}'(으)로 설정되었습니다.", ephemeral=True
        )

    async def _elevenlabs_voice_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        if self.elevenlabs_client is None:
            return []
        try:
            response = await self.elevenlabs_client.voices.get_all()
        except Exception as e:
            logger.error(f"ElevenLabs 목소리 목록 조회 실패: {e}")
            return []

        current_lower = current.lower()
        choices: list[app_commands.Choice[str]] = []
        for v in response.voices:
            if current_lower in v.name.lower():
                choices.append(app_commands.Choice(name=v.name, value=v.voice_id))
            if len(choices) >= 25:  # 디스코드 자동완성 선택지 최대 개수
                break
        return choices

    @app_commands.command(
        name="tts프리미엄목소리",
        description="ElevenLabs 프리미엄 TTS 목소리를 검색해서 선택합니다 (API 키 설정 필요).",
    )
    @app_commands.describe(목소리="이름으로 검색해서 원하는 목소리를 선택하세요.")
    @app_commands.autocomplete(목소리=_elevenlabs_voice_autocomplete)
    async def set_premium_voice(self, interaction: discord.Interaction, 목소리: str) -> None:
        if self.elevenlabs_client is None:
            await interaction.response.send_message(
                "ElevenLabs API 키가 설정되지 않아 프리미엄 목소리를 사용할 수 없습니다. "
                "무료 목소리는 `/tts목소리`를 사용해주세요.",
                ephemeral=True,
            )
            return

        await db.execute(
            """
            INSERT INTO user_tts_voice (user_id, voice, provider) VALUES (?, ?, 'elevenlabs')
            ON CONFLICT(user_id) DO UPDATE SET voice = excluded.voice, provider = 'elevenlabs'
            """,
            (interaction.user.id, 목소리),
        )
        await interaction.response.send_message(
            "내 TTS 목소리가 ElevenLabs 프리미엄 목소리로 설정되었습니다.", ephemeral=True
        )

    @set_tts_channel.error
    @unset_tts_channel.error
    async def _permission_error_handler(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message(
                "이 명령어를 사용하려면 '서버 관리' 권한이 필요합니다.", ephemeral=True
            )
        else:
            raise error

    # ---------- 이벤트 ----------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None:
            return
        if not message.content.strip():
            return
        if not self._tts_available:
            return

        tts_channel_id = await self._get_tts_channel_id(message.guild.id)
        if tts_channel_id is None or message.channel.id != tts_channel_id:
            return

        voice_client = message.guild.voice_client
        if voice_client is None or not voice_client.is_connected():
            return

        text = self._clean_text_for_tts(message)[:MAX_TTS_CHARS]
        if not text:
            return

        provider, voice = await self._get_user_voice(message.author.id)
        self._ensure_worker(message.guild)
        await self._queues[message.guild.id].put((text, provider, voice))


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TtsCog(bot))
