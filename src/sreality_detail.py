# Dotahování detailů nabídek ze Sreality a mapování na vstupy modelu.
# Mapování schváleno uživatelem 2026-07-05:
#   stav:  Po rekonstrukci → Generální (+6 %) [jen ≤10 let či rok neuveden, jinak 0 %]
#          Částečně po rekonstrukci → Částečná (+3 %) [stejná logika]
#          Novostavba/Ve výstavbě/Projekt/Velmi dobrý/Dobrý/Špatný/Před rek. → 0 %
#   rok:   object_age → acceptance_year → rok z finish_date;
#          novostavba/ve výstavbě/projekt bez roku → ROK_OCENENI (věk 0)
#   balkon: balcony NEBO terrace NEBO loggia → "Ano"
#   parkování: ≥2 stání/garáže → "Ano 2*"; garáž či stání → "Ano"; jinak "Ne"
# Lokalita se zde NEnastavuje (samostatný budoucí krok).
#
# Vlastnictví a anuita (2026-07-15, na žádost uživatele; ověřeno diagnostikou
# v Actions proti reálným datům, viz PREDAVACI.md bod 5g):
#   vlastnictví: pole `ownership.name` ze Sreality ("Osobní"/"Družstevní"/
#     "Státní/obecní") — stejné jako filtr na samotném Sreality.cz.
#   anuita: pole `annuity` ze Sreality NENÍ spolehlivé (u ověřených vzorků
#     vždy 0/null, i když text popisu jasně mluví o konkrétní nesplacené
#     částce) — proto se stav anuity odvozuje z volného textu
#     `advert_description`. Je to HEURISTIKA na klíčová slova, ne jistota:
#       "nesplacen(á)"/"neuhrazen(á)"/"dluh...anuit"/"zbývá doplatit"/
#         "zbývá splatit" u zmínky o anuitě → "nesplacena" (RED FLAG)
#       "splacen(á)"/"uhrazen(á)"/"vypořádán(á)" u zmínky o anuitě
#         (a NEobsahuje předchozí negaci) → "splacena"
#       anuita zmíněná, ale žádný z výše uvedených vzorů nesedí → "neznamo"
#       anuita v textu vůbec nezmíněná → None (nelze určit / nevztahuje se)
import re
import sqlite3
import time

import requests

ANUITA_NESPLACENA_VZORY = (
    r"nesplacen\w*", r"neuhrazen\w*", r"dluh\w*\s*(?:na|za)?\s*anuit\w*",
    r"zbýv\w*\s+(?:doplatit|splatit)", r"k\s+doplacení",
)
ANUITA_SPLACENA_VZORY = (r"splacen\w*", r"uhrazen\w*", r"vypořádán\w*")


def _anuita_stav(popis: str):
    """Vrátí 'nesplacena' / 'splacena' / 'neznamo' / None (bez zmínky)."""
    if not popis or "anuit" not in popis.lower():
        return None
    for vzor in ANUITA_NESPLACENA_VZORY:
        if re.search(vzor, popis, re.I):
            return "nesplacena"
    for vzor in ANUITA_SPLACENA_VZORY:
        if re.search(vzor, popis, re.I):
            return "splacena"
    return "neznamo"

from . import db
from .valuation import ROK_OCENENI

API = "https://www.sreality.cz/api/v1/estates/{}"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
           "Accept": "application/json"}

STAV_BEZNA = "Žádná / běžná údržba/Novostavba"
STAV_CASTECNA = "Částečná rekonstrukce (kuchyň/koupelna ≤10 let)"
STAV_GENERALNI = "Generální rekonstrukce (≤10 let)"
NOVOSTAVBY = {"novostavba", "ve výstavbě", "projekt"}


def _nazev(pole):
    return (pole or {}).get("name", "") if isinstance(pole, dict) else str(pole or "")


def _stav(d):
    bc = _nazev(d.get("building_condition")).strip().lower()
    rek = d.get("reconstruction_year")
    stary = bool(rek) and (ROK_OCENENI - int(rek)) > 10
    if bc == "po rekonstrukci":
        return STAV_BEZNA if stary else STAV_GENERALNI
    if "částečně po rekonstrukci" in bc:
        return STAV_BEZNA if stary else STAV_CASTECNA
    return STAV_BEZNA


