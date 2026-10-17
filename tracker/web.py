"""Web GUI + JSON API."""
import asyncio
import base64
import logging
import secrets
import time
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config, db, images, monitor, service, sites
from .notify import Notice, notifier

STATIC = Path(__file__).parent / "static"
app = FastAPI(title="Price Tracker", docs_url="/api/docs", redoc_url=None)
app.mount("/static", StaticFiles(directory=STATIC), name="static")
bot_ref = {"bot": None}
log = logging.getLogger("web")


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    if config.WEB_PASSWORD:
        ok = False
        h = request.headers.get("authorization", "")
        if h.lower().startswith("basic "):
            try:
                _, _, pw = base64.b64decode(h[6:]).decode().partition(":")
                ok = secrets.compare_digest(pw, config.WEB_PASSWORD)
            except Exception:
                ok = False
        if not ok:
            return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="Price Tracker"'})
    return await call_next(request)


@app.exception_handler(service.UserError)
async def user_error(_, exc: service.UserError):
    return JSONResponse({"detail": str(exc)}, status_code=400)


def _tracker_or_404(tid: int) -> dict:
    t = db.get_tracker(tid)
    if not t:
        raise HTTPException(404, "Tracker not found")
    return t


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/api/img", include_in_schema=False)
async def image(u: str):
    res = await asyncio.to_thread(images.get, u)
    if not res:
        raise HTTPException(404)
    data, ctype = res
    return Response(data, media_type=ctype, headers={"Cache-Control": "public, max-age=604800"})


# ---------- status / settings ----------
@app.get("/api/status")
async def status():
    bot = bot_ref["bot"]
    trackers = db.list_trackers()
    return {
        "bot": {"configured": bool(config.DISCORD_TOKEN), "ready": bool(bot and bot.is_ready()),
                "user": str(bot.user) if bot and bot.user else None,
                "user_id": str(bot.user.id) if bot and bot.user else None,
                "guilds": [{"id": str(g.id), "name": g.name} for g in bot.guilds] if bot and bot.is_ready() else []},
        "trackers": len(trackers),
        "enabled": sum(1 for t in trackers if t["enabled"]),
        "failing": sum(1 for t in trackers if t["fail_count"]),
        "time": time.time(),
    }


@app.get("/api/channels")
async def channels():
    bot = bot_ref["bot"]
    if not (bot and bot.is_ready()):
        return []
    out = []
    for g in bot.guilds:
        for c in g.text_channels:
            if c.permissions_for(g.me).send_messages:
                out.append({"id": str(c.id), "name": f"{g.name} / #{c.name}"})
    return out


@app.get("/api/settings")
async def get_settings():
    return db.get_settings()


@app.put("/api/settings")
async def put_settings(values: dict):
    allowed = set(config.DEFAULT_SETTINGS)
    db.set_settings({k: v for k, v in values.items() if k in allowed})
    return db.get_settings()


@app.post("/api/settings/test")
async def test_notification():
    ok = await notifier.send([Notice("test", "🔔 Test notification",
                                     description="If you can see this, notifications are working.")])
    if not ok:
        raise HTTPException(400, "Not delivered. Set a default channel (bot must be online) or a webhook URL.")
    return {"ok": True}


# ---------- trackers ----------
class PreviewIn(BaseModel):
    url: str


class AlertIn(BaseModel):
    type: str = "below"
    value: Optional[float] = None
    series: str = "lowest"
    item_id: Optional[int] = None
    note: str = ""
    enabled: bool = True


class TrackerIn(BaseModel):
    url: str
    name: str = ""
    interval_min: Optional[int] = None
    channel_id: Optional[str] = None
    options: dict = {}
    alerts: list[AlertIn] = []


class TrackerPatch(BaseModel):
    name: Optional[str] = None
    enabled: Optional[bool] = None
    interval_min: Optional[int] = None
    channel_id: Optional[str] = None
    options: Optional[dict] = None
    url: Optional[str] = None


@app.post("/api/preview")
async def preview(body: PreviewIn):
    try:
        return await service.preview(body.url)
    except service.UserError:
        raise
    except Exception as e:
        log.warning("preview of %s failed: %s", body.url, e)
        raise HTTPException(502, f"Couldn't load that page: {e}")


@app.get("/api/trackers")
async def list_trackers():
    return [service.tracker_summary(t) for t in db.list_trackers()]


@app.post("/api/trackers")
async def create_tracker(body: TrackerIn):
    t = await service.create_tracker(body.url, body.name, body.interval_min, body.options,
                                     body.channel_id, [a.model_dump() for a in body.alerts])
    return service.tracker_summary(t)


