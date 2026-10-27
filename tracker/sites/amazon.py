"""Amazon: product pages (Amazon / new / used-by-condition offers) and search pages."""
import re
import time
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup

from ..fetch import FetchError, fetcher
from . import ScrapedItem, ScrapeError, ScrapeResult, parse_price

BASE = "https://www.amazon.com"
SERIES = {
    "amazon": "Amazon (new)",
    "new": "Lowest new",
    "used": "Lowest used",
    "used_like_new": "Used - Like New",
    "used_very_good": "Used - Very Good",
    "used_good": "Used - Good",
    "used_acceptable": "Used - Acceptable",
    "renewed": "Renewed",
    "price": "Price",   # search results
}


def series_for(kind: str) -> dict:
    if kind == "search":
        return {"price": SERIES["price"]}
    return {k: v for k, v in SERIES.items() if k != "price"}


USED_SERIES = ("used_like_new", "used_very_good", "used_good", "used_acceptable")
ASIN_RE = re.compile(r"/(?:dp|gp/product|gp/aw/d|exec/obidos/ASIN|product|d)/([A-Z0-9]{10})(?:[/?]|$)", re.I)
SHORT_HOSTS = ("amzn.to", "a.co", "amzn.com", "www.amzn.com")


def product_url(asin: str) -> str:
    return f"{BASE}/dp/{asin.upper()}?psc=1"


def detect(url: str):
    p = urlparse(url)
    if (p.hostname or "").lower() in SHORT_HOSTS:
        # Short link: follow the redirect to find the real page.
        page = fetcher.get("amazon", url)
        url, p = page.url, urlparse(page.url)
    m = ASIN_RE.search(p.path + ("?" if p.query else ""))
    if m:
        return "product", product_url(m.group(1))
    if p.path.rstrip("/") == "/s" or "k" in parse_qs(p.query):
        return "search", url
    return None


def condition_series(cond: str):
    c = (cond or "").lower()
    if "renewed" in c or "refurbished" in c:
        return "renewed"
    if c.startswith("new"):
        return "new"
    if "like new" in c:
        return "used_like_new"
    if "very good" in c:
        return "used_very_good"
    if "acceptable" in c:
        return "used_acceptable"
    if "good" in c:
        return "used_good"
    if "used" in c:
        return "used_good"
    return None


def _offer_price(el):
    lab = el.select_one(".apex-pricetopay-accessibility-label")
    p = parse_price(lab.get_text()) if lab else None
    if p is None:
        off = el.select_one(".a-price .a-offscreen")
        p = parse_price(off.get_text()) if off else None
    if p is None:
        w, f = el.select_one(".a-price-whole"), el.select_one(".a-price-fraction")
        if w:
            p = parse_price("$" + w.get_text(strip=True).rstrip(".") + "." + (f.get_text(strip=True) if f else "00"))
    return p


def _shipping(text: str):
    t = re.sub(r"\s+", " ", text)
    m = re.search(r"\$\s*([\d,]+\.\d{2})\s*(?:shipping|delivery)", t, re.I)
    if m:
        return float(m.group(1).replace(",", ""))
    return 0.0


def _parse_offers(html: str) -> tuple[list[dict], int | None]:
    soup = BeautifulSoup(html, "lxml")
    offers = []
    for o in soup.select("#aod-pinned-offer, #aod-offer"):
        price = _offer_price(o.select_one("#aod-offer-price") or o)
        head = o.select_one("#aod-offer-heading")
        cond = re.sub(r"\s+", " ", head.get_text(" ", strip=True)) if head else ""
        if price is None or not cond:
            continue
        sold = o.select_one("#aod-offer-soldBy a") or o.select_one("#aod-offer-soldBy .a-col-right span")
        ships = o.select_one("#aod-offer-shipsFrom .a-col-right span")
        seller = sold.get_text(strip=True) if sold else ""
        offers.append({
            "price": price,
            "shipping": _shipping(o.get_text(" ")),
            "condition": cond,
            "series": condition_series(cond),
            "seller": seller,
            "ships_from": ships.get_text(strip=True) if ships else "",
            "amazon": seller.lower().startswith("amazon.com"),
        })
    total = soup.select_one("#aod-total-offer-count")
    try:
        total = int(total.get("value")) if total else None
    except (TypeError, ValueError):
        total = None
    return offers, total


