"""웹 레벨 상점 - 봇 프로세스 안에서 함께 도는 웹서버 (aiohttp).

동작 방식:
    1. 봇이 켜질 때 웹서버도 같이 시작된다 (기본 http://localhost:8080)
    2. 멤버가 웹에서 "디스코드로 로그인" → Discord OAuth로 본인 확인
    3. 봇 DB(user_levels, user_points)에서 내 레벨/포인트를 그대로 보여줌
    4. 구매하면 포인트 차감 → 봇이 디스코드에서 즉시 역할(칭호/이름색)을 달아줌

필요한 설정 (.env):
    - DISCORD_CLIENT_SECRET: 개발자 포털 > OAuth2 > Client Secret
    - OAUTH_REDIRECT_URI: 개발자 포털 OAuth2 > Redirects에 등록한 주소와 완전히 같아야 함
      (기본값 http://localhost:8080/callback)
    - SHOP_GUILD_ID: 상점을 운영할 서버 ID (비우면 이름에 "금단증상" 포함된 서버 자동 탐색)

상품 목록은 아래 ITEMS 리스트에서 관리한다. 가격/이름은 자유롭게 수정 가능.
"""
from __future__ import annotations

import logging
import secrets
import time
from pathlib import Path

import aiohttp
import discord
from aiohttp import web
from discord.ext import commands, tasks

from config import (
    DISCORD_CLIENT_ID,
    DISCORD_CLIENT_SECRET,
    OAUTH_REDIRECT_URI,
    SHOP_GUILD_ID,
    WEB_PORT,
)
from utils import db

logger = logging.getLogger("anyang.web_shop")

DISCORD_API = "https://discord.com/api/v10"
SHOP_HTML = Path(__file__).resolve().parent.parent / "web" / "shop.html"

# ---- 상점 상품 목록 ----
# type: "timed_role"  = 기간제 역할 (만료되면 expire_roles_loop가 자동 회수, 재구매 시 연장)
#       "custom_role" = 개인 역할 제작 (원하는 이름+색, 재구매 시 이름/색 변경)
ITEMS = [
    {
        "id": "boost_7d", "type": "timed_role", "name": "@Boost 7일권",
        "role_names": ["@Boost", "Boost"],  # 서버에서 이 이름들로 역할을 찾고, 없으면 첫 번째 이름으로 생성
        "days": 7, "price": 100_000, "icon": "🚀", "rarity": "r", "cat": "perk",
        "flavor": "일주일 동안, 조금 더 특별하게.",
        "meta": "@Boost 역할 7일 이용권 · 보유 중 경험치 2배 · 재구매하면 7일 연장",
    },
    {
        "id": "custom_role", "type": "custom_role", "name": "개인역할 제작권",
        "price": 250_000, "icon": "👑", "rarity": "l", "cat": "perk",
        "flavor": "내 이름 옆, 내가 만든 자리.",
        "meta": "원하는 이름·색의 개인 역할 제작 · 재구매하면 이름/색 변경",
    },
]
ITEMS_BY_ID = {item["id"]: item for item in ITEMS}


def _json_error(message: str, status: int = 400) -> web.Response:
    return web.json_response({"ok": False, "error": message}, status=status)


class WebShopCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.runner: web.AppRunner | None = None

    async def cog_load(self) -> None:
        app = web.Application()
        app.add_routes(
            [
                web.get("/", self.page),
                web.get("/login", self.login),
                web.get("/callback", self.callback),
                web.get("/logout", self.logout),
                web.get("/api/me", self.api_me),
                web.get("/api/items", self.api_items),
                web.post("/api/buy", self.api_buy),
            ]
        )
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "0.0.0.0", WEB_PORT)
        await site.start()
        self.expire_roles_loop.start()
        logger.info(f"웹 상점 서버 시작: http://localhost:{WEB_PORT}")
        if not DISCORD_CLIENT_SECRET:
            logger.warning(
                "DISCORD_CLIENT_SECRET이 .env에 없습니다. 웹 상점 페이지는 열리지만 "
                "디스코드 로그인이 동작하지 않습니다."
            )

    async def cog_unload(self) -> None:
        self.expire_roles_loop.cancel()
        if self.runner:
            await self.runner.cleanup()

    # ------------------------------------------------------------ 기간제 역할 만료 처리

    @tasks.loop(minutes=30)
    async def expire_roles_loop(self) -> None:
        """30분마다 기간이 끝난 역할(예: @Boost 7일권)을 회수한다."""
        now = int(time.time())
        rows = await db.fetch_all(
            "SELECT guild_id, user_id, role_id FROM timed_roles WHERE expires_at <= ?",
            (now,),
        )
        for r in rows:
            guild = self.bot.get_guild(r["guild_id"])
            if guild is not None:
                role = guild.get_role(r["role_id"])
                member = guild.get_member(r["user_id"])
                if role is not None and member is not None:
                    try:
                        await member.remove_roles(role, reason="기간제 역할 만료")
                        logger.info(f"기간제 역할 회수: user={r['user_id']} role={role.name}")
                    except discord.HTTPException:
                        logger.warning(f"기간제 역할 회수 실패: user={r['user_id']} role_id={r['role_id']}")
            await db.execute(
                "DELETE FROM timed_roles WHERE guild_id = ? AND user_id = ? AND role_id = ?",
                (r["guild_id"], r["user_id"], r["role_id"]),
            )

    @expire_roles_loop.before_loop
    async def _wait_for_ready(self) -> None:
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------ 헬퍼

    def _guild(self) -> discord.Guild | None:
        """상점을 운영할 서버를 찾는다."""
        if SHOP_GUILD_ID:
            return self.bot.get_guild(SHOP_GUILD_ID)
        for g in self.bot.guilds:
            if "금단증상" in g.name:
                return g
        return self.bot.guilds[0] if self.bot.guilds else None

    async def _session_user(self, request: web.Request) -> dict | None:
        """쿠키의 세션 토큰으로 로그인 유저를 찾는다."""
        token = request.cookies.get("session")
        if not token:
            return None
        row = await db.fetch_one(
            "SELECT user_id, username, avatar_url FROM web_sessions WHERE token = ?",
            (token,),
        )
        return dict(row) if row else None

    async def _get_member(self, guild: discord.Guild, user_id: int) -> discord.Member | None:
        member = guild.get_member(user_id)
        if member is None:
            try:
                member = await guild.fetch_member(user_id)
            except discord.NotFound:
                return None
        return member

    # ------------------------------------------------------------ 페이지 / 로그인

    async def page(self, request: web.Request) -> web.Response:
        return web.FileResponse(SHOP_HTML)

    async def login(self, request: web.Request) -> web.Response:
        if not DISCORD_CLIENT_SECRET:
            return web.Response(
                text="아직 로그인 설정이 안 됐어요. 관리자에게 알려주세요. "
                "(.env에 DISCORD_CLIENT_SECRET 필요)",
                content_type="text/plain",
                charset="utf-8",
                status=503,
            )
        url = (
            f"{DISCORD_API}/oauth2/authorize"
            f"?client_id={DISCORD_CLIENT_ID}"
            f"&response_type=code&scope=identify"
            f"&redirect_uri={OAUTH_REDIRECT_URI}"
        )
        raise web.HTTPFound(url)

    async def callback(self, request: web.Request) -> web.Response:
        code = request.query.get("code")
        if not code:
            raise web.HTTPFound("/")

        async with aiohttp.ClientSession() as http:
            token_resp = await http.post(
                f"{DISCORD_API}/oauth2/token",
                data={
                    "client_id": DISCORD_CLIENT_ID,
                    "client_secret": DISCORD_CLIENT_SECRET,
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": OAUTH_REDIRECT_URI,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            token_data = await token_resp.json()
            access_token = token_data.get("access_token")
            if not access_token:
                logger.warning(f"OAuth 토큰 교환 실패: {token_data}")
                return web.Response(
                    text="로그인에 실패했어요. 다시 시도해주세요.",
                    content_type="text/plain", charset="utf-8", status=400,
                )

            me_resp = await http.get(
                f"{DISCORD_API}/users/@me",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            me = await me_resp.json()

        user_id = int(me["id"])
        username = me.get("global_name") or me.get("username") or "이름없음"
        avatar_url = (
            f"https://cdn.discordapp.com/avatars/{me['id']}/{me['avatar']}.png?size=128"
            if me.get("avatar")
            else "https://cdn.discordapp.com/embed/avatars/0.png"
        )

        session_token = secrets.token_urlsafe(32)
        await db.execute(
            "INSERT OR REPLACE INTO web_sessions (token, user_id, username, avatar_url) "
            "VALUES (?, ?, ?, ?)",
            (session_token, user_id, username, avatar_url),
        )

        resp = web.HTTPFound("/")
        resp.set_cookie(
            "session", session_token,
            max_age=60 * 60 * 24 * 30, httponly=True, samesite="Lax",
        )
        return resp

    async def logout(self, request: web.Request) -> web.Response:
        token = request.cookies.get("session")
        if token:
            await db.execute("DELETE FROM web_sessions WHERE token = ?", (token,))
        resp = web.HTTPFound("/")
        resp.del_cookie("session")
        return resp

    # ------------------------------------------------------------ API

    async def api_items(self, request: web.Request) -> web.Response:
        items = [
            {k: v for k, v in item.items() if k not in ("role", "role_names")}
            for item in ITEMS
        ]
        return web.json_response({"ok": True, "items": items})

    async def api_me(self, request: web.Request) -> web.Response:
        sess = await self._session_user(request)
        if not sess:
            return web.json_response({"ok": True, "logged_in": False})

        guild = self._guild()
        if guild is None:
            return _json_error("봇이 아직 서버에 연결되지 않았어요.", 503)

        user_id = sess["user_id"]
        lrow = await db.fetch_one(
            "SELECT level, xp FROM user_levels WHERE guild_id = ? AND user_id = ?",
            (guild.id, user_id),
        )
        level = lrow["level"] if lrow else 0
        xp = lrow["xp"] if lrow else 0
        needed = 5 * (level**2) + 50 * level + 100

        prow = await db.fetch_one(
            "SELECT points FROM user_points WHERE guild_id = ? AND user_id = ?",
            (guild.id, user_id),
        )
        points = prow["points"] if prow else 0

        owned_rows = await db.fetch_all(
            "SELECT DISTINCT item_id FROM shop_purchases WHERE guild_id = ? AND user_id = ?",
            (guild.id, user_id),
        )
        owned = [r["item_id"] for r in owned_rows]

        # 현재 이용 중인 기간제 역할 (item_id -> 만료 시각 유닉스 초).
        # 상점에서 "적용중" 표시에 쓴다.
        active: dict[str, int] = {}
        now = int(time.time())
        for it in ITEMS:
            if it["type"] != "timed_role":
                continue
            role = None
            for cand in it["role_names"]:
                role = discord.utils.get(guild.roles, name=cand)
                if role is not None:
                    break
            if role is None:
                continue
            trow = await db.fetch_one(
                "SELECT expires_at FROM timed_roles WHERE guild_id = ? AND user_id = ? AND role_id = ?",
                (guild.id, user_id, role.id),
            )
            if trow and trow["expires_at"] > now:
                active[it["id"]] = trow["expires_at"]

        return web.json_response(
            {
                "ok": True,
                "logged_in": True,
                "username": sess["username"],
                "avatar_url": sess["avatar_url"],
                "guild_name": guild.name,
                "level": level,
                "xp": xp,
                "xp_needed": needed,
                "points": points,
                "owned": owned,
                "active": active,
            }
        )

    async def api_buy(self, request: web.Request) -> web.Response:
        sess = await self._session_user(request)
        if not sess:
            return _json_error("로그인이 필요해요.", 401)

        try:
            data = await request.json()
        except Exception:
            return _json_error("잘못된 요청이에요.")

        item = ITEMS_BY_ID.get(data.get("item_id"))
        if item is None:
            return _json_error("없는 상품이에요.")

        guild = self._guild()
        if guild is None:
            return _json_error("봇이 아직 서버에 연결되지 않았어요.", 503)

        user_id = sess["user_id"]
        member = await self._get_member(guild, user_id)
        if member is None:
            return _json_error(f"'{guild.name}' 서버의 멤버가 아니에요. 서버에 먼저 들어와주세요.")

        prow = await db.fetch_one(
            "SELECT points FROM user_points WHERE guild_id = ? AND user_id = ?",
            (guild.id, user_id),
        )
        points = prow["points"] if prow else 0
        if points < item["price"]:
            return _json_error(f"포인트가 부족해요. (보유 {points:,}P / 필요 {item['price']:,}P)")

        # ---- 지급 (성공한 경우에만 포인트 차감) ----
        try:
            if item["type"] == "timed_role":
                role = None
                for cand in item["role_names"]:
                    role = discord.utils.get(guild.roles, name=cand)
                    if role is not None:
                        break
                if role is None:
                    role = await guild.create_role(
                        name=item["role_names"][0],
                        colour=discord.Colour(0xF47FFF),
                        reason="웹 상점 기간제 역할 자동 생성",
                    )
                await member.add_roles(role, reason="웹 상점 구매")

                # 이미 이용 중이면 남은 기간에 이어서 연장
                now = int(time.time())
                trow = await db.fetch_one(
                    "SELECT expires_at FROM timed_roles WHERE guild_id = ? AND user_id = ? AND role_id = ?",
                    (guild.id, user_id, role.id),
                )
                base = max(now, trow["expires_at"]) if trow else now
                expires = base + item["days"] * 86400
                await db.execute(
                    """
                    INSERT INTO timed_roles (guild_id, user_id, role_id, expires_at) VALUES (?, ?, ?, ?)
                    ON CONFLICT(guild_id, user_id, role_id) DO UPDATE SET
                        expires_at = excluded.expires_at
                    """,
                    (guild.id, user_id, role.id, expires),
                )

            elif item["type"] == "custom_role":
                role_name = str(data.get("role_name", "")).strip()
                if not (1 <= len(role_name) <= 20):
                    return _json_error("역할 이름을 1~20자로 입력해주세요.")
                if "@" in role_name or "#" in role_name:
                    return _json_error("역할 이름에 @, # 기호는 쓸 수 없어요.")

                hex_color = str(data.get("color", "")).lstrip("#")
                if len(hex_color) != 6:
                    return _json_error("색상을 골라주세요.")
                try:
                    colour = discord.Colour(int(hex_color, 16))
                except ValueError:
                    return _json_error("색상 코드가 올바르지 않아요.")

                # 이미 만든 개인 역할이 있으면 새로 만들지 않고 이름/색만 바꾼다
                prow2 = await db.fetch_one(
                    "SELECT role_id FROM personal_roles WHERE guild_id = ? AND user_id = ?",
                    (guild.id, user_id),
                )
                role = guild.get_role(prow2["role_id"]) if prow2 else None
                if role is not None:
                    await role.edit(name=role_name, colour=colour, reason="웹 상점 개인 역할 변경")
                else:
                    role = await guild.create_role(
                        name=role_name, colour=colour, reason="웹 상점 개인 역할 제작"
                    )
                    await db.execute(
                        """
                        INSERT INTO personal_roles (guild_id, user_id, role_id) VALUES (?, ?, ?)
                        ON CONFLICT(guild_id, user_id) DO UPDATE SET role_id = excluded.role_id
                        """,
                        (guild.id, user_id, role.id),
                    )
                await member.add_roles(role, reason="웹 상점 구매")
                # 역할 색이 보이려면 다른 색 역할보다 위에 있어야 하므로 봇 바로 아래로 올려본다
                try:
                    await role.edit(position=max(1, guild.me.top_role.position - 1))
                except discord.HTTPException:
                    logger.warning(f"개인 역할 위치 조정 실패: {role_name}")

        except discord.Forbidden:
            return _json_error(
                "봇 권한이 부족해서 역할을 달지 못했어요. 관리자에게 알려주세요. "
                "(봇에게 '역할 관리' 권한 필요)", 500,
            )

        # ---- 포인트 차감 + 구매 기록 ----
        await db.execute(
            "UPDATE user_points SET points = points - ? WHERE guild_id = ? AND user_id = ?",
            (item["price"], guild.id, user_id),
        )
        await db.execute(
            "INSERT INTO shop_purchases (guild_id, user_id, item_id, price) VALUES (?, ?, ?, ?)",
            (guild.id, user_id, item["id"], item["price"]),
        )
        logger.info(f"상점 구매: user={user_id} item={item['id']} price={item['price']}")
        return web.json_response({"ok": True, "points": points - item["price"]})


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(WebShopCog(bot))
