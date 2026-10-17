"""Notification delivery: Discord bot channel and/or a Discord webhook.
Every notice is also logged to the events table for the GUI activity feed."""
import asyncio
import io
import json
import logging
from dataclasses import dataclass, field
from typing import Optional

import aiohttp
import discord

from . import db, images

log = logging.getLogger("notify")

COLORS = {
    "new_item": 0x2ECC71,
    "returned": 0x1ABC9C,
    "price_alert": 0xE67E22,
    "price_drop": 0x3498DB,
    "all_time_low": 0x9B59B6,
    "back_in_stock": 0x2ECC71,
    "error": 0xE74C3C,
    "recovered": 0x95A5A6,
    "test": 0x95A5A6,
}


@dataclass
class Notice:
    type: str
    title: str
    url: str = ""
    description: str = ""
    fields: list = field(default_factory=list)  # [(name, value, inline)]
    image: str = ""
    tracker_id: Optional[int] = None
    item_id: Optional[int] = None
    channel_id: Optional[str] = None
    footer: str = ""

    def embed(self) -> discord.Embed:
        e = discord.Embed(title=self.title[:256], url=self.url or None,
                          description=self.description[:4000] or None,
                          color=COLORS.get(self.type, 0x7289DA))
        for name, value, inline in self.fields[:25]:
            e.add_field(name=str(name)[:256], value=str(value)[:1024] or "-", inline=inline)
        if self.image:
            e.set_thumbnail(url=self.image)
        if self.footer:
            e.set_footer(text=self.footer[:2048])
        return e


class Notifier:
    def __init__(self):
        self.bot: Optional[discord.Client] = None

    def attach_bot(self, bot: discord.Client):
        self.bot = bot

    def _channel_id(self, notice: Notice) -> str:
        return (notice.channel_id or db.get_setting("default_channel_id") or "").strip()

    @staticmethod
    async def _prepare(notices: list[Notice]) -> list[tuple[discord.Embed, Optional[tuple[str, bytes]]]]:
        """Build embeds; product images are attached as files because some image
        CDNs (Micro Center) block Discord's image proxy."""
        out = []
        for idx, n in enumerate(notices):
            e = n.embed()
            att = None
            if n.image:
                res = await asyncio.to_thread(images.get, n.image)
                if res:
                    ext = {"image/png": "png", "image/webp": "webp", "image/gif": "gif"}.get(res[1], "jpg")
                    name = f"img{idx}.{ext}"
                    att = (name, res[0])
                    e.set_thumbnail(url=f"attachment://{name}")
            out.append((e, att))
        return out

    async def _send_bot(self, channel_id: str, prepared: list, content: str) -> bool:
        if not (self.bot and self.bot.is_ready() and channel_id):
            return False
        try:
            ch = self.bot.get_channel(int(channel_id)) or await self.bot.fetch_channel(int(channel_id))
            for i in range(0, len(prepared), 10):
                chunk = prepared[i:i + 10]
                files = [discord.File(io.BytesIO(a[1]), filename=a[0]) for _, a in chunk if a]
                await ch.send(content=content if i == 0 and content else None,
                              embeds=[e for e, _ in chunk], files=files)
            return True
        except Exception as e:
            log.warning("bot send to %s failed: %s", channel_id, e)
            return False

    async def _send_webhook(self, prepared: list, content: str) -> bool:
        url = db.get_setting("webhook_url").strip()
        if not url:
            return False
        try:
            async with aiohttp.ClientSession() as s:
                for i in range(0, len(prepared), 10):
                    chunk = prepared[i:i + 10]
                    payload = {"embeds": [e.to_dict() for e, _ in chunk], "username": "Price Tracker"}
                    if content and i == 0:
                        payload["content"] = content
                    form = aiohttp.FormData()
                    form.add_field("payload_json", json.dumps(payload), content_type="application/json")
                    for n, (_, a) in enumerate(c for c in chunk if c[1]):
                        form.add_field(f"files[{n}]", a[1], filename=a[0])
                    async with s.post(url, data=form) as r:
                        if r.status >= 300:
                            log.warning("webhook HTTP %s: %s", r.status, await r.text())
                            return False
            return True
        except Exception as e:
            log.warning("webhook failed: %s", e)
            return False

    async def send(self, notices: list[Notice]) -> bool:
        """Send notices (grouped per channel). Returns True if delivered anywhere."""
        if not notices:
            return True
        mention = db.get_setting("mention").strip()
        groups: dict[str, list[Notice]] = {}
        for n in notices:
            groups.setdefault(self._channel_id(n), []).append(n)
        delivered_all = True
        for channel_id, group in groups.items():
            prepared = await self._prepare(group)
            ok = await self._send_bot(channel_id, prepared, mention)
            if not ok:
                ok = await self._send_webhook(prepared, mention)
            delivered_all &= ok
            for n in group:
                db.add_event(n.type, n.title, n.description, n.tracker_id, n.item_id, n.url, sent=ok)
            if not ok:
                log.warning("no delivery route for %d notice(s); configure a channel or webhook", len(group))
        return delivered_all


notifier = Notifier()
