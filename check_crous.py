"""
CROUS Logement Alert Bot — v2

What this version does:
- Uses CROUS tool 45, which matches the 2026-2027 search links.
- Monitors only Paris, Saclay, and Orsay.
- Scrapes the public CROUS pages instead of the blocked internal API.
- Fetches each accommodation detail page when possible.
- Sends Telegram alerts with price, surface, residence/name, city/zone, and demand flags like "Très demandé".
- Supports manual reset through GitHub Actions input RESET_STATE=true.
"""

import hashlib
import html
import json
import os
import re
import time
from datetime import datetime, timezone
from typing import Dict, List, Set
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

# ─── CONFIG ────────────────────────────────────────────────────────────────────

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]

STATE_FILE = "state.json"

# Set to true only when you manually run the workflow and want alerts for every
# currently available room again.
RESET_STATE = os.environ.get("RESET_STATE", "false").lower() in {"1", "true", "yes", "y"}

# If you ever want to stop receiving rooms marked as very demanded, set this to True.
# For now it is False, so you still receive them, but the message clearly warns you.
SKIP_HIGH_DEMAND = False

TOOL_ID = "45"
BASE_URL = "https://trouverunlogement.lescrous.fr"

# Only the zones you asked to keep.
# You can adjust the bounds later if you want a bigger/smaller area.
ZONES = [
    {
        "name": "Paris",
        "url": (
            f"{BASE_URL}/tools/{TOOL_ID}/search"
            "?bounds=2.224122_48.902156_2.4697602_48.8155755"
            "&locationName=Paris"
        ),
    },
    {
        "name": "Saclay",
        "url": (
            f"{BASE_URL}/tools/{TOOL_ID}/search"
            "?bounds=2.0900_48.7400_2.2500_48.6700"
            "&locationName=Saclay"
        ),
    },
    {
        "name": "Orsay",
        "url": (
            f"{BASE_URL}/tools/{TOOL_ID}/search"
            "?bounds=2.1400_48.7300_2.2200_48.6600"
            "&locationName=Orsay"
        ),
    },
]

HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.7",
    "User-Agent": "Mozilla/5.0 (compatible; CrousAlertBot/2.0)",
    "Referer": BASE_URL + "/",
}

# ─── STATE ─────────────────────────────────────────────────────────────────────

def load_state() -> Set[str]:
    if RESET_STATE:
        print("🔄 RESET_STATE=true → ignoring old state; all current rooms can alert again.")
        return set()

    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return set(data.get("seen_ids", []))
        except Exception as e:
            print(f"⚠️ Could not read state.json: {e}")
            return set()

    return set()


def save_state(seen_ids: Set[str]) -> None:
    data = {
        "seen_ids": sorted(seen_ids),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "tool_id": TOOL_ID,
        "zones": [z["name"] for z in ZONES],
    }
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ─── HELPERS ───────────────────────────────────────────────────────────────────

def clean_text(value: str) -> str:
    value = html.unescape(value or "")
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def stable_id_from_url(url: str) -> str:
    # Use the accommodation slug/id when possible. Otherwise hash the URL.
    m = re.search(r"/accommodations/([^/?#]+)", url)
    if m:
        return f"{TOOL_ID}:{m.group(1)}"
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


def extract_result_count(page_text: str):
    m = re.search(r"(\d+)\s+logements?\s+trouv", page_text, flags=re.IGNORECASE)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d+)\s+résultats?", page_text, flags=re.IGNORECASE)
    if m:
        return int(m.group(1))
    return None


def extract_accommodation_links(html_text: str) -> List[str]:
    soup = BeautifulSoup(html_text, "html.parser")
    links = set()

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if re.search(rf"/tools/{TOOL_ID}/accommodations/", href):
            links.add(urljoin(BASE_URL, href))

    # Regex fallback in case links are embedded in JSON/script sections.
    for href in re.findall(r'["\']([^"\']*/tools/\d+/accommodations/[^"\']+)["\']', html_text):
        if f"/tools/{TOOL_ID}/accommodations/" in href:
            links.add(urljoin(BASE_URL, href))

    return sorted(links)


