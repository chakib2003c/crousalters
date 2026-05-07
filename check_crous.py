"""
CROUS Logement Alert Bot
Checks trouverunlogement.lescrous.fr every 5 minutes via GitHub Actions
and sends Telegram alerts when new rooms appear in Île-de-France.

Updated version:
- Uses CROUS tool 45 = "année prochaine 2026-2027"
- Scrapes the public search page instead of the blocked /api/fr/search endpoint
- Extracts accommodation links directly from the page HTML
"""

import os
import json
import time
import re
import hashlib
from urllib.parse import urljoin
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

# ─── CONFIG ────────────────────────────────────────────────────────────────────

BASE_URL = "https://trouverunlogement.lescrous.fr"
TOOL_ID = "45"  # 45 = "Mon logement pour l'année prochaine 2026-2027"
STATE_FILE = "state.json"

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")

if not TELEGRAM_TOKEN:
    raise RuntimeError("Missing GitHub secret: TELEGRAM_TOKEN")
if not CHAT_ID:
    raise RuntimeError("Missing GitHub secret: CHAT_ID")

# Each zone uses the public CROUS search URL.
# Bounds format: lon_min_lat_max_lon_max_lat_min
ZONES = [
    {
        "name": "Paris",
        "url": "https://trouverunlogement.lescrous.fr/tools/45/search"
               "?bounds=2.224122_48.902156_2.4697602_48.8155755&locationName=Paris",
    },
    {
        "name": "Saclay / Gif-sur-Yvette / Orsay",
        "url": "https://trouverunlogement.lescrous.fr/tools/45/search"
               "?bounds=2.0900_48.7400_2.2500_48.6700&locationName=Orsay",
    },
    {
        "name": "Palaiseau / Massy / Verrières",
        "url": "https://trouverunlogement.lescrous.fr/tools/45/search"
               "?bounds=2.1800_48.7300_2.3000_48.6800&locationName=Palaiseau",
    },
    {
        "name": "Versailles / Saint-Quentin-en-Yvelines",
        "url": "https://trouverunlogement.lescrous.fr/tools/45/search"
               "?bounds=1.9500_48.8200_2.1500_48.7000&locationName=Versailles",
    },
    {
        "name": "Créteil / Val-de-Marne",
        "url": "https://trouverunlogement.lescrous.fr/tools/45/search"
               "?bounds=2.3500_48.8000_2.5000_48.7400&locationName=Créteil",
    },
    {
        "name": "Saint-Denis / Villetaneuse",
        "url": "https://trouverunlogement.lescrous.fr/tools/45/search"
               "?bounds=2.3000_48.9600_2.4500_48.9000&locationName=Saint-Denis",
    },
    {
        "name": "Nanterre / La Défense",
        "url": "https://trouverunlogement.lescrous.fr/tools/45/search"
               "?bounds=2.1600_48.9200_2.2500_48.8700&locationName=Nanterre",
    },
    {
        "name": "Évry / Courcouronnes",
        "url": "https://trouverunlogement.lescrous.fr/tools/45/search"
               "?bounds=2.3800_48.6500_2.5000_48.5800&locationName=Évry-Courcouronnes",
    },
]

HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
    "User-Agent": "Mozilla/5.0 (compatible; CrousAlertBot/2.0; +https://github.com)",
    "Referer": "https://trouverunlogement.lescrous.fr/",
}

# ─── STATE ─────────────────────────────────────────────────────────────────────

def now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

def load_state() -> set:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return set(data.get("seen_ids", []))
        except Exception as e:
            print(f"⚠️ Could not read state.json, starting empty: {e}")
    return set()

def save_state(seen_ids: set):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(
            {"seen_ids": sorted(seen_ids), "updated_at": now_utc()},
            f,
            indent=2,
            ensure_ascii=False,
        )

# ─── CROUS PAGE SCRAPER ────────────────────────────────────────────────────────

def extract_result_count(text: str):
    """
    Extracts counts like:
    - 12 logements trouvés pour Paris
    - 1 logement trouvé pour ...
    """
    match = re.search(r"(\d+)\s+logements?\s+trouv", text, flags=re.IGNORECASE)
    if match:
        return int(match.group(1))
    return None

def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()

def extract_price(text: str) -> str:
    match = re.search(r"(?:de\s+)?\d+(?:[,.]\d+)?(?:\s*à\s*\d+(?:[,.]\d+)?)?\s*€", text)
    return clean_text(match.group(0)) if match else ""

def extract_surface(text: str) -> str:
    match = re.search(r"(?:de\s+)?\d+(?:[,.]\d+)?(?:\s*à\s*\d+(?:[,.]\d+)?)?\s*m²", text, flags=re.IGNORECASE)
    return clean_text(match.group(0)) if match else ""

