"""Entry point: runs the web GUI, the scheduler and the Discord bot in one event loop."""
import asyncio
import logging
import time

import uvicorn

from . import config, db, monitor, web
from .notify import notifier

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
logging.getLogger("discord.gateway").setLevel(logging.WARNING)
log = logging.getLogger("main")

# The search the original bot watched; added once on first start.
SEED_TRACKER = {
    "name": "Micro Center: Open Box 1440p/4K monitors",
    "url": "https://www.microcenter.com/search/search_results.aspx?Ntk=all&sortby=match&prt=clearance&N=4294966896+4294821085+4294818903",
}


def seed():
    if db.get_setting("seeded") == "1":
        return
    if not db.list_trackers():
        db.insert("trackers", {**SEED_TRACKER, "site": "microcenter", "kind": "search", "enabled": 1,
                               "options": {}, "created_at": time.time()})
        log.info("added the original Micro Center search as tracker #1")
    db.set_settings({"seeded": "1"})


async def run_bot(bot):
    """Keep the bot connected without taking the web GUI down if Discord fails."""
    import discord
    while True:
        try:
            await bot.start(config.DISCORD_TOKEN)
            return
        except discord.LoginFailure:
            log.error("Discord login failed: DISCORD_TOKEN is invalid. Running without the bot.")
            return
        except Exception:
            log.exception("bot crashed; reconnecting in 60s")
            await asyncio.sleep(60)


async def main():
    db.init()
    seed()
    server = uvicorn.Server(uvicorn.Config(web.app, host=config.WEB_HOST, port=config.WEB_PORT,
                                           log_level="warning", access_log=False))
    tasks = [server.serve(), monitor.run_forever()]
    if config.DISCORD_TOKEN:
        from .bot import TrackerBot
        bot = TrackerBot()
        notifier.attach_bot(bot)
        web.bot_ref["bot"] = bot
        tasks.append(run_bot(bot))
    else:
        log.warning("DISCORD_TOKEN not set: running without the bot (webhook notifications only)")
    log.info("web GUI on http://%s:%s", config.WEB_HOST, config.WEB_PORT)
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())
