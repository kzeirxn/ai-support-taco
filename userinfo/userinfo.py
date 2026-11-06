"""
user_info - Modmail Plugin (Evalyn)
Looks the ticket opener up on Evalyn when a support thread opens and posts a
summary embed for staff to review.

Ported from the Taco Licensing version: same card and same `?userinfo`
command, but it reads Evalyn's support lookup instead of the licensing
staff API (`/staff/users`), which is no longer supported.

Setup
-----
On Evalyn, set:

    SUPPORT_BOT_SECRET   - a long random string

Then, from Discord (bot owner only; it deletes your message so the token
doesn't stay in the channel):

    ?userinfo setup https://evalyn.tacogroup.uk <SUPPORT_BOT_SECRET>

That's stored in Modmail's database, so it works on Docker installs where
there's no .env to edit. Environment variables still win if they're set:

    EVALYN_BASE_URL      - e.g. https://evalyn.tacogroup.uk
    EVALYN_SUPPORT_TOKEN - the same value as SUPPORT_BOT_SECRET

Never hard-code the token in this file. It reads every Evalyn account.

If the `evalyn` plugin (modmail-plugin/evalyn) is also installed, this one
leaves the automatic thread card to it so tickets don't get two cards.
`?userinfo` keeps working either way.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from datetime import datetime, timezone

import aiohttp
import discord
from discord.ext import commands

from core import checks
from core.models import PermissionLevel

log = logging.getLogger("Modmail.user_info")

COLOUR_OK    = 0x5865F2   # blurple  – clean account
COLOUR_WARN  = 0xFEE75C   # yellow   – suspended / flags
COLOUR_ERROR = 0xED4245   # red      – fetch failed

DEFAULT_BASE_URL = "https://evalyn.tacogroup.uk"


def _normalize_url(url: str | None) -> str:
    """`evalyn.tacogroup.uk` and `<https://evalyn.tacogroup.uk/>` both become
    `https://evalyn.tacogroup.uk`. Without a scheme aiohttp refuses the URL."""
    url = (url or "").strip().strip("<>").strip().rstrip("/")
    if url and not re.match(r"^https?://", url, re.IGNORECASE):
        url = "https://" + url
    return url

TIER_LABELS = {
    "master":   "🌟 Master",
    "partner":  "🤝 Partner",
    "verified": "✅ Verified",
    "standard": "Standard",
}

TICKET_ICONS = {
    "open":    "🟢",
    "pending": "🟡",
    "closed":  "⚪",
}

# Fixes for each error Evalyn can answer with, shown on the error card.
ERROR_HINTS = {
    "not_configured":        "Run `?userinfo setup <base_url> <token>` (or set `EVALYN_SUPPORT_TOKEN`).",
    "invalid_token":         "The token doesn't match `SUPPORT_BOT_SECRET` on Evalyn. Run `?userinfo setup` again.",
    "missing_authorization": "No token was sent. Run `?userinfo setup`.",
    "rate_limited":          "Too many lookups this minute. Try again shortly.",
    "unreachable":           "Couldn't reach Evalyn. Check `EVALYN_BASE_URL` and the status page.",
    "invalid_url":           "Run `?userinfo setup https://evalyn.tacogroup.uk <token>` with the full address.",
    "timeout":               "Evalyn took longer than 8 seconds to answer.",
}


class UserInfo(commands.Cog):
    """Fetches Evalyn account data when a support thread is opened."""

    def __init__(self, bot):
        self.bot = bot
        self.db = bot.api.get_plugin_partition(self)
        # Config saved by `?userinfo setup`. None until first read.
        self._stored: dict | None = None
        self.session: aiohttp.ClientSession | None = None
        # The thread card and a `?userinfo` a moment later ask the same thing.
        # Short enough that a suspension never reads as stale.
        self._cache: dict[str, tuple[float, dict]] = {}
        self._cache_ttl = 60.0

    async def cog_load(self):
        self.session = aiohttp.ClientSession()
        await self._ensure_config()
        log.info("user_info: base_url=%s token_set=%s", self.base_url, bool(self.token))

    async def cog_unload(self):
        if self.session:
            await self.session.close()

    # ── config ────────────────────────────────────────────────────────────────

    async def _ensure_config(self) -> None:
        """Read what `?userinfo setup` saved, once. Retried next call on failure."""
        if self._stored is not None:
            return
        try:
            self._stored = (await self.db.find_one({"_id": "config"})) or {}
        except Exception as exc:
            log.error("user_info: couldn't read stored config: %s", exc)

    async def _save_config(self, **values) -> None:
        await self.db.find_one_and_update({"_id": "config"}, {"$set": values}, upsert=True)
        self._stored = None
        self._cache.clear()
        await self._ensure_config()

    @property
    def base_url(self) -> str:
        stored = self._stored or {}
        return _normalize_url(os.environ.get("EVALYN_BASE_URL") or stored.get("base_url") or DEFAULT_BASE_URL)

    @property
    def token(self) -> str:
        return os.environ.get("EVALYN_SUPPORT_TOKEN") or (self._stored or {}).get("token") or ""

    @property
    def headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
        }

    # ── data fetching ─────────────────────────────────────────────────────────

    async def lookup(self, **target) -> dict:
        """
        GET /api/support/lookup. Accepts discord_id, roblox_id, roblox_username,
        username, email or user_id.

        Returns the response body (`found` tells you whether an account
        matched), or a dict with `_error` set. Never raises: a failed lookup
        must not stop a ticket opening. The `ai` plugin calls this too.
        """
        await self._ensure_config()
        if not (self.base_url and self.token):
            return {"_error": "not_configured"}

        params = {k: str(v) for k, v in target.items() if v}
        key = repr(sorted(params.items()))
        hit = self._cache.get(key)
        if hit and time.monotonic() - hit[0] < self._cache_ttl:
            return hit[1]

        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()

        url = f"{self.base_url}/api/support/lookup"
        try:
            async with self.session.get(
                url, params=params, headers=self.headers,
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                try:
                    data = await resp.json(content_type=None)
                except (aiohttp.ContentTypeError, ValueError):
                    data = {}
                if resp.status != 200:
                    log.warning("Evalyn returned %s for %s", resp.status, url)
                    return {
                        "_error": (data or {}).get("code") or f"http_{resp.status}",
                        "_message": (data or {}).get("error"),
                    }
        except asyncio.TimeoutError:
            log.error("Timeout fetching %s", url)
            return {"_error": "timeout"}
        except aiohttp.InvalidURL:
            log.error("Invalid Evalyn URL: %s", url)
            return {"_error": "invalid_url", "_message": f"`{self.base_url}` isn't a valid web address."}
        except aiohttp.ClientError as exc:
            log.error("HTTP error fetching %s: %s", url, exc)
            return {"_error": "unreachable", "_message": str(exc)}

        self._cache[key] = (time.monotonic(), data)
        return data

    async def fetch_by_discord_id(self, discord_id: int) -> dict:
        return await self.lookup(discord_id=discord_id)

    # ── embed builder ─────────────────────────────────────────────────────────

    @staticmethod
    def _fmt_date(value: str | None, style: str = "D") -> str:
        """ISO string → Discord timestamp, so it reads in the viewer's timezone."""
        if not value:
            return "—"
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return str(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return f"<t:{int(dt.timestamp())}:{style}>"

    @staticmethod
    def _clip(text: str, limit: int = 1024) -> str:
        """Embed fields cap at 1024 characters. Cut on a line boundary."""
        if len(text) <= limit:
            return text
        cut = text[: limit - 2]
        if "\n" in cut:
            cut = cut[: cut.rindex("\n")]
        return cut + "…"

    def _build_embed(self, discord_user: discord.abc.User, data: dict) -> discord.Embed:
        base = discord.Embed(title="User Information", timestamp=datetime.now(timezone.utc))
        base.set_author(
            name=f"{discord_user} (ID: {discord_user.id})",
            icon_url=discord_user.display_avatar.url,
        )

        if data.get("_error"):
            code = data["_error"]
            base.description = (
                f"⚠️ Couldn't look this user up on Evalyn.\n"
                f"{data.get('_message') or ERROR_HINTS.get(code, 'Unknown error.')}"
            )
            if data.get("_message") and code in ERROR_HINTS:
                base.add_field(name="Fix", value=ERROR_HINTS[code], inline=False)
            base.colour = COLOUR_ERROR
            base.set_footer(text=f"Evalyn · code: {code}")
            return base

        if not data.get("found"):
            base.description = (
                "No Evalyn account is linked to this Discord user. They've either "
                "never signed up, or signed up with a different Discord account."
            )
            base.colour = COLOUR_OK
            base.set_footer(text="Evalyn · support data")
            return base

        account    = data.get("account") or {}
        workspaces = data.get("workspaces") or []
        tickets    = data.get("tickets") or {}
        flags      = data.get("flags") or []

        is_suspended  = bool(account.get("suspended"))
        ws_suspended  = [w for w in workspaces if w.get("is_suspended")]
        base.colour   = COLOUR_WARN if (is_suspended or ws_suspended) else COLOUR_OK

        roblox = account.get("roblox") or {}
        avatar = roblox.get("avatar") or account.get("avatar")
        if avatar:
            base.set_thumbnail(url=avatar)

        # ── Account overview ──────────────────────────────────────────────────
        username  = account.get("username") or "Unknown"
        display   = account.get("display_name")
        name_line = f"{display} (@{username})" if display and display != username else username
        roblox_line = (
            f"[{roblox.get('username')}]({roblox.get('profile_url')}) · `{roblox.get('id')}`"
            if roblox else "Not linked"
        )
        owned     = sum(1 for w in workspaces if w.get("owner"))
        priority  = "Yes" if "priority_support" in flags else "No"
        staff     = account.get("platform_role") or ("admin" if account.get("is_taco_admin") else None)

        last = account.get("last_login_detail") or {}
        if last:
            failed = "" if last.get("succeeded", True) else " ⚠️ **failed**"
            last_line = f"{self._fmt_date(last.get('at'), 'R')} via {last.get('method') or 'unknown'}{failed}"
        else:
            last_line = self._fmt_date(account.get("last_login"), "R")

        lines = [
            f"**Username:** {name_line}",
            f"**Evalyn ID:** `{account.get('user_id')}`",
            f"**Roblox:** {roblox_line}",
            f"**Workspaces:** {len(workspaces)} ({owned} owned)  |  **Priority Support:** {priority}",
            f"**Open tickets:** {tickets.get('open', 0)}",
            f"**Registered:** {self._fmt_date(account.get('created_at'))}",
            f"**Last sign-in:** {last_line}",
        ]
        if account.get("email"):
            lines.insert(2, f"**Email:** {account['email']}")
        if staff:
            lines.append(f"**Platform role:** {staff}")

        base.add_field(name="Account", value=self._clip("\n".join(lines)), inline=False)

        # ── Suspension status ─────────────────────────────────────────────────
        if is_suspended:
            suspension = account.get("suspension") or {}
            reason = suspension.get("reason") or "No reason on record"
            since  = f"\n**Since:** {self._fmt_date(suspension.get('since'), 'R')}" if suspension.get("since") else ""
            base.add_field(
                name="🚫 Account Suspended",
                value=self._clip(f"**Reason:** {reason}{since}\nThey can appeal at `/suspended/appeal`."),
                inline=False,
            )
        elif ws_suspended:
            base.add_field(
                name="⚠️ Moderation",
                value="Account is fine, but one of their workspaces is suspended (see below).",
                inline=False,
            )
        else:
            base.add_field(name="✅ Moderation", value="No active suspension.", inline=False)

        # ── Workspaces (what licenses used to be on this card) ────────────────
        if workspaces:
            rows = []
            for w in workspaces[:6]:
                role = "Owner" if w.get("owner") else f"{w.get('role')} (rank {w.get('rank')})"
                tier = TIER_LABELS.get(w.get("tier"), w.get("tier_label") or "Standard")
                row  = f"**[{w.get('name')}]({w.get('url')})** · {tier} · {role}"
                if w.get("is_suspended"):
                    row += f"\n  ⚠️ Suspended {self._fmt_date(w.get('suspended_at'), 'R')}"
                    if w.get("suspended_reason"):
                        row += f" — {w['suspended_reason']}"
                else:
                    row += f" · {w.get('member_count', 0)} members"
                rows.append(row)
            if len(workspaces) > 6:
                rows.append(f"…and {len(workspaces) - 6} more")
            base.add_field(name=f"Workspaces ({len(workspaces)})", value=self._clip("\n".join(rows)), inline=False)
        else:
            base.add_field(name="Workspaces", value="None. Signed up but never created or joined one.", inline=False)

        # ── Recent tickets (what coin transactions used to be on this card) ───
        recent = tickets.get("recent") or []
        if not recent:
            base.add_field(name="Recent Tickets", value="None on record.", inline=False)
        else:
            rows = []
            for t in recent[:5]:
                icon = TICKET_ICONS.get(t.get("status"), "⚪")
                esc  = " ⬆️" if t.get("escalated") else ""
                rows.append(
                    f"{icon} `{self._fmt_date(t.get('created_at'), 'd')}` **{t.get('subject')}** "
                    f"— {t.get('category')}/{t.get('priority')}{esc}"
                )
            base.add_field(name=f"Recent Tickets ({len(rows)} shown)", value=self._clip("\n".join(rows)), inline=False)

        footer = "Evalyn · support data"
        if not data.get("pii_included"):
            footer += " · email/IP withheld"
        base.set_footer(text=footer)
        return base

    # ── shared fetch + post ───────────────────────────────────────────────────

    async def _fetch_and_send(self, channel: discord.abc.Messageable, discord_user: discord.abc.User):
        data  = await self.fetch_by_discord_id(discord_user.id)
        embed = self._build_embed(discord_user, data)
        try:
            await channel.send(embed=embed)
        except discord.HTTPException as exc:
            log.error("Failed to send user info embed: %s", exc)

    # ── auto-post on thread open ──────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_thread_ready(self, thread, creator, category, initial_message):
        await self._ensure_config()
        if not self.token:
            log.warning("user_info: no Evalyn token. Run ?userinfo setup <base_url> <token>.")
            return
        if self.bot.get_cog("Evalyn"):
            return  # the evalyn plugin already posts a card on every thread
        recipient = thread.recipient or creator
        if recipient is None:
            log.warning("user_info: could not determine recipient, skipping.")
            return
        log.info("Fetching Evalyn data for %s (%s)", recipient, recipient.id)
        await self._fetch_and_send(thread.channel, recipient)

    # ── manual command ────────────────────────────────────────────────────────

    @commands.group(name="userinfo", invoke_without_command=True)
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    async def userinfo_command(self, ctx, user: discord.User | None = None):
        """
        Manually fetch Evalyn info for a user.
        Usage: ?userinfo [@user | user_id]
        Defaults to the current thread's recipient.
        """
        if user is None:
            thread = await self.bot.threads.find(channel=ctx.channel)
            if thread is None or thread.recipient is None:
                await ctx.send("Provide a user or run this inside a Modmail thread.")
                return
            user = thread.recipient

        async with ctx.typing():
            await self._fetch_and_send(ctx.channel, user)

    @userinfo_command.command(name="setup")
    @checks.has_permissions(PermissionLevel.OWNER)
    async def userinfo_setup(self, ctx, base_url: str, token: str):
        """
        Connect to Evalyn: ?userinfo setup <base_url> <SUPPORT_BOT_SECRET>
        Saved in Modmail's database. Your message is deleted straight away.
        """
        # The token was just typed into a channel. Get it out of the log first.
        try:
            await ctx.message.delete()
        except (discord.Forbidden, discord.NotFound):
            pass

        await self._save_config(base_url=_normalize_url(base_url), token=token.strip())

        overridden = [k for k in ("EVALYN_BASE_URL", "EVALYN_SUPPORT_TOKEN") if os.environ.get(k)]
        probe = await self.lookup(discord_id=ctx.author.id)
        if probe.get("_error"):
            await ctx.send(embed=self._build_embed(ctx.author, probe))
            return

        embed = discord.Embed(
            title="✅ Evalyn connected",
            description=f"Talking to {self.base_url}. Your message with the token was deleted.",
            colour=COLOUR_OK,
        )
        embed.add_field(
            name="Email / IP on cards",
            value="Included." if probe.get("pii_included") else "Withheld (set `SUPPORT_BOT_INCLUDE_PII=true` on Evalyn to include).",
            inline=False,
        )
        if overridden:
            embed.add_field(
                name="Note",
                value=f"{', '.join(f'`{k}`' for k in overridden)} is set on the host and takes priority over this.",
                inline=False,
            )
        embed.set_footer(text="If anyone saw the token, change SUPPORT_BOT_SECRET and run this again.")
        await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(UserInfo(bot))
