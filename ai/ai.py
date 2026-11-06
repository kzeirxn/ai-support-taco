"""
ai_assistant - Modmail Plugin (Evalyn)
=======================================
A virtual AI assistant powered by Ollama that automatically replies to users
while a ticket is unclaimed, attempting to resolve issues before a mod steps in.

When a mod claims the thread, the AI stops responding immediately.

Ported from the Taco Licensing version. It now answers about Evalyn, from
support_knowledge.md in this folder. If the `userinfo` or `evalyn` plugin is
installed, it also reads the user's Evalyn account (Roblox linked? suspended?
which workspaces?) so it can answer "why won't my group sync" without asking
first. No extra setup: it borrows that plugin's lookup and token.

Setup, from Discord (bot owner only, saved in Modmail's database, so it works
on Docker installs with no .env to edit). Everything has a default, so this
is only for changing them:
  ?ai_config                                  show current settings
  ?ai_config ollama_url https://ai.tacogroup.uk
  ?ai_config ollama_model gemma4:e2b
  ?ai_config evalyn_url https://evalyn.tacogroup.uk   (links the AI gives out)
  ?ai_config escalation_role @Support                 (or a role id, or none)
  ?ai_config <setting> reset                          back to the default

evalyn_url follows `?userinfo setup` unless you set it here. Environment
variables (OLLAMA_URL, OLLAMA_MODEL, EVALYN_BASE_URL, ESCALATION_ROLE_ID)
still win if they're set on the host.
"""

import asyncio
import os
import re

import aiohttp
import discord
from discord.ext import commands

from core import checks
from core.models import PermissionLevel


BASE_SYSTEM_PROMPT = """You are a helpful AI support assistant for Evalyn, a free staff management platform for Roblox groups, run by Taco.
A user has opened a support ticket and no staff member has claimed it yet.
Your job is to:
1. Greet the user warmly and acknowledge their issue.
2. Use the knowledge base below to try to resolve their issue accurately.
3. If unsure, ask a clarifying question. Do NOT guess or make up answers, menu paths, or features.
4. Let them know a staff member will follow up if you cannot fully resolve it.
5. Links: the dashboard is {base_url}, the guides are {base_url}/docs, and in-app support tickets are at {base_url}/tickets.
Keep your replies concise, friendly, and professional.
Do NOT pretend to be a human staff member. You are an AI assistant.
If the issue requires account-specific action only staff can perform, say so clearly.
Never ask for passwords, PINs, Open Cloud keys, bot tokens or API secrets, and tell the user not to post them.
Taco Licensing is no longer supported. If someone asks about licenses, products or TacoCoins, say so politely and offer help with Evalyn instead.

--- ABOUT THIS USER ---
{account}
--- END ABOUT THIS USER ---
Use this to tailor your answer (for example, a user with no Roblox account linked can't have shifts tracked).
Don't recite it back, and don't share internal details beyond what the user needs.
If their account is suspended, don't speculate about why. Tell them they can appeal at {base_url}/suspended/appeal and that staff will follow up.

--- KNOWLEDGE BASE ---
{knowledge}
--- END KNOWLEDGE BASE ---
"""

KNOWLEDGE_FILE = os.path.join(os.path.dirname(__file__), "support_knowledge.md")

SENTIMENT_PROMPT = """You are a sentiment classifier. Analyse the user message below and respond with ONLY one word:
- NEGATIVE  (user is angry, frustrated, upset, threatening, or using strong negative language)
- NEUTRAL   (user is calm, asking a question, or making a normal request)
- POSITIVE  (user is happy, grateful, or clearly satisfied)

Respond with ONLY the single word. No punctuation, no explanation.

Message: {message}"""