@app.get("/api/trackers/{tid}")
async def get_tracker(tid: int):
    t = _tracker_or_404(tid)
    d = service.tracker_summary(t)
    d["options_effective"] = monitor.tracker_options(t)
    d["items"] = db.tracker_items(tid)
    for it in d["items"]:
        it["lowest"] = monitor.lowest(it["prices"])
    d["alerts"] = [{**a, "label": service.describe_alert(a, t["site"], t["kind"])} for a in db.tracker_alerts(tid)]
    d["events"] = db.recent_events(30, tid)
    return d


@app.patch("/api/trackers/{tid}")
async def patch_tracker(tid: int, body: TrackerPatch):
    t = _tracker_or_404(tid)
    fields = body.model_dump(exclude_unset=True)
    if "enabled" in fields:
        fields["enabled"] = int(fields["enabled"])
    if "options" in fields:
        fields["options"] = {**t["options"], **fields["options"]}
    if "url" in fields:
        site, kind, norm = await service.detect(fields["url"])
        if site != t["site"] or kind != t["kind"]:
            raise HTTPException(400, f"New URL must also be a {t['site']} {t['kind']} page")
        fields["url"] = norm
    for k in ("interval_min", "channel_id"):
        if k in fields and not fields[k]:
            fields[k] = None
    db.update("trackers", tid, fields)
    return service.tracker_summary(db.get_tracker(tid))


@app.delete("/api/trackers/{tid}")
async def delete_tracker(tid: int):
    _tracker_or_404(tid)
    db.ex("DELETE FROM trackers WHERE id = ?", (tid,))
    return {"ok": True}


@app.post("/api/trackers/{tid}/check")
async def check_tracker(tid: int):
    _tracker_or_404(tid)
    return await monitor.check_now(tid)


@app.post("/api/trackers/{tid}/reset-baseline")
async def reset_baseline(tid: int):
    """Forget all items so the next check re-learns the search without notifying."""
    _tracker_or_404(tid)
    db.ex("DELETE FROM items WHERE tracker_id = ?", (tid,))
    db.update("trackers", tid, {"baseline_done": 0})
    return {"ok": True}


# ---------- items ----------
@app.get("/api/items/{iid}")
async def get_item(iid: int, days: float = 0):
    it = db.get_item(iid)
    if not it:
        raise HTTPException(404, "Item not found")
    t = db.get_tracker(it["tracker_id"])
    since = time.time() - days * 86400 if days else 0
    return {**it, "lows": db.lows(iid), "history": db.history(iid, since),
            "series": sites.series_labels(t["site"], t["kind"]), "site": t["site"], "tracker_id": t["id"]}


@app.patch("/api/items/{iid}")
async def patch_item(iid: int, body: dict):
    if not db.get_item(iid):
        raise HTTPException(404, "Item not found")
    if "ignored" in body:
        db.update("items", iid, {"ignored": int(bool(body["ignored"]))})
    return db.get_item(iid)


@app.post("/api/items/{iid}/track")
async def track_item(iid: int):
    """Promote an item from a search tracker to its own product tracker."""
    it = db.get_item(iid)
    if not it:
        raise HTTPException(404, "Item not found")
    t = await service.create_tracker(it["url"], it["name"])
    return service.tracker_summary(t)


# ---------- alerts ----------
@app.post("/api/trackers/{tid}/alerts")
async def add_alert(tid: int, body: AlertIn):
    _tracker_or_404(tid)
    return service.add_alert(tid, **body.model_dump())


@app.patch("/api/alerts/{aid}")
async def patch_alert(aid: int, body: dict):
    a = db.q1("SELECT * FROM alerts WHERE id = ?", (aid,))
    if not a:
        raise HTTPException(404, "Alert not found")
    fields = {k: body[k] for k in ("enabled", "value", "note", "series", "type") if k in body}
    if "enabled" in fields:
        fields["enabled"] = int(bool(fields["enabled"]))
    db.update("alerts", aid, fields)
    if {"value", "series", "type"} & fields.keys():
        db.ex("DELETE FROM alert_state WHERE alert_id = ?", (aid,))  # re-arm after edits
    return db.q1("SELECT * FROM alerts WHERE id = ?", (aid,))


@app.delete("/api/alerts/{aid}")
async def delete_alert(aid: int):
    db.ex("DELETE FROM alerts WHERE id = ?", (aid,))
    return {"ok": True}


# ---------- events ----------
@app.get("/api/events")
async def events(limit: int = 200):
    return db.recent_events(min(limit, 1000))
