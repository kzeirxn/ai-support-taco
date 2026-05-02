"""
ai_assistant - Modmail Plugin
==============================
A virtual AI assistant powered by Ollama that automatically replies to users
while a ticket is unclaimed, attempting to resolve issues before a mod steps in.

When a mod claims the thread, the AI stops responding immediately.

Setup:
  Set OLLAMA_URL in your bot's .env or config (default: https://ai.tacogroup.uk)
  Set OLLAMA_MODEL in your bot's .env or config (default: llama3)
"""

import asyncio
import os

import aiohttp
import discord
from discord.ext import commands


BASE_SYSTEM_PROMPT = """You are a helpful AI support assistant for Taco Support.
A user has opened a support ticket and no staff member has claimed it yet.
Your job is to:
1. Greet the user warmly and acknowledge their issue.
2. Use the knowledge base below to try to resolve their issue accurately.
3. If unsure, ask a clarifying question — do NOT guess or make up answers.
4. Let them know a staff member will follow up if you cannot fully resolve it.
5. If the user asks for a link or url to the dashboard - it is https://licensing.tacogroup.uk
Keep your replies concise, friendly, and professional.
Do NOT pretend to be a human staff member — you are an AI assistant.
If the issue requires account-specific action only staff can perform, say so clearly.



--- KNOWLEDGE BASE ---
{knowledge}
--- END KNOWLEDGE BASE ---
"""

KNOWLEDGE_FILE = os.path.join(os.path.dirname(__file__), "support_knowledge")

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
OLLAMA_TIMEOUT   = 120  # seconds — llama3 can be slow on first response
OLLAMA_RETRIES   = 2    # number of retries on timeout before giving up


def _load_system_prompt() -> str:
    """Load support_knowledge file and inject into the system prompt."""
    try:
        with open(KNOWLEDGE_FILE, "r", encoding="utf-8") as f:
            knowledge = f.read().strip()
    except FileNotFoundError:
        knowledge = "(No knowledge base found — answer using general knowledge only.)"
    return BASE_SYSTEM_PROMPT.format(knowledge=knowledge)


