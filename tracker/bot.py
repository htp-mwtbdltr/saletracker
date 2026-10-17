"""Discord bot with slash commands for managing trackers and alerts."""
import datetime
import logging
from typing import Optional

import discord
from discord import app_commands

from . import db, monitor, service, sites
from .monitor import money

log = logging.getLogger("bot")

ALERT_TYPE_CHOICES = [
    app_commands.Choice(name="Price at or below", value="below"),
    app_commands.Choice(name="Drops by % (value = percent)", value="drop_pct"),
    app_commands.Choice(name="New all-time low", value="all_time_low"),
]


class TrackerBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        register(self.tree)

    async def _sync(self, guild: discord.Guild):
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)

    async def on_ready(self):
        log.info("logged in as %s in %d guild(s)", self.user, len(self.guilds))
        for g in self.guilds:
            try:
                await self._sync(g)
            except Exception as e:
                log.warning("command sync failed for %s: %s", g.name, e)

    async def on_guild_join(self, guild: discord.Guild):
        await self._sync(guild)


# ---------- helpers ----------
def web_link(path: str = "") -> str:
    base = db.get_setting("web_url").rstrip("/")
    return f"{base}/{path}" if base else ""


async def tracker_autocomplete(_: discord.Interaction, current: str):
    cur = current.lower().lstrip("#")
    out = []
    for t in db.list_trackers():
        label = f"#{t['id']} {t['name']}"
        if not cur or cur in label.lower():
            out.append(app_commands.Choice(name=label[:100], value=str(t["id"])))
    return out[:25]


async def series_autocomplete(interaction: discord.Interaction, current: str):
    t = service.find_tracker(getattr(interaction.namespace, "tracker", "") or "")
    labels = sites.series_labels(t["site"], t["kind"]) if t else {**sites.series_labels("microcenter"),
                                                       **sites.series_labels("amazon")}
    return [app_commands.Choice(name=v, value=k) for k, v in labels.items()
            if current.lower() in v.lower() or current.lower() in k][:25]


def get_tracker(ref: str) -> dict:
    t = service.find_tracker(ref)
    if not t:
        raise service.UserError(f"No tracker matches `{ref}`. Use /list to see them.")
    return t


def status_line(t: dict) -> str:
    if not t["enabled"]:
        return "⏸️ paused"
    if t["fail_count"]:
        return f"⚠️ failing ({t['fail_count']}x)"
    if t["last_ok"]:
        return f"✅ <t:{int(t['last_ok'])}:R>"
    return "⏳ pending"


def tracker_embed(t: dict) -> discord.Embed:
    s = service.tracker_summary(t)
    labels = sites.series_labels(t["site"], t["kind"])
    e = discord.Embed(title=f"#{t['id']} {t['name']}"[:256], url=t["url"], color=0x5865F2)
    e.add_field(name="Type", value=f"{t['site'].title()} {t['kind']}", inline=True)
    e.add_field(name="Every", value=f"{s['interval_effective']} min", inline=True)
    e.add_field(name="Status", value=status_line(t), inline=True)
    if t["kind"] == "product" and s.get("item"):
        it = s["item"]
        lines = []
        for k, v in it["prices"].items():
            if v is None:
                continue
            low = (it["lows"].get(k) or {}).get("low")
            lines.append(f"**{labels.get(k, k)}**: {money(v)}" + (f"  (low {money(low)})" if low and low < v else ""))
        e.add_field(name="Prices", value="\n".join(lines) or "No prices available", inline=False)
        ex = it["extra"]
        if ex.get("stock") is not None or ex.get("availability"):
            e.add_field(name="Stock", value=str(ex.get("stock") or ex.get("availability") or "-")[:200], inline=True)
        if it["image"]:
            e.set_thumbnail(url=it["image"])
    else:
        items = [i for i in db.tracker_items(t["id"]) if i["present"]]
        items.sort(key=lambda i: monitor.lowest(i["prices"]) or 1e12)
        lines = [f"{money(monitor.lowest(i['prices']))} · [{i['name'][:60]}]({i['url']})" for i in items[:10]]
        e.add_field(name=f"Cheapest of {len(items)} items", value="\n".join(lines)[:1024] or "None", inline=False)
    alerts = db.tracker_alerts(t["id"])
    if alerts:
        e.add_field(name="Alerts", value="\n".join(f"`{a['id']}` {service.describe_alert(a, t['site'], t['kind'])}"
                                                    for a in alerts)[:1024], inline=False)
    if t["last_error"]:
        e.add_field(name="Last error", value=f"`{t['last_error'][:300]}`", inline=False)
    link = web_link(f"#/t/{t['id']}")
    if link:
        e.description = f"[Open in web GUI]({link})"
    return e


