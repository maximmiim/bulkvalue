"""
Phase 1: Referenzdatenbank aufbauen (TCGdex-Version, kein API-Key noetig).

Laedt Karten eines Sets ueber die TCGdex API, speichert Metadaten + Preise
in einer lokalen SQLite-Datenbank und cached die Kartenbilder lokal.

WICHTIG: TCGdex liefert Cardmarket-Preise als EIN globales Objekt (avg/low/trend),
nicht erkennbar nach Sprache/Land aufgeschluesselt (anders als z.B. pokemon-api.com).
Dieses Skript prueft das aktiv: es fragt dieselbe Karte in mehreren Sprachen ab
und vergleicht die Preise. Wenn sie identisch sind, wissen wir, dass TCGdex
keine sprachspezifischen Preise liefert -- dann brauchen wir eine zweite
Preisquelle fuer die Sprachdifferenzierung.

Nutzung:
    python scripts/fetch_cards.py --set-search "Crown Zenith"
    python scripts/fetch_cards.py --set-id swsh12pt5
"""

import argparse
import sqlite3
import sys
from pathlib import Path

import requests
from tqdm import tqdm

# --- Konfiguration ---------------------------------------------------------

BASE_URL = "https://api.tcgdex.net/v2"

# Sprachen, die wir fuer dein Projekt zuerst abdecken (siehe Roadmap Phase 0)
LANGUAGES = ["de", "en", "fr", "es", "it"]
PRIMARY_LANGUAGE = "en"  # fuer die Set-Suche & Kartenliste

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
IMAGES_DIR = DATA_DIR / "raw_images"
DB_PATH = DATA_DIR / "card_database.db"


# --- Datenbank ---------------------------------------------------------------

