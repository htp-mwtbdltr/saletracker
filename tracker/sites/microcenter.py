"""Micro Center: search/category result pages and individual product pages."""
import html
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from .. import db
from ..fetch import fetcher
from . import ScrapedItem, ScrapeError, ScrapeResult, parse_price

BASE = "https://www.microcenter.com"
SERIES = {
    "price": "New",
    "open_box": "Open Box",
    "clearance": "Clearance",
}
PRODUCT_RE = re.compile(r"/product/(\d+)")
NON_PRODUCT_RE = re.compile(r"(service|installation|consultation|gift-?card|repair|protection-plan)", re.I)


def detect(url: str):
    p = urlparse(url)
    m = PRODUCT_RE.search(p.path)
    if m:
        return "product", f"{BASE}{p.path}"
    if "/search/" in p.path or "/category/" in p.path or "/site/" in p.path:
        return "search", url
    return None


def _cookies(tracker: dict) -> dict:
    opts = tracker.get("options") or {}
    store = str(opts.get("store_id") or db.get_setting("mc_store_id", "141"))
    in_stock_only = opts.get("in_stock_only", True)
    return {
        "storeSelected": store,
        "myStore": "true" if in_stock_only else "false",
        "rpp": "96",
        "SortBy": "pricelow",
    }


def _clean(text: str) -> str:
    # data-name attributes are double-escaped (&amp;quot;)
    return re.sub(r"\s+", " ", html.unescape(html.unescape(text or ""))).strip()


def _stock(text: str):
    """'16 IN STOCK' -> (True, 16); 'SOLD OUT' -> (False, 0)."""
    t = (text or "").upper()
    m = re.search(r"(\d+)\+?\s*(NEW\s+)?IN STOCK", t)
    if m:
        return True, int(m.group(1))
    if "IN STOCK" in t:
        return True, None
    if "SOLD OUT" in t or "OUT OF STOCK" in t or "UNAVAILABLE" in t:
        return False, 0
    return None, None


# ---------------- search ----------------
def _parse_search(soup: BeautifulSoup) -> list[ScrapedItem]:
    items = []
    for li in soup.select("li.product_wrapper"):
        a = li.select_one("a.productClickItemV2[data-id]") or li.select_one("a[href*='/product/']")
        if not a:
            continue
        href = a.get("href", "")
        if NON_PRODUCT_RE.search(href):
            continue
        pid = a.get("data-id") or (PRODUCT_RE.search(href) or [None, None])[1]
        if not pid:
            continue
        h2 = li.select_one(".h2 a")
        name = _clean(h2.get_text(" ") if h2 else a.get("data-name", ""))
        brand = a.get("data-brand", "")
        if brand and not name.lower().startswith(brand.lower()):
            name = f"{brand} {name}"
        prices = {"price": parse_price(a.get("data-price"))}
        ob = li.select_one(".price-label.compareTo strong")
        prices["open_box"] = parse_price(ob.get_text()) if ob else None
        if prices["open_box"] is None:
            m = re.search(r"Open Box From\s*\$\s*([\d,]+\.\d{2})", li.get_text(" "), re.I)
            prices["open_box"] = float(m.group(1).replace(",", "")) if m else None
        clr = li.select_one(".clearance")
        prices["clearance"] = parse_price(clr.get_text(" ")) if clr else None
        if prices["price"] == 0:
            prices["price"] = None
        img = li.select_one("img.SearchResultProductImage")
        sku = li.select_one("p.sku")
        stock_el = li.select_one(".inventoryCnt") or li.select_one(".stock")
        in_stock, count = _stock(stock_el.get_text(" ") if stock_el else "")
        items.append(ScrapedItem(
            key=str(pid),
            name=name or f"Product {pid}",
            url=urljoin(BASE, href),
            image=img.get("src", "") if img else "",
            prices=prices,
            extra={"sku": sku.get_text(strip=True).replace("SKU:", "").strip() if sku else "",
                   "in_stock": in_stock, "stock": count, "brand": brand},
        ))
    return items


def _next_page(soup: BeautifulSoup):
    for a in soup.select("ul.pages a"):
        if a.get_text(strip=True) in (">", "›", "Next"):
            return urljoin(BASE, a.get("href"))
    return None


