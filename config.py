"""환경변수 기반 설정 모듈.

.env 파일(python-dotenv)에서 토큰/API 키 등을 읽어온다.
"""
import os

from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN: str | None = os.getenv("DISCORD_TOKEN")
GEMINI_API_KEY: str | None = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
ELEVENLABS_API_KEY: str | None = os.getenv("ELEVENLABS_API_KEY")
GOOGLE_TTS_API_KEY: str | None = os.getenv("GOOGLE_TTS_API_KEY")
DB_PATH: str = os.getenv("DB_PATH", "data/anyang.db")

# ---- 웹 상점 (cogs/web_shop.py) ----
# Discord OAuth 로그인용. Client ID는 봇의 Application ID와 같다.
DISCORD_CLIENT_ID: str = os.getenv("DISCORD_CLIENT_ID", "1524212886229094512")
DISCORD_CLIENT_SECRET: str | None = os.getenv("DISCORD_CLIENT_SECRET")
# 개발자 포털 OAuth2 > Redirects에 이 주소를 그대로 등록해야 한다.
OAUTH_REDIRECT_URI: str = os.getenv("OAUTH_REDIRECT_URI", "http://localhost:8080/callback")
WEB_PORT: int = int(os.getenv("WEB_PORT", "8080"))
# 상점을 운영할 서버 ID. 비워두면 이름에 "금단증상"이 들어간 서버를 자동으로 찾는다.
SHOP_GUILD_ID: int | None = int(os.getenv("SHOP_GUILD_ID")) if os.getenv("SHOP_GUILD_ID") else None

if not DISCORD_TOKEN:
    raise RuntimeError(
        "DISCORD_TOKEN이 설정되지 않았습니다. .env 파일에 DISCORD_TOKEN을 추가해주세요 "
        "(.env.example 참고)."
    )

if not GEMINI_API_KEY:
    # AI 기능은 키가 없으면 비활성화된 채로 동작하도록 하고, 여기서는 경고만 남긴다.
    import logging

    logging.getLogger("anyang.config").warning(
        "GEMINI_API_KEY가 설정되지 않았습니다. AI 질의응답/요약/모더레이션 기능이 "
        "비활성화됩니다."
    )

if not GOOGLE_TTS_API_KEY:
    # Google Cloud TTS는 키가 없으면 /tts목소리가 비활성화된 채로 동작한다.
    import logging

    logging.getLogger("anyang.config").warning(
        "GOOGLE_TTS_API_KEY가 설정되지 않았습니다. Google Cloud Standard TTS 목소리가 "
        "비활성화됩니다."
    )

if not ELEVENLABS_API_KEY:
    # ElevenLabs 프리미엄 TTS는 키가 없으면 비활성화된 채로 동작한다.
    import logging

    logging.getLogger("anyang.config").warning(
        "ELEVENLABS_API_KEY가 설정되지 않았습니다. ElevenLabs 프리미엄 TTS 목소리가 "
        "비활성화됩니다."
    )
