"""Operations shared by the web API and the Discord bot."""
import asyncio
import time
from typing import Optional

from . import db, monitor, sites

ALERT_TYPES = {"below", "drop_pct", "all_time_low"}


class UserError(Exception):
    pass


def _detect(url: str):
    url = url.strip().strip("<>")
    if not url.startswith("http"):
        url = "https://" + url
    site = sites.site_for(url)
    if not site:
        raise UserError("Only microcenter.com and amazon.com URLs are supported")
    found = sites.module(site).detect(url)
    if not found:
        raise UserError("Couldn't tell whether that's a search page or a product page")
    kind, norm = found
    return site, kind, norm


async def detect(url: str):
    return await asyncio.to_thread(_detect, url)


async def preview(url: str) -> dict:
    """Scrape a URL without saving it, so the GUI can show what will be tracked."""
    site, kind, norm = await detect(url)
    fake = {"id": 0, "url": norm, "site": site, "kind": kind, "options": {}}
    result = await asyncio.to_thread(sites.module(site).scrape, fake)
    return {"site": site, "kind": kind, "url": norm, "title": result.title,
            "total_count": result.total_count, "pages": result.pages,
            "series": sites.series_labels(site, kind),
            "items": [{"key": i.key, "name": i.name, "url": i.url, "image": i.image,
                       "prices": i.prices, "extra": {k: v for k, v in i.extra.items() if k != "offers"},
                       "offers": i.extra.get("offers", [])[:15]}
                      for i in result.items[:100]]}


async def create_tracker(url: str, name: str = "", interval_min: Optional[int] = None,
                         options: Optional[dict] = None, channel_id: Optional[str] = None,
                         alerts: Optional[list] = None, check: bool = True) -> dict:
    site, kind, norm = await detect(url)
    dup = db.q1("SELECT id, name FROM trackers WHERE url = ?", (norm,))
    if dup:
        raise UserError(f"Already tracking that URL as #{dup['id']} ({dup['name']})")
    tid = db.insert("trackers", {
        "name": (name or "").strip() or norm, "url": norm, "site": site, "kind": kind,
        "enabled": 1, "interval_min": interval_min or None, "channel_id": channel_id or None,
        "options": options or {}, "created_at": time.time()})
    for a in alerts or []:
        add_alert(tid, **a)
    if check:
        asyncio.create_task(monitor.check_now(tid))
    return db.get_tracker(tid)


def add_alert(tracker_id: int, type: str = "below", value: Optional[float] = None,
              series: str = "lowest", item_id: Optional[int] = None, note: str = "",
              enabled: bool = True) -> dict:
    t = db.get_tracker(tracker_id)
    if not t:
        raise UserError("Tracker not found")
    if type not in ALERT_TYPES:
        raise UserError(f"Alert type must be one of {', '.join(sorted(ALERT_TYPES))}")
    labels = sites.series_labels(t["site"], t["kind"])
    if series not in labels:
        raise UserError(f"Price type must be one of: {', '.join(labels)}")
    if type != "all_time_low":
        if value is None or float(value) <= 0:
            raise UserError("Alert needs a positive value")
        value = float(value)
    if item_id and not db.q1("SELECT 1 FROM items WHERE id = ? AND tracker_id = ?", (item_id, tracker_id)):
        raise UserError("Item does not belong to this tracker")
    aid = db.insert("alerts", {"tracker_id": tracker_id, "item_id": item_id or None, "series": series,
                               "type": type, "value": value, "enabled": int(enabled), "note": note or "",
                               "created_at": time.time()})
    return db.q1("SELECT * FROM alerts WHERE id = ?", (aid,))


def describe_alert(a: dict, site: str, kind: Optional[str] = None) -> str:
    label = sites.series_labels(site, kind).get(a["series"], a["series"])
    if a["type"] == "below":
        s = f"{label} ≤ {monitor.money(a['value'])}"
    elif a["type"] == "drop_pct":
        s = f"{label} drops ≥ {a['value']:g}%"
    else:
        s = f"{label} hits all-time low"
    if a.get("item_id"):
        it = db.get_item(a["item_id"])
        if it:
            s += f" (only: {it['name'][:40]})"
    return s + ("" if a["enabled"] else " [off]")


def find_tracker(ref) -> Optional[dict]:
    """Look up by id or (partial) name."""
    ref = str(ref).strip().lstrip("#")
    if ref.isdigit():
        return db.get_tracker(int(ref))
    rows = db.q("SELECT * FROM trackers WHERE name LIKE ? ORDER BY id", (f"%{ref}%",))
    return rows[0] if len(rows) == 1 else None


def tracker_summary(t: dict) -> dict:
    items = db.tracker_items(t["id"])
    present = [i for i in items if i["present"]]
    d = dict(t)
    d["interval_effective"] = db.tracker_interval(t)
    d["item_count"] = len(present)
    d["series"] = sites.series_labels(t["site"], t["kind"])
    d["alert_count"] = db.q1("SELECT COUNT(*) AS n FROM alerts WHERE tracker_id = ?", (t["id"],))["n"]
    if t["kind"] == "product" and items:
        it = items[0]
        d["item"] = {"id": it["id"], "name": it["name"], "image": it["image"], "prices": it["prices"],
                     "extra": {k: v for k, v in it["extra"].items() if k != "offers"},
                     "lows": db.lows(it["id"]), "present": it["present"]}
    else:
        lows = [monitor.lowest(i["prices"]) for i in present]
        lows = [v for v in lows if v is not None]
        d["cheapest"] = min(lows) if lows else None
        # Items from the first (baseline) check don't count as new.
        cutoff = max(time.time() - 86400, t["created_at"] + 600)
        d["new_24h"] = sum(1 for i in items if i["first_seen"] and i["first_seen"] > cutoff)
        d["thumbs"] = [i["image"] for i in present[:4] if i["image"]]
    return d