def _stav_sreality(d):
    """Surová hodnota building_condition ze Sreality (Novostavba/Projekt/
    Ve výstavbě/Velmi dobrý/Dobrý/Špatný/Před rekonstrukcí/Po rekonstrukci/
    Částečně po rekonstrukci...) — na žádost uživatele 2026-07-28, čistě
    informační/filtrovací pole (jako filtr na Sreality.cz), NEVSTUPUJE do
    oceňovacího vzorce. Ten dál používá jen zjednodušené 3 kategorie z
    _stav() výše (cornerstone, beze změny)."""
    nazev = _nazev(d.get("building_condition")).strip()
    return nazev or None


def _rok(d):
    for k in ("object_age", "acceptance_year"):
        v = d.get(k)
        if v and str(v).isdigit() and 1800 <= int(v) <= ROK_OCENENI + 10:
            return int(v)
    fd = str(d.get("finish_date") or "")
    if len(fd) >= 4 and fd[:4].isdigit():
        return int(fd[:4])
    if _nazev(d.get("building_condition")).strip().lower() in NOVOSTAVBY:
        return ROK_OCENENI
    return None


def _balkon(d):
    return "Ano" if (d.get("balcony") or d.get("terrace") or d.get("loggia")) else "Ne"


def _parkovani(d):
    stani = 0
    for k in ("parking", "garage_count"):
        v = d.get(k)
        if isinstance(v, (int, float)):
            stani = max(stani, int(v))
    if stani >= 2:
        return "Ano 2*"
    if stani >= 1 or d.get("garage") or d.get("parking_lots"):
        return "Ano"
    return "Ne"


# Typ parkování — Garáž / Stání / Venkovní (2026-08-04, na žádost uživatele,
# JEN PRAHA — viz valuation.py a PREDAVACI.md). Zjištěno reálným trhem
# Sreality (2026-08-04, prodej garáží/garážových stání v Praze): samostatná
# garáž medián ~990k Kč, garážové stání medián ~710k Kč — výrazně jiná
# hodnota, dosavadní jediné PARKOVANI_KC to nerozlišovalo.
# Priorita rozlišení:
#   1. Garáž — spolehlivé strukturované pole Sreality (`garage`/`garage_count`).
#   2. Venkovní — Sreality to jako vlastní pole NEMÁ, jen textová heuristika
#      v popisu inzerátu (klíčová slova "venkovní stání/parkování",
#      "nekryté stání", "otevřené (parkovací) stání") — MÉNĚ jistá než
#      Garáž, ale bez ní bychom venkovní stání nedokázali rozlišit vůbec.
#   3. Stání (výchozí) — je nějaké parkovací místo (`parking`/`parking_lots`),
#      ale není to garáž a text nepotvrzuje venkovní — "garážové stání" je
#      v inzerátech běžnější formulace než venkovní, proto výchozí.
#   4. None — žádné parkování.
VENKOVNI_STANI_VZORY = (
    r"venkovní\s*(?:parkovací\s*)?stání", r"venkovní\s*parkování",
    r"nekryté\s*stání", r"otevřené\s*(?:parkovací\s*)?stání",
)


def _typ_parkovani(d, popis: str):
    if d.get("garage") or (isinstance(d.get("garage_count"), (int, float)) and d.get("garage_count")):
        return "Garáž"
    ma_stani = d.get("parking_lots") or (isinstance(d.get("parking"), (int, float)) and d.get("parking"))
    if not ma_stani:
        return None
    if popis and any(re.search(v, popis, re.I) for v in VENKOVNI_STANI_VZORY):
        return "Venkovní"
    return "Stání"


def _vlastnictvi(d):
    nazev = _nazev(d.get("ownership")).strip()
    return nazev or None


# Doplňkové informační pole (2026-07-15, na žádost uživatele) — čistě pro
# zobrazení/filtr v appce, NEVSTUPUJÍ do oceňovacího vzorce (valuation.py
# je cornerstone a mění se jen s výslovným souhlasem). Ověřeno diagnostikou
# proti reálným datům ze Sreality, viz PREDAVACI.md.

def _energeticky_stitek(d):
    nazev = _nazev(d.get("energy_efficiency_rating_cb")).strip()
    return nazev or None


def _patro(d):
    v = d.get("floor_number")
    return int(v) if isinstance(v, (int, float)) else None


def _pater_celkem(d):
    v = d.get("floors")
    return int(v) if isinstance(v, (int, float)) else None


def _vytah(d):
    nazev = _nazev(d.get("elevator")).strip()
    if not nazev or nazev.lower().startswith("- nezad"):
        return None
    return nazev


def _sklep(d):
    return 1 if d.get("cellar") else 0