def find_card_text(anchor) -> str:
    """
    Walks up the HTML tree until it finds a block that looks like a logement card.
    """
    node = anchor
    best_text = clean_text(anchor.get_text(" ", strip=True))

    for _ in range(8):
        if not getattr(node, "parent", None):
            break
        node = node.parent
        txt = clean_text(node.get_text(" ", strip=True))

        # A result card normally contains a price and/or m².
        if "€" in txt or "m²" in txt:
            best_text = txt
            break

        # Keep a reasonable text block if it grows.
        if len(txt) > len(best_text) and len(txt) < 1200:
            best_text = txt

    return best_text

def fetch_rooms(zone: dict) -> list:
    """
    Fetch the public CROUS search page and extract accommodation results.
    This avoids the old /api/fr/search endpoint, which now returns 405 Method Not Allowed.
    """
    try:
        resp = requests.get(zone["url"], headers=HEADERS, timeout=25)
        resp.raise_for_status()
    except Exception as e:
        print(f"  ⚠️ Page error for {zone['name']}: {e}")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    page_text = clean_text(soup.get_text(" ", strip=True))
    count = extract_result_count(page_text)

    print(f"   Page result count: {count if count is not None else 'unknown'}")

    if count == 0 or "Aucun logement trouvé" in page_text:
        return []

    rooms = []
    seen_links = set()

    # Links look like /tools/45/accommodations/...
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/accommodations/" not in href:
            continue

        link = urljoin(BASE_URL, href)
        if link in seen_links:
            continue
        seen_links.add(link)

        id_match = re.search(r"/accommodations/([^/?#]+)", link)
        room_id = id_match.group(1) if id_match else hashlib.sha1(link.encode()).hexdigest()[:16]

        title = clean_text(a.get_text(" ", strip=True)) or "Logement CROUS disponible"
        card_text = find_card_text(a)
        price = extract_price(card_text)
        surface = extract_surface(card_text)

        rooms.append({
            "id": room_id,
            "title": title,
            "price": price,
            "surface": surface,
            "city": zone["name"],
            "url": link,
            "details": card_text[:500],
        })

    # Fallback: sometimes the page shows the number but links are not extracted.
    # In that case, alert once with the search URL.
    if not rooms and count and count > 0:
        fallback_id = "search-" + hashlib.sha1(f"{zone['url']}:{count}".encode()).hexdigest()[:16]
        rooms.append({
            "id": fallback_id,
            "title": f"{count} logements CROUS trouvés",
            "price": "",
            "surface": "",
            "city": zone["name"],
            "url": zone["url"],
            "details": f"{count} logements trouvés sur la page de recherche.",
        })

    return rooms

# ─── TELEGRAM ──────────────────────────────────────────────────────────────────

def send_telegram(message: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }

    try:
        r = requests.post(url, json=payload, timeout=15)
        r.raise_for_status()
        print("   ✅ Telegram sent OK")
    except Exception as e:
        print(f"   ❌ Telegram error: {e}")

def format_room_message(room: dict, zone_name: str) -> str:
    title = room.get("title") or "Logement CROUS disponible"
    price = room.get("price") or ""
    surface = room.get("surface") or ""
    city = room.get("city") or zone_name
    link = room.get("url") or f"https://trouverunlogement.lescrous.fr/tools/{TOOL_ID}/search"

    lines = [
        "🏠 <b>Nouveau logement CROUS disponible !</b>",
        f"📍 <b>Zone :</b> {zone_name}",
        f"📋 <b>Résidence / logement :</b> {title}",
    ]

    if price:
        lines.append(f"💶 <b>Loyer :</b> {price}")
    if surface:
        lines.append(f"📐 <b>Surface :</b> {surface}")
    if city:
        lines.append(f"🌆 <b>Ville :</b> {city}")

    lines.append(f"\n👉 <a href=\"{link}\">Voir le logement</a>")
    return "\n".join(lines)

# ─── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    print(f"🕐 CROUS check started at {now_utc()}")
    print(f"🔧 Using CROUS tool ID: {TOOL_ID}")

    seen_ids = load_state()
    new_rooms_found = 0
    all_current_ids = set()

    for zone in ZONES:
        print(f"\n🔍 Checking: {zone['name']}")
        rooms = fetch_rooms(zone)
        print(f"   → {len(rooms)} room(s) extracted from page")

        for room in rooms:
            room_id = str(room.get("id", "")).strip()
            if not room_id:
                continue

            all_current_ids.add(room_id)

            if room_id not in seen_ids:
                print(f"   🆕 NEW room found: {room_id} — {room.get('title', '')}")
                msg = format_room_message(room, zone["name"])
                send_telegram(msg)
                new_rooms_found += 1
                time.sleep(1)

        time.sleep(1)

    print(f"\n📊 Summary: {new_rooms_found} new room(s) found and notified")

    updated_ids = seen_ids | all_current_ids
    save_state(updated_ids)
    print(f"💾 State saved: {len(updated_ids)} total known IDs")

    if new_rooms_found == 0:
        print("✅ No new rooms — no Telegram message sent")

if __name__ == "__main__":
    main()
