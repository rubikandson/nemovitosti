# Import nabídek ze Sreality.cz přes /api/v1/estates/search (nové API, 2026).
# Použití: python -m src.main import-sreality "https://www.sreality.cz/hledani/prodej/byty/praha"
#
# POZOR: Neoficiální API — Sreality může formát kdykoli změnit. Diagnostika
# posledního běhu v Actions: docs/import_log.txt
import csv
import json
import re
import time
from urllib.parse import urlparse, parse_qs

import requests

from . import db

# Matice hodnocení lokality (kalibruje se v data/lokalita_matice.csv):
# kritéria = vzdálenosti k POI ze Sreality; skóre → kategorie lokality modelu.
MATICE_CSV = db.ROOT / "data" / "lokalita_matice.csv"

# Rozlišení stejnojmenných obcí v jiném okrese (2026-08-04, oprava zjištěného
# bugu — viz PREDAVACI.md a scripts/stredocesky_cenova_mapa.py): Středočeský
# kraj má obce se STEJNÝM jménem v RŮZNÝCH okresech (potvrzeno na Sreality
# API, 2026-08-04: "Roztoky" existují v okrese Praha-západ — velké město na
# našem seznamu — i jako malá obec v okrese Rakovník; totéž "Jesenice").
# Bez rozlišení podle okresu by nabídka z malé obce omylem dostala cenu
# velkého města se stejným jménem (nebo naopak) — zjištěno na 2 konkrétních
# aktivních nabídkách (Roztoky id 8000, Jesenice id 7844), obě reálně
# v okrese Rakovník, obě omylem oceněné cenou Praha-západ.
MESTA_STC_CSV = db.ROOT / "data" / "mesta_stredocechy.csv"