def _sklep_m2(d):
    v = d.get("cellar_area")
    return float(v) if isinstance(v, (int, float)) else None


def _zahrada_m2(d, popis: str):
    """Zahrada se do modelu započítá JEN když popis inzerátu obsahuje
    "podíl" (spoluvlastnický podíl na zahradě/pozemku) — 2026-07-28,
    na žádost uživatele, po zjištění konkrétního případu (byt Košíře,
    Vrchlického, 4. patro paneláku): Sreality pole `garden_area` bylo
    500 m², ale popis mluvil jen o "vlastní klidný vnitroblok domu" —
    žádná zmínka o podílu. U bytu nad přízemím sloužil sdílený vnitroblok/
    pozemek klidně 20–30 jednotkám, takže hodnota připsaná JEDNOMU bytu
    byla absurdní (efektivní plocha 78→153 m², tržní hodnota skoro
    dvojnásobná). Rozlišení: sdílená zahrada domu s výslovně napsaným
    podílem (typicky menší domy) = reálná, ocenitelná hodnota; velký
    nerozdělený vnitroblok/pozemek bez podílu = neoceňovat (váha 0).
    Heuristika na klíčové slovo v popisu, ne jistota."""
    v = d.get("garden_area")
    plocha = float(v) if isinstance(v, (int, float)) else None
    if plocha is None:
        return None
    if not popis or not re.search(r"podíl", popis, re.I):
        return None
    return plocha


def _typ_stavby(d):
    nazev = _nazev(d.get("building_type")).strip()
    return nazev or None


def _datum_vlozeni(d):
    """Skutečné datum zveřejnění inzerátu na Sreality (pole `since`),
    přesnější než first_seen (což je jen okamžik prvního zachycení
    naším importem — u zpětně dotahovaných nabídek může být pozdější)."""
    v = d.get("since")
    return str(v) if v else None


# Vedlejší plochy (2026-07-27, na žádost uživatele) — ze strukturovaných
# číselných polí Sreality (floor_area/terrace_area/loggia_area/balcony_area),
# ověřeno přímo v REST API (ne jen v odhadu). Populovaná jsou JEN když je
# realitka vyplnila — jinak zůstávají None, žádné odhadování z volného textu.
# Použití: valuation.py, jadro_a_vedlejsi_plocha().
def _cislo(v):
    return float(v) if isinstance(v, (int, float)) else None


def _plocha_cista(d):
    return _cislo(d.get("floor_area"))


def _terasa_m2(d):
    return _cislo(d.get("terrace_area"))


def _lodzie_m2(d):
    return _cislo(d.get("loggia_area"))


def _balkon_m2(d):
    return _cislo(d.get("balcony_area"))


# Postoupení smlouvy s developerem na splátky (2026-07-27, na žádost
# uživatele, viz PREDAVACI.md) — u rozestavěných/předprodejních bytů bývá
# inzerovaná cena_czk jen PRVNÍ SPLÁTKA, ne celková cena (zbytek je v
# "rozpisu plateb" v popisu). To dělá slevu vs. tržní hodnotě uměle
# obrovskou. Heuristika na klíčové slovo "postoupen*" v popisu inzerátu —
# čistě informační red-flag pole, cena_czk se NEMĚNÍ (bez jistoty o přesné
# celkové ceně bychom jen nahrazovali jeden odhad jiným).
def _postoupeni_stav(popis: str):
    if not popis:
        return None
    return "ano" if re.search(r"postoupen", popis, re.I) else None


