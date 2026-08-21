"""SQLite 데이터 접근 레이어.

지금은 aiosqlite로 시작하되, 나중에 PostgreSQL 등으로 옮기기 쉽도록
쿼리 로직을 이 모듈 하나에 모아두고, 각 cog는 여기서 제공하는
execute/fetch_all/fetch_one 헬퍼만 사용하도록 한다.
"""
from __future__ import annotations

import logging
from pathlib import Path

import aiosqlite

from config import DB_PATH

logger = logging.getLogger("anyang.db")


async def _ensure_column(conn: aiosqlite.Connection, table: str, column: str, coltype: str) -> None:
    """table에 column이 없으면 추가한다 (이미 배포된 DB에 새 컬럼을 안전하게 반영)."""
    cursor = await conn.execute(f"PRAGMA table_info({table})")
    existing = {row[1] for row in await cursor.fetchall()}
    if column not in existing:
        await conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")


async def init_db() -> None:
    """DB 파일 및 테이블을 생성한다. 봇 시작 시 한 번 호출."""
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)

    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS triggers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                keyword TEXT NOT NULL,
                response_type TEXT NOT NULL,      -- 'text' | 'image'
                response_content TEXT NOT NULL,
                created_by INTEGER NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(guild_id, keyword)
            )
            """
        )

        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS guild_faq (
                guild_id INTEGER PRIMARY KEY,
                content TEXT NOT NULL DEFAULT ''
            )
            """
        )

        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS guild_config (
                guild_id INTEGER PRIMARY KEY,
                ai_qa_channel_id INTEGER,
                moderation_channel_id INTEGER,
                persona TEXT,
                welcome_channel_id INTEGER,
                welcome_message TEXT,
                tts_channel_id INTEGER,
                levelup_channel_id INTEGER
            )
            """
        )
        # guild_config에 컬럼이 추가되기 전에 이미 생성된 DB를 위한 마이그레이션
        await _ensure_column(conn, "guild_config", "persona", "TEXT")
        await _ensure_column(conn, "guild_config", "welcome_channel_id", "INTEGER")
        await _ensure_column(conn, "guild_config", "welcome_message", "TEXT")
        await _ensure_column(conn, "guild_config", "tts_channel_id", "INTEGER")
        await _ensure_column(conn, "guild_config", "levelup_channel_id", "INTEGER")

        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_tts_voice (
                user_id INTEGER PRIMARY KEY,
                voice TEXT NOT NULL,
                provider TEXT NOT NULL DEFAULT 'edge'
            )
            """
        )
        # user_tts_voice에 provider 컬럼이 추가되기 전에 이미 생성된 DB를 위한 마이그레이션
        await _ensure_column(conn, "user_tts_voice", "provider", "TEXT NOT NULL DEFAULT 'edge'")

        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS conversation_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                role TEXT NOT NULL,      -- 'user' | 'model'
                content TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        # 아래 테이블들은 reputation / activity_report 등 이후 구현될 cog들이
        # 바로 사용할 수 있도록 스켈레톤 단계에서 미리 만들어둔다.
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS reputation (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                thanks_count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (guild_id, user_id)
            )
            """
        )

        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS message_activity (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_levels (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                level INTEGER NOT NULL DEFAULT 1,
                xp INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (guild_id, user_id)
            )
            """
        )

        # 포인트(상점 화폐): 활동으로 적립, 웹 상점에서 사용
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_points (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                points INTEGER NOT NULL DEFAULT 0,
                last_daily TEXT,              -- 마지막 출석체크 날짜 'YYYY-MM-DD' (KST)
                PRIMARY KEY (guild_id, user_id)
            )
            """
        )

        # 웹 상점 구매 내역
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS shop_purchases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                item_id TEXT NOT NULL,
                price INTEGER NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        # 기간제 역할 (예: @Boost 7일권) - expires_at(유닉스 초)이 지나면 봇이 역할 회수
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS timed_roles (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                role_id INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                PRIMARY KEY (guild_id, user_id, role_id)
            )
            """
        )

        # 개인역할 제작권으로 만든 유저별 개인 역할 (재구매 시 이름/색 수정용)
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS personal_roles (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                role_id INTEGER NOT NULL,
                PRIMARY KEY (guild_id, user_id)
            )
            """
        )

        # 웹 로그인 세션 (Discord OAuth 후 발급되는 쿠키 토큰)
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS web_sessions (
                token TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                username TEXT,
                avatar_url TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        await conn.commit()

    logger.info(f"SQLite 초기화 완료: {DB_PATH}")


async def execute(query: str, params: tuple = ()) -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(query, params)
        await conn.commit()


async def fetch_all(query: str, params: tuple = ()) -> list[aiosqlite.Row]:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(query, params) as cursor:
            return await cursor.fetchall()


async def fetch_one(query: str, params: tuple = ()) -> aiosqlite.Row | None:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(query, params) as cursor:
            return await cursor.fetchone()