RESOLUTION_PROMPT = """You are reviewing a support conversation between an AI assistant and a user.
Your job is to determine if the user's issue is FULLY resolved.

Rules:
- If the user's LAST message contains a question, uncertainty, or asks for more help → UNRESOLVED
- If the user's LAST message is a thank you, confirmation, or indicates they are satisfied → RESOLVED
- If the AI just gave an answer but the user has NOT yet responded to confirm it helped → UNRESOLVED
- When in doubt → UNRESOLVED

Respond with ONLY one word: RESOLVED or UNRESOLVED. No punctuation, no explanation.

Conversation:
{history}

What is the user's last message? Does it contain a question or request? If yes → UNRESOLVED."""

SUMMARY_PROMPT = """You are summarising a support ticket conversation for a staff member who is taking over.
Write a concise summary (3-5 bullet points max) covering:
- What the user's issue was
- What the AI tried or explained
- Current status (resolved, unresolved, escalated)
- Any important details the staff member should know before replying

Be brief and factual. Use plain text, no markdown headers.

Conversation:
{history}"""

AUTO_CLOSE_DELAY = 120  # seconds to wait for user confirmation before closing
OLLAMA_TIMEOUT   = 120  # seconds, the model can be slow on first response
OLLAMA_RETRIES   = 2    # number of retries on timeout before giving up

# Staff running these in a thread is looking something up, not claiming it.
PASSIVE_COMMANDS = {"userinfo", "evalyn", "ai_status", "ai_config"}

# What `?ai_config` can change: setting -> (env var that overrides it, default).
SETTINGS = {
    "ollama_url":      ("OLLAMA_URL", "https://ai.tacogroup.uk"),
    "ollama_model":    ("OLLAMA_MODEL", "gemma4:e2b"),
    "evalyn_url":      ("EVALYN_BASE_URL", "https://evalyn.tacogroup.uk"),
    "escalation_role": ("ESCALATION_ROLE_ID", "1401301393767141541"),
}

NO_ACCOUNT_INFO = "(No account information available.)"


def _load_system_prompt(base_url: str, account: str | None) -> str:
    """Load the knowledge base and the user's account summary into the system prompt."""
    try:
        with open(KNOWLEDGE_FILE, "r", encoding="utf-8") as f:
            knowledge = f.read().strip()
    except FileNotFoundError:
        print(f"[ai_assistant] Knowledge base not found at {KNOWLEDGE_FILE}")
        knowledge = "(No knowledge base found. Answer using general knowledge only.)"
    return BASE_SYSTEM_PROMPT.format(
        base_url=base_url,
        account=account or NO_ACCOUNT_INFO,
        knowledge=knowledge,
    )


def _account_summary(data: dict | None) -> str | None:
    """
    Turn an Evalyn support lookup into a few lines the model can use.

    Deliberately leaves out ids, email, IP, staff flags and the suspension
    reason. The model's reply goes straight to the user, so anything here
    could be repeated back to them.
    """
    if not data or data.get("_error"):
        return None
    if not data.get("found"):
        return (
            "No Evalyn account is linked to this user's Discord account. They may not have "
            "signed up yet, or they signed up with Roblox or a different Discord account."
        )

    account    = data.get("account") or {}
    workspaces = data.get("workspaces") or []
    tickets    = data.get("tickets") or {}
    roblox     = account.get("roblox")

    lines = [f"Evalyn username: {account.get('username') or 'unknown'}"]
    lines.append(
        f"Roblox account linked: yes ({roblox.get('username')})" if roblox
        else "Roblox account linked: NO (shift tracking and group role changes need one; link it at /account/linking)"
    )
    lines.append("Account suspended: YES" if account.get("suspended") else "Account suspended: no")

    if workspaces:
        lines.append("Workspaces:")
        for w in workspaces[:8]:
            role = "owner" if w.get("owner") else (w.get("role") or "member")
            state = ", SUSPENDED" if w.get("is_suspended") else ""
            lines.append(f"- {w.get('name')} ({w.get('tier_label') or w.get('tier') or 'Standard'}, {role}{state})")
    else:
        lines.append("Workspaces: none (they haven't created or joined one)")

    if tickets.get("open"):
        lines.append(f"Open in-app support tickets on Evalyn: {tickets['open']}")
    return "\n".join(lines)


