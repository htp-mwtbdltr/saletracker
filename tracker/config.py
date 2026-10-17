"""Static configuration from environment variables.

Runtime-editable settings (channel, webhook, intervals...) live in the database
and are edited from the web GUI; see db.get_settings().
"""
import os
from pathlib import Path

DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
DB_PATH = DATA_DIR / "tracker.db"

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
WEB_HOST = os.getenv("WEB_HOST", "0.0.0.0")
WEB_PORT = int(os.getenv("WEB_PORT", "8000"))
# Optional: protect the web GUI with HTTP basic auth (username is ignored).
WEB_PASSWORD = os.getenv("WEB_PASSWORD", "").strip()

# Defaults for settings that can be changed in the GUI.
DEFAULT_SETTINGS = {
    "default_channel_id": os.getenv("DISCORD_CHANNEL_ID", ""),
    "webhook_url": os.getenv("DISCORD_WEBHOOK_URL", ""),
    "mention": "",
    "default_interval_min": "15",
    "mc_store_id": os.getenv("MC_STORE_ID", "141"),
    "flaresolverr_url": os.getenv("FLARESOLVERR_URL", ""),
    "notify_failures": "1",
    # Public URL of the web GUI, used for links in Discord messages.
    "web_url": os.getenv("WEB_URL", ""),
}

# How often to write an unchanged price into history (keeps charts continuous).
HISTORY_HEARTBEAT_SEC = 6 * 3600
# Consecutive failures before a "tracker is failing" notification is sent.
FAIL_NOTIFY_THRESHOLD = 3
