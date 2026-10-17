"""Scheduler + check logic: scrape a tracker, store prices/history, evaluate
alerts and new-item rules, and send notifications."""
import asyncio
import logging
import random
import time
from typing import Optional

from . import config, db, sites
from .notify import Notice, notifier

log = logging.getLogger("monitor")

_site_locks: dict[str, asyncio.Lock] = {}
_running: dict[int, asyncio.Task] = {}
MAX_NEW_ITEM_NOTICES = 20

SEARCH_DEFAULTS = {"notify_new": True, "notify_returning": True, "return_after_hours": 12,
                   "notify_price_drop": False, "notify_first_run": False}
PRODUCT_DEFAULTS = {"notify_stock": True}


def money(v: Optional[float]) -> str:
    return "—" if v is None else f"${v:,.2f}"


def lowest(prices: dict) -> Optional[float]:
    vals = [v for v in prices.values() if isinstance(v, (int, float))]
    return min(vals) if vals else None


def series_value(prices: dict, series: str) -> Optional[float]:
    return lowest(prices) if series == "lowest" else prices.get(series)


def tracker_options(t: dict) -> dict:
    base = SEARCH_DEFAULTS if t["kind"] == "search" else PRODUCT_DEFAULTS
    return {**base, **(t.get("options") or {})}


def _keywords(s) -> list[str]:
    if isinstance(s, list):
        return [k.strip().lower() for k in s if k.strip()]
    return [k.strip().lower() for k in (s or "").split(",") if k.strip()]


def passes_filters(opts: dict, name: str) -> bool:
    n = (name or "").lower()
    inc, exc = _keywords(opts.get("include_keywords")), _keywords(opts.get("exclude_keywords"))
    if inc and not any(k in n for k in inc):
        return False
    return not any(k in n for k in exc)


def price_fields(site: str, prices: dict, extra: dict) -> list:
    labels = sites.module(site).SERIES
    fields = [(labels.get(k, k), money(v), True) for k, v in prices.items() if v is not None]
    if extra.get("stock") is not None or extra.get("in_stock") is not None:
        stock = extra.get("stock")
        txt = "In stock" if extra.get("in_stock") else "Out of stock"
        if stock:
            txt = f"{stock} in stock"
        if extra.get("store"):
            txt += f" ({extra['store']})"
        fields.append(("Stock", txt, True))
    return fields


def _item_notice(type_: str, title: str, t: dict, item_id: int, it, description: str = "",
                 extra_fields: Optional[list] = None) -> Notice:
    return Notice(
        type=type_, title=title, url=it.url, description=description,
        fields=price_fields(t["site"], it.prices, it.extra) + (extra_fields or []),
        image=it.image, tracker_id=t["id"], item_id=item_id, channel_id=t.get("channel_id"),
        footer=f"{t['name']} · #{t['id']}",
    )


def _eval_alerts(t: dict, alerts: list, item_id: int, it, prev: dict, lows_before: dict, now: float) -> list:
    notices = []
    labels = sites.series_labels(t["site"], t["kind"])
    for a in alerts:
        if not a["enabled"] or (a["item_id"] and a["item_id"] != item_id):
            continue
        series = a["series"] or "lowest"
        cur = series_value(it.prices, series)
        before = series_value(prev, series) if prev else None
        label = labels.get(series, series)
        st = db.get_alert_state(a["id"], item_id)
        fired = None
        if a["type"] == "below":
            if cur is not None and a["value"] is not None and cur <= a["value"]:
                if not st or st["fired_price"] is None or cur < st["fired_price"] - 0.005:
                    fired = f"**{label}** is **{money(cur)}** (your alert: ≤ {money(a['value'])})"
                    if st and st["fired_price"] is not None:
                        fired += f"\nDropped further from {money(st['fired_price'])}"
                    db.set_alert_state(a["id"], item_id, cur, now)
            elif cur is not None and st and st["fired_price"] is not None:
                # Re-arm only once the price is seen above the level again; offers that
                # briefly disappear (common for Amazon used listings) don't re-trigger.
                db.set_alert_state(a["id"], item_id, None, None)
        elif a["type"] == "drop_pct":
            if cur is not None and before and a["value"] and cur <= before * (1 - a["value"] / 100):
                pct = (before - cur) / before * 100
                fired = f"**{label}** dropped {pct:.0f}%: {money(before)} → **{money(cur)}**"
        elif a["type"] == "all_time_low":
            if series == "lowest":
                low = lowest({k: v["low"] for k, v in lows_before.items()})
            else:
                low = (lows_before.get(series) or {}).get("low")
            if cur is not None and low is not None and cur < low - 0.005:
                fired = f"**{label}** hit an all-time low: **{money(cur)}** (previous low {money(low)})"
        if fired:
            if a.get("note"):
                fired += f"\n_{a['note']}_"
            ntype = "all_time_low" if a["type"] == "all_time_low" else "price_alert"
            notices.append(_item_notice(ntype, f"💰 {it.name}", t, item_id, it, fired))
    return notices