def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS cards (
            card_id TEXT NOT NULL,
            language TEXT NOT NULL,
            name TEXT,
            local_id TEXT,
            rarity TEXT,
            set_id TEXT,
            set_name TEXT,
            image_url TEXT,
            local_image_path TEXT,
            cardmarket_avg REAL,
            cardmarket_low REAL,
            cardmarket_trend REAL,
            raw_json TEXT,
            PRIMARY KEY (card_id, language)
        );
        """
    )
    conn.commit()


def upsert_card(conn: sqlite3.Connection, card_id: str, language: str, card: dict,
                 local_image_path: str) -> None:
    pricing = (card.get("pricing") or {}).get("cardmarket") or {}
    conn.execute(
        """
        INSERT INTO cards (
            card_id, language, name, local_id, rarity, set_id, set_name,
            image_url, local_image_path, cardmarket_avg, cardmarket_low,
            cardmarket_trend, raw_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(card_id, language) DO UPDATE SET
            name=excluded.name,
            local_id=excluded.local_id,
            rarity=excluded.rarity,
            set_id=excluded.set_id,
            set_name=excluded.set_name,
            image_url=excluded.image_url,
            local_image_path=excluded.local_image_path,
            cardmarket_avg=excluded.cardmarket_avg,
            cardmarket_low=excluded.cardmarket_low,
            cardmarket_trend=excluded.cardmarket_trend,
            raw_json=excluded.raw_json
        """,
        (
            card_id,
            language,
            card.get("name"),
            card.get("localId"),
            card.get("rarity"),
            (card.get("set") or {}).get("id"),
            (card.get("set") or {}).get("name"),
            card.get("image"),
            local_image_path,
            pricing.get("avg"),
            pricing.get("low"),
            pricing.get("trend"),
            str(card),
        ),
    )
    conn.commit()


# --- API-Aufrufe ---------------------------------------------------------------

def api_get(language: str, path: str) -> dict:
    url = f"{BASE_URL}/{language}{path}"
    response = requests.get(url, timeout=30)
    response.raise_for_status()
    return response.json()


def find_set_id(search_term: str) -> str:
    """Sucht ein Set per Namen (Teilstring, case-insensitive) und gibt die ID zurueck."""
    sets = api_get(PRIMARY_LANGUAGE, "/sets")
    search_lower = search_term.lower()
    matches = [s for s in sets if search_lower in s.get("name", "").lower()]
    if not matches:
        raise ValueError(f"Kein Set gefunden fuer Suchbegriff: '{search_term}'")
    match = matches[0]
    print(f"Gefunden: {match['name']} (ID: {match['id']})")
    return match["id"]


def fetch_set_card_ids(set_id: str) -> list[str]:
    """Gibt die Karten-IDs eines Sets zurueck (Kartenliste im Set-Endpoint ist nur ein Kurzformat)."""
    set_data = api_get(PRIMARY_LANGUAGE, f"/sets/{set_id}")
    return [c["id"] for c in set_data.get("cards", [])]


# --- Bilder-Download ---------------------------------------------------------

def download_image(image_base_url: str, card_id: str, language: str) -> str:
    """TCGdex liefert eine Basis-Bild-URL, an die Qualitaet+Format angehaengt werden muss."""
    if not image_base_url:
        return ""
    full_url = f"{image_base_url}/high.webp"
    local_path = IMAGES_DIR / f"{card_id}_{language}.webp"

    if local_path.exists():
        return str(local_path)

    response = requests.get(full_url, timeout=30)
    response.raise_for_status()
    local_path.write_bytes(response.content)
    return str(local_path)


# --- Sanity-Check: liefert TCGdex sprachspezifische Preise? -------------------

def check_price_language_dependency(sample_card_id: str) -> None:
    print("\n--- Sanity-Check: Sind Cardmarket-Preise sprachabhaengig? ---")
    prices = {}
    for lang in LANGUAGES:
        try:
            card = api_get(lang, f"/cards/{sample_card_id}")
            pricing = (card.get("pricing") or {}).get("cardmarket") or {}
            prices[lang] = pricing.get("avg")
        except requests.HTTPError:
            prices[lang] = "nicht verfuegbar"
    for lang, avg in prices.items():
        print(f"  {lang}: avg={avg}")

    unique_values = {v for v in prices.values() if v not in (None, "nicht verfuegbar")}
    if len(unique_values) <= 1:
        print(
            "\n  => ACHTUNG: Preise sind ueber alle Sprachen identisch. "
            "TCGdex liefert vermutlich KEINE sprachspezifischen Preise. "
            "Fuer dein Kernfeature (Preise pro Sprache) brauchen wir "
            "wahrscheinlich eine zweite Datenquelle zusaetzlich zu TCGdex."
        )
    else:
        print("\n  => Preise unterscheiden sich nach Sprache. Gute Nachricht!")
    print("--- Ende Sanity-Check ---\n")


# --- Hauptablauf ---------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Kartendaten + Bilder fuer ein Set laden (TCGdex)")
    parser.add_argument("--set-search", type=str, help="Set-Name, z.B. 'Crown Zenith'")
    parser.add_argument("--set-id", type=str, help="Direkte Set-ID, falls bekannt")
    parser.add_argument(
        "--skip-price-check", action="store_true",
        help="Ueberspringt den Sanity-Check zu sprachspezifischen Preisen"
    )
    args = parser.parse_args()

    if not args.set_search and not args.set_id:
        sys.exit("FEHLER: Bitte --set-search ODER --set-id angeben.")

    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    set_id = args.set_id or find_set_id(args.set_search)

    print("Lade Karten-IDs des Sets...")
    card_ids = fetch_set_card_ids(set_id)
    print(f"{len(card_ids)} Karten gefunden.")

    if not args.skip_price_check and card_ids:
        check_price_language_dependency(card_ids[0])

    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    for card_id in tqdm(card_ids, desc="Verarbeite Karten"):
        for lang in LANGUAGES:
            try:
                card = api_get(lang, f"/cards/{card_id}")
            except requests.HTTPError:
                continue  # Karte in dieser Sprache nicht verfuegbar
            local_image_path = download_image(card.get("image", ""), card_id, lang)
            upsert_card(conn, card_id, lang, card, local_image_path)

    conn.close()
    print(f"Fertig! Datenbank: {DB_PATH}")
    print(f"Bilder gespeichert unter: {IMAGES_DIR}")


if __name__ == "__main__":
    main()