def _fetch_offers(asin: str, referer: str, max_pages: int = 4) -> list[dict]:
    offers, seen = [], set()
    total = None
    for pageno in range(1, max_pages + 1):
        url = (f"{BASE}/gp/product/ajax/aodAjaxMain/?asin={asin}&pc=dp&experienceId=aodAjaxMain"
               + (f"&pageno={pageno}" if pageno > 1 else ""))
        page = fetcher.get_with_session("amazon", url, headers={"Referer": referer})
        if page.status != 200 or "aod-" not in page.text:
            if pageno == 1:
                raise FetchError(f"offer listing unavailable (HTTP {page.status})")
            break
        batch, t = _parse_offers(page.text)
        total = total or t
        new = 0
        for o in batch:
            sig = (o["price"], o["condition"], o["seller"])
            if sig not in seen:
                seen.add(sig)
                offers.append(o)
                new += 1
        if not new or (total and len(offers) >= total):
            break
        time.sleep(1.0)
    return offers


def _page_fallback(soup: BeautifulSoup) -> list[dict]:
    """Offers visible on the product page itself, used if the offer listing fails."""
    offers = []
    core = soup.select_one("#corePrice_feature_div .a-offscreen, #corePriceDisplay_desktop_feature_div .priceToPay .a-offscreen")
    new_price = parse_price(core.get_text()) if core else None
    acc = soup.select_one("#newAccordionRow, #buyBoxAccordion [id*=newAccordionRow]")
    if acc and not new_price:
        new_price = _offer_price(acc)
    merchant = soup.select_one("#merchant-info, #sellerProfileTriggerId, [offer-display-feature-name='desktop-merchant-info']")
    seller = re.sub(r"\s+", " ", merchant.get_text(" ", strip=True)) if merchant else ""
    if new_price:
        offers.append({"price": new_price, "shipping": 0.0, "condition": "New", "series": "new",
                       "seller": seller[:60], "ships_from": "", "amazon": "amazon.com" in seller.lower()})
    used = soup.select_one("#usedAccordionRow")
    if used:
        p = _offer_price(used)
        m = re.search(r"Used\s*-\s*[A-Za-z ]+?(?=\s*\$|\s{2}|$)", used.get_text(" ", strip=True))
        cond = m.group(0).strip() if m else "Used"
        if p:
            offers.append({"price": p, "shipping": 0.0, "condition": cond, "series": condition_series(cond),
                           "seller": "", "ships_from": "", "amazon": False})
    return offers


def _series_from_offers(offers: list[dict], include_shipping: bool) -> dict:
    def cost(o):
        return round(o["price"] + (o["shipping"] if include_shipping else 0), 2)

    prices = {k: None for k in ("amazon", "new", "used", *USED_SERIES, "renewed")}
    for o in offers:
        c = cost(o)
        keys = []
        s = o["series"]
        if s == "new":
            keys.append("new")
            if o["amazon"]:
                keys.append("amazon")
        elif s in USED_SERIES:
            keys += [s, "used"]
        elif s == "renewed":
            keys.append("renewed")
        for k in keys:
            if prices[k] is None or c < prices[k]:
                prices[k] = c
    return prices