def page_has_high_demand(text: str) -> bool:
    lowered = text.lower()
    demand_phrases = [
        "très demandé",
        "très demandée",
        "fortement demandé",
        "forte demande",
        "logement demandé",
    ]
    return any(phrase in lowered for phrase in demand_phrases)


def extract_price(text: str) -> str:
    # Examples: 350 €, 350.00 €, 350,00 €
    matches = re.findall(r"(\d{2,4}(?:[,.]\d{1,2})?)\s*€", text)
    if not matches:
        return ""
    # Choose the first plausible monthly price.
    return matches[0].replace(".", ",")


def extract_surface(text: str) -> str:
    m = re.search(r"(\d{1,3}(?:[,.]\d{1,2})?)\s*m(?:²|2)\b", text, flags=re.IGNORECASE)
    return m.group(1).replace(".", ",") if m else ""


def extract_city(text: str, zone_name: str) -> str:
    # Simple extraction fallback. Many CROUS pages put city in normal page text.
    city_candidates = [
        "Paris", "Orsay", "Saclay", "Gif-sur-Yvette", "Palaiseau", "Massy",
        "Boulogne", "Montreuil", "Le Pré-Saint-Gervais", "Saint-Gervais",
    ]
    lowered = text.lower()
    for city in city_candidates:
        if city.lower() in lowered:
            return city
    return zone_name


def extract_title_and_residence(soup: BeautifulSoup, text: str) -> Dict[str, str]:
    title = ""
    residence = ""

    h1 = soup.find("h1")
    if h1:
        title = clean_text(h1.get_text(" "))

    og_title = soup.find("meta", attrs={"property": "og:title"})
    if not title and og_title and og_title.get("content"):
        title = clean_text(og_title["content"])

    # Look for common words around residence.
    m = re.search(r"(?:Résidence|Residence)\s*:?\s*([A-ZÀ-Ÿ0-9][A-Za-zÀ-ÿ0-9 '\-–—]{3,80})", text)
    if m:
        residence = clean_text(m.group(1))

    if not title:
        title = "Logement CROUS disponible"

    return {"title": title, "residence": residence}


def fetch_detail(link: str, zone_name: str) -> Dict[str, str]:
    room_id = stable_id_from_url(link)

    room = {
        "id": room_id,
        "url": link,
        "zone": zone_name,
        "title": "Logement CROUS disponible",
        "residence": "",
        "city": zone_name,
        "price": "",
        "surface": "",
        "high_demand": False,
        "demand_label": "Non indiqué",
    }

    try:
        resp = requests.get(link, headers=HEADERS, timeout=20)
        resp.raise_for_status()
    except Exception as e:
        print(f"  ⚠️ Detail page error for {link}: {e}")
        return room

    soup = BeautifulSoup(resp.text, "html.parser")
    text = clean_text(soup.get_text(" "))

    title_data = extract_title_and_residence(soup, text)
    room["title"] = title_data["title"]
    room["residence"] = title_data["residence"]
    room["city"] = extract_city(text, zone_name)
    room["price"] = extract_price(text)
    room["surface"] = extract_surface(text)

    high_demand = page_has_high_demand(text)
    room["high_demand"] = high_demand
    room["demand_label"] = "Très demandé" if high_demand else "Non indiqué"

    return room


# ─── CROUS SCRAPING ────────────────────────────────────────────────────────────

def fetch_rooms(zone: Dict[str, str]) -> List[Dict[str, str]]:
    """
    Fetch public CROUS search page, extract accommodation links, then fetch each
    detail page to enrich the alert with price/surface/demand info.
    """
    print(f"   URL: {zone['url']}")

    try:
        resp = requests.get(zone["url"], headers=HEADERS, timeout=20)
        resp.raise_for_status()
    except Exception as e:
        print(f"  ⚠️ Search page error for {zone['name']}: {e}")
        return []

    page_text = clean_text(resp.text)
    count = extract_result_count(page_text)
    if count is not None:
        print(f"   Page says: {count} logement(s) found")

    if "Aucun logement trouvé" in page_text:
        return []

    links = extract_accommodation_links(resp.text)
    print(f"   Found {len(links)} accommodation link(s) on page")

    rooms: List[Dict[str, str]] = []

    # Normal case: extract every detail page.
    for link in links:
        room = fetch_detail(link, zone["name"])
        if SKIP_HIGH_DEMAND and room.get("high_demand"):
            print(f"   ⏭ Skipping high-demand room: {room['id']}")
            continue
        rooms.append(room)
        time.sleep(0.3)

    # Fallback: CROUS page says rooms exist but links were not captured.
    if not rooms and count and count > 0:
        fallback_id = hashlib.sha256((zone["url"] + str(count)).encode("utf-8")).hexdigest()[:16]
        rooms.append({
            "id": f"fallback:{TOOL_ID}:{fallback_id}",
            "url": zone["url"],
            "zone": zone["name"],
            "title": f"{count} logement(s) CROUS disponible(s)",
            "residence": "",
            "city": zone["name"],
            "price": "",
            "surface": "",
            "high_demand": False,
            "demand_label": "Non indiqué",
        })

    return rooms