class AIAssistant(commands.Cog):
    """Virtual AI assistant for unclaimed Modmail threads."""

    def __init__(self, bot):
        self.bot = bot
        self.ollama_url = os.environ.get("OLLAMA_URL", "https://ai.tacogroup.uk")
        self.ollama_model = os.environ.get("OLLAMA_MODEL", "ibm/granite4.1:8b")
        # Role ID to ping when escalating (set ESCALATION_ROLE_ID in .env)
        self.escalation_role_id: int | None = int(r) if (r := os.environ.get("ESCALATION_ROLE_ID")) else None
        # Tracks active AI sessions: thread channel_id -> list of message dicts
        self.active_threads: dict[int, list[dict]] = {}
        # Tracks claimed threads so AI stops responding
        self.claimed_threads: set[int] = set()
        # Maps recipient user_id -> thread channel_id for DM lookup
        self.user_to_channel: dict[int, int] = {}
        # Tracks threads pending auto-close: channel_id -> asyncio.Task
        self.pending_close: dict[int, asyncio.Task] = {}

    # ------------------------------------------------------------------
    # Thread lifecycle events
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_thread_ready(self, thread, creator, category, initial_message):
        """Called when a new Modmail thread channel is fully set up."""
        channel_id = thread.channel.id
        self.active_threads[channel_id] = []
        self.user_to_channel[thread.recipient.id] = channel_id

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
        if thread.recipient:
            self.user_to_channel.pop(thread.recipient.id, None)
        task = self.pending_close.pop(channel_id, None)
        if task:
            task.cancel()

    # ------------------------------------------------------------------
    # Message listener — respond to user messages in unclaimed threads
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

        # If there's a pending auto-close, the user replied — cancel it
        task = self.pending_close.pop(channel_id, None)
        if task:
            task.cancel()
            # Let the user know we're still here
            still_here = discord.Embed(
                description="No problem — let's keep going. What else can I help you with?",
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
            ai_reply = await self._call_ollama(history)

        if not ai_reply:
            return

        # Append AI reply to history for multi-turn context
        history.append({"role": "assistant", "content": ai_reply})
        self.active_threads[channel_id] = history

        # 1. Send the reply to the user's DMs directly
        user_embed = discord.Embed(
            description=ai_reply,
            color=discord.Color.blurple(),
        )
        user_embed.set_author(name="🤖 AI Assistant")
        user_embed.set_footer(text="A staff member will be with you shortly if needed.")

        try:
            await thread.recipient.send(embed=user_embed)
        except discord.Forbidden:
            await channel.send("⚠️ Could not DM the user (DMs may be disabled).")
            return

        # 2. Post a visible note in the staff thread channel so mods can see what was said
        staff_embed = discord.Embed(
            description=f"**AI replied to user:**\n{ai_reply}",
            color=discord.Color.blurple(),
        )
        staff_embed.set_author(name="🤖 AI Assistant — sent to user")
        await channel.send(embed=staff_embed)

        # 3. Check if the issue appears resolved and schedule auto-close if so
        if await self._check_resolved(history):
            print(f"[ai_assistant] Issue appears resolved in channel {channel_id} — scheduling auto-close")
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
            description=summary,
            color=discord.Color.yellow(),
        )
        embed.set_footer(text="Handoff summary generated by AI assistant — AI has been silenced.")
        await channel.send(embed=embed)

    async def _check_resolved(self, history: list[dict]) -> bool:
        """Ask Ollama if the conversation looks resolved. Returns True if RESOLVED."""
        url = f"{self.ollama_url.rstrip('/')}/api/chat"
        history_text = "\n".join(
            f"{'User' if m['role'] == 'user' else 'AI'}: {m['content']}" for m in history
        )
        payload = {
            "model": self.ollama_model,
            "messages": [
                {"role": "user", "content": RESOLUTION_PROMPT.format(history=history_text)}
            ],
            "stream": False,
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                    if resp.status != 200:
                        return False
                    data = await resp.json(content_type=None)
                    result = data.get("message", {}).get("content", "").strip().upper()
                    return "RESOLVED" in result
        except Exception as exc:
            print(f"[ai_assistant] Resolution check failed ({type(exc).__name__}): {exc}")
            return False  # Fail safe — don't auto-close if check errors

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
        confirm_embed.set_author(name="🤖 AI Assistant")
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
            # User replied — close was cancelled in on_message
            print(f"[ai_assistant] Auto-close cancelled for channel {channel_id} — user replied")
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

    async def _check_sentiment(self, message: str) -> str:
        """Ask Ollama to classify sentiment. Returns NEGATIVE, NEUTRAL, or POSITIVE."""
        url = f"{self.ollama_url.rstrip('/')}/api/chat"
        payload = {
            "model": self.ollama_model,
            "messages": [
                {"role": "user", "content": SENTIMENT_PROMPT.format(message=message)}
            ],
            "stream": False,
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                    if resp.status != 200:
                        return "NEUTRAL"
                    data = await resp.json(content_type=None)
                    result = data.get("message", {}).get("content", "").strip().upper()
                    if "NEGATIVE" in result:
                        return "NEGATIVE"
                    if "POSITIVE" in result:
                        return "POSITIVE"
                    return "NEUTRAL"
        except Exception as exc:
            print(f"[ai_assistant] Sentiment check failed ({type(exc).__name__}): {exc}")
            return "NEUTRAL"  # Fail safe — don't block the AI on error

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
        await channel.send(content=ping or None, embed=staff_embed)

        # Let the user know a human is on the way
        user_embed = discord.Embed(
            description="I've flagged this conversation for a staff member who will be with you shortly. Sorry for any frustration!",
            color=discord.Color.orange(),
        )
        user_embed.set_author(name="🤖 AI Assistant")
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
            print(f"[ai_assistant] Resolution pre-check: last user message looks like a question — skipping")
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
            return False  # Fail safe — don't auto-close if check errors

        resolved = result.strip().upper().startswith("RESOLVED")
        print(f"[ai_assistant] Resolution Ollama result: '{result.strip()}' → resolved={resolved}")
        return resolved

    async def _check_sentiment(self, message: str) -> str:
        """Ask Ollama to classify sentiment. Returns NEGATIVE, NEUTRAL, or POSITIVE."""
        messages = [{"role": "user", "content": SENTIMENT_PROMPT.format(message=message)}]
        result = await self._ollama_post(messages)
        if result is None:
            return "NEUTRAL"  # Fail safe — don't block the AI on error
        result = result.upper()
        if "NEGATIVE" in result:
            return "NEGATIVE"
        if "POSITIVE" in result:
            return "POSITIVE"
        return "NEUTRAL"

    async def _call_ollama(self, messages: list[dict]) -> str | None:
        """Send a chat completion request to Ollama and return the response text."""
        full_messages = [{"role": "system", "content": _load_system_prompt()}] + messages
        print(f"[ai_assistant] Calling Ollama at {self.ollama_url} with model {self.ollama_model}")
        return await self._ollama_post(full_messages)

    async def _ollama_post(self, messages: list[dict]) -> str | None:
        """Shared Ollama HTTP caller with retry logic and extended timeout."""
        url = f"{self.ollama_url.rstrip('/')}/api/chat"
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
                print(f"[ai_assistant] Timeout on attempt {attempt}/{OLLAMA_RETRIES + 1} — retrying in {wait}s")
                if attempt <= OLLAMA_RETRIES:
                    await asyncio.sleep(wait)
            except aiohttp.ClientConnectorError as exc:
                print(f"[ai_assistant] Connection error — could not reach {url}: {exc}")
                return None
            except Exception as exc:
                import traceback
                print(f"[ai_assistant] Unexpected error ({type(exc).__name__}): {exc}")
                traceback.print_exc()
                return None

        print(f"[ai_assistant] All {OLLAMA_RETRIES + 1} attempts timed out — giving up")
        return None

    # ------------------------------------------------------------------
    # Admin commands
    # ------------------------------------------------------------------

    @commands.command(name="ai_status")
    @commands.has_permissions(manage_guild=True)
    async def ai_status(self, ctx):
        """Show which threads the AI is currently active in."""
        active = len(self.active_threads)
        claimed = len(self.claimed_threads)
        await ctx.send(
            f"🤖 **AI Assistant Status**\n"
            f"Active (unclaimed) threads: **{active}**\n"
            f"Claimed (AI silenced) threads: **{claimed}**"
        )

    @commands.command(name="ai_off")
    @commands.has_permissions(manage_guild=True)
    async def ai_off(self, ctx):
        """Manually silence the AI in the current thread."""
        self.claimed_threads.add(ctx.channel.id)
        self.active_threads.pop(ctx.channel.id, None)
        await ctx.send("🤖 AI Assistant has been silenced in this thread.")


async def setup(bot):
    await bot.add_cog(AIAssistant(bot))