async def reply(interaction: discord.Interaction, content=None, embed=None, ephemeral=False):
    if interaction.response.is_done():
        await interaction.followup.send(content=content, embed=embed, ephemeral=ephemeral)
    else:
        await interaction.response.send_message(content=content, embed=embed, ephemeral=ephemeral)


# ---------- commands ----------
def register(tree: app_commands.CommandTree):

    @tree.error
    async def on_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
        err = getattr(error, "original", error)
        msg = f"❌ {err}" if isinstance(err, service.UserError) else f"❌ Error: {err}"
        if not isinstance(err, service.UserError):
            log.exception("command error", exc_info=err)
        await reply(interaction, msg, ephemeral=True)

    @tree.command(name="track", description="Track a Micro Center / Amazon search or product URL")
    @app_commands.describe(url="Search results page or product page URL",
                           below="Optional: alert when price is at or below this",
                           price_type="Which price the alert watches (default: lowest of any)",
                           name="Optional display name")
    @app_commands.autocomplete(price_type=series_autocomplete)
    async def track(interaction: discord.Interaction, url: str, below: Optional[float] = None,
                    price_type: Optional[str] = None, name: Optional[str] = None):
        await interaction.response.defer(thinking=True)
        alerts = [{"type": "below", "value": below, "series": price_type or "lowest"}] if below else []
        t = await service.create_tracker(url, name or "", alerts=alerts, check=False)
        res = await monitor.check_now(t["id"])
        t = db.get_tracker(t["id"])
        note = "" if res.get("ok") else f"\n⚠️ First check failed: {res.get('error')}"
        await reply(interaction, f"✅ Now tracking **#{t['id']}**{note}", embed=tracker_embed(t))

    @tree.command(name="list", description="List all trackers")
    async def list_cmd(interaction: discord.Interaction):
        ts = db.list_trackers()
        if not ts:
            return await reply(interaction, "Nothing tracked yet. Use `/track <url>`.")
        lines = []
        for t in ts:
            s = service.tracker_summary(t)
            if t["kind"] == "product":
                price = money(monitor.lowest((s.get("item") or {}).get("prices", {})))
                detail = f"lowest {price}"
            else:
                detail = f"{s['item_count']} items, from {money(s.get('cheapest'))}"
            lines.append(f"`#{t['id']}` **{t['name'][:60]}** · {t['site']} {t['kind']} · {detail} · {status_line(t)}")
        e = discord.Embed(title="Trackers", description="\n".join(lines)[:4000], color=0x5865F2)
        if web_link():
            e.url = web_link()
        await reply(interaction, embed=e)

    @tree.command(name="info", description="Show prices, alerts and status for a tracker")
    @app_commands.autocomplete(tracker=tracker_autocomplete)
    async def info(interaction: discord.Interaction, tracker: str):
        await reply(interaction, embed=tracker_embed(get_tracker(tracker)))

    @tree.command(name="check", description="Check a tracker right now")
    @app_commands.autocomplete(tracker=tracker_autocomplete)
    async def check(interaction: discord.Interaction, tracker: str):
        t = get_tracker(tracker)
        await interaction.response.defer(thinking=True)
        res = await monitor.check_now(t["id"])
        msg = (f"🔎 Checked: {res['items']} item(s), {res['notices']} notification(s)" if res.get("ok")
               else f"⚠️ Check failed: {res.get('error')}")
        await reply(interaction, msg, embed=tracker_embed(db.get_tracker(t["id"])))

    @tree.command(name="remove", description="Stop tracking and delete a tracker")
    @app_commands.autocomplete(tracker=tracker_autocomplete)
    async def remove(interaction: discord.Interaction, tracker: str):
        t = get_tracker(tracker)
        db.ex("DELETE FROM trackers WHERE id = ?", (t["id"],))
        await reply(interaction, f"🗑️ Removed #{t['id']} {t['name']}")

    @tree.command(name="pause", description="Pause a tracker")
    @app_commands.autocomplete(tracker=tracker_autocomplete)
    async def pause(interaction: discord.Interaction, tracker: str):
        t = get_tracker(tracker)
        db.update("trackers", t["id"], {"enabled": 0})
        await reply(interaction, f"⏸️ Paused #{t['id']} {t['name']}")

    @tree.command(name="resume", description="Resume a paused tracker")
    @app_commands.autocomplete(tracker=tracker_autocomplete)
    async def resume(interaction: discord.Interaction, tracker: str):
        t = get_tracker(tracker)
        db.update("trackers", t["id"], {"enabled": 1})
        await reply(interaction, f"▶️ Resumed #{t['id']} {t['name']}")

    @tree.command(name="interval", description="Set how often a tracker is checked")
    @app_commands.autocomplete(tracker=tracker_autocomplete)
    async def interval(interaction: discord.Interaction, tracker: str,
                       minutes: app_commands.Range[int, 2, 1440]):
        t = get_tracker(tracker)
        db.update("trackers", t["id"], {"interval_min": minutes})
        await reply(interaction, f"⏱️ #{t['id']} now checks every {minutes} min")

    alert = app_commands.Group(name="alert", description="Manage price alerts")

    @alert.command(name="add", description="Add a price alert to a tracker")
    @app_commands.describe(value="Price (e.g. 130) or percent for drop alerts",
                           price_type="Which price to watch (default: lowest of any)")
    @app_commands.autocomplete(tracker=tracker_autocomplete, price_type=series_autocomplete)
    @app_commands.choices(kind=ALERT_TYPE_CHOICES)
    async def alert_add(interaction: discord.Interaction, tracker: str, value: Optional[float] = None,
                        price_type: Optional[str] = None, kind: Optional[app_commands.Choice[str]] = None,
                        note: Optional[str] = None):
        t = get_tracker(tracker)
        a = service.add_alert(t["id"], type=kind.value if kind else "below", value=value,
                              series=price_type or "lowest", note=note or "")
        await reply(interaction, f"🔔 Alert `{a['id']}` on #{t['id']}: {service.describe_alert(a, t['site'], t['kind'])}")
        await monitor.check_now(t["id"])

    @alert.command(name="list", description="List alerts")
    @app_commands.autocomplete(tracker=tracker_autocomplete)
    async def alert_list(interaction: discord.Interaction, tracker: Optional[str] = None):
        ts = [get_tracker(tracker)] if tracker else db.list_trackers()
        lines = []
        for t in ts:
            for a in db.tracker_alerts(t["id"]):
                lines.append(f"`{a['id']}` #{t['id']} {t['name'][:40]}: {service.describe_alert(a, t['site'], t['kind'])}")
        await reply(interaction, "\n".join(lines)[:1900] or "No alerts set.")

    @alert.command(name="remove", description="Delete an alert by its id (see /alert list)")
    async def alert_remove(interaction: discord.Interaction, alert_id: int):
        if not db.q1("SELECT 1 FROM alerts WHERE id = ?", (alert_id,)):
            raise service.UserError(f"No alert with id {alert_id}")
        db.ex("DELETE FROM alerts WHERE id = ?", (alert_id,))
        await reply(interaction, f"🗑️ Removed alert `{alert_id}`")

    tree.add_command(alert)

    @tree.command(name="setchannel", description="Send notifications to a channel (default: this one)")
    @app_commands.describe(tracker="Only for this tracker (leave empty to set the default channel)")
    @app_commands.autocomplete(tracker=tracker_autocomplete)
    async def setchannel(interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None,
                         tracker: Optional[str] = None):
        ch = channel or interaction.channel
        if tracker:
            t = get_tracker(tracker)
            db.update("trackers", t["id"], {"channel_id": str(ch.id)})
            return await reply(interaction, f"📣 #{t['id']} will notify {ch.mention}")
        db.set_settings({"default_channel_id": str(ch.id)})
        await reply(interaction, f"📣 Default notification channel set to {ch.mention}")

    @tree.command(name="status", description="Tracker service status")
    async def status(interaction: discord.Interaction):
        ts = db.list_trackers()
        ch = db.get_setting("default_channel_id")
        e = discord.Embed(title="Price Tracker status", color=0x5865F2, url=web_link() or None)
        e.add_field(name="Trackers", value=f"{sum(t['enabled'] for t in ts)} active / {len(ts)}", inline=True)
        e.add_field(name="Failing", value=str(sum(1 for t in ts if t["fail_count"])), inline=True)
        e.add_field(name="Channel", value=f"<#{ch}>" if ch else "not set (/setchannel)", inline=True)
        recent = db.recent_events(5)
        if recent:
            e.add_field(name="Recent notifications", inline=False,
                        value="\n".join(f"<t:{int(ev['ts'])}:R> {ev['title'][:70]}" for ev in recent))
        await reply(interaction, embed=e)

    @tree.command(name="count", description="Count messages in this channel")
    @app_commands.describe(days="Days to look back (leave empty for all time)")
    async def count(interaction: discord.Interaction, days: Optional[int] = None):
        await interaction.response.defer(thinking=True)
        after = (discord.utils.utcnow() - datetime.timedelta(days=days)) if days else None
        n = 0
        async for _ in interaction.channel.history(limit=None, after=after):
            n += 1
        span = f"the last {days} day{'s' if days != 1 else ''}" if days else "all time"
        await reply(interaction, f"📊 Messages in {span}: **{n}**")