# ─── TELEGRAM ──────────────────────────────────────────────────────────────────

def send_telegram(message: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }

    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
        print("   ✅ Telegram sent OK")
    except Exception as e:
        print(f"   ❌ Telegram error: {e}")


def format_room_message(room: Dict[str, str], zone_name: str) -> str:
    title = clean_text(room.get("title") or "Logement CROUS disponible")
    residence = clean_text(room.get("residence") or "")
    city = clean_text(room.get("city") or zone_name)
    price = clean_text(room.get("price") or "")
    surface = clean_text(room.get("surface") or "")
    demand_label = clean_text(room.get("demand_label") or "Non indiqué")
    link = room.get("url") or zone_name

    lines = [
        "🏠 <b>Nouveau logement CROUS disponible !</b>",
        f"📍 <b>Zone :</b> {html.escape(zone_name)}",
    ]

    if residence:
        lines.append(f"🏢 <b>Résidence :</b> {html.escape(residence)}")

    lines.append(f"📋 <b>Logement :</b> {html.escape(title)}")

    if price:
        lines.append(f"💶 <b>Loyer :</b> {html.escape(price)} €/mois")
    else:
        lines.append("💶 <b>Loyer :</b> Non indiqué")

    if surface:
        lines.append(f"📐 <b>Surface :</b> {html.escape(surface)} m²")

    if city:
        lines.append(f"🌆 <b>Ville :</b> {html.escape(city)}")

    if room.get("high_demand"):
        lines.append("🔥 <b>Demande :</b> Très demandé — candidate rapidement")
    else:
        lines.append(f"🔥 <b>Demande :</b> {html.escape(demand_label)}")

    lines.append(f"\n👉 <a href='{html.escape(link, quote=True)}'>Voir le logement</a>")

    return "\n".join(lines)


# ─── MAIN ──────────────────────────────────────────────────────────────────────

def main() -> None:
    print(f"🕐 CROUS check started at {datetime.now(timezone.utc).isoformat()}")
    print(f"Using CROUS tool ID: {TOOL_ID}")
    print(f"Zones monitored: {', '.join(z['name'] for z in ZONES)}")

    seen_ids = load_state()
    new_rooms_found = 0
    all_current_ids: Set[str] = set()

    for zone in ZONES:
        print(f"\n🔍 Checking: {zone['name']}")
        rooms = fetch_rooms(zone)
        print(f"   → {len(rooms)} room(s) extracted")

        for room in rooms:
            room_id = str(room.get("id", "")).strip()
            if not room_id:
                continue

            all_current_ids.add(room_id)

            if room_id not in seen_ids:
                print(f"   🆕 NEW room found: {room_id}")
                print(f"      Title: {room.get('title', '')}")
                print(f"      Price: {room.get('price') or 'not indicated'}")
                print(f"      Demand: {room.get('demand_label')}")
                msg = format_room_message(room, zone["name"])
                send_telegram(msg)
                new_rooms_found += 1
                time.sleep(1)

        time.sleep(1)

    updated_ids = seen_ids | all_current_ids
    save_state(updated_ids)

    print(f"\n📊 Summary: {new_rooms_found} new room(s) found and notified")
    print(f"💾 State saved: {len(updated_ids)} total known IDs")

    if new_rooms_found == 0:
        print("✅ No new rooms — no Telegram message sent")


if __name__ == "__main__":
    main()