class AIAssistant(commands.Cog):
    """Virtual AI assistant for unclaimed Modmail threads."""

    def __init__(self, bot):
        self.bot = bot
        self.db = bot.api.get_plugin_partition(self)
        # Settings saved by `?ai_config`. None until first read.
        self._stored: dict | None = None
        # Tracks active AI sessions: thread channel_id -> list of message dicts
        self.active_threads: dict[int, list[dict]] = {}
        # Tracks claimed threads so AI stops responding
        self.claimed_threads: set[int] = set()
        # Maps recipient user_id -> thread channel_id for DM lookup
        self.user_to_channel: dict[int, int] = {}
        # Tracks threads pending auto-close: channel_id -> asyncio.Task
        self.pending_close: dict[int, asyncio.Task] = {}
        # Evalyn account summary per thread: channel_id -> text
        self.account_context: dict[int, str] = {}

    async def cog_load(self):
        await self._ensure_config()

    # ------------------------------------------------------------------
    # Settings: env var, then `?ai_config`, then the default
    # ------------------------------------------------------------------

    async def _ensure_config(self) -> None:
        """Read what `?ai_config` saved, once. Retried next call on failure."""
        if self._stored is not None:
            return
        try:
            self._stored = (await self.db.find_one({"_id": "config"})) or {}
        except Exception as exc:
            print(f"[ai_assistant] Couldn't read stored config: {exc}")

    def _setting(self, key: str) -> str:
        env, default = SETTINGS[key]
        return os.environ.get(env) or (self._stored or {}).get(key) or default

    def _source(self, key: str) -> str:
        if os.environ.get(SETTINGS[key][0]):
            return "host env var"
        return "saved" if key in (self._stored or {}) else "default"

    @property
    def ollama_url(self) -> str:
        return self._setting("ollama_url").rstrip("/")

    @property
    def ollama_model(self) -> str:
        return self._setting("ollama_model")

    @property
    def evalyn_url(self) -> str:
        env, default = SETTINGS["evalyn_url"]
        url = os.environ.get(env) or (self._stored or {}).get("evalyn_url")
        if not url:
            # Follow `?userinfo setup` so the URL only has to be set once.
            info = self.bot.get_cog("UserInfo")
            url = getattr(info, "base_url", None) if info else None
        return (url or default).rstrip("/")

    @property
    def escalation_role_id(self) -> int | None:
        value = str(self._setting("escalation_role"))
        return int(value) if value.isdigit() else None  # "none" turns the ping off

    # ------------------------------------------------------------------
    # Thread lifecycle events
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_thread_ready(self, thread, creator, category, initial_message):
        """Called when a new Modmail thread channel is fully set up."""
        if thread.recipient is None:
            return
        await self._ensure_config()
        channel_id = thread.channel.id
        self.active_threads[channel_id] = []
        self.user_to_channel[thread.recipient.id] = channel_id

        summary = await self._lookup_account(thread.recipient.id)
        if summary:
            self.account_context[channel_id] = summary

        # Send an initial greeting from the AI
        user_text = (
            initial_message.content
            if initial_message and initial_message.content
            else "Hello, I need some help."
        )
        await self._reply_as_ai(thread, user_text)

    @commands.Cog.listener()
    async def on_thread_close(self, thread, closer, silent, delete_channel, message, scheduled):
        """Clean up state when a thread is closed."""
        channel_id = thread.channel.id
        self.active_threads.pop(channel_id, None)
        self.claimed_threads.discard(channel_id)
        self.account_context.pop(channel_id, None)
        if thread.recipient:
            self.user_to_channel.pop(thread.recipient.id, None)
        task = self.pending_close.pop(channel_id, None)
        if task:
            task.cancel()

    # ------------------------------------------------------------------
    # Message listener: respond to user messages in unclaimed threads
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # Ignore bots (including ourselves)
        if message.author.bot:
            return

        # --- Staff-side: message in a guild channel ---
        if message.guild:
            channel_id = message.channel.id
            # If a real human typed in a tracked thread channel, a mod has claimed it
            if channel_id in self.active_threads and channel_id not in self.claimed_threads:
                if await self._is_passive_command(message):
                    return

                history = self.active_threads.get(channel_id, [])

                # Silence the AI first
                self.claimed_threads.add(channel_id)
                self.active_threads.pop(channel_id, None)
                task = self.pending_close.pop(channel_id, None)
                if task:
                    task.cancel()
                for uid, cid in list(self.user_to_channel.items()):
                    if cid == channel_id:
                        self.user_to_channel.pop(uid, None)
                        break

                # Post a summary if there's any conversation history
                if history:
                    asyncio.create_task(self._post_summary(message.channel, history))
            return

        # --- User-side: DM message ---
        # Look up the thread channel by the user's ID
        channel_id = self.user_to_channel.get(message.author.id)
        if channel_id is None:
            return  # Not a user we're tracking

        # Stop if a mod has claimed the thread
        if channel_id in self.claimed_threads:
            return

        # Look up the live thread object directly from the cache (keyed by user ID)
        thread = self.bot.threads.cache.get(message.author.id)
        if thread is None:
            return

        # If there's a pending auto-close, the user replied, so cancel it
        task = self.pending_close.pop(channel_id, None)
        if task:
            task.cancel()
            # Let the user know we're still here
            still_here = discord.Embed(
                description="No problem, let's keep going. What else can I help you with?",
                color=discord.Color.blurple(),
            )
            still_here.set_author(name="🤖 AI Assistant")
            try:
                await thread.recipient.send(embed=still_here)
            except discord.Forbidden:
                pass

        await self._reply_as_ai(thread, message.content)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _is_passive_command(self, message: discord.Message) -> bool:
        """True for lookup commands that shouldn't count as claiming the thread."""
        try:
            ctx = await self.bot.get_context(message)
        except Exception:
            return False
        return bool(ctx.command and ctx.command.qualified_name.split()[0] in PASSIVE_COMMANDS)

    async def _lookup_account(self, discord_id: int) -> str | None:
        """Borrow the userinfo / evalyn plugin's Evalyn lookup, if either is loaded."""
        cog = self.bot.get_cog("UserInfo") or self.bot.get_cog("Evalyn")
        if cog is None or not hasattr(cog, "lookup"):
            return None
        try:
            data = await cog.lookup(discord_id=discord_id)
        except Exception as exc:
            print(f"[ai_assistant] Evalyn lookup failed ({type(exc).__name__}): {exc}")
            return None
        return _account_summary(data)

    async def _reply_as_ai(self, thread, user_message: str):
        """Build conversation history, call Ollama, DM the user, and post a staff note."""
        channel = thread.channel
        channel_id = channel.id
        history = self.active_threads.get(channel_id, [])

        # --- Sentiment check before replying ---
        sentiment = await self._check_sentiment(user_message)
        print(f"[ai_assistant] Sentiment for channel {channel_id}: {sentiment}")
        if sentiment == "NEGATIVE":
            await self._escalate(thread, user_message)
            return

        # Append the new user message to history
        history.append({"role": "user", "content": user_message})

        async with channel.typing():
            ai_reply = await self._call_ollama(history, self.account_context.get(channel_id))

        if not ai_reply:
            return

        # A mod may have claimed the thread while Ollama was thinking
        if channel_id in self.claimed_threads:
            return

        # Append AI reply to history for multi-turn context
        history.append({"role": "assistant", "content": ai_reply})
        self.active_threads[channel_id] = history

        # 1. Send the reply to the user's DMs directly
        user_embed = discord.Embed(
            description=ai_reply[:4096],
            color=discord.Color.blurple(),
        )
        user_embed.set_author(name="🤖 Evalyn AI Assistant")
        user_embed.set_footer(text="A staff member will be with you shortly if needed.")

        try:
            await thread.recipient.send(embed=user_embed)
        except discord.Forbidden:
            await channel.send("⚠️ Could not DM the user (DMs may be disabled).")
            return

        # 2. Post a visible note in the staff thread channel so mods can see what was said
        staff_embed = discord.Embed(
            description=f"**AI replied to user:**\n{ai_reply}"[:4096],
            color=discord.Color.blurple(),
        )
        staff_embed.set_author(name="🤖 AI Assistant (sent to user)")
        await channel.send(embed=staff_embed)

        # 3. Check if the issue appears resolved and schedule auto-close if so
        if await self._check_resolved(history):
            print(f"[ai_assistant] Issue appears resolved in channel {channel_id}, scheduling auto-close")
            task = asyncio.create_task(self._schedule_auto_close(thread))
            self.pending_close[channel_id] = task

    async def _post_summary(self, channel: discord.TextChannel, history: list[dict]):
        """Generate and post a handoff summary for the claiming mod."""
        history_text = "\n".join(
            f"{'User' if m['role'] == 'user' else 'AI'}: {m['content']}" for m in history
        )
        messages = [{"role": "user", "content": SUMMARY_PROMPT.format(history=history_text)}]
        summary = await self._ollama_post(messages)

        if not summary:
            return

        embed = discord.Embed(
            title="📋 AI Session Summary",
            description=summary[:4096],
            color=discord.Color.yellow(),
        )
        embed.set_footer(text="Handoff summary generated by AI assistant. AI has been silenced.")
        await channel.send(embed=embed)

    async def _schedule_auto_close(self, thread):
        """Notify the user the ticket will close, wait for a reply, then close."""
        channel = thread.channel
        channel_id = channel.id

        # Tell the user what's happening
        confirm_embed = discord.Embed(
            description=(
                f"It looks like your issue has been resolved! 🎉\n\n"
                f"This ticket will automatically close in **{AUTO_CLOSE_DELAY // 60} minutes** "
                f"if there's nothing else you need.\n\n"
                f"If you still need help, just reply here and I'll cancel the close."
            ),
            color=discord.Color.green(),
        )
        confirm_embed.set_author(name="🤖 Evalyn AI Assistant")
        try:
            await thread.recipient.send(embed=confirm_embed)
        except discord.Forbidden:
            pass

        # Also note in the staff channel
        await channel.send(
            embed=discord.Embed(
                description=f"⏱️ AI believes issue is resolved. Thread will auto-close in {AUTO_CLOSE_DELAY // 60} min unless user replies.",
                color=discord.Color.green(),
            )
        )

        try:
            await asyncio.sleep(AUTO_CLOSE_DELAY)
        except asyncio.CancelledError:
            # User replied, so the close was cancelled in on_message
            print(f"[ai_assistant] Auto-close cancelled for channel {channel_id}, user replied")
            return

        # Double-check thread is still active (not already claimed/closed by a mod)
        if channel_id not in self.active_threads or channel_id in self.claimed_threads:
            return

        self.pending_close.pop(channel_id, None)

        # Close the thread via Modmail's own close method
        try:
            await thread.close(
                closer=self.bot.user,
                silent=False,
                delete_channel=False,
                message="✅ Ticket automatically closed by AI assistant after issue was resolved.",
            )
            print(f"[ai_assistant] Auto-closed thread {channel_id}")
        except Exception as exc:
            print(f"[ai_assistant] Failed to auto-close thread {channel_id}: {exc}")

    async def _escalate(self, thread, trigger_message: str):
        """Silence the AI and alert staff that a frustrated user needs attention."""
        channel = thread.channel
        channel_id = channel.id

        # Silence AI for this thread
        self.claimed_threads.add(channel_id)
        self.active_threads.pop(channel_id, None)
        self.user_to_channel.pop(thread.recipient.id, None)

        # Build staff alert
        ping = f"<@&{self.escalation_role_id}> " if self.escalation_role_id else ""
        staff_embed = discord.Embed(
            title="⚠️ Sentiment escalation",
            description=(
                f"The AI detected frustration or anger in this ticket and has stood down.\n\n"
                f"**Last user message:**\n{trigger_message[:500]}"
            ),
            color=discord.Color.red(),
        )
        staff_embed.set_footer(text="AI assistant has been silenced for this thread.")
        await channel.send(
            content=ping or None,
            embed=staff_embed,
            allowed_mentions=discord.AllowedMentions(roles=True),
        )

        # Let the user know a human is on the way
        user_embed = discord.Embed(
            description="I've flagged this conversation for a staff member who will be with you shortly. Sorry for any frustration!",
            color=discord.Color.orange(),
        )
        user_embed.set_author(name="🤖 Evalyn AI Assistant")
        try:
            await thread.recipient.send(embed=user_embed)
        except discord.Forbidden:
            pass

    async def _check_resolved(self, history: list[dict]) -> bool:
        """Ask Ollama if the conversation looks resolved. Returns True only if clearly RESOLVED."""
        if not history:
            return False

        # Fast pre-check: if the last user message looks like a question, skip Ollama entirely
        last_user = next(
            (m["content"] for m in reversed(history) if m["role"] == "user"), ""
        ).lower().strip()

        question_signals = ("?", "how", "what", "why", "when", "where", "who", "which",
                            "can you", "could you", "is there", "do i", "should i",
                            "doesn't", "don't", "not working", "still", "help")
        if any(sig in last_user for sig in question_signals):
            print("[ai_assistant] Resolution pre-check: last user message looks like a question, skipping")
            return False

        # Also require at least 2 user messages (one question, one confirmation)
        user_messages = [m for m in history if m["role"] == "user"]
        if len(user_messages) < 2:
            return False

        history_text = "\n".join(
            f"{'User' if m['role'] == 'user' else 'AI'}: {m['content']}" for m in history
        )
        messages = [{"role": "user", "content": RESOLUTION_PROMPT.format(history=history_text)}]
        result = await self._ollama_post(messages)
        if result is None:
            return False  # Fail safe: don't auto-close if the check errors

        resolved = result.strip().upper().startswith("RESOLVED")
        print(f"[ai_assistant] Resolution Ollama result: '{result.strip()}' → resolved={resolved}")
        return resolved

    async def _check_sentiment(self, message: str) -> str:
        """Ask Ollama to classify sentiment. Returns NEGATIVE, NEUTRAL, or POSITIVE."""
        messages = [{"role": "user", "content": SENTIMENT_PROMPT.format(message=message)}]
        result = await self._ollama_post(messages)
        if result is None:
            return "NEUTRAL"  # Fail safe: don't block the AI on error
        result = result.upper()
        if "NEGATIVE" in result:
            return "NEGATIVE"
        if "POSITIVE" in result:
            return "POSITIVE"
        return "NEUTRAL"

    async def _call_ollama(self, messages: list[dict], account: str | None = None) -> str | None:
        """Send a chat completion request to Ollama and return the response text."""
        await self._ensure_config()
        system = _load_system_prompt(self.evalyn_url, account)
        full_messages = [{"role": "system", "content": system}] + messages
        print(f"[ai_assistant] Calling Ollama at {self.ollama_url} with model {self.ollama_model}")
        return await self._ollama_post(full_messages)

    async def _ollama_post(self, messages: list[dict]) -> str | None:
        """Shared Ollama HTTP caller with retry logic and extended timeout."""
        await self._ensure_config()
        url = f"{self.ollama_url}/api/chat"
        payload = {"model": self.ollama_model, "messages": messages, "stream": False}

        for attempt in range(1, OLLAMA_RETRIES + 2):  # +2 = initial try + retries
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        url, json=payload, timeout=aiohttp.ClientTimeout(total=OLLAMA_TIMEOUT)
                    ) as resp:
                        raw = await resp.text()
                        if resp.status != 200:
                            print(f"[ai_assistant] Ollama HTTP {resp.status}: {raw[:300]}")
                            return None
                        data = await resp.json(content_type=None)
                        return data.get("message", {}).get("content", "").strip()
            except (TimeoutError, asyncio.TimeoutError, aiohttp.ServerTimeoutError):
                wait = 2 ** attempt
                print(f"[ai_assistant] Timeout on attempt {attempt}/{OLLAMA_RETRIES + 1}, retrying in {wait}s")
                if attempt <= OLLAMA_RETRIES:
                    await asyncio.sleep(wait)
            except aiohttp.ClientConnectorError as exc:
                print(f"[ai_assistant] Connection error, could not reach {url}: {exc}")
                return None
            except Exception as exc:
                import traceback
                print(f"[ai_assistant] Unexpected error ({type(exc).__name__}): {exc}")
                traceback.print_exc()
                return None

        print(f"[ai_assistant] All {OLLAMA_RETRIES + 1} attempts timed out, giving up")
        return None

    # ------------------------------------------------------------------
    # Admin commands
    # ------------------------------------------------------------------

    async def _probe_ollama(self) -> str:
        """One line on whether Ollama answers and has the configured model."""
        url = f"{self.ollama_url}/api/tags"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                    if resp.status != 200:
                        return f"⚠️ Ollama answered HTTP {resp.status} at {self.ollama_url}."
                    data = await resp.json(content_type=None)
        except Exception as exc:
            return f"⚠️ Couldn't reach Ollama at {self.ollama_url} ({type(exc).__name__})."

        model = self.ollama_model
        names = set()
        for m in (data or {}).get("models", []):
            names.update(n for n in (m.get("name"), m.get("model")) if n)
        if model in names or f"{model}:latest" in names:
            return f"✅ Ollama is reachable and has `{model}`."
        return (
            f"⚠️ Ollama is reachable but doesn't have `{model}`. "
            f"Run `ollama pull {model}` on the Ollama host, or pick another with `?ai_config ollama_model`."
        )

    def _display(self, key: str) -> str:
        if key == "escalation_role":
            rid = self.escalation_role_id
            return f"<@&{rid}>" if rid else "none (no ping)"
        return f"`{self._setting(key)}`"

    @commands.command(name="ai_status")
    @commands.has_permissions(manage_guild=True)
    async def ai_status(self, ctx):
        """Show which threads the AI is currently active in."""
        await self._ensure_config()
        active = len(self.active_threads)
        claimed = len(self.claimed_threads)
        async with ctx.typing():
            ollama = await self._probe_ollama()
        await ctx.send(
            f"🤖 **AI Assistant Status**\n"
            f"Active (unclaimed) threads: **{active}**\n"
            f"Claimed (AI silenced) threads: **{claimed}**\n"
            f"Knowledge base: **{'loaded' if os.path.exists(KNOWLEDGE_FILE) else 'MISSING'}**\n"
            f"Evalyn account context: **{'on' if (self.bot.get_cog('UserInfo') or self.bot.get_cog('Evalyn')) else 'off (install userinfo or evalyn)'}**\n"
            f"Model: `{self.ollama_model}` at `{self.ollama_url}`\n"
            f"{ollama}",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.command(name="ai_config")
    @checks.has_permissions(PermissionLevel.OWNER)
    async def ai_config(self, ctx, key: str = None, *, value: str = None):
        """
        Show or change AI settings (saved in Modmail's database).

        ?ai_config                                     show everything
        ?ai_config ollama_url <url>
        ?ai_config ollama_model <model>
        ?ai_config evalyn_url <url>
        ?ai_config escalation_role <@role | role id | none>
        ?ai_config <setting> reset                     back to the default
        """
        await self._ensure_config()

        if key is None:
            embed = discord.Embed(title="🤖 AI Assistant settings", color=discord.Color.blurple())
            for k in SETTINGS:
                embed.add_field(name=k, value=f"{self._display(k)} ({self._source(k)})", inline=False)
            embed.set_footer(text="Change one: ?ai_config <setting> <value>   ·   Undo: ?ai_config <setting> reset")
            return await ctx.send(embed=embed)

        key = key.lower()
        if key not in SETTINGS:
            return await ctx.send(embed=discord.Embed(
                description=f"❌ Unknown setting. Use one of {', '.join(f'`{k}`' for k in SETTINGS)}.",
                color=discord.Color.red(),
            ))
        if not value:
            return await ctx.send(embed=discord.Embed(
                description=f"⚠️ Give a value: `?ai_config {key} <value>`, or `?ai_config {key} reset`.",
                color=discord.Color.orange(),
            ))

        value = value.strip()
        if value.lower() == "reset":
            await self.db.find_one_and_update({"_id": "config"}, {"$unset": {key: ""}}, upsert=True)
        else:
            parsed, error = self._parse(ctx, key, value)
            if error:
                return await ctx.send(embed=discord.Embed(description=f"❌ {error}", color=discord.Color.red()))
            await self.db.find_one_and_update({"_id": "config"}, {"$set": {key: parsed}}, upsert=True)

        self._stored = None
        await self._ensure_config()

        lines = [f"✅ `{key}` is now {self._display(key)} ({self._source(key)})."]
        env = SETTINGS[key][0]
        if os.environ.get(env):
            lines.append(f"Note: `{env}` is set on the host, so that value is used instead.")
        if key == "escalation_role" and self.escalation_role_id and ctx.guild and not ctx.guild.get_role(self.escalation_role_id):
            lines.append("⚠️ That role isn't in this server. Escalations post in ticket channels, so use a role from here.")
        if key in ("ollama_url", "ollama_model"):
            async with ctx.typing():
                lines.append(await self._probe_ollama())
        await ctx.send(
            embed=discord.Embed(description="\n".join(lines), color=discord.Color.green()),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @staticmethod
    def _parse(ctx, key: str, value: str) -> tuple[str | None, str | None]:
        """Validate one `?ai_config` value. Returns (value to save, error)."""
        if key in ("ollama_url", "evalyn_url"):
            url = value.strip("<>").rstrip("/")  # Discord users wrap links in <> to hide the preview
            if not re.match(r"^https?://[^\s/]+", url):
                return None, "That isn't a URL. It should start with `https://` (or `http://`)."
            return url, None
        if key == "ollama_model":
            if not re.match(r"^[\w.\-/:]+$", value):
                return None, "Model names have no spaces, e.g. `gemma4:e2b` or `llama3`."
            return value, None
        if key == "escalation_role":
            if value.lower() in ("none", "off"):
                return "none", None
            digits = re.sub(r"\D", "", value)  # accepts <@&123> or 123
            if not digits:
                return None, "Mention the role, paste its id, or use `none`."
            return digits, None
        return None, "Unknown setting."

    @commands.command(name="ai_off")
    @commands.has_permissions(manage_guild=True)
    async def ai_off(self, ctx):
        """Manually silence the AI in the current thread."""
        self.claimed_threads.add(ctx.channel.id)
        self.active_threads.pop(ctx.channel.id, None)
        task = self.pending_close.pop(ctx.channel.id, None)
        if task:
            task.cancel()
        await ctx.send("🤖 AI Assistant has been silenced in this thread.")


async def setup(bot):
    await bot.add_cog(AIAssistant(bot))
