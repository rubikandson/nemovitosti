# Měsíční aktualizace data/price_map.csv ze Sreality (Atlas cen prodaných bytů).
# Zdroj: https://www.sreality.cz/cenova-mapa/hledani/byty/hlavni-mesto-praha-10
# (schváleno uživatelem 2026-07-10 — je to týž zdroj, ze kterého vznikla
# původní tabulka v uživatelově Google Sheetu, jen teď tažený automaticky).
#
# Stránka je Next.js appka; data jsou v <script id="__NEXT_DATA__"> jako
# pageProps.aggregatedLocalities — tahle jedna URL (kraj Hlavní město Praha,
# kategorie byty) rovnou vrací všechny pražské městské části (entityType=
# "ward") s průměrnou cenou/m2 a počtem transakcí za posledních 12 měsíců.
# Ověřeno 2026-07-10 diagnostikou v GitHub Actions + reálnými URL čtvrtí,
# které poslal uživatel (viz PREDAVACI.md, sekce 10).
#
# POZOR: sloupec najem_m2_mesic v price_map.csv NENÍ ze Sreality (ta ho
# neposkytuje) — dopočítá se jinde (zatím jen Radlice, Dejvice) a tento
# skript ho NIKDY nepřepisuje, jen ho beze změny přenese ze stávajícího CSV.
#
# Sloupec kraj (2026-08, na žádost uživatele — rozšíření na Střední Čechy,
# viz scripts/stredocesky_cenova_mapa.py): tenhle skript smí přepisovat JEN
# řádky s kraj="Praha" — řádky ostatních krajů v CSV zůstávají beze změny,
# aby si regionální scrapery navzájem nepřepisovaly data při každém běhu.
import csv
import json
import re
import sys

import requests

URL = "https://www.sreality.cz/cenova-mapa/hledani/byty/hlavni-mesto-praha-10"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36"}
CSV_PATH = "data/price_map.csv"
FIELDNAMES = ["klic", "ctvrt", "cena_za_m2_czk", "pocet_transakci", "najem_m2_mesic", "kraj"]


def _norm(s):
    return s.strip().lower().replace(" ", "_").replace("-", "_")


def nacti_stavajici_najmy():
    """{klic: najem_m2_mesic} ze stávajícího CSV — zachováme beze změny."""
    najmy = {}
    try:
        with open(CSV_PATH, encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                if row.get("najem_m2_mesic"):
                    najmy[row["klic"]] = row["najem_m2_mesic"]
    except FileNotFoundError:
        pass
    return najmy


def nacti_ostatni_kraje():
    """Řádky jiných krajů (ne Praha) ze stávajícího CSV — zachováme beze
    změny, tenhle skript se stará jen o Prahu."""
    ostatni = []
    try:
        with open(CSV_PATH, encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                if (row.get("kraj") or "Praha").strip() != "Praha":
                    ostatni.append(row)
    except FileNotFoundError:
        pass
    return ostatni


def stahni_ctvrti():
    r = requests.get(URL, headers=HEADERS, timeout=30)
    r.raise_for_status()
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.S)
    if not m:
        raise RuntimeError("__NEXT_DATA__ nenalezen — Sreality patrně změnila strukturu stránky")
    data = json.loads(m.group(1))
    al = data["props"]["pageProps"].get("aggregatedLocalities") or []
    ctvrti = [a for a in al if a["locality"]["entityType"] == "ward"]
    if len(ctvrti) < 50:
        raise RuntimeError(f"Neočekávaně málo čtvrtí ({len(ctvrti)}) — zastavuji, nenahrazuji tichým odhadem")
    return ctvrti


def aktualizuj():
    ctvrti = stahni_ctvrti()
    najmy = nacti_stavajici_najmy()
    ostatni = nacti_ostatni_kraje()
    radky = []
    for a in sorted(ctvrti, key=lambda a: -a["avgPricePerSqm"]):
        nazev = a["locality"]["name"]
        klic = _norm(nazev)
        radky.append({
            "klic": klic,
            "ctvrt": nazev,
            "cena_za_m2_czk": int(a["avgPricePerSqm"]),
            "pocet_transakci": int(a["numTransactions"]),
            "najem_m2_mesic": najmy.get(klic, ""),
            "kraj": "Praha",
        })
    radky.extend(ostatni)
    with open(CSV_PATH, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDNAMES)
        w.writeheader()
        w.writerows(radky)
    print(f"Aktualizováno {len(radky) - len(ostatni)} čtvrtí (Praha) v {CSV_PATH} ze Sreality ({URL}), "
          f"zachováno {len(ostatni)} řádků jiných krajů.")


if __name__ == "__main__":
    try:
        aktualizuj()
    except Exception as e:
        print(f"CHYBA při aktualizaci cenové mapy: {e}", file=sys.stderr)
        sys.exit(1)
