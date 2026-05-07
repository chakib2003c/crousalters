"""
CROUS Logement Alert Bot
Checks trouverunlogement.lescrous.fr every 5 minutes via GitHub Actions
and sends Telegram alerts when new rooms appear in Île-de-France.
"""

import os
import json
import time
import requests
from datetime import datetime

# ─── CONFIG ────────────────────────────────────────────────────────────────────

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID        = os.environ["CHAT_ID"]
STATE_FILE     = "state.json"

# CROUS "sector" ID for Île-de-France is 42
# Each search zone is defined by a bounding box: lon_min_lat_max_lon_max_lat_min
ZONES = [
    {
        "name": "Paris (intramuros)",
        "url":  "https://trouverunlogement.lescrous.fr/tools/42/search"
                "?bounds=2.2241_48.9022_2.4697_48.8156",
    },
    {
        "name": "Saclay / Gif-sur-Yvette / Orsay",
        "url":  "https://trouverunlogement.lescrous.fr/tools/42/search"
                "?bounds=2.0900_48.7400_2.2500_48.6700",
    },
    {
        "name": "Palaiseau / Massy / Verrières",
        "url":  "https://trouverunlogement.lescrous.fr/tools/42/search"
                "?bounds=2.1800_48.7300_2.3000_48.6800",
    },
    {
        "name": "Versailles / Saint-Quentin-en-Yvelines",
        "url":  "https://trouverunlogement.lescrous.fr/tools/42/search"
                "?bounds=1.9500_48.8200_2.1500_48.7000",
    },
    {
        "name": "Créteil / Val-de-Marne",
        "url":  "https://trouverunlogement.lescrous.fr/tools/42/search"
                "?bounds=2.3500_48.8000_2.5000_48.7400",
    },
    {
        "name": "Saint-Denis / Villetaneuse (Nord IDF)",
        "url":  "https://trouverunlogement.lescrous.fr/tools/42/search"
                "?bounds=2.3000_48.9600_2.4500_48.9000",
    },
    {
        "name": "Nanterre / La Défense",
        "url":  "https://trouverunlogement.lescrous.fr/tools/42/search"
                "?bounds=2.1600_48.9200_2.2500_48.8700",
    },
    {
        "name": "Évry / Courcouronnes",
        "url":  "https://trouverunlogement.lescrous.fr/tools/42/search"
                "?bounds=2.3800_48.6500_2.5000_48.5800",
    },
]

HEADERS = {
    "Accept":          "application/json",
    "Accept-Language": "fr-FR,fr;q=0.9",
    "User-Agent":      "Mozilla/5.0 (compatible; CrousAlertBot/1.0)",
    "Referer":         "https://trouverunlogement.lescrous.fr/",
}

# ─── STATE ─────────────────────────────────────────────────────────────────────

def load_state() -> set:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            data = json.load(f)
            return set(data.get("seen_ids", []))
    return set()

def save_state(seen_ids: set):
    with open(STATE_FILE, "w") as f:
        json.dump({"seen_ids": list(seen_ids), "updated_at": datetime.utcnow().isoformat()}, f, indent=2)

# ─── CROUS API ─────────────────────────────────────────────────────────────────

def fetch_rooms(zone: dict) -> list:
    """
    The CROUS site is a React SPA. The actual data comes from a paginated
    JSON API endpoint derived from the search URL.
    We call the internal API directly.
    """
    # Extract the tool ID and bounds from the URL
    url = zone["url"]
    
    # Build the API endpoint — CROUS internal REST API
    # Pattern: /api/fr/search/{tool_id}?bounds=...&page=1
    import re
    tool_match = re.search(r"/tools/(\d+)/search", url)
    bounds_match = re.search(r"bounds=([^&]+)", url)
    
    if not tool_match:
        return []
    
    tool_id = tool_match.group(1)
    api_base = f"https://trouverunlogement.lescrous.fr/api/fr/search/{tool_id}"
    
    params = {"inCampus": "false", "page": 1}
    if bounds_match:
        coords = bounds_match.group(1).split("_")
        if len(coords) == 4:
            params["lon1"] = coords[0]
            params["lat1"] = coords[1]
            params["lon2"] = coords[2]
            params["lat2"] = coords[3]

    all_rooms = []
    page = 1

    while True:
        params["page"] = page
        try:
            resp = requests.get(api_base, params=params, headers=HEADERS, timeout=20)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"  ⚠️  API error for {zone['name']} page {page}: {e}")
            break

        items = []
        # Handle different possible response shapes
        if isinstance(data, dict):
            items = (
                data.get("data", []) or
                data.get("results", []) or
                data.get("accommodations", []) or
                data.get("items", []) or
                []
            )
            # Some endpoints wrap in "data" -> "items"
            if not items and "data" in data and isinstance(data["data"], dict):
                items = data["data"].get("items", [])
        elif isinstance(data, list):
            items = data

        if not items:
            break

        all_rooms.extend(items)

        # Pagination
        total = None
        if isinstance(data, dict):
            total = data.get("total") or data.get("nbResults") or data.get("count")
        if total and len(all_rooms) >= int(total):
            break
        if len(items) < 20:  # typical page size
            break

        page += 1
        time.sleep(0.5)  # be polite

    return all_rooms

