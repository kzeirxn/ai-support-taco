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

Built to cope with a slow, CPU-only Ollama:
  - Only the knowledge-base sections that match the question are sent, not
    the whole file, so the prompt fits a 4096-token context and reads fast.
  - Replies are streamed, so a proxy in front of Ollama sees data arriving
    and doesn't cut a slow answer off at its timeout (~90s on openresty).
  - One request at a time from this bot. Extras wait here, where waiting
    doesn't count against that proxy timeout.
  - When Ollama fails, the ticket gets one note saying why, not silence.

Setup, from Discord (bot owner only, saved in Modmail's database, so it works
on Docker installs with no .env to edit). Everything has a default, so this
is only for changing them:
  ?ai_config                                  show current settings
  ?ai_config ollama_url https://ai.tacogroup.uk
  ?ai_config ollama_model qwen3.5:4b
  ?ai_config evalyn_url https://evalyn.tacogroup.uk   (links the AI gives out)
  ?ai_config escalation_role @Support                 (or a role id, or none)
  ?ai_config <setting> reset                          back to the default

evalyn_url follows `?userinfo setup` unless you set it here. Environment
variables (OLLAMA_URL, OLLAMA_MODEL, EVALYN_BASE_URL, ESCALATION_ROLE_ID)
still win if they're set on the host.
"""

import asyncio
import contextlib
import json
import math
import os
import re
import time
import traceback
from collections import Counter

import aiohttp
import discord
from discord.ext import commands

from core import checks
from core.models import PermissionLevel


BASE_SYSTEM_PROMPT = """You are a helpful AI support assistant for Evalyn, a free staff management platform for Roblox groups, run by Taco.
A user has opened a support ticket and no staff member has claimed it yet.
Your job is to:
1. Greet the user warmly and acknowledge their issue.
2. Use the knowledge base excerpts below to try to resolve their issue accurately.
3. If unsure, ask a clarifying question. Do NOT guess or make up answers, menu paths, or features.
4. Let them know a staff member will follow up if you cannot fully resolve it.
5. Links: the dashboard is {base_url}, the guides are {base_url}/docs, and in-app support tickets are at {base_url}/tickets.
Keep replies under 120 words, friendly, and professional.
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

--- KNOWLEDGE BASE EXCERPTS ---
These are the parts of Evalyn's knowledge base that match this conversation.
The full knowledge base also covers: {topics}.
If the excerpts don't answer the question, ask a clarifying question or say a staff member will help.

{knowledge}
--- END KNOWLEDGE BASE EXCERPTS ---
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

AUTO_CLOSE_DELAY = 120   # seconds to wait for user confirmation before closing
OLLAMA_TIMEOUT   = 120   # seconds Ollama may send nothing (loading, reading the prompt) before giving up
REPLY_TOKENS     = 400   # longest reply the model may write
KB_BUDGET_CHARS  = 3500  # knowledge-base text per prompt, ~900 tokens
KB_MAX_SECTIONS  = 5
HISTORY_CHARS    = 4000  # conversation text per prompt

# Staff running these in a thread is looking something up, not claiming it.
PASSIVE_COMMANDS = {"userinfo", "evalyn", "ai_status", "ai_config"}

# What `?ai_config` can change: setting -> (env var that overrides it, default).
SETTINGS = {
    "ollama_url":      ("OLLAMA_URL", "https://ai.tacogroup.uk"),
    "ollama_model":    ("OLLAMA_MODEL", "qwen3.5:4b"),
    "evalyn_url":      ("EVALYN_BASE_URL", "https://evalyn.tacogroup.uk"),
    "escalation_role": ("ESCALATION_ROLE_ID", "1401301393767141541"),
}

NO_ACCOUNT_INFO = "(No account information available.)"


# ----------------------------------------------------------------------
# Knowledge base: split into sections, send only the ones that match
# ----------------------------------------------------------------------

STOPWORDS = frozenset("""
a an and are as at be been but by can could do does dont for from get got had has have how i if in into is it its
just me my no not of on or our please so some still that the their them then there they this to too up us was we
what when where which who why will with would you your hi hello hey thanks thank help need want im ive cant cannot
wont isnt doesnt didnt also any all about
""".split())

