from __future__ import annotations

import hashlib
import html
import json
import math
import os
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests
import yaml
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).parent
STATE_PATH = ROOT / "state.json"
DIAGNOSTICS = ROOT / "diagnostics"
UA = "flat-finder/1.0 (+https://github.com/merloty/flat_finder)"
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA, "Accept": "application/json"})


def norm(value: str) -> str:
    import unicodedata
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", value.lower()).strip()


def haversine(a: dict[str, float], b: dict[str, float]) -> float:
    lat1, lat2 = math.radians(a["lat"]), math.radians(b["lat"])
    dlat = lat2 - lat1
    dlon = math.radians(b["lon"] - a["lon"])
    x = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(x))


def get_json(url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    response = SESSION.get(url, params=params, timeout=30)
    response.raise_for_status()
    return response.json()


def geocode(location: str) -> dict[str, float] | None:
    """Resolve a Czech locality through the public OSM geocoder."""
    try:
        response = SESSION.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": f"{location}, Česko", "format": "jsonv2", "limit": 1, "countrycodes": "cz"},
            timeout=30,
        )
        response.raise_for_status()
        rows = response.json()
        if rows:
            return {"lat": float(rows[0]["lat"]), "lon": float(rows[0]["lon"])}
    except (requests.RequestException, ValueError, KeyError):
        pass
    return None


def dismiss_consent(page: Any) -> None:
    labels = (
        "Odmítnout vše", "Nesouhlasím", "Pokračovat bez souhlasu",
        "Souhlasím", "Přijmout vše", "Reject all", "Accept all",
    )
    page.wait_for_timeout(2500)
    for frame in page.frames:
        for label in labels:
            try:
                candidate = frame.get_by_text(label, exact=False)
                if candidate.count() and candidate.first.is_visible():
                    candidate.first.click(timeout=5000, force=True)
                    page.wait_for_timeout(2500)
                    return
            except PlaywrightTimeoutError:
                continue


def save_diagnostics(page: Any, search_id: str) -> None:
    DIAGNOSTICS.mkdir(exist_ok=True)
    try:
        page.screenshot(path=str(DIAGNOSTICS / f"{search_id}.png"), full_page=True)
        (DIAGNOSTICS / f"{search_id}.html").write_text(page.content(), encoding="utf-8")
        (DIAGNOSTICS / f"{search_id}.txt").write_text(
            f"URL: {page.url}\nTITLE: {page.title()}\n", encoding="utf-8"
        )
    except Exception as exc:
        print(f"Cannot save diagnostics: {exc}", file=sys.stderr)


def scrape_sreality(search: dict[str, Any]) -> list[dict[str, Any]]:
    """Scrape the public search UI; Sreality retired its former JSON endpoint."""
    found: list[dict[str, Any]] = []
    with sync_playwright() as runner:
        browser = runner.chromium.launch(headless=True)
        context = browser.new_context(locale="cs-CZ", user_agent=UA)
        page = context.new_page()
        links: list[str] = []
        # Walk the entire result set. Repeated URLs mark the last page because
        # Sreality may redirect an out-of-range page to the final valid one.
        for page_number in range(1, 201):
            parts = urlsplit(search["sreality_url"])
            query = dict(parse_qsl(parts.query))
            if page_number > 1:
                query["strana"] = str(page_number)
            page_url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))
            page.goto(page_url, wait_until="domcontentloaded", timeout=60000)
            if "cmp.seznam.cz" in page.url:
                dismiss_consent(page)
            try:
                page.wait_for_selector('a[href*="/detail/prodej/byt/"]', timeout=45000)
            except PlaywrightTimeoutError:
                save_diagnostics(page, f"{search['id']}-page-{page_number}")
                if page_number == 1:
                    # A valid search page with no cards means zero results,
                    # while the consent host means navigation really failed.
                    if "sreality.cz" in page.url and "cmp.seznam.cz" not in page.url:
                        return []
                    raise
                break
            page_links = page.locator('a[href*="/detail/prodej/byt/"]').evaluate_all(
                "els => [...new Set(els.map(e => e.href))]"
            )
            before = len(links)
            links.extend(url for url in page_links if url not in links)
            if len(links) == before:
                break
        else:
            raise RuntimeError("Sreality pagination did not terminate after 200 pages")
        for url in links:
            detail_page = context.new_page()
            try:
                detail_page.goto(url, wait_until="domcontentloaded", timeout=45000)
                if "cmp.seznam.cz" in detail_page.url:
                    dismiss_consent(detail_page)
                body = detail_page.locator("body").inner_text(timeout=20000)
                item = parse_sreality_page(url, body)
                if item and matches(item, search):
                    found.append(item)
            except PlaywrightTimeoutError as exc:
                print(f"detail timeout {url}: {exc}", file=sys.stderr)
            finally:
                detail_page.close()
        browser.close()
    return found