def _scrape_product(tracker: dict) -> ScrapeResult:
    opts = tracker.get("options") or {}
    url = tracker["url"]
    m = ASIN_RE.search(urlparse(url).path + "?")
    if not m:
        raise ScrapeError("No ASIN in URL")
    asin = m.group(1).upper()
    page = fetcher.get("amazon", url, headers={"Accept-Language": "en-US,en;q=0.9"},
                       check=lambda t: "productTitle" in t or "dp-container" in t)
    soup = BeautifulSoup(page.text, "lxml")
    title_el = soup.select_one("#productTitle")
    name = re.sub(r"\s+", " ", title_el.get_text(" ", strip=True)) if title_el else ""
    img = soup.select_one("#landingImage, #imgBlkFront, #main-image")
    image = (img.get("data-old-hires") or img.get("src") or "") if img else ""
    avail = soup.select_one("#availability")
    availability = re.sub(r"\s+", " ", avail.get_text(" ", strip=True)) if avail else ""

    source = "offers"
    try:
        offers = _fetch_offers(asin, page.url)
    except Exception:
        offers = []
    if not offers:
        offers = _page_fallback(soup)
        source = "page"
    offers.sort(key=lambda o: o["price"] + o["shipping"])
    prices = _series_from_offers(offers, bool(opts.get("include_shipping")))

    avail_l = availability.lower()
    unavailable = any(p in avail_l for p in ("currently unavailable", "out of stock", "no longer available"))
    if not offers and not unavailable:
        # Amazon sometimes serves a stripped page (no title, buy box or offers) to
        # suspected bots. Treat it as a failed check instead of "out of stock".
        raise ScrapeError("Amazon returned an incomplete product page (no offers and no "
                          "out-of-stock message)" + ("" if name else "; no title either"))
    item = ScrapedItem(
        key=asin, name=name, url=product_url(asin), image=image, prices=prices,
        extra={"asin": asin, "availability": availability, "in_stock": bool(offers) and not unavailable,
               "offer_count": len(offers), "offers": offers[:40], "offer_source": source},
    )
    return ScrapeResult(items=[item], title=name or asin, final_url=page.url)


def _scrape_search(tracker: dict) -> ScrapeResult:
    opts = tracker.get("options") or {}
    max_pages = int(opts.get("max_pages") or 2)
    skip_sponsored = opts.get("skip_sponsored", True)
    url, pages, items, seen, title = tracker["url"], 0, [], set(), ""
    check = lambda t: 's-search-result' in t or "No results for" in t  # noqa: E731
    while url and pages < max_pages:
        page = fetcher.get("amazon", url, check=check)
        pages += 1
        soup = BeautifulSoup(page.text, "lxml")
        if pages == 1:
            t = soup.select_one("title")
            title = t.get_text(strip=True) if t else ""
        for d in soup.select('div[data-component-type="s-search-result"][data-asin]'):
            asin = d.get("data-asin")
            if not asin or asin in seen:
                continue
            if skip_sponsored and (d.select_one(".puis-sponsored-label-text, .s-sponsored-label-text")
                                   or "AdHolder" in " ".join(d.get("class", []))):
                continue
            seen.add(asin)
            h2 = d.select_one("h2")
            name = re.sub(r"\s+", " ", h2.get_text(" ", strip=True)) if h2 else asin
            p = d.select_one(".a-price:not(.a-text-price) .a-offscreen")
            img = d.select_one("img.s-image")
            rating = d.select_one(".a-icon-alt")
            items.append(ScrapedItem(
                key=asin, name=name, url=product_url(asin),
                image=img.get("src", "") if img else "",
                prices={"price": parse_price(p.get_text()) if p else None},
                extra={"asin": asin, "rating": rating.get_text(strip=True) if rating else ""},
            ))
        nxt = soup.select_one("a.s-pagination-next")
        url = urljoin(BASE, nxt["href"]) if nxt and nxt.get("href") else None
        if url:
            time.sleep(1.5)
    return ScrapeResult(items=items, title=title, final_url=tracker["url"], pages=pages)


def scrape(tracker: dict) -> ScrapeResult:
    if tracker["kind"] == "product":
        return _scrape_product(tracker)
    return _scrape_search(tracker)
