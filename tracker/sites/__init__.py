"""Site scrapers. Each site module exposes:
    detect(url) -> (kind, normalized_url) | None
    scrape(tracker) -> ScrapeResult
    SERIES: {series_key: label} for the prices it reports
"""
import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse

from ..fetch import BlockedError, FetchError  # noqa: F401  (re-exported)


class ScrapeError(Exception):
    pass


@dataclass
class ScrapedItem:
    key: str
    name: str
    url: str
    image: str = ""
    prices: dict = field(default_factory=dict)   # series -> float | None
    extra: dict = field(default_factory=dict)    # stock, offers, sku...


@dataclass
class ScrapeResult:
    items: list
    title: str = ""
    final_url: str = ""
    total_count: Optional[int] = None
    pages: int = 1


PRICE_RE = re.compile(r"\$\s*([\d,]+(?:\.\d{1,2})?)")


def parse_price(text) -> Optional[float]:
    if text is None:
        return None
    text = str(text)
    m = PRICE_RE.search(text) or re.search(r"([\d,]+\.\d{2})", text)
    if not m:
        return None
    try:
        return round(float(m.group(1).replace(",", "")), 2)
    except ValueError:
        return None


def site_for(url: str) -> Optional[str]:
    host = (urlparse(url).hostname or "").lower()
    if host.endswith("microcenter.com"):
        return "microcenter"
    if "amazon." in host or host in ("amzn.to", "a.co", "amzn.com"):
        return "amazon"
    return None


def module(site: str):
    from . import amazon, microcenter
    return {"amazon": amazon, "microcenter": microcenter}[site]


def series_labels(site: str, kind: Optional[str] = None) -> dict:
    mod = module(site)
    labels = dict(mod.series_for(kind) if kind and hasattr(mod, "series_for") else mod.SERIES)
    labels["lowest"] = "Lowest of any"
    return labels