def parse_sreality_page(url: str, body: str) -> dict[str, Any] | None:
    estate_match = re.search(r"/(\d+)(?:[/?#]|$)", url)
    price_match = re.search(r"([\d\s.]+)\s*Kč", body)
    title_match = re.search(r"Prodej bytu[^\n]*", body, re.IGNORECASE)
    location_match = re.search(r"(?:Prodej bytu[^\n]*\n)([^\n]+)", body, re.IGNORECASE)
    if not estate_match or not price_match or not title_match or not location_match:
        return None
    location = location_match.group(1).strip()
    coords = geocode(location)
    if not coords:
        return None
    price = int(re.sub(r"\D", "", price_match.group(1)))
    estate_id = estate_match.group(1)
    return {
        "key": f"sreality:{estate_id}", "source": "Sreality",
        "title": title_match.group(0).strip(), "location": location,
        "price": price, "lat": coords["lat"], "lon": coords["lon"],
        "text": body, "url": url,
        # The upstream search URL itself is constrained by
        # vlastnictvi=osobni, so this is stronger than wording in free text.
        "ownership_verified": True,
    }


def iter_json(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from iter_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_json(child)


def parse_generic_page(page: Any, url: str, source_id: str, source_name: str) -> dict[str, Any] | None:
    body = page.locator("body").inner_text(timeout=20000)
    title = ""
    location = ""
    price = 0
    coords: dict[str, float] | None = None
    for raw in page.locator('script[type="application/ld+json"]').all_text_contents():
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            continue
        for node in iter_json(data):
            title = title or str(node.get("name") or "")
            offers = node.get("offers") if isinstance(node.get("offers"), dict) else {}
            raw_price = offers.get("price") or node.get("price")
            if raw_price and not price:
                digits = re.sub(r"\D", "", str(raw_price))
                price = int(digits) if digits else 0
            address = node.get("address")
            if isinstance(address, dict):
                location = location or ", ".join(str(address.get(k, "")) for k in
                    ("streetAddress", "addressLocality", "postalCode") if address.get(k))
            elif isinstance(address, str):
                location = location or address
            geo = node.get("geo")
            if isinstance(geo, dict) and geo.get("latitude") and geo.get("longitude"):
                coords = {"lat": float(geo["latitude"]), "lon": float(geo["longitude"])}
    if not title:
        headings = page.locator("h1").all_text_contents()
        title = headings[0].strip() if headings else "Prodej bytu"
    if not price:
        match = re.search(r"([\d\s.]{3,})\s*Kč", body)
        price = int(re.sub(r"\D", "", match.group(1))) if match else 0
    if not location:
        address_nodes = page.locator('address, [class*="address"], [class*="location"]').all_text_contents()
        location = next((x.strip() for x in address_nodes if 3 < len(x.strip()) < 180), "")
    if not location:
        return None
    coords = coords or geocode(location)
    if not coords:
        return None
    stable = hashlib.sha1(urlsplit(url)._replace(query="", fragment="").geturl().encode()).hexdigest()[:20]
    return {
        "key": f"{source_id}:{stable}", "source": source_name,
        "title": title, "location": location, "price": price,
        "lat": coords["lat"], "lon": coords["lon"], "text": body, "url": url,
    }


def scrape_generic(search: dict[str, Any], source_id: str, cfg: dict[str, Any]) -> list[dict[str, Any]]:
    start_url = search["source_urls"][source_id]
    found: list[dict[str, Any]] = []
    links: list[str] = []
    with sync_playwright() as runner:
        browser = runner.chromium.launch(headless=True)
        context = browser.new_context(locale="cs-CZ", user_agent=UA)
        page = context.new_page()
        for page_number in range(1, 201):
            parts = urlsplit(start_url)
            query = dict(parse_qsl(parts.query))
            path = parts.path
            if page_number > 1 and cfg.get("page_mode") == "offset_path":
                path = parts.path.rstrip("/") + f"/{(page_number - 1) * 20}/"
            elif page_number > 1:
                query[cfg["page_param"]] = str(page_number)
            page_url = urlunsplit((parts.scheme, parts.netloc, path, urlencode(query), parts.fragment))
            page.goto(page_url, wait_until="domcontentloaded", timeout=60000)
            title = page.title().lower()
            if "404" in title or "error occurred" in page.locator("body").inner_text(timeout=10000).lower():
                raise RuntimeError(f"source returned an error page: {page.url}")
            selector = f'a[href*="{cfg["link_fragment"]}"]'
            try:
                # Cookie overlays do not matter: listing anchors are already
                # present in the DOM on Bazoš and Bezrealitky.
                page.wait_for_selector(selector, state="attached", timeout=20000)
            except PlaywrightTimeoutError:
                save_diagnostics(page, f"{source_id}-{search['id']}-{page_number}")
                if page_number == 1:
                    raise
                break
            page_links = page.locator(selector).evaluate_all("els => [...new Set(els.map(e => e.href))]")
            before = len(links)
            links.extend(x for x in page_links if x not in links)
            if len(links) == before:
                break
        for url in links:
            detail = context.new_page()
            try:
                detail.goto(url, wait_until="domcontentloaded", timeout=45000)
                item = parse_generic_page(detail, url, source_id, cfg["name"])
                if item and matches(item, search):
                    found.append(item)
            except PlaywrightTimeoutError:
                print(f"{source_id} detail timeout: {url}", file=sys.stderr)
            finally:
                detail.close()
        browser.close()
    return found


def route_minutes(origin: dict[str, float], destinations: list[dict[str, Any]]) -> tuple[int, str] | None:
    best: tuple[int, str] | None = None
    for dest in destinations:
        url = ("https://router.project-osrm.org/route/v1/driving/"
               f"{origin['lon']},{origin['lat']};{dest['lon']},{dest['lat']}")
        try:
            data = get_json(url, {"overview": "false", "alternatives": "false", "steps": "false"})
            minutes = math.ceil(data["routes"][0]["duration"] / 60)
            if best is None or minutes < best[0]:
                best = (minutes, dest["name"])
        except (requests.RequestException, KeyError, IndexError):
            continue
    return best


def matches(item: dict[str, Any], search: dict[str, Any]) -> bool:
    if not item["price"] or item["price"] > search["max_price_czk"]:
        return False
    cfg = CONFIG["filters"]
    text = norm(item["title"] + " " + item["location"] + " " + item["text"])
    if any(norm(term) in text for term in cfg["excluded_terms"]):
        return False
    if (cfg["require_personal_ownership"] and not item.get("ownership_verified")
            and not any(norm(term) in text for term in cfg["required_terms"])):
        return False
    origin = {"lat": item["lat"], "lon": item["lon"]}
    if "center" in search:
        item["distance_km"] = round(haversine(origin, search["center"]), 1)
        return item["distance_km"] <= search["max_radius_km"]
    routed = route_minutes(origin, search["destinations"])
    if not routed:
        return False
    item["drive_minutes"], item["drive_destination"] = routed
    return item["drive_minutes"] <= search["max_drive_minutes"]


def telegram(text: str) -> None:
    token, chat_id = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required")
    response = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True},
        timeout=30,
    )
    response.raise_for_status()


