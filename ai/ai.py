import os
import asyncio
from datetime import datetime, timezone

import aiohttp
from discord.ext import commands


OLLAMA_URL = os.getenv("OLLAMA_URL", "https://ai.tacogroup.uk")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.1:8b")

AUTO_REPLY_ENABLED = os.getenv("AUTO_REPLY_ENABLED", "true").lower() == "true"
AUTO_REPLY_DELAY_SEC = int(os.getenv("AUTO_REPLY_DELAY_SEC", "120"))

AUTO_REPLY_DISCLOSE = os.getenv("AUTO_REPLY_DISCLOSE", "true").lower() == "true"

AI_ASSIST_ENABLED = os.getenv("AI_ASSIST_ENABLED", "true").lower() == "true"
AI_REPLY_DELAY_SEC = int(os.getenv("AI_REPLY_DELAY_SEC", "2"))
AI_REPLY_MAX_CHARS = int(os.getenv("AI_REPLY_MAX_CHARS", "1200"))

KNOWLEDGE_PATH = os.getenv(
    "KNOWLEDGE_PATH",
    os.path.join(os.path.dirname(__file__), "support_knowledge.md"),
)


def _shorten(text, limit):
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


class AISupport(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.coll = bot.api.get_plugin_partition(self)
        self._pending_initial = {}  # thread_id -> task
        self._pending_user = {}  # thread_id -> task
        self.knowledge = self._load_knowledge()

    def _load_knowledge(self):
        try:
            with open(KNOWLEDGE_PATH, "r", encoding="utf-8") as f:
                content = f.read().strip()
                return content
        except FileNotFoundError:
            return ""
        except Exception:
            return ""

    async def _ollama_generate(self, prompt, system):
        payload = {
            "model": OLLAMA_MODEL,
            "prompt": prompt,
            "system": system,
            "stream": False,
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(f"{OLLAMA_URL}/api/generate", json=payload, timeout=45) as resp:
                resp.raise_for_status()
                data = await resp.json()
                return data.get("response", "").strip()

    def _system_with_knowledge(self, base_system):
        if not self.knowledge:
            return base_system
        return (
            base_system
            + "\n\nYou have access to internal dashboard knowledge. "
            + "Use it to answer questions accurately. "
            + "If something is not covered, say you’re unsure and ask a clarifying question.\n\n"
            + "=== SUPPORT KNOWLEDGE ===\n"
            + self.knowledge
            + "\n=== END KNOWLEDGE ==="
        )

    async def _send_to_thread(self, thread, content):
        if hasattr(thread, "reply"):
            return await thread.reply(content)

        channel = getattr(thread, "channel", None)
        if channel:
            return await channel.send(content)

    async def _send_to_staff_channel(self, thread, content):
        channel = getattr(thread, "channel", None)
        if channel:
            return await channel.send(content)

    async def _is_claimed_or_replied(self, thread_id):
        doc = await self.coll.find_one({"_id": f"thread:{thread_id}"})
        if not doc:
            return False
        return bool(doc.get("claimed") or doc.get("staff_replied"))

    async def _schedule_initial_auto_reply(self, thread, creator, initial_message):
        if not AUTO_REPLY_ENABLED:
            return

        thread_id = getattr(thread, "id", None)
        if thread_id is None:
            return

        existing = self._pending_initial.get(thread_id)
        if existing:
            existing.cancel()

        async def _task():
            try:
                await asyncio.sleep(AUTO_REPLY_DELAY_SEC)

                if await self._is_claimed_or_replied(thread_id):
                    return

                user_text = getattr(initial_message, "content", "")
                user_text = _shorten(user_text or "", 1200)

                base_system = (
                    "You are a support assistant for a Discord server. "
                    "Be concise, polite, and helpful. Ask one clarifying question if needed. "
                    "Do not promise anything or claim to be a human."
                )
                system = self._system_with_knowledge(base_system)

                if AUTO_REPLY_DISCLOSE:
                    prefix = "Thanks for reaching out! I'm an automated assistant while a staff member is away. "
                else:
                    prefix = "Thanks for reaching out! "

                prompt = (
                    f"User message:\n{user_text}\n\n"
                    "Draft a short reply that acknowledges the issue, offers a helpful next step, "
                    "and asks for missing info if needed."
                )

                reply = await self._ollama_generate(prompt, system)
                reply = _shorten(reply, 1200)

                await self._send_to_thread(thread, prefix + reply)

            except asyncio.CancelledError:
                return
            except Exception as e:
                channel = getattr(thread, "channel", None)
                if channel:
                    await channel.send(f"[AI auto-reply failed] {e}")

        self._pending_initial[thread_id] = asyncio.create_task(_task())

    async def _schedule_user_ai_reply(self, thread, message):
        if not AI_ASSIST_ENABLED:
            return

        thread_id = getattr(thread, "id", None)
        if thread_id is None:
            return

        existing = self._pending_user.get(thread_id)
        if existing:
            existing.cancel()

        async def _task():
            try:
                await asyncio.sleep(AI_REPLY_DELAY_SEC)

                if await self._is_claimed_or_replied(thread_id):
                    return

                user_text = getattr(message, "content", "")
                user_text = _shorten(user_text or "", 1200)

                base_system = (
                    "You are a support assistant for a Discord server. "
                    "Be concise, polite, and helpful. Ask one clarifying question if needed. "
                    "Do not promise anything or claim to be a human."
                )
                system = self._system_with_knowledge(base_system)

                if AUTO_REPLY_DISCLOSE:
                    prefix = "I'm an automated assistant while a staff member is away. "
                else:
                    prefix = ""

                prompt = (
                    f"User message:\n{user_text}\n\n"
                    "Draft a helpful reply that moves the issue forward."
                )

                reply = await self._ollama_generate(prompt, system)
                reply = _shorten(reply, AI_REPLY_MAX_CHARS)

                await self._send_to_thread(thread, prefix + reply)

            except asyncio.CancelledError:
                return
            except Exception as e:
                await self._send_to_staff_channel(thread, f"[AI reply failed] {e}")

        self._pending_user[thread_id] = asyncio.create_task(_task())

    @commands.Cog.listener()
    async def on_message(self, message):
        # Catch user DMs and map to a thread
        if message.author.bot:
            return
        if message.guild is not None:
            return

        thread = await self.bot.thread_manager.find(recipient=message.author)
        if thread is None:
            await asyncio.sleep(1)
            thread = await self.bot.thread_manager.find(recipient=message.author)
        if thread is None:
            return

        await self._schedule_user_ai_reply(thread, message)

    @commands.Cog.listener()
    async def on_thread_ready(self, thread, creator, category, initial_message):
        await self._schedule_initial_auto_reply(thread, creator, initial_message)

    @commands.Cog.listener()
    async def on_thread_reply(self, thread, from_mod, message, anonymous, plain):
        thread_id = getattr(thread, "id", None)
        if thread_id is None:
            return

        if from_mod:
            await self.coll.update_one(
                {"_id": f"thread:{thread_id}"},
                {"$set": {"staff_replied": True, "claimed": True, "staff_replied_at": datetime.now(timezone.utc)}},
                upsert=True,
            )
            task = self._pending_initial.pop(thread_id, None)
            if task:
                task.cancel()
            task = self._pending_user.pop(thread_id, None)
            if task:
                task.cancel()
            return

        await self._schedule_user_ai_reply(thread, message)

    @commands.Cog.listener()
    async def on_thread_claim(self, thread, mod):
        thread_id = getattr(thread, "id", None)
        if thread_id is None:
            return
        await self.coll.update_one(
            {"_id": f"thread:{thread_id}"},
            {"$set": {"claimed": True, "claimed_at": datetime.now(timezone.utc)}},
            upsert=True,
        )
        task = self._pending_initial.pop(thread_id, None)
        if task:
            task.cancel()
        task = self._pending_user.pop(thread_id, None)
        if task:
            task.cancel()

    @commands.Cog.listener()
    async def on_thread_unclaim(self, thread, mod):
        thread_id = getattr(thread, "id", None)
        if thread_id is None:
            return
        await self.coll.update_one(
            {"_id": f"thread:{thread_id}"},
            {"$set": {"claimed": False}},
            upsert=True,
        )

    @commands.command()
    async def ai(self, ctx, *, message):
        """Staff command: -ai <message> — generate a draft reply."""
        base_system = (
            "You are an assistant that drafts responses for staff. "
            "Return a short, professional reply. Do not mention AI."
        )
        system = self._system_with_knowledge(base_system)

        prompt = f"User message:\n{message}\n\nDraft a helpful reply for staff to send."
        try:
            draft = await self._ollama_generate(prompt, system)
            draft = _shorten(draft, AI_REPLY_MAX_CHARS)
            await ctx.send(f"**AI draft (not sent):**\n{draft}")
        except Exception as e:
            await ctx.send(f"[AI draft failed] {e}")

    @commands.command()
    async def ai_reload_knowledge(self, ctx):
        """Staff command: -ai_reload_knowledge — reloads support_knowledge.md."""
        self.knowledge = self._load_knowledge()
        if self.knowledge:
            await ctx.send("Reloaded AI knowledge.")
        else:
            await ctx.send("Knowledge file missing or empty.")


async def setup(bot):
    await bot.add_cog(AISupport(bot))