# Words people type -> words the knowledge base uses.
SYNONYMS = {
    "login": "sign", "log": "sign", "logged": "sign", "signin": "sign", "password": "sign",
    "bot": "discord", "bots": "discord", "guild": "discord", "slash": "discord", "intent": "discord",
    "command": "discord", "commands": "discord",
    "script": "handler", "studio": "handler", "http": "handler", "game": "handler", "ingame": "handler",
    "track": "shift", "tracking": "shift", "clock": "shift", "hours": "shift", "afk": "shift",
    "banned": "ban", "kick": "moderation", "warn": "logbook", "warning": "logbook",
    "rank": "role", "ranks": "role", "promote": "role", "promotion": "role", "demote": "role",
    "access": "permission", "denied": "permission",
    "suspension": "suspend", "appeal": "suspend",
    "group": "workspace", "groups": "workspace",
    "email": "mail", "domain": "mail", "mailbox": "mail",
    "loa": "leave", "absence": "leave",
    "event": "session", "events": "session", "training": "session", "trainings": "session",
    "license": "licensing", "licenses": "licensing", "tacocoins": "licensing", "coins": "licensing",
    "product": "licensing", "products": "licensing",
    "verify": "verified", "verification": "verified", "partner": "tier", "tiers": "tier",
    "link": "linking", "unlink": "linking", "connect": "linking",
}

_KB_CACHE: dict = {"mtime": None}


def _stem(word: str) -> str:
    for suffix, min_len in (("ing", 6), ("ed", 5), ("s", 4)):
        if word.endswith(suffix) and len(word) >= min_len:
            return word[: -len(suffix)]
    return word


def _terms(text: str, expand: bool = False) -> list[str]:
    words = [w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 1 and w not in STOPWORDS]
    if expand:
        words += [SYNONYMS[w] for w in words if w in SYNONYMS]
    return [_stem(w) for w in words]


def _clean_heading(text: str) -> str:
    return re.sub(r"^[^\w]+", "", text).strip()  # drop the leading emoji


def _parse_kb(text: str) -> dict:
    """Split the markdown into `# Topic › ## Question` sections."""
    sections, topics = [], []
    h1 = h2 = None
    buf: list[str] = []

    def flush():
        body = "\n".join(buf).strip()
        if not (body and h1) or "knowledgebase" in h1.lower() or h1.lower() == "summary":
            return
        title = f"{h1} › {h2}" if h2 else h1
        sections.append({
            "title": title,
            "text": f"## {title}\n{body}",
            "terms": Counter(_terms(f"{title}\n{body}")),
            "head": set(_terms(title)),
        })

    for line in text.splitlines():
        if line.startswith("# "):
            flush(); buf = []
            h1, h2 = _clean_heading(line[2:]), None
            if "knowledgebase" not in h1.lower() and h1.lower() != "summary" and h1 not in topics:
                topics.append(h1)
        elif line.startswith("## "):
            flush(); buf = []
            h2 = _clean_heading(line[3:])
        elif line.strip() != "---":
            buf.append(line)
    flush()

    # The "What is Evalyn?" intro (links, what it is) goes in every prompt.
    intro = next((s for s in sections if s["title"].lower().startswith("what is evalyn")), None)
    rest = [s for s in sections if s is not intro]
    return {
        "intro": intro["text"] if intro else "",
        "sections": rest,
        "topics": ", ".join(t for t in topics if not t.lower().startswith("what is evalyn")),
    }


def _knowledge() -> dict | None:
    """The parsed knowledge base, re-read only when the file changes."""
    try:
        mtime = os.path.getmtime(KNOWLEDGE_FILE)
    except OSError:
        return None
    if _KB_CACHE.get("mtime") != mtime:
        with open(KNOWLEDGE_FILE, "r", encoding="utf-8") as f:
            _KB_CACHE.clear()
            _KB_CACHE.update(_parse_kb(f.read()), mtime=mtime)
    return _KB_CACHE