def ingest(t: dict, result) -> list[Notice]:
    """Store scrape results and return the notices to send. Runs in a worker thread."""
    now = time.time()
    opts = tracker_options(t)
    is_search = t["kind"] == "search"
    baseline = is_search and not t["baseline_done"]
    existing = {i["key"]: i for i in db.tracker_items(t["id"])}
    alerts = db.tracker_alerts(t["id"])
    notices, new_items, seen = [], [], set()

    for it in result.items:
        if is_search and not passes_filters(opts, it.name):
            continue
        seen.add(it.key)
        ex = existing.get(it.key)
        if ex is None:
            item_id = db.insert("items", {
                "tracker_id": t["id"], "key": it.key, "name": it.name, "url": it.url, "image": it.image,
                "first_seen": now, "last_seen": now, "present": 1, "prices": it.prices, "extra": it.extra})
            prev, lows_before, ignored = {}, {}, False
        else:
            item_id = ex["id"]
            prev, ignored = ex["prices"], bool(ex["ignored"])
            lows_before = db.lows(item_id)
            db.update("items", item_id, {"name": it.name or ex["name"], "url": it.url or ex["url"],
                                         "image": it.image or ex["image"], "last_seen": now, "present": 1,
                                         "prices": it.prices, "extra": it.extra})
        for series, price in it.prices.items():
            db.record_price(item_id, series, price, now)
        if ignored:
            continue

        cur_low = lowest(it.prices)
        if is_search:
            max_price = opts.get("max_price")
            under_max = not max_price or (cur_low is not None and cur_low <= float(max_price))
            if ex is None and opts["notify_new"] and under_max and (not baseline or opts["notify_first_run"]):
                new_items.append(_item_notice("new_item", f"🆕 {it.name}", t, item_id, it))
            elif (ex is not None and not ex["present"] and opts["notify_returning"] and under_max
                  and now - (ex["last_seen"] or 0) >= float(opts["return_after_hours"]) * 3600):
                notices.append(_item_notice("returned", f"🔁 Back: {it.name}", t, item_id, it,
                                            "This item is showing up in the search again."))
            elif ex is not None and opts["notify_price_drop"]:
                prev_low = lowest(prev)
                if prev_low and cur_low is not None and cur_low < prev_low - 0.005:
                    notices.append(_item_notice("price_drop", f"📉 {it.name}", t, item_id, it,
                                                f"Price dropped {money(prev_low)} → **{money(cur_low)}**"))
        elif ex is not None and opts["notify_stock"]:
            if ex["extra"].get("in_stock") is False and it.extra.get("in_stock") is True:
                notices.append(_item_notice("back_in_stock", f"✅ Back in stock: {it.name}", t, item_id, it))

        notices += _eval_alerts(t, alerts, item_id, it, prev, lows_before, now)

    if len(new_items) > MAX_NEW_ITEM_NOTICES:
        extra = len(new_items) - MAX_NEW_ITEM_NOTICES
        new_items = new_items[:MAX_NEW_ITEM_NOTICES]
        new_items.append(Notice("new_item", f"…and {extra} more new items in {t['name']}", url=t["url"],
                                tracker_id=t["id"], channel_id=t.get("channel_id")))
    notices = new_items + notices

    if is_search:
        for key, ex in existing.items():
            if key not in seen and ex["present"]:
                db.update("items", ex["id"], {"present": 0})
                for series, price in ex["prices"].items():
                    if price is not None:
                        db.record_price(ex["id"], series, None, now)

    if t["fail_count"] >= config.FAIL_NOTIFY_THRESHOLD and db.get_setting("notify_failures") == "1":
        notices.append(Notice("recovered", f"✅ Tracker working again: {t['name']}", url=t["url"],
                              tracker_id=t["id"], channel_id=t.get("channel_id")))
    fields = {"last_checked": now, "last_ok": now, "last_error": None, "fail_count": 0, "baseline_done": 1}
    if not t["name"] or t["name"] == t["url"]:
        fields["name"] = (result.title or t["url"])[:120]
    db.update("trackers", t["id"], fields)
    return notices


async def _check(tid: int) -> dict:
    t = db.get_tracker(tid)
    if not t:
        return {"ok": False, "error": "tracker not found"}
    mod = sites.module(t["site"])
    lock = _site_locks.setdefault(t["site"], asyncio.Lock())
    async with lock:
        started = time.time()
        try:
            result = await asyncio.to_thread(mod.scrape, t)
        except Exception as e:
            err = f"{type(e).__name__}: {e}"[:500]
            log.warning("check #%s %s failed: %s", tid, t["name"], err)
            fails = t["fail_count"] + 1
            db.update("trackers", tid, {"last_checked": time.time(), "last_error": err, "fail_count": fails})
            if fails == config.FAIL_NOTIFY_THRESHOLD and db.get_setting("notify_failures") == "1":
                await notifier.send([Notice("error", f"⚠️ Tracker failing: {t['name']}", url=t["url"],
                                            description=f"{fails} checks in a row failed.\n`{err[:300]}`",
                                            tracker_id=tid, channel_id=t.get("channel_id"))])
            return {"ok": False, "error": err}
        finally:
            # Be polite between consecutive requests to the same site.
            await asyncio.sleep(random.uniform(2, 6))
    notices = await asyncio.to_thread(ingest, t, result)
    await notifier.send(notices)
    log.info("check #%s %s: %d items, %d notices (%.1fs)", tid, t["name"], len(result.items),
             len(notices), time.time() - started)
    return {"ok": True, "items": len(result.items), "notices": len(notices)}


async def check_now(tid: int) -> dict:
    """Run a check (or join one already in progress) and return its summary."""
    task = _running.get(tid)
    if task is None or task.done():
        task = asyncio.create_task(_check(tid))
        _running[tid] = task
    return await asyncio.shield(task)


def is_due(t: dict, now: float) -> bool:
    if not t["enabled"]:
        return False
    if t["last_checked"] is None:
        return True
    backoff = min(2 ** max(0, t["fail_count"] - 1), 8)
    return now - t["last_checked"] >= db.tracker_interval(t) * 60 * backoff


async def run_forever():
    await asyncio.sleep(3)
    log.info("scheduler started")
    while True:
        try:
            now = time.time()
            for t in db.list_trackers():
                task = _running.get(t["id"])
                if (task is None or task.done()) and is_due(t, now):
                    _running[t["id"]] = asyncio.create_task(_check(t["id"]))
        except Exception:
            log.exception("scheduler error")
        await asyncio.sleep(20)