def import_detaily(limit: int = 500) -> int:
    con = db.connect()
    try:
        con.execute("ALTER TABLE listings ADD COLUMN detail_at TEXT")
    except sqlite3.OperationalError:
        pass
    # Priorita fronty (2026-07-20, na žádost uživatele): dřív se šlo čistě
    # FIFO podle id (nejstarší nabídka první). To ale u nových nabídek s
    # extrémní slevou znamenalo, že se dostanou do týdenního reportu
    # příležitostí dřív, než se u nich stihne dotáhnout vlastnictví/anuita
    # (=riziko, že se ukáže jako "příležitost" nabídka, kterou ve
    # skutečnosti squeezuje nesplacená anuita nebo jiná externalita).
    # Nově: přednost mají nabídky s nejvyšší už spočtenou slevou (sleva_pct
    # z tabulky valuations, počítaná i bez detailu na základě defaultních
    # koeficientů) — a úplně nové nabídky bez jakéhokoli ocenění mají
    # prioritu nejvyšší ze všech, aby detail měly hotový hned při prvním
    # výpočtu tržní hodnoty. Rychlost (500/den) se neměnila.
    radky = con.execute(
        "SELECT l.id, l.external_id FROM listings l "
        "LEFT JOIN valuations v ON v.listing_id = l.id "
        "WHERE l.source='sreality' AND l.active=1 AND l.detail_at IS NULL "
        "ORDER BY CASE WHEN v.listing_id IS NULL THEN 999999 "
        "ELSE COALESCE(v.sleva_pct, -1000) END DESC "
        "LIMIT ?", (limit,)).fetchall()
    n, chyby = 0, 0
    for r in radky:
        try:
            resp = requests.get(API.format(r["external_id"]), headers=HEADERS, timeout=30)
            resp.raise_for_status()
            d = resp.json().get("result", {})
            con.execute(
                "UPDATE listings SET stav=?, rok_vystavby=?, balkon=?, parkovani=?, "
                "vlastnictvi=?, anuita_stav=?, energeticky_stitek=?, patro=?, "
                "pater_celkem=?, vytah=?, sklep=?, sklep_m2=?, zahrada_m2=?, "
                "typ_stavby=?, datum_vlozeni=?, plocha_cista_m2=?, terasa_m2=?, "
                "lodzie_m2=?, balkon_m2=?, postoupeni_stav=?, stav_sreality=?, "
                "typ_parkovani=?, detail_at=datetime('now') WHERE id=?",
                (_stav(d), _rok(d), _balkon(d), _parkovani(d),
                 _vlastnictvi(d), _anuita_stav(d.get("advert_description")),
                 _energeticky_stitek(d), _patro(d), _pater_celkem(d), _vytah(d),
                 _sklep(d), _sklep_m2(d), _zahrada_m2(d, d.get("advert_description")), _typ_stavby(d),
                 _datum_vlozeni(d), _plocha_cista(d), _terasa_m2(d),
                 _lodzie_m2(d), _balkon_m2(d),
                 _postoupeni_stav(d.get("advert_description")),
                 _stav_sreality(d), _typ_parkovani(d, d.get("advert_description")), r["id"]))
            n += 1
        except Exception as e:
            chyby += 1
            if chyby <= 3:
                print(f"chyba u {r['external_id']}: {e}")
            if chyby > 20:
                print("příliš mnoho chyb, končím")
                break
        time.sleep(0.25)
    con.commit()
    con.close()
    print(f"Detaily dotaženy: {n} nabídek (chyb: {chyby}).")
    return n


def doplnit_typ_parkovani(limit: int = 2000) -> int:
    """Jednorázový/dobíhací backfill `typ_parkovani` (2026-08-04) u nabídek,
    které měly detail stažený PŘED zavedením tohoto pole — běžný
    import_detaily() se řídí `detail_at IS NULL`, takže by je normálně
    nikdy znovu nenavštívil. Praha i Střední Čechy (viz valuation.py,
    PARKOVANI_ZAKLAD_KRAJ) — u ostatních krajů se pole nepoužívá, ale
    nevadí je mít vyplněné. Aktualizuje VÝHRADNĚ typ_parkovani, nic
    jiného nepřepisuje.
    Prvotní backfill (2026-08-04: 1742 Praha, 341 Střední Čechy) proveden
    jednorázově manuálně mimo tuto funkci — viz PREDAVACI.md; tahle funkce
    je pro budoucí dohnání (např. nové nabídky importované mezitím)."""
    con = db.connect()
    radky = con.execute(
        "SELECT id, external_id FROM listings WHERE source='sreality' "
        "AND active=1 AND parkovani IN ('Ano', 'Ano 2*') "
        "AND typ_parkovani IS NULL AND detail_at IS NOT NULL LIMIT ?", (limit,)).fetchall()
    n, chyby = 0, 0
    for r in radky:
        try:
            resp = requests.get(API.format(r["external_id"]), headers=HEADERS, timeout=30)
            resp.raise_for_status()
            d = resp.json().get("result", {})
            con.execute("UPDATE listings SET typ_parkovani=? WHERE id=?",
                        (_typ_parkovani(d, d.get("advert_description")), r["id"]))
            n += 1
        except Exception as e:
            chyby += 1
            if chyby <= 3:
                print(f"chyba u {r['external_id']}: {e}")
            if chyby > 20:
                print("příliš mnoho chyb, končím")
                break
        time.sleep(0.25)
    con.commit()
    con.close()
    print(f"Doplněn typ_parkovani: {n} nabídek (chyb: {chyby}).")
    return n
