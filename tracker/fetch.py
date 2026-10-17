"""HTTP fetching with browser TLS impersonation (curl_cffi), automatic profile
rotation when blocked, and an optional FlareSolverr fallback for Cloudflare."""
import logging
import random
import re
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

from bs4 import BeautifulSoup
from curl_cffi import requests as cffi

from . import db

log = logging.getLogger("fetch")

# Profiles that currently get through each site, best first. The last profile
# that worked is remembered and tried first next time.
PROFILES = {
    "microcenter": ["safari184", "safari180", "safari17_0", "safari155"],
    "amazon": ["chrome", "safari184", "chrome131", "safari17_0"],
}


class BlockedError(Exception):
    pass


class FetchError(Exception):
    pass


@dataclass
class Page:
    url: str
    status: int
    text: str


def is_blocked(site: str, status: int, text: str) -> bool:
    head = text[:20000]
    if site == "microcenter":
        return status in (403, 429, 503) or "Just a moment..." in head or "cf-chl" in head
    if site == "amazon":
        return (status in (429, 503) or "validateCaptcha" in text or "bm-verify" in head
                or "Type the characters you see in this image" in text
                or "api-services-support@amazon.com" in head)
    return status in (403, 429, 503)


class Fetcher:
    def __init__(self):
        self._sessions: dict[tuple[str, str], cffi.Session] = {}
        self._preferred: dict[str, str] = {}
        self._lock = threading.Lock()

    def _session(self, site: str, profile: str) -> cffi.Session:
        with self._lock:
            key = (site, profile)
            if key in self._sessions:
                return self._sessions[key]
            s = cffi.Session(impersonate=profile, timeout=30)
            self._sessions[key] = s
        if site == "amazon":
            self._amazon_warmup(s)
        return s

    def _amazon_warmup(self, s: cffi.Session):
        """Cookie-less sessions get 503s/challenges on search pages; pick up session
        cookies from a lightweight page first."""
        url = "https://www.amazon.com/gp/cart/view.html"
        try:
            r = s.get(url, headers={"Accept-Language": "en-US,en;q=0.9"})
            self._amazon_solve(s, r, url, None, None)
            time.sleep(random.uniform(1, 2))
        except Exception as e:
            log.warning("amazon warmup failed: %s", e)

    def reset(self, site: str):
        with self._lock:
            for key in [k for k in self._sessions if k[0] == site]:
                self._sessions.pop(key).close()

    def get(self, site: str, url: str, cookies: Optional[dict] = None,
            headers: Optional[dict] = None, check: Optional[Callable[[str], bool]] = None) -> Page:
        """Fetch a page, rotating impersonation profiles if blocked.
        `check` can reject a 200 response that doesn't look like real content."""
        profiles = list(PROFILES.get(site, ["chrome"]))
        pref = self._preferred.get(site)
        if pref in profiles:
            profiles.remove(pref)
            profiles.insert(0, pref)

        errors = []
        for i, profile in enumerate(profiles):
            if i:
                time.sleep(random.uniform(1.5, 4))
            try:
                s = self._session(site, profile)
                r = s.get(url, cookies=cookies, headers=headers or {}, allow_redirects=True)
            except Exception as e:  # network errors, timeouts
                errors.append(f"{profile}: {e}")
                continue
            if site == "amazon":
                r = self._amazon_solve(s, r, url, cookies, headers) or r
            if is_blocked(site, r.status_code, r.text):
                errors.append(f"{profile}: blocked (HTTP {r.status_code})")
                # Drop the session so a retry later starts with fresh cookies.
                with self._lock:
                    self._sessions.pop((site, profile), None)
                continue
            if r.status_code == 404:
                raise FetchError("Page not found (HTTP 404)")
            if r.status_code >= 400:
                errors.append(f"{profile}: HTTP {r.status_code}")
                continue
            if check and not check(r.text):
                errors.append(f"{profile}: unexpected page content")
                continue
            self._preferred[site] = profile
            return Page(str(r.url), r.status_code, r.text)

        fs = db.get_setting("flaresolverr_url").strip()
        if fs:
            try:
                page = self._flaresolverr(fs, url, cookies)
                if not is_blocked(site, page.status, page.text) and (not check or check(page.text)):
                    return page
                errors.append("flaresolverr: still blocked")
            except Exception as e:
                errors.append(f"flaresolverr: {e}")

        if all("blocked" in e for e in errors):
            raise BlockedError("Blocked by site anti-bot protection (" + "; ".join(errors) + ")")
        raise FetchError("; ".join(errors))

    def get_with_session(self, site: str, url: str, headers: Optional[dict] = None) -> Page:
        """Follow-up request on the site's preferred session (keeps cookies), no rotation."""
        profile = self._preferred.get(site) or PROFILES.get(site, ["chrome"])[0]
        s = self._session(site, profile)
        r = s.get(url, headers=headers or {}, allow_redirects=True)
        if site == "amazon":
            r = self._amazon_solve(s, r, url, None, headers) or r
        if is_blocked(site, r.status_code, r.text):
            raise BlockedError(f"Blocked (HTTP {r.status_code})")
        return Page(str(r.url), r.status_code, r.text)

    @staticmethod
    def _amazon_solve(s: cffi.Session, r, url: str, cookies, headers):
        """Get past Amazon's lightweight interstitials on the same session:
        - 'Click the button below to continue shopping' form (no image to solve)
        - Akamai 'bm-verify' page with a trivial proof-of-work sum
        Returns the re-fetched response, or None if nothing could be done."""
        solved = None
        for _ in range(3):
            text = r.text
            try:
                if "validateCaptcha" in text:
                    soup = BeautifulSoup(text, "lxml")
                    form = soup.select_one('form[action*="validateCaptcha"]')
                    if not form or soup.select_one('img[src*="captcha"], input#captchacharacters'):
                        return solved  # real image captcha; can't be solved automatically
                    params = {i["name"]: i.get("value", "") for i in form.select("input[type=hidden]") if i.get("name")}
                    time.sleep(random.uniform(1, 2.5))
                    r = s.get("https://www.amazon.com" + form.get("action", "/errors/validateCaptcha"),
                              params=params, headers={"Referer": url}, allow_redirects=True)
                    log.info("submitted Amazon continue-shopping interstitial")
                    if "bm-verify" in r.text or "validateCaptcha" in r.text:
                        continue
                elif "bm-verify" in text and "/_sec/verify" in text:
                    i = int(re.search(r"var i = (\d+);", text).group(1))
                    expr = re.search(r"var j = i \+ ([^;]+);", text).group(1)
                    pow_ = i + int("".join(re.findall(r'"(\d+)"', expr)))
                    token = re.search(r'"bm-verify":\s*"([^"]+)"', text).group(1)
                    time.sleep(random.uniform(1, 2))
                    s.post("https://www.amazon.com/_sec/verify?provider=interstitial",
                           json={"bm-verify": token, "pow": pow_},
                           headers={"Referer": url, "Origin": "https://www.amazon.com"})
                    log.info("answered Amazon bm-verify interstitial")
                else:
                    return solved
                time.sleep(random.uniform(1, 2))
                r = solved = s.get(url, cookies=cookies, headers=headers or {}, allow_redirects=True)
            except Exception as e:
                log.warning("amazon interstitial handling failed: %s", e)
                return solved
        return solved

    @staticmethod
    def _flaresolverr(endpoint: str, url: str, cookies: Optional[dict]) -> Page:
        endpoint = endpoint.rstrip("/")
        if not endpoint.endswith("/v1"):
            endpoint += "/v1"
        payload = {"cmd": "request.get", "url": url, "maxTimeout": 60000}
        if cookies:
            payload["cookies"] = [{"name": k, "value": str(v)} for k, v in cookies.items()]
        r = cffi.post(endpoint, json=payload, timeout=90)
        data = r.json()
        if data.get("status") != "ok":
            raise FetchError(data.get("message") or "FlareSolverr error")
        sol = data["solution"]
        return Page(sol.get("url", url), int(sol.get("status", 200)), sol.get("response", ""))


fetcher = Fetcher()
