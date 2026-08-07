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

import requests
import yaml

ROOT = Path(__file__).parent
STATE_PATH = ROOT / "state.json"
API = "https://www.sreality.cz/api/cs/v2"
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


def scrape_sreality(search: dict[str, Any]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for region in search["sreality_region_ids"]:
        params = {
            "category_main_cb": 1, "category_type_cb": 1,
            "locality_region_id": region, "price_to": search["max_price_czk"],
            "per_page": 100, "page": 1,
        }
        payload = get_json(f"{API}/estates", params)
        for raw in payload.get("_embedded", {}).get("estates", []):
            estate_id = str(raw.get("hash_id") or raw.get("id"))
            if not estate_id or estate_id == "None":
                continue
            try:
                detail = get_json(f"{API}/estates/{estate_id}")
            except requests.RequestException as exc:
                print(f"detail {estate_id}: {exc}", file=sys.stderr)
                continue
            item = parse_sreality(estate_id, raw, detail)
            if item and matches(item, search):
                found.append(item)
            time.sleep(0.08)
    return found


def parse_sreality(estate_id: str, raw: dict[str, Any], detail: dict[str, Any]) -> dict[str, Any] | None:
    gps = detail.get("map", {}).get("gps") or raw.get("gps") or {}
    lat, lon = gps.get("lat"), gps.get("lon")
    if lat is None or lon is None:
        return None
    text_parts = [str(detail.get("text", {}).get("value", ""))]
    for group in detail.get("items", []):
        text_parts.extend(f"{x.get('name', '')}: {x.get('value', '')}" for x in group.get("items", []))
    price = detail.get("price_czk", {}).get("value_raw") or raw.get("price_czk", {}).get("value_raw") or 0
    return {
        "key": f"sreality:{estate_id}", "source": "Sreality",
        "title": raw.get("name", "Byt"), "location": raw.get("locality", ""),
        "price": int(price or 0), "lat": float(lat), "lon": float(lon),
        "text": " ".join(text_parts),
        "url": f"https://www.sreality.cz/detail/prodej/byt/_/_/{estate_id}",
    }


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
    if cfg["require_personal_ownership"] and not any(norm(term) in text for term in cfg["required_terms"]):
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
        try:
            items = scrape_sreality(search)
        except requests.RequestException as exc:
            # One unavailable portal must not prevent Telegram diagnostics or
            # future adapters from running.
            message = f"Sreality / {search['id']}: {type(exc).__name__}"
            print(message, file=sys.stderr)
            failures.append(message)
            continue
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