# ─── TELEGRAM ──────────────────────────────────────────────────────────────────

def send_telegram(message: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id":    CHAT_ID,
        "text":       message,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
        print(f"  ✅ Telegram sent OK")
    except Exception as e:
        print(f"  ❌ Telegram error: {e}")

def format_room_message(room: dict, zone_name: str) -> str:
    # CROUS API field names (may vary slightly)
    room_id    = room.get("id", "?")
    title      = room.get("title") or room.get("name") or room.get("label") or "Logement CROUS"
    price      = room.get("price") or room.get("rent") or room.get("loyer") or "?"
    residence  = room.get("residence") or room.get("building") or ""
    city       = room.get("city") or room.get("ville") or zone_name
    room_type  = room.get("type") or room.get("roomType") or ""
    surface    = room.get("surface") or room.get("area") or ""
    available  = room.get("available") or room.get("disponible") or ""
    
    # Build direct link
    link = room.get("url") or room.get("link") or f"https://trouverunlogement.lescrous.fr/tools/42/accommodations/{room_id}"
    
    lines = [
        f"🏠 <b>Nouveau logement CROUS disponible !</b>",
        f"📍 <b>Zone :</b> {zone_name}",
    ]
    if residence:
        lines.append(f"🏢 <b>Résidence :</b> {residence}")
    if title and title != residence:
        lines.append(f"📋 <b>Type :</b> {title}")
    if room_type:
        lines.append(f"🛏 <b>Catégorie :</b> {room_type}")
    if surface:
        lines.append(f"📐 <b>Surface :</b> {surface} m²")
    if price and price != "?":
        lines.append(f"💶 <b>Loyer :</b> {price} €/mois")
    if city:
        lines.append(f"🌆 <b>Ville :</b> {city}")
    if available:
        lines.append(f"📅 <b>Disponible :</b> {available}")
    lines.append(f"\n👉 <a href='{link}'>Voir le logement</a>")
    
    return "\n".join(lines)

# ─── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    print(f"🕐 CROUS check started at {datetime.utcnow().isoformat()} UTC")
    
    seen_ids = load_state()
    new_rooms_found = 0
    all_current_ids = set()

    for zone in ZONES:
        print(f"\n🔍 Checking: {zone['name']}")
        rooms = fetch_rooms(zone)
        print(f"   → {len(rooms)} room(s) returned by API")

        for room in rooms:
            room_id = str(room.get("id", ""))
            if not room_id:
                continue

            all_current_ids.add(room_id)

            if room_id not in seen_ids:
                print(f"   🆕 NEW room found: {room_id}")
                msg = format_room_message(room, zone["name"])
                send_telegram(msg)
                new_rooms_found += 1
                time.sleep(1)  # avoid Telegram rate limit

        time.sleep(1)  # between zones

    print(f"\n📊 Summary: {new_rooms_found} new room(s) found and notified")
    
    # Update state: keep all currently seen IDs
    # (also keep old IDs so we don't re-alert if API temporarily returns empty)
    updated_ids = seen_ids | all_current_ids
    save_state(updated_ids)
    print(f"💾 State saved: {len(updated_ids)} total known IDs")

    if new_rooms_found == 0:
        print("✅ No new rooms — no Telegram message sent")


if __name__ == "__main__":
    main()
