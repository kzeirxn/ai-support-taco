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

import os

import aiohttp
import discord
from discord.ext import commands


BASE_SYSTEM_PROMPT = """You are a helpful AI support assistant for the Taco Support dashboard.
A user has opened a support ticket and no staff member has claimed it yet.
Your job is to:
1. Greet the user warmly and acknowledge their issue.
2. Use the knowledge base below to try to resolve their issue accurately.
3. If unsure, ask a clarifying question — do NOT guess or make up answers.
4. Let them know a staff member will follow up if you cannot fully resolve it.

Keep your replies concise, friendly, and professional.
Do NOT pretend to be a human staff member — you are an AI assistant.
If the issue requires account-specific action only staff can perform, say so clearly.

--- KNOWLEDGE BASE ---
{knowledge}
--- END KNOWLEDGE BASE ---
"""

KNOWLEDGE_FILE = os.path.join(os.path.dirname(__file__), "support_knowledge")


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
        self.ollama_model = os.environ.get("OLLAMA_MODEL", "llama3")
        # Tracks active AI sessions: thread channel_id -> list of message dicts
        self.active_threads: dict[int, list[dict]] = {}
        # Tracks claimed threads so AI stops responding
        self.claimed_threads: set[int] = set()
        # Maps recipient user_id -> thread channel_id for DM lookup
        self.user_to_channel: dict[int, int] = {}

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
                self.claimed_threads.add(channel_id)
                self.active_threads.pop(channel_id, None)
                # Clean up user mapping too
                for uid, cid in list(self.user_to_channel.items()):
                    if cid == channel_id:
                        self.user_to_channel.pop(uid, None)
                        break
            return

        # --- User-side: DM message ---
        # Look up the thread channel by the user's ID
        channel_id = self.user_to_channel.get(message.author.id)
        if channel_id is None:
            return  # Not a user we're tracking

        # Stop if a mod has claimed the thread
        if channel_id in self.claimed_threads:
            return

        # Look up the live thread object and reply
        thread = self.bot.threads.find_by_id(message.author.id)
        if thread is None:
            return

        await self._reply_as_ai(thread, message.content)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _reply_as_ai(self, thread, user_message: str):
        """Build conversation history, call Ollama, DM the user, and post a staff note."""
        channel = thread.channel
        channel_id = channel.id
        history = self.active_threads.get(channel_id, [])

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

    async def _call_ollama(self, messages: list[dict]) -> str | None:
        """Send a chat request to the Ollama API and return the response text."""
        url = f"{self.ollama_url.rstrip('/')}/api/chat"
        payload = {
            "model": self.ollama_model,
            "messages": [{"role": "system", "content": _load_system_prompt()}] + messages,
            "stream": False,
        }

        print(f"[ai_assistant] Calling Ollama at {url} with model {self.ollama_model}")
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    raw = await resp.text()
                    print(f"[ai_assistant] Ollama HTTP {resp.status} — response: {raw[:500]}")
                    if resp.status != 200:
                        return None
                    data = await resp.json(content_type=None)
                    return data.get("message", {}).get("content", "").strip()
        except aiohttp.ClientConnectorError as exc:
            print(f"[ai_assistant] Connection error — could not reach {url}: {exc}")
        except aiohttp.ServerTimeoutError:
            print(f"[ai_assistant] Timeout — Ollama did not respond within 30s")
        except Exception as exc:
            import traceback
            print(f"[ai_assistant] Unexpected error ({type(exc).__name__}): {exc}")
            traceback.print_exc()
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
