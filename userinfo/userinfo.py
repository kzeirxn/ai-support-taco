"""
user_info - Modmail Plugin
Fetches player/user data from your existing staff API when a support thread
opens, and posts a summary embed for staff to review.

Setup
-----
Add these to your Modmail config (.env / config.py):

    BACKEND_BASE_URL   - e.g. https://api.yoursite.com/api/v1
    BACKEND_API_KEY    - JWT for a staff/master-admin service account
"""

import asyncio
import logging
from datetime import datetime, timezone

import aiohttp
import discord
from discord.ext import commands

log = logging.getLogger("Modmail.user_info")

COLOUR_OK      = 0x5865F2   # blurple  – clean account
COLOUR_WARN    = 0xFEE75C   # yellow   – suspended / flags
COLOUR_ERROR   = 0xED4245   # red      – fetch failed

PLAN_LABELS = {
    "pro":        "⭐ Pro",
    "enterprise": "💎 Enterprise",
    "free":       "Free",
}


class UserInfo(commands.Cog):
    """Fetches backend user data when a support thread is opened."""

    def __init__(self, bot):
        self.bot = bot
        self.session: aiohttp.ClientSession | None = None

    async def cog_load(self):
        self.session = aiohttp.ClientSession()

    async def cog_unload(self):
        if self.session:
            await self.session.close()

    # ── config ────────────────────────────────────────────────────────────────

    @property
    def base_url(self) -> str:
        return ("https://app.tacolicensing.org/api/v1").rstrip("/")

    @property
    def headers(self) -> dict:
        return {
            "Authorization": f"Bearer {'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ1c2VySWQiOiJjbWtlczZjdGgwMDAwcWdrMHZ0Z284ZWExIiwicm9ibG94SWQiOjU1NDUzNjcxLCJ1c2VybmFtZSI6Imt6ZWlyeG4iLCJpc1RhY29BZG1pbiI6dHJ1ZSwiaWF0IjoxNzc1OTIxNjA3LCJleHAiOjE3NzY1MjY0MDd9.XkHR-PS5ueY1Ot6K0ikXHexPoIpCxCAyvLS3VpE4WOQ'}",
            "Accept": "application/json",
        }

    # ── data fetching ─────────────────────────────────────────────────────────

    async def _get(self, path: str) -> dict | None:
        """GET from the staff API. Returns parsed JSON, {} on 404, None on error."""
        url = f"{self.base_url}{path}"
        try:
            async with self.session.get(
                url, headers=self.headers, timeout=aiohttp.ClientTimeout(total=8)
            ) as resp:
                if resp.status == 404:
                    return {}
                if resp.status == 403:
                    log.error("user_info: BACKEND_API_KEY lacks staff access (%s)", url)
                    return None
                if resp.status != 200:
                    log.warning("Backend returned %s for %s", resp.status, url)
                    return None
                return await resp.json()
        except asyncio.TimeoutError:
            log.error("Timeout fetching %s", url)
            return None
        except aiohttp.ClientError as exc:
            log.error("HTTP error fetching %s: %s", url, exc)
            return None

    async def fetch_by_discord_id(self, discord_id: int) -> dict | None:
        """
        The staff /users list supports searching by discordId.
        Returns the first matching user dict, {} if not found, None on error.
        """
        result = await self._get(f"/staff/users?search={discord_id}&limit=1")
        if result is None:
            return None
        users = result.get("users", [])
        if not users:
            return {}
        # Fetch the full user record (includes coinTransactions)
        full = await self._get(f"/staff/users/{users[0]['id']}")
        if full is None:
            return None
        return full.get("user", {})

    # ── embed builder ─────────────────────────────────────────────────────────

    @staticmethod
    def _fmt_date(value: str | None, fmt: str = "%Y-%m-%d") -> str:
        if not value:
            return "—"
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).strftime(fmt)
        except (ValueError, TypeError):
            return str(value)

    def _build_embed(self, discord_user: discord.User, user: dict | None) -> discord.Embed:
        base = discord.Embed(timestamp=datetime.now(timezone.utc))
        base.set_author(
            name=f"{discord_user} (ID: {discord_user.id})",
            icon_url=discord_user.display_avatar.url,
        )

        if user is None:
            base.title = "User Information"
            base.description = (
                "⚠️ Could not reach the backend.\n"
                "Check `BACKEND_BASE_URL` and `BACKEND_API_KEY`."
            )
            base.colour = COLOUR_ERROR
            return base

        if not user:
            base.title = "User Information"
            base.description = "No account found in the backend for this Discord user."
            base.colour = COLOUR_OK
            return base

        is_suspended = user.get("isSuspended", False)
        base.title = "User Information"
        base.colour = COLOUR_WARN if is_suspended else COLOUR_OK

        # ── Account overview ──────────────────────────────────────────────────
        plan_label = PLAN_LABELS.get(user.get("plan", "free"), user.get("plan", "Free").title())
        coins      = user.get("tacoCoins", 0)
        vip        = "Yes" if user.get("hasVipBadge") else "No"
        priority   = "Yes" if user.get("hasPrioritySupport") else "No"
        roblox_id  = user.get("robloxId") or "—"
        username   = user.get("username", "Unknown")
        registered = self._fmt_date(user.get("createdAt"))

        # Extra entitlements
        bonus_ws   = user.get("bonusWorkspaces", 0)
        bonus_ps   = user.get("bonusProductSlots", 0)
        ws_count   = user.get("_count", {}).get("ownedWorkspaces", "—")
        lic_count  = user.get("_count", {}).get("licenses", "—")

        base.add_field(
            name="Account",
            value=(
                f"**Username:** {username}\n"
                f"**Roblox ID:** `{roblox_id}`\n"
                f"**Plan:** {plan_label}  |  **TacoCoins:** {coins:,}\n"
                f"**VIP:** {vip}  |  **Priority Support:** {priority}\n"
                f"**Workspaces:** {ws_count} (+{bonus_ws} bonus)  |  "
                f"**Licenses:** {lic_count} (+{bonus_ps} bonus slots)\n"
                f"**Registered:** {registered}"
            ),
            inline=False,
        )

        # ── Suspension status ─────────────────────────────────────────────────
        if is_suspended:
            reason = user.get("suspendReason") or "No reason provided"
            base.add_field(
                name="🚫 Account Suspended",
                value=f"**Reason:** {reason}",
                inline=False,
            )
        else:
            base.add_field(name="✅ Moderation", value="No active suspension.", inline=False)

        # ── Recent coin transactions (proxy for purchase/activity history) ────
        txns = user.get("coinTransactions", [])
        if not txns:
            base.add_field(name="Recent Transactions", value="None on record.", inline=False)
        else:
            lines = []
            for tx in txns[:8]:
                amount  = tx.get("amount", 0)
                reason  = tx.get("reason", "—")
                date    = self._fmt_date(tx.get("createdAt"), "%Y-%m-%d %H:%M")
                sign    = "+" if amount >= 0 else ""
                lines.append(f"`{date}` **{sign}{amount:,}** — {reason}")
            base.add_field(
                name=f"Recent Transactions ({len(txns)} shown)",
                value="\n".join(lines),
                inline=False,
            )

        base.set_footer(text="Taco Licensing · staff data")
        return base

    # ── shared fetch + post ───────────────────────────────────────────────────

    async def _fetch_and_send(self, channel: discord.TextChannel, discord_user: discord.User):
        user  = await self.fetch_by_discord_id(discord_user.id)
        embed = self._build_embed(discord_user, user)
        try:
            await channel.send(embed=embed)
        except discord.HTTPException as exc:
            log.error("Failed to send user info embed: %s", exc)

    # ── auto-post on thread open ──────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_thread_ready(self, thread, creator, category, initial_message):
        if not self.base_url:
            log.warning("user_info: BACKEND_BASE_URL is not set.")
            return
        log.info("Fetching backend data for %s (%s)", creator, creator.id)
        await self._fetch_and_send(thread.channel, creator)

    # ── manual command ────────────────────────────────────────────────────────

    @commands.command(name="userinfo")
    @commands.has_permissions(manage_messages=True)
    async def userinfo_command(self, ctx, user: discord.User | None = None):
        """
        Manually fetch backend info for a user.
        Usage: ?userinfo [@user | user_id]
        Defaults to the current thread's recipient.
        """
        if user is None:
            thread = await self.bot.threads.find(channel=ctx.channel)
            if thread is None:
                await ctx.send("Provide a user or run this inside a Modmail thread.")
                return
            user = thread.recipient

        async with ctx.typing():
            await self._fetch_and_send(ctx.channel, user)


async def setup(bot):
    await bot.add_cog(UserInfo(bot))