def _nacti_mesta_okresy() -> dict:
    """{název velkého města (lowercase): okres} z data/mesta_stredocechy.csv."""
    mapa = {}
    if not MESTA_STC_CSV.exists():
        return mapa
    with open(MESTA_STC_CSV, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            mapa[row["nazev"].strip().lower()] = row["okres"].strip()
    return mapa


MESTA_OKRESY = _nacti_mesta_okresy()
KAT_PLUS = "u MHD / metro, u obchodu a služeb, tiché místo, parky v docházce"
KAT_STANDARD = "standardní dostupnost, běžná občanská vybavenost, žádné extrémy"
KAT_MINUS = "daleko od MHD/služeb, hluk / bariéry (rušná silnice, železnice), horší pěší dostupnost"


def nacti_matici():
    pravidla, hranice = [], {"min_skore": 4.0, "max_skore": -2.0}
    if not MATICE_CSV.exists():
        return pravidla, hranice
    with open(MATICE_CSV, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if r["podminka"] in ("min_skore", "max_skore"):
                hranice[r["podminka"]] = float(r["metry"])
            elif r.get("pole"):
                pravidla.append((r["kriterium"], r["pole"], r["podminka"],
                                 float(r["metry"]), float(r["body"])))
    return pravidla, hranice


def ohodnot_lokalitu(estate, pravidla, hranice):
    """Vrátí (kategorie, skóre, detail) z POI vzdáleností nabídky.
    Pravidla 'do' se pro stejné pole vyhodnocují od nejmenší vzdálenosti,
    platí první splněné; pravidla 'nad' se vyhodnocují všechna."""
    skore, detail, pouzita_pole = 0.0, [], set()
    for nazev, pole, podminka, metry, body in sorted(
            pravidla, key=lambda x: (x[1], x[2] != "do", x[3])):
        d = estate.get(pole)
        if not isinstance(d, (int, float)):
            continue
        if podminka == "do" and (pole, "do") not in pouzita_pole and d <= metry:
            pouzita_pole.add((pole, "do"))
            skore += body
            detail.append(f"{nazev} ({int(d)} m: {body:+g})")
        elif podminka == "nad" and d > metry:
            skore += body
            detail.append(f"{nazev} ({int(d)} m: {body:+g})")
    if skore >= hranice["min_skore"]:
        kat = KAT_PLUS
    elif skore <= hranice["max_skore"]:
        kat = KAT_MINUS
    else:
        kat = KAT_STANDARD
    return kat, skore, "; ".join(detail)

API = "https://www.sreality.cz/api/v1/estates/search"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
           "Accept": "application/json"}
LIMIT = 100

CATEGORY_MAIN = {"byty": 1, "domy": 2, "pozemky": 3, "komercni": 4, "ostatni": 5}
CATEGORY_TYPE = {"prodej": 1, "pronajem": 2, "drazby": 3}
REGIONY = {"praha": 10, "hlavni-mesto-praha": 10, "stredocesky-kraj": 11,
           "jihocesky-kraj": 12, "plzensky-kraj": 13, "karlovarsky-kraj": 3,
           "ustecky-kraj": 15, "liberecky-kraj": 16, "kralovehradecky-kraj": 17,
           "pardubicky-kraj": 18, "vysocina": 19, "jihomoravsky-kraj": 20,
           "olomoucky-kraj": 21, "zlinsky-kraj": 25, "moravskoslezsky-kraj": 24}
# Zobrazovaný název kraje (uložený do listings.kraj a price_map.kraj) — jen
# pro rozlišení regionů při importu/deaktivaci, viz import_sreality().
REGION_NAZEV = {10: "Praha", 11: "Středočeský"}


def _params_from_url(url: str) -> dict:
    """Best-effort převod URL vyhledávání na parametry API."""
    parsed = urlparse(url)
    seg = [s for s in parsed.path.split("/") if s]
    params = {}
    for s in seg:
        if s in CATEGORY_TYPE:
            params["category_type_cb"] = CATEGORY_TYPE[s]
        if s in CATEGORY_MAIN:
            params["category_main_cb"] = CATEGORY_MAIN[s]
    znama = set(CATEGORY_TYPE) | set(CATEGORY_MAIN) | {"hledani"}
    lokalita = [s for s in seg if s not in znama]
    if lokalita:
        reg = REGIONY.get(lokalita[-1].lower())
        if reg:
            params["locality_region_id"] = reg
    for k, v in parse_qs(parsed.query).items():
        params[k] = v[0]
    return params


def _ctvrt(locality) -> str:
    """Z lokality typu 'Štorkánova, Praha 5 - Smíchov' vytáhne 'Smíchov'.

    Mimo Prahu (2026-08, rozšíření na Střední Čechy — viz
    scripts/stredocesky_cenova_mapa.py) bereme rovnou pole `city` (např.
    'Beroun'), NE `citypart`: cenová mapa mimo Prahu je kalibrovaná na
    úrovni celé obce, ne jejích místních částí, a `citypart` tam bývá tvar
    'Beroun-Závodí' (místní část obce) — kdyby se použila stejná logika
    jako u Prahy (vzít citypart, rozdělit podle pomlčky), vytáhlo by to
    'Závodí' místo 'Beroun' a nabídka by v cenové mapě nenašla shodu.
    Ověřeno na reálném vzorku (Sreality API, kraj Středočeský).

    Rozlišení stejnojmenných obcí v jiném okrese (viz MESTA_OKRESY výše):
    když se `city` shoduje s jedním z velkých měst ze seznamu, ale skutečný
    okres nabídky (pole `district`) NESEDÍ s okresem zaznamenaným pro to
    velké město, přidá se okres do názvu (např. "Roztoky (Rakovník)") —
    nabídka tak nenajde shodu v cenové mapě (ta má jen tu SPRÁVNOU obec)
    a spadne do standardního „bez shody → deaktivovat", stejně jako
    jakákoli jiná malá obec mimo náš seznam. Žádná tichá záměna cen."""
    if isinstance(locality, dict):
        city = str(locality.get("city") or "").strip()
        if city and city != "Praha":
            okres_ocekavany = MESTA_OKRESY.get(city.lower())
            okres_skutecny = str(locality.get("district") or "").strip()
            if okres_ocekavany and okres_skutecny and okres_ocekavany != okres_skutecny:
                return f"{city} ({okres_skutecny})"
            return city
        locality = locality.get("citypart") or city or ""
    s = str(locality or "")
    if "-" in s:
        return s.rsplit("-", 1)[1].strip()
    return s.split(",")[-1].strip()


def _plocha(nazev: str):
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*m", nazev or "")
    return float(m.group(1).replace(",", ".")) if m else None


def _dispozice(nazev: str, e: dict | None = None):
    """Dispozice: primárně z category_sub_cb.name (přesné, ze Sreality),
    fallback na regex z názvu inzerátu, pokud pole chybí."""
    if e:
        sub = (e.get("category_sub_cb") or {}).get("name")
        if sub:
            return sub
    m = re.search(r"(\d\+(?:kk|\d)|atypick)", nazev or "", re.I)
    return m.group(1) if m else None


def _detail_url(e: dict, dispozice: str | None, hash_id: str) -> str:
    """Sestaví skutečnou URL detailu (Sreality od 2026 vyžaduje reálný slug,
    placeholder 'x/x' vrací 404 — zjištěno a ověřeno 2026-07-09 přes diagnostiku
    v Actions + WebSearch). Slug se skládá z locality.*_seo_name polí, která
    Sreality posílá přímo v odpovědi API — žádné vlastní odhadování."""
    typ_prodej = "prodej"  # tento import řeší jen prodej bytů (viz CATEGORY_TYPE)
    kategorie = "byt"
    loc = e.get("locality") or {}
    casti = [loc.get("city_seo_name"), loc.get("citypart_seo_name"), loc.get("street_seo_name")]
    slug = "-".join(c for c in casti if c) or "x"
    disp = dispozice or "x"
    return f"https://www.sreality.cz/detail/{typ_prodej}/{kategorie}/{disp}/{slug}/{hash_id}"


MIN_CENA = 100_000  # Sreality posílá 1 Kč u "ceny na vyžádání" — ignorovat


def _cena(r) -> float | None:
    for k in ("price_czk", "price_summary_czk", "price"):
        v = r.get(k)
        if isinstance(v, dict):
            v = v.get("value_raw") or v.get("amount")
        if v:
            try:
                cena = float(str(v).replace(" ", ""))
            except ValueError:
                continue
            return cena if cena >= MIN_CENA else None
    return None


def import_sreality(url: str, max_pages: int = 5) -> int:
    params = _params_from_url(url)
    region_id = params.get("locality_region_id")
    kraj = REGION_NAZEV.get(region_id, params.get("locality_region_id") and str(region_id))
    con = db.connect()
    db.migruj(con)
    pravidla, hranice = nacti_matici()
    n, offset, total = 0, 0, None
    videne = set()   # hash_id nabídek viděných v tomto importu
    for _ in range(max_pages):
        r = requests.get(API, params={**params, "limit": LIMIT, "offset": offset},
                         headers=HEADERS, timeout=30)
        print(f"offset {offset}: HTTP {r.status_code}, url: {r.url}")
        r.raise_for_status()
        data = r.json()
        if total is None:
            total = (data.get("pagination") or {}).get("total")
            print(f"celkem nabídek dle API: {total}")
        results = data.get("results", [])
        if not results:
            break
        for e in results:
            nazev = e.get("advert_name", "")
            cena = _cena(e)  # None = "cena na vyžádání" → speciální kategorie bez ceny
            hash_id = str(e.get("hash_id"))
            videne.add(hash_id)
            kat, skore, detail = ohodnot_lokalitu(e, pravidla, hranice)
            dispozice = _dispozice(nazev, e)
            db.upsert_listing(con, {
                "source": "sreality",
                "external_id": hash_id,
                "url": _detail_url(e, dispozice, hash_id),
                "nazev": nazev,
                "dispozice": dispozice,
                "ctvrt": _ctvrt(e.get("locality")),
                "plocha_m2": _plocha(nazev),
                "cena_czk": cena,
                "kraj": kraj,
                # stav/rok/balkon/parkování doplní import-detaily
                "lokalita_auto": kat,
                "lokalita_skore": skore,
                "lokalita_detail": detail,
            })
            n += 1
        offset += LIMIT
        if total and offset >= total:
            break
        time.sleep(1)  # šetrnost k API
    # Deaktivace zmizelých nabídek (schváleno 2026-07-06): jen když import prošel
    # celou nabídku (offset >= total) a viděl rozumný počet — pojistka proti výpadku API.
    #
    # KRITICKÉ (2026-08, rozšíření na Střední Čechy): musí se omezit na STEJNÝ
    # kraj (WHERE kraj=:kraj) — bez toho by import jednoho kraje (třeba
    # Středočeského) považoval VŠECHNY nabídky jiného kraje (Praha) za
    # "zmizelé", protože se logicky nemůžou objevit ve výsledcích vyhledávání
    # jiného kraje. Staré nabídky bez vyplněného kraje (import před tímhle
    # datem) se do žádného scope nepočítají, dokud je import nedožene.
    aktivnich = con.execute(
        "SELECT COUNT(*) FROM listings WHERE source='sreality' AND active=1 AND kraj=?",
        (kraj,)).fetchone()[0]
    if total and offset >= total and len(videne) > 0.5 * max(aktivnich, 1):
        ph = ",".join("?" * len(videne))
        deakt = con.execute(
            f"UPDATE listings SET active=0 WHERE source='sreality' AND active=1 "
            f"AND kraj=? AND external_id NOT IN ({ph})", (kraj, *videne)).rowcount
        print(f"Deaktivováno zmizelých nabídek ({kraj}): {deakt}")
    con.commit()
    con.close()
    print(f"Importováno {n} nabídek ze Sreality.")
    return n