def _select_sections(sections: list[dict], query: str) -> list[dict]:
    """The best-matching sections for the query, within the size budget, in file order."""
    wanted = set(_terms(query, expand=True))
    if not wanted or not sections:
        return []
    df = Counter(t for s in sections for t in s["terms"])
    n = len(sections)
    scored = []
    for i, s in enumerate(sections):
        score = 0.0
        for t in wanted:
            if t in s["terms"]:
                idf = math.log(1 + n / df[t])
                score += idf * (min(s["terms"][t], 3) + (3 if t in s["head"] else 0))
        if score > 0:
            scored.append((score, i))
    if not scored:
        return []
    scored.sort(reverse=True)
    floor = scored[0][0] * 0.25  # drop sections that only brushed one common word
    chosen, used = [], 0
    for score, i in scored:
        if score < floor or len(chosen) >= KB_MAX_SECTIONS:
            break
        size = len(sections[i]["text"])
        if used + size > KB_BUDGET_CHARS:
            continue
        chosen.append(i)
        used += size
    return [sections[i] for i in sorted(chosen)]


def _build_system_prompt(base_url: str, account: str | None, query: str) -> str:
    kb = _knowledge()
    if kb is None:
        print(f"[ai_assistant] Knowledge base not found at {KNOWLEDGE_FILE}")
        knowledge, topics = "(No knowledge base found. Answer using general knowledge only.)", "nothing else"
    else:
        picked = _select_sections(kb["sections"], query)
        knowledge = "\n\n".join([kb["intro"]] + [s["text"] for s in picked]).strip()
        topics = kb["topics"] or "nothing else"
    return BASE_SYSTEM_PROMPT.format(
        base_url=base_url,
        account=account or NO_ACCOUNT_INFO,
        topics=topics,
        knowledge=knowledge,
    )


def _recent(history: list[dict], max_chars: int = HISTORY_CHARS) -> list[dict]:
    """The latest messages that fit the budget (always at least the last one)."""
    out, used = [], 0
    for m in reversed(history):
        used += len(m["content"])
        if out and used > max_chars:
            break
        out.append(m)
    return list(reversed(out))


def _history_text(history: list[dict]) -> str:
    return "\n".join(f"{'User' if m['role'] == 'user' else 'AI'}: {m['content']}" for m in _recent(history))