def _scrape_search(tracker: dict) -> ScrapeResult:
    opts = tracker.get("options") or {}
    max_pages = int(opts.get("max_pages") or 5)
    cookies = _cookies(tracker)
    url = tracker["url"]
    seen, items, pages, total, final_url, title = set(), [], 0, None, url, ""
    check = lambda t: "product_wrapper" in t or "productSearchCount" in t  # noqa: E731
    while url and pages < max_pages:
        page = fetcher.get("microcenter", url, cookies=cookies, check=check)
        pages += 1
        if pages == 1:
            final_url = page.url
        soup = BeautifulSoup(page.text, "lxml")
        if pages == 1:
            t = soup.select_one("title")
            title = _clean(t.get_text()) if t else ""
            m = re.search(r"([\d,]+)\s+items?\s+in-stock", page.text)
            total = int(m.group(1).replace(",", "")) if m else None
            cnt = soup.select_one(".productSearchCount")
            if cnt and cnt.get_text(strip=True) == "0" and total is None:
                total = 0
        for it in _parse_search(soup):
            if it.key not in seen:
                seen.add(it.key)
                items.append(it)
        url = _next_page(soup)
    if not items and total != 0 and "productSearchCount" not in page.text:
        raise ScrapeError("No products found; the page layout may have changed")
    if total == 0:
        items = []
    return ScrapeResult(items=items, title=title, final_url=final_url, total_count=total, pages=pages)


# ---------------- product ----------------
def _dl(text: str, key: str):
    m = re.search(r"'%s'\s*:\s*'([^']*)'" % re.escape(key), text)
    return m.group(1) if m else None


def _scrape_product(tracker: dict) -> ScrapeResult:
    url = tracker["url"]
    page = fetcher.get("microcenter", url, cookies=_cookies(tracker),
                       check=lambda t: "productPrice" in t or "big-price" in t)
    soup = BeautifulSoup(page.text, "lxml")
    text = page.text
    pid = _dl(text, "productID") or (PRODUCT_RE.search(url) or [None, ""])[1]

    og = soup.select_one('meta[property="og:title"]')
    name = _clean(og.get("content", "")) if og else ""
    name = re.sub(r"\s*-\s*Micro Center$", "", name)
    if not name:
        h1 = soup.select_one("h1")
        name = _clean(h1.get_text(" ")) if h1 else f"Product {pid}"

    price = parse_price(_dl(text, "productPrice"))
    if not price:
        bp = soup.select_one("#pricing .big-price, p.big-price")
        price = parse_price(bp.get_text()) if bp else None

    open_box = None
    ob = soup.select_one("#opCostNew")
    if ob:
        open_box = parse_price(ob.get_text())
    modal = soup.select_one("#openBoxModal")
    if modal:
        vals = [parse_price(m.group(0)) for m in re.finditer(r"\$\s*[\d,]+\.\d{2}", modal.get_text(" "))]
        vals = [v for v in vals if v]
        if vals:
            open_box = min(vals + ([open_box] if open_box else []))

    clearance = None
    clr = soup.select_one("#pricing .clearance, .clearance-price")
    if clr:
        clearance = parse_price(clr.get_text(" "))

    stock_el = soup.select_one(".inventory .inventoryCnt") or soup.select_one(".inventoryCnt")
    in_stock, count = _stock(stock_el.get_text(" ") if stock_el else "")
    if in_stock is None:
        flag = _dl(text, "inStock")
        in_stock = flag == "True" if flag else None
    ob_count = None
    obb = soup.select_one(".openBoxModalButton")
    if obb:
        m = re.search(r"(\d+)\s+Open Box", obb.get_text(" "))
        ob_count = int(m.group(1)) if m else None

    img = soup.select_one('meta[property="og:image"]')
    store = soup.select_one(".inventory .storeName")
    item = ScrapedItem(
        key=str(pid),
        name=name,
        url=url,
        image=img.get("content", "") if img else "",
        prices={"price": price, "open_box": open_box, "clearance": clearance},
        extra={"sku": _dl(text, "SKU") or "", "brand": _dl(text, "brand") or "",
               "in_stock": in_stock, "stock": count, "open_box_count": ob_count,
               "store": _clean(store.get_text()).removeprefix("at ").strip() if store else "",
               "availability": _dl(text, "AvailabilityCode") or ""},
    )
    if price is None and open_box is None:
        raise ScrapeError("Could not find a price on the product page")
    return ScrapeResult(items=[item], title=name, final_url=page.url)


def scrape(tracker: dict) -> ScrapeResult:
    if tracker["kind"] == "product":
        return _scrape_product(tracker)
    return _scrape_search(tracker)
