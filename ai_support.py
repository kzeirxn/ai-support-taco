import os
import asyncio
import json
from datetime import datetime, timezone

import aiohttp
from discord.ext import commands


OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.1:8b")

AUTO_REPLY_ENABLED = os.getenv("AUTO_REPLY_ENABLED", "true").lower() == "true"
AUTO_REPLY_DELAY_SEC = int(os.getenv("AUTO_REPLY_DELAY_SEC", "120"))

AUTO_REPLY_DISCLOSE = os.getenv("AUTO_REPLY_DISCLOSE", "true").lower() == "true"

SUGGEST_ENABLED = os.getenv("SUGGEST_ENABLED", "true").lower() == "true"
SUGGEST_MAX_CHARS = int(os.getenv("SUGGEST_MAX_CHARS", "900"))


def _shorten(text, limit):
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


class AISupport(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.coll = bot.api.get_plugin_partition(self)
        self._pending = {}  # thread_id -> task

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

    async def _send_to_thread(self, thread, content):
        # Prefer thread.reply if available
        if hasattr(thread, "reply"):
            return await thread.reply(content)

        # Fallback to channel.send
        channel = getattr(thread, "channel", None)
        if channel:
            return await channel.send(content)

    async def _send_to_staff_channel(self, thread, content):
        channel = getattr(thread, "channel", None)
        if channel:
            return await channel.send(content)

    async def _schedule_auto_reply(self, thread, creator, initial_message):
        if not AUTO_REPLY_ENABLED:
            return

        thread_id = getattr(thread, "id", None)
        if thread_id is None:
            return

        # cancel any existing task
        existing = self._pending.get(thread_id)
        if existing:
            existing.cancel()

        async def _task():
            try:
                await asyncio.sleep(AUTO_REPLY_DELAY_SEC)

                # If staff replied since, skip
                doc = await self.coll.find_one({"_id": f"thread:{thread_id}"})
                if doc and doc.get("staff_replied"):
                    return

                user_text = getattr(initial_message, "content", "")
                user_text = _shorten(user_text or "", 1200)

                system = (
                    "You are a support assistant for a Discord server. "
                    "Be concise, polite, and helpful. Ask one clarifying question if needed. "
                    "Do not promise anything or claim to be a human."
                )

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

        self._pending[thread_id] = asyncio.create_task(_task())

    @commands.Cog.listener()
    async def on_thread_ready(self, thread, creator, category, initial_message):
        await self._schedule_auto_reply(thread, creator, initial_message)

    @commands.Cog.listener()
    async def on_thread_reply(self, thread, from_mod, message, anonymous, plain):
        thread_id = getattr(thread, "id", None)
        if thread_id is None:
            return

        # mark staff replied
        if from_mod:
            await self.coll.update_one(
                {"_id": f"thread:{thread_id}"},
                {"$set": {"staff_replied": True, "staff_replied_at": datetime.now(timezone.utc)}},
                upsert=True,
            )
            # cancel pending auto reply
            task = self._pending.pop(thread_id, None)
            if task:
                task.cancel()
            return

        if not SUGGEST_ENABLED:
            return

        # Draft suggestion in staff channel
        user_text = getattr(message, "content", "")
        if not user_text:
            return

        system = (
            "You are an assistant that drafts responses for staff. "
            "Return a short, professional reply. Do not mention AI."
        )

        prompt = (
            f"User message:\n{user_text}\n\n"
            "Draft a helpful reply for staff to send."
        )

        try:
            draft = await self._ollama_generate(prompt, system)
            draft = _shorten(draft, SUGGEST_MAX_CHARS)
            await self._send_to_staff_channel(thread, f"**AI draft (not sent):**\n{draft}")
        except Exception as e:
            await self._send_to_staff_channel(thread, f"[AI draft failed] {e}")

    @commands.command()
    async def ai(self, ctx, *, message):
        """Staff command: ?ai <message> — generate a draft reply."""
        if not SUGGEST_ENABLED:
            return await ctx.send("AI drafts are disabled.")

        system = (
            "You are an assistant that drafts responses for staff. "
            "Return a short, professional reply. Do not mention AI."
        )
        prompt = f"User message:\n{message}\n\nDraft a helpful reply for staff to send."
        try:
            draft = await self._ollama_generate(prompt, system)
            draft = _shorten(draft, SUGGEST_MAX_CHARS)
            await ctx.send(f"**AI draft (not sent):**\n{draft}")
        except Exception as e:
            await ctx.send(f"[AI draft failed] {e}")


async def setup(bot):
    await bot.add_cog(AISupport(bot))