def _clean_reply(text: str) -> str:
    """Remove any <think>…</think> a reasoning model writes inline."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    if "<think>" in text:  # cut off mid-thought: nothing after it is an answer
        text = text.split("<think>", 1)[0]
    return text.strip()


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
        # Tickets already told the AI failed, so a dead Ollama posts one note, not one per message
        self.failure_noted: set[int] = set()
        # One Ollama request at a time. A CPU-only Ollama works through them one by one anyway,
        # and a request waiting here doesn't count against the proxy's timeout like one queued there.
        self._ollama_lock = asyncio.Semaphore(1)
        # Ask models not to "think" first; dropped if the model doesn't understand the option.
        self._send_think = True

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
        self.failure_noted.discard(channel_id)
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

                # Post a summary if the AI actually said something
                if any(m["role"] == "assistant" for m in history):
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
        # Kept even if Ollama fails, so the next try and the handoff summary still see it
        history.append({"role": "user", "content": user_message})
        self.active_threads[channel_id] = history

        # --- Sentiment check before replying ---
        sentiment, error = await self._check_sentiment(user_message)
        if error:
            # Ollama is down or stuck. The reply would fail the same way, so don't wait on it twice.
            await self._report_failure(channel, error)
            return
        print(f"[ai_assistant] Sentiment for channel {channel_id}: {sentiment}")
        if sentiment == "NEGATIVE":
            await self._escalate(thread, user_message)
            return

        async with channel.typing():
            ai_reply, error = await self._call_ollama(history, self.account_context.get(channel_id))

        # A mod may have claimed the thread while Ollama was thinking
        if channel_id in self.claimed_threads:
            return
        if error or not ai_reply:
            await self._report_failure(channel, error or "The model sent back an empty reply.")
            return
        self.failure_noted.discard(channel_id)

        # Append AI reply to history for multi-turn context
        history.append({"role": "assistant", "content": ai_reply})

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

    async def _report_failure(self, channel, error: str):
        """Tell staff in the ticket why the AI said nothing. Once per ticket until it works again."""
        print(f"[ai_assistant] Ollama failed in channel {channel.id}: {error}")
        if channel.id in self.failure_noted:
            return
        self.failure_noted.add(channel.id)
        embed = discord.Embed(
            title="⚠️ AI couldn't answer",
            description=(
                f"{error}\n\n"
                "The user hasn't been sent anything, so reply to them yourself. "
                "`?ai_status` tests the AI server."
            ),
            color=discord.Color.orange(),
        )
        embed.set_footer(text="Shown once per ticket. The AI tries again on the user's next message.")
        try:
            await channel.send(embed=embed)
        except discord.HTTPException:
            pass

    async def _post_summary(self, channel: discord.TextChannel, history: list[dict]):
        """Generate and post a handoff summary for the claiming mod."""
        messages = [{"role": "user", "content": SUMMARY_PROMPT.format(history=_history_text(history))}]
        summary, error = await self._ollama_post(messages, num_predict=300)

        if error or not summary:
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

        messages = [{"role": "user", "content": RESOLUTION_PROMPT.format(history=_history_text(history))}]
        result, error = await self._ollama_post(messages, num_predict=8)
        if error or not result:
            return False  # Fail safe: don't auto-close if the check errors

        resolved = result.strip().upper().startswith("RESOLVED")
        print(f"[ai_assistant] Resolution Ollama result: '{result.strip()}' → resolved={resolved}")
        return resolved

    async def _check_sentiment(self, message: str) -> tuple[str, str | None]:
        """Classify sentiment: (NEGATIVE | NEUTRAL | POSITIVE, error if Ollama failed)."""
        messages = [{"role": "user", "content": SENTIMENT_PROMPT.format(message=message)}]
        result, error = await self._ollama_post(messages, num_predict=8)
        if error:
            return "NEUTRAL", error
        result = (result or "").upper()
        if "NEGATIVE" in result:
            return "NEGATIVE", None
        if "POSITIVE" in result:
            return "POSITIVE", None
        return "NEUTRAL", None

    async def _call_ollama(self, history: list[dict], account: str | None = None) -> tuple[str | None, str | None]:
        """The reply to the conversation: system prompt with matching knowledge, plus recent history."""
        await self._ensure_config()
        recent = _recent(history)
        query = " ".join([m["content"] for m in recent if m["role"] == "user"][-2:])
        system = _build_system_prompt(self.evalyn_url, account, query)
        print(f"[ai_assistant] Calling Ollama at {self.ollama_url} with model {self.ollama_model} "
              f"({len(system)} chars of system prompt)")
        return await self._ollama_post([{"role": "system", "content": system}] + recent, num_predict=REPLY_TOKENS)

    async def _ollama_post(
        self, messages: list[dict], *, num_predict: int | None = None,
        timeout: int = OLLAMA_TIMEOUT, use_lock: bool = True,
    ) -> tuple[str | None, str | None]:
        """
        Stream one chat request to Ollama. Returns (reply, None), or (None, why it failed).

        Streamed because a proxy in front of Ollama cuts a request off after ~90s of
        silence, and a non-streamed reply is silent until the last word is written.
        """
        await self._ensure_config()
        payload: dict = {"model": self.ollama_model, "messages": messages, "stream": True}
        if num_predict:
            payload["options"] = {"num_predict": num_predict}

        lock = self._ollama_lock if use_lock else contextlib.nullcontext()
        async with lock:
            text, error, retry = await self._stream_chat(payload, timeout)
            if retry:
                # A proxy or Ollama restarting answers 502/503 or refuses the connection for a moment
                print(f"[ai_assistant] {error} Retrying once.")
                await asyncio.sleep(3)
                text, error, _ = await self._stream_chat(payload, timeout)
        return text, error

    async def _stream_chat(self, payload: dict, timeout: int) -> tuple[str | None, str | None, bool]:
        """One streamed POST /api/chat. Returns (reply, error, worth retrying)."""
        url = f"{self.ollama_url}/api/chat"
        body = dict(payload)
        if self._send_think:
            # Reasoning models (qwen3.x and others) would otherwise "think" for hundreds of tokens first
            body["think"] = False
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url, json=body,
                    timeout=aiohttp.ClientTimeout(total=timeout * 3, sock_read=timeout),
                ) as resp:
                    if resp.status != 200:
                        detail = (await resp.text())[:300]
                        if resp.status == 400 and "think" in body and "think" in detail.lower():
                            self._send_think = False  # this model has no thinking switch; ask again without it
                            return await self._stream_chat(payload, timeout)
                        return None, self._describe_http(resp.status, detail), resp.status in (502, 503)

                    parts = []
                    async for raw in resp.content:  # one JSON object per line
                        line = raw.strip()
                        if not line:
                            continue
                        try:
                            chunk = json.loads(line)
                        except ValueError:
                            continue
                        if chunk.get("error"):
                            return None, f"Ollama stopped with an error: {chunk['error']}", False
                        parts.append((chunk.get("message") or {}).get("content") or "")
                        if chunk.get("done"):
                            break
                    return _clean_reply("".join(parts)), None, False
        except (asyncio.TimeoutError, TimeoutError):
            return None, (
                f"Ollama timed out (nothing for {timeout}s). It's stuck, overloaded, or too slow to answer."
            ), False
        except aiohttp.ClientConnectorError:
            return None, f"Couldn't connect to Ollama at {self.ollama_url}.", True
        except aiohttp.ClientError as exc:
            return None, f"The connection to Ollama failed ({type(exc).__name__}).", True
        except Exception as exc:
            traceback.print_exc()
            return None, f"Unexpected error talking to Ollama ({type(exc).__name__}: {exc}).", False

    def _describe_http(self, status: int, detail: str) -> str:
        """An Ollama/proxy HTTP error, in words staff can act on."""
        low = detail.lower()
        if status == 404 and "not found" in low:
            return (f"The Ollama server doesn't have the model `{self.ollama_model}`. "
                    f"Pick one it has with `?ai_config ollama_model <name>`.")
        if status in (504, 524):
            return (f"The proxy in front of Ollama gave up waiting (HTTP {status}). "
                    f"Ollama is stuck, overloaded, or too slow to answer.")
        if status in (502, 503):
            return f"Ollama isn't answering behind its proxy (HTTP {status}). It may be stopped or restarting."
        if status in (401, 403):
            return f"Ollama refused the request (HTTP {status}). Check the login or IP allowlist in front of it."
        return f"Ollama returned HTTP {status}: {detail[:150]}"

    # ------------------------------------------------------------------
    # Admin commands
    # ------------------------------------------------------------------

    async def _probe_ollama(self) -> str:
        """Whether Ollama has the model and actually answers a test message."""
        await self._ensure_config()
        model = self.ollama_model
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{self.ollama_url}/api/tags", timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status != 200:
                        return f"⚠️ Ollama answered HTTP {resp.status} at {self.ollama_url}."
                    data = await resp.json(content_type=None)
        except Exception as exc:
            return f"⚠️ Couldn't reach Ollama at {self.ollama_url} ({type(exc).__name__})."

        names = set()
        for m in (data or {}).get("models", []):
            names.update(n for n in (m.get("name"), m.get("model")) if n)
        if model not in names and f"{model}:latest" not in names:
            return (
                f"⚠️ Ollama is reachable but doesn't have `{model}`. "
                f"Run `ollama pull {model}` on the Ollama host, or pick another with `?ai_config ollama_model`."
            )

        # Having the model isn't the same as answering. Send a real message.
        busy = self._ollama_lock.locked()
        started = time.monotonic()
        text, error = await self._ollama_post(
            [{"role": "user", "content": "Reply with the single word OK."}],
            num_predict=5, timeout=60, use_lock=False,
        )
        took = time.monotonic() - started
        note = " The AI was answering a ticket at the same time, which slows this test." if busy else ""
        if error:
            return f"⚠️ Ollama has `{model}`, but a test message failed: {error}{note}"
        if took > 30:
            return (f"⚠️ Ollama answered a one-word test in {took:.0f}s. Real replies are much longer, "
                    f"so expect slow answers or timeouts.{note}")
        return f"✅ Ollama answered a test message in {took:.1f}s with `{model}`.{note}"

    def _display(self, key: str) -> str:
        if key == "escalation_role":
            rid = self.escalation_role_id
            return f"<@&{rid}>" if rid else "none (no ping)"
        return f"`{self._setting(key)}`"

    @commands.command(name="ai_status")
    @commands.has_permissions(manage_guild=True)
    async def ai_status(self, ctx):
        """Show which threads the AI is active in, and test the AI server."""
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
        self._send_think = True  # a different model may support the option again
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
                return None, "Model names have no spaces, e.g. `qwen3.5:4b` or `llama3`."
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