def render(item: dict[str, Any]) -> str:
    route = (f"🚗 ≈ {item['drive_minutes']} мин до {html.escape(item['drive_destination'])}"
             if "drive_minutes" in item else f"📍 {item['distance_km']} км от центра Zábřeh")
    return (f"🏠 <b>{html.escape(item['title'])}</b>\n"
            f"💰 {item['price']:,} Kč\n📍 {html.escape(item['location'])}\n{route}\n"
            f"Источник: {item['source']} · <a href=\"{html.escape(item['url'])}\">открыть объявление</a>").replace(",", " ")


def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {"seen": {}}
    return json.loads(STATE_PATH.read_text(encoding="utf-8"))


def main() -> None:
    state = load_state()
    failures: list[str] = []
    for search in CONFIG["searches"]:
        items: list[dict[str, Any]] = []
        try:
            items.extend(scrape_sreality(search))
        except (requests.RequestException, PlaywrightError, RuntimeError) as exc:
            message = f"Sreality / {search['id']}: {type(exc).__name__}: {str(exc)[:140]}"
            print(message, file=sys.stderr)
            failures.append(message)
        for source_id, source_cfg in CONFIG["sources"].items():
            try:
                items.extend(scrape_generic(search, source_id, source_cfg))
            except (requests.RequestException, PlaywrightError, RuntimeError) as exc:
                message = f"{source_cfg['name']} / {search['id']}: {type(exc).__name__}: {str(exc)[:140]}"
                print(message, file=sys.stderr)
                failures.append(message)
        fresh = [x for x in items if x["key"] not in state["seen"]]
        if fresh:
            limit = CONFIG["max_results_per_message"]
            for start in range(0, len(fresh), limit):
                body = "\n\n".join(render(x) for x in fresh[start:start + limit])
                telegram(f"<b>{html.escape(search['title'])}</b>\n\n{body}")
        for item in items:
            state["seen"][item["key"]] = hashlib.sha1(item["url"].encode()).hexdigest()[:12]
    if failures:
        telegram("⚠️ <b>Flat Finder запущен, но источник временно недоступен</b>\n\n" +
                 "\n".join(html.escape(x) for x in failures) +
                 "\n\nTelegram настроен правильно; адаптер источника требует обновления.")
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


CONFIG = yaml.safe_load((ROOT / "config.yml").read_text(encoding="utf-8"))
if __name__ == "__main__":
    main()
