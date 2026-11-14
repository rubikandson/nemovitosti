# Databáze trhu — denní snímek VŠECH nabídek na prodej (byty, domy, pozemky)
# ze Sreality v celé ČR, s historií: kdy se nabídka objevila, jak se měnila
# cena a kdy zmizela. Podklad pro budoucí ocenění podle adresy (srovnatelné
# nabídky v okolí, doba na trhu, zlevňování).
#
# Spouštět z kořene projektu:
#   python -m src.trh stahnout                      # vše (3 kategorie × 14 krajů)
#   python -m src.trh stahnout --kategorie dum --kraje 10 --max-stranek 2
#   python -m src.trh prehled                       # stav databáze
#   python -m src.trh souhrn                        # připíše dnešek do data/trh_souhrn.csv
#   python -m src.trh sync                          # pošle změny do Ocenito (Cloudflare D1)
#
# Záměrně ODDĚLENÉ od appky (src/db.py, src/sreality.py, src/valuation.py):
# vlastní databáze data/trh.db, žádný zásah do tabulky listings ani do
# oceňovacího modelu. Na GitHubu se trh.db neukládá do gitu (rostla by
# historie repa), ale jako příloha releasu „trh-db" (viz .github/workflows/trh.yml).
#
# Ukládají se jen fakta o nabídce (cena, plochy, poloha, typ, vzdálenosti
# k vybavenosti, které posílá API). Fotky ani texty inzerátů se NEUKLÁDAJÍ —
# na inzerát se jen odkazuje.
import argparse
import csv
import hashlib
import json
import os
import re
import sqlite3
import statistics
import sys
import time
import unicodedata
from datetime import date, datetime
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "trh.db"
SOUHRN_CSV = ROOT / "data" / "trh_souhrn.csv"

API = "https://www.sreality.cz/api/v1/estates/search"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
           "Accept": "application/json"}
LIMIT = 100
# API vrací nejvýš offset 9 900 (vyšší → HTTP 422 „too_high_offset", ověřeno
# 2026-10-31). Proto se stahuje po krajích; kraj nad tuto hranici se označí
# jako neúplný a zmizelé nabídky se v něm nedeaktivují.
MAX_OFFSET = 9_900
PAUZA_S = 1.0       # šetrnost k API
MIN_CENA = 10_000   # Sreality posílá 1 Kč u „ceny na vyžádání"

KATEGORIE = {"byt": 1, "dum": 2, "pozemek": 3}
KATEGORIE_URL = {"byt": "byt", "dum": "dum", "pozemek": "pozemek"}
# ID krajů ve vyhledávání Sreality (ověřeno dotazem na každé ID 2026-10-31;
# 15+ už nevrací nic). Pozor: liší se od REGIONY v src/sreality.py.
KRAJE = {1: "Jihočeský", 2: "Plzeňský", 3: "Karlovarský", 4: "Ústecký",
         5: "Liberecký", 6: "Královéhradecký", 7: "Pardubický", 8: "Olomoucký",
         9: "Zlínský", 10: "Praha", 11: "Středočeský", 12: "Moravskoslezský",
         13: "Vysočina", 14: "Jihomoravský"}

POI = ("school", "kindergarten", "shop", "small_shop", "medic", "bus_public_transport",
       "train", "metro", "tram", "post_office", "restaurant", "playground")

SCHEMA = """
CREATE TABLE IF NOT EXISTS nabidky (
    hash_id TEXT PRIMARY KEY,
    kategorie TEXT NOT NULL, podtyp TEXT, nazev TEXT, url TEXT,
    kraj TEXT, okres TEXT, obec TEXT, cast_obce TEXT, ulice TEXT,
    lat REAL, lon REAL, presnost_polohy TEXT,
    plocha_m2 REAL, pozemek_m2 REAL,
    cena_czk REAL, cena_m2 REAL, jednotka_ceny TEXT,
    cena_prvni REAL, pocet_zmen_ceny INTEGER DEFAULT 0,
    poi TEXT,
    datum_prvni TEXT NOT NULL, datum_posledni TEXT NOT NULL,
    aktivni INTEGER DEFAULT 1, datum_zmizeni TEXT
);
CREATE INDEX IF NOT EXISTS ix_nab_kat_kraj ON nabidky (kategorie, kraj, aktivni);
CREATE INDEX IF NOT EXISTS ix_nab_poloha ON nabidky (lat, lon);

-- typ: nova | zmena_ceny | zmizela | znovu
CREATE TABLE IF NOT EXISTS udalosti (
    id INTEGER PRIMARY KEY, hash_id TEXT NOT NULL, datum TEXT NOT NULL,
    typ TEXT NOT NULL, cena_pred REAL, cena_po REAL
);
CREATE INDEX IF NOT EXISTS ix_udal_hash ON udalosti (hash_id);

-- Jeden řádek = stažení jedné kategorie v jednom kraji. kompletni=1 jen když
-- prošly všechny stránky; jen pak se smí deaktivovat zmizelé nabídky.
CREATE TABLE IF NOT EXISTS behy (
    id INTEGER PRIMARY KEY, datum TEXT, cas_start TEXT, cas_konec TEXT,
    kategorie TEXT, kraj TEXT, celkem_api INTEGER, videno INTEGER,
    kompletni INTEGER, zmizelo INTEGER, poznamka TEXT
);
"""


def connect(cesta: Path = DB_PATH):
    cesta.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(cesta)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


# ---------------------------------------------------------------- parsování


def _m2(text: str, za: str | None = None):
    """Číslo před „m²" v názvu inzerátu; se za='pozemek' to za tímto slovem.
    „Prodej rodinného domu 100 m², pozemek 659 m²" → 100 / 659."""
    s = (text or "").replace("\xa0", " ")
    if za:
        i = s.lower().find(za)
        if i < 0:
            return None
        s = s[i + len(za):]
    # Tisíce jen jako skupiny po třech číslicích („1 250 m²“) a číslo nesmí
    # navazovat na dispozici: v „bytu 1+1 37 m²“ je plocha 37, ne 137.
    m = re.search(r"(?<![\d+.,])(\d{1,3}(?: \d{3})+|\d+(?:[.,]\d+)?)\s*m²", s)
    if not m:
        return None
    try:
        return float(m.group(1).replace(" ", "").replace(",", "."))
    except ValueError:
        return None


def _cena(e: dict):
    """Celková cena. Přednost má price_summary_czk: u pozemků s cenou „za m²"
    je price_czk jen cena za metr (např. 2 500), celková je v price_summary_czk."""
    v = e.get("price_summary_czk")
    if not v and (e.get("price_unit_cb") or {}).get("name") == "za nemovitost":
        v = e.get("price_czk")
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return v if v >= MIN_CENA else None


def _url(e: dict, kategorie: str, podtyp: str | None, sub_id, hash_id: str) -> str:
    """Stejné skládání URL jako src/sreality.py::_detail_url (Sreality od 2026
    vyžaduje reálný slug, placeholder vrací 404)."""
    loc = e.get("locality") or {}
    casti = [loc.get("city_seo_name"), loc.get("citypart_seo_name"), loc.get("street_seo_name")]
    slug = "-".join(c for c in casti if c) or "x"
    sub = SUB_SLUG.get(sub_id) or _slug(podtyp or "x")
    return f"https://www.sreality.cz/detail/prodej/{KATEGORIE_URL[kategorie]}/{sub}/{slug}/{hash_id}"


def _slug(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9+]+", "-", s).strip("-") or "x"


# Podtypy, u kterých se slug v URL neshoduje s názvem (ověřeno 2026-10-31
# proti sreality.cz — ostatní fungují jako _slug(název), např. „Na klíč" →
# na-klic). Část URL s lokalitou si Sreality samo přesměruje na správnou.
SUB_SLUG = {35: "pamatka", 21: "les", 22: "louka", 23: "zahrada",
            24: "ostatni-pozemky", 46: "rybnik"}


def zaznam(e: dict, kategorie: str, kraj: str) -> dict:
    """Z položky výsledků vyhledávání udělá řádek tabulky nabidky. Kraj se bere
    z hledaného segmentu (ne z locality.region), aby seděl s deaktivací."""
    hash_id = str(e.get("hash_id"))
    nazev = e.get("advert_name") or ""
    podtyp = (e.get("category_sub_cb") or {}).get("name")
    loc = e.get("locality") or {}
    plocha = _m2(nazev)
    pozemek = _m2(nazev, za="pozemek")
    if kategorie == "pozemek":   # u pozemku je jediné číslo výměra pozemku
        pozemek, plocha = plocha, None
    poi = {p: e.get(f"poi_{p}_distance") for p in POI if e.get(f"poi_{p}_distance") is not None}
    return {
        "hash_id": hash_id, "kategorie": kategorie, "podtyp": podtyp, "nazev": nazev,
        "url": _url(e, kategorie, podtyp, (e.get("category_sub_cb") or {}).get("value"), hash_id),
        "kraj": kraj,
        "okres": loc.get("district"), "obec": loc.get("city"),
        "cast_obce": loc.get("citypart"), "ulice": loc.get("street") or None,
        "lat": loc.get("gps_lat"), "lon": loc.get("gps_lon"),
        "presnost_polohy": loc.get("inaccuracy_type"),
        "plocha_m2": plocha, "pozemek_m2": pozemek,
        "cena_czk": _cena(e), "cena_m2": e.get("price_czk_m2") or None,
        "jednotka_ceny": (e.get("price_unit_cb") or {}).get("name"),
        "poi": ",".join(f"{k}:{v}" for k, v in poi.items()) or None,
    }


# ---------------------------------------------------------------- zápis


SLOUPCE = ["kategorie", "podtyp", "nazev", "url", "kraj", "okres", "obec", "cast_obce",
           "ulice", "lat", "lon", "presnost_polohy", "plocha_m2", "pozemek_m2",
           "cena_czk", "cena_m2", "jednotka_ceny", "poi"]


def uloz(con, z: dict, dnes: str) -> str:
    """Vloží nebo aktualizuje nabídku a zapíše událost. Vrací typ změny
    („nova", „zmena_ceny", „znovu", nebo "" když se nic podstatného nestalo)."""
    stara = con.execute("SELECT cena_czk, aktivni FROM nabidky WHERE hash_id=?",
                        (z["hash_id"],)).fetchone()
    if stara is None:
        con.execute(
            f"INSERT INTO nabidky (hash_id, {', '.join(SLOUPCE)}, cena_prvni, "
            f"datum_prvni, datum_posledni, aktivni) VALUES (:hash_id, "
            f"{', '.join(':' + c for c in SLOUPCE)}, :cena_czk, :dnes, :dnes, 1)",
            {**z, "dnes": dnes})
        con.execute("INSERT INTO udalosti (hash_id, datum, typ, cena_po) VALUES (?, ?, 'nova', ?)",
                    (z["hash_id"], dnes, z["cena_czk"]))
        return "nova"

    typ = ""
    if not stara["aktivni"]:
        typ = "znovu"
        con.execute("INSERT INTO udalosti (hash_id, datum, typ, cena_pred, cena_po) "
                    "VALUES (?, ?, 'znovu', ?, ?)", (z["hash_id"], dnes, stara["cena_czk"], z["cena_czk"]))
    zmena = (z["cena_czk"] is not None and stara["cena_czk"] is not None
             and abs(z["cena_czk"] - stara["cena_czk"]) >= 1)
    if zmena:
        typ = typ or "zmena_ceny"
        con.execute("INSERT INTO udalosti (hash_id, datum, typ, cena_pred, cena_po) "
                    "VALUES (?, ?, 'zmena_ceny', ?, ?)", (z["hash_id"], dnes, stara["cena_czk"], z["cena_czk"]))
    # Cena „na vyžádání" (None) nepřepíše poslední známou cenu.
    upd = ", ".join(f"{c}=COALESCE(:{c}, {c})" if c == "cena_czk" else f"{c}=:{c}" for c in SLOUPCE)
    con.execute(
        f"UPDATE nabidky SET {upd}, datum_posledni=:dnes, aktivni=1, datum_zmizeni=NULL, "
        f"pocet_zmen_ceny=pocet_zmen_ceny+:zmena WHERE hash_id=:hash_id",
        {**z, "dnes": dnes, "zmena": int(zmena)})
    return typ


# ---------------------------------------------------------------- stahování


def _stranka(params: dict, offset: int, pokusu: int = 3) -> dict:
    for i in range(pokusu):
        try:
            r = requests.get(API, params={**params, "limit": LIMIT, "offset": offset},
                             headers=HEADERS, timeout=30)
            if r.status_code == 200:
                return r.json()
            print(f"  HTTP {r.status_code} (offset {offset}): {r.text[:150]}")
        except requests.RequestException as ex:
            print(f"  chyba sítě (offset {offset}): {ex}")
        time.sleep(5 * (i + 1))
    raise RuntimeError(f"stránka offset {offset} se nepodařila stáhnout")


def stahni_segment(con, kategorie: str, kraj_id: int, dnes: str, max_stranek: int | None = None) -> dict:
    """Stáhne jednu kategorii v jednom kraji a zapíše změny.
    Zmizelé nabídky deaktivuje jen po kompletním průchodu."""
    params = {"category_main_cb": KATEGORIE[kategorie], "category_type_cb": 1,
              "locality_region_id": kraj_id}
    kraj = KRAJE[kraj_id]
    start = datetime.now().isoformat(timespec="seconds")
    videne, stat = set(), {"nova": 0, "zmena_ceny": 0, "znovu": 0}
    celkem, offset, poznamka, kompletni = None, 0, "", False
    try:
        while True:
            data = _stranka(params, offset)
            if celkem is None:
                celkem = (data.get("pagination") or {}).get("total") or 0
            vysledky = data.get("results") or []
            for e in vysledky:
                z = zaznam(e, kategorie, kraj)
                videne.add(z["hash_id"])
                t = uloz(con, z, dnes)
                if t:
                    stat[t] += 1
            offset += LIMIT
            if not vysledky or offset >= celkem:
                kompletni = True
                break
            if offset > MAX_OFFSET:
                poznamka = f"nad limit API ({celkem} nabídek, staženo {len(videne)})"
                break
            if max_stranek and offset >= max_stranek * LIMIT:
                poznamka = f"omezeno na {max_stranek} stránek"
                break
            time.sleep(PAUZA_S)
    except RuntimeError as ex:
        poznamka = str(ex)

    zmizelo = 0
    # Pojistka proti výpadku API: deaktivovat jen když průchod doběhl a viděl
    # aspoň 90 % toho, co API hlásí (stejný princip jako src/sreality.py).
    if kompletni and celkem and len(videne) >= 0.9 * celkem:
        tmp = "_videne"
        con.execute(f"CREATE TEMP TABLE IF NOT EXISTS {tmp} (h TEXT PRIMARY KEY)")
        con.execute(f"DELETE FROM {tmp}")
        con.executemany(f"INSERT OR IGNORE INTO {tmp} VALUES (?)", ((h,) for h in videne))
        mizi = [r[0] for r in con.execute(
            f"SELECT hash_id FROM nabidky WHERE aktivni=1 AND kategorie=? AND kraj=? "
            f"AND hash_id NOT IN (SELECT h FROM {tmp})", (kategorie, kraj))]
        for h in mizi:
            cena = con.execute("SELECT cena_czk FROM nabidky WHERE hash_id=?", (h,)).fetchone()[0]
            con.execute("UPDATE nabidky SET aktivni=0, datum_zmizeni=? WHERE hash_id=?", (dnes, h))
            con.execute("INSERT INTO udalosti (hash_id, datum, typ, cena_pred) VALUES (?, ?, 'zmizela', ?)",
                        (h, dnes, cena))
        zmizelo = len(mizi)
    elif kompletni:
        poznamka = poznamka or f"viděno jen {len(videne)} z {celkem}, deaktivace přeskočena"
        kompletni = False

    con.execute("INSERT INTO behy (datum, cas_start, cas_konec, kategorie, kraj, celkem_api, videno, "
                "kompletni, zmizelo, poznamka) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (dnes, start, datetime.now().isoformat(timespec="seconds"), kategorie, kraj,
                 celkem, len(videne), int(kompletni), zmizelo, poznamka or None))
    con.commit()
    print(f"{kategorie:8} {kraj:16} API {celkem or 0:5}  staženo {len(videne):5}  "
          f"nové {stat['nova']:4}  změna ceny {stat['zmena_ceny']:4}  znovu {stat['znovu']:3}  "
          f"zmizelo {zmizelo:4}  {'OK' if kompletni else 'NEÚPLNÉ: ' + poznamka}")
    return {"kompletni": kompletni, "videno": len(videne)}


def stahnout(kategorie=None, kraje=None, max_stranek=None) -> int:
    con = connect()
    dnes = date.today().isoformat()
    vysledky = []
    for kat in kategorie or KATEGORIE:
        for k in kraje or KRAJE:
            vysledky.append(stahni_segment(con, kat, k, dnes, max_stranek))
    con.close()
    neuplne = sum(not v["kompletni"] for v in vysledky)
    print(f"Hotovo: {len(vysledky)} segmentů, neúplných {neuplne}.")
    # Celé stažení selhalo (nic neviděno) → nenulový návratový kód, ať to
    # GitHub Actions ukáže jako chybu.
    return 1 if sum(v["videno"] for v in vysledky) == 0 else 0


# ---------------------------------------------------------------- přehledy


def prehled():
    con = connect()
    print("Aktivní nabídky podle kategorie:")
    for r in con.execute("SELECT kategorie, SUM(aktivni) a, COUNT(*) c FROM nabidky GROUP BY kategorie"):
        print(f"  {r['kategorie']:8} aktivních {r['a']:6}  celkem v historii {r['c']:6}")
    print("Události:")
    for r in con.execute("SELECT typ, COUNT(*) c, MIN(datum) od, MAX(datum) do FROM udalosti GROUP BY typ"):
        print(f"  {r['typ']:11} {r['c']:7}  ({r['od']} – {r['do']})")
    r = con.execute("SELECT datum, SUM(kompletni) k, COUNT(*) c FROM behy GROUP BY datum ORDER BY datum DESC LIMIT 1").fetchone()
    if r:
        print(f"Poslední běh {r['datum']}: kompletních segmentů {r['k']} z {r['c']}")
    con.close()


SOUHRN_SLOUPCE = ["datum", "kategorie", "kraj", "aktivnich", "novych", "zmizelo",
                  "zlevneno", "zdrazeno", "median_cena", "median_cena_m2"]


def souhrn(dnes: str | None = None):
    """Připíše denní agregát (kategorie × kraj) do data/trh_souhrn.csv.
    Malý textový soubor, který se commituje do gitu — vývoj trhu je tak vidět
    i bez databáze. Stejný den se nepřipisuje dvakrát."""
    dnes = dnes or date.today().isoformat()
    con = connect()
    radky = []
    for kat in KATEGORIE:
        for kraj in KRAJE.values():
            aktivni = con.execute("SELECT cena_czk, cena_m2 FROM nabidky WHERE aktivni=1 AND kategorie=? AND kraj=?",
                                  (kat, kraj)).fetchall()
            ud = dict(con.execute(
                "SELECT u.typ, COUNT(*) FROM udalosti u JOIN nabidky n USING (hash_id) "
                "WHERE u.datum=? AND n.kategorie=? AND n.kraj=? GROUP BY u.typ", (dnes, kat, kraj)).fetchall())
            dolu = con.execute(
                "SELECT COUNT(*) FROM udalosti u JOIN nabidky n USING (hash_id) WHERE u.datum=? AND u.typ='zmena_ceny' "
                "AND u.cena_po < u.cena_pred AND n.kategorie=? AND n.kraj=?", (dnes, kat, kraj)).fetchone()[0]
            ceny = [r[0] for r in aktivni if r[0]]
            m2 = [r[1] for r in aktivni if r[1]]
            radky.append({
                "datum": dnes, "kategorie": kat, "kraj": kraj, "aktivnich": len(aktivni),
                "novych": ud.get("nova", 0), "zmizelo": ud.get("zmizela", 0),
                "zlevneno": dolu, "zdrazeno": ud.get("zmena_ceny", 0) - dolu,
                "median_cena": round(statistics.median(ceny)) if ceny else "",
                "median_cena_m2": round(statistics.median(m2)) if m2 else "",
            })
    con.close()
    existuje = SOUHRN_CSV.exists()
    if existuje:
        with open(SOUHRN_CSV, encoding="utf-8") as f:
            if any(r.get("datum") == dnes for r in csv.DictReader(f)):
                print(f"Souhrn za {dnes} už v {SOUHRN_CSV.name} je, nepřipisuji.")
                return
    with open(SOUHRN_CSV, "a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=SOUHRN_SLOUPCE)
        if not existuje:
            w.writeheader()
        w.writerows(radky)
    print(f"Souhrn za {dnes}: {len(radky)} řádků připsáno do {SOUHRN_CSV.name}.")


# ---------------------------------------------------------------- Ocenito (D1)


SYNC_POLE = ["hash_id", "kategorie", "podtyp", "nazev", "url", "obec", "cast_obce", "ulice",
             "lat", "lon", "plocha_m2", "pozemek_m2", "cena_czk", "cena_prvni", "pocet_zmen_ceny",
             "datum_prvni", "datum_zmizeni", "aktivni"]
SYNC_DAVKA = 500


def sync(max_radku: int = 40_000) -> int:
    """Pošle do Ocenito (Cloudflare D1, endpoint /api/sync) jen nabídky, které
    se od minulého odeslání změnily. Co už bylo odesláno, eviduje tabulka d1_stav
    (otisk polí). D1 zdarma dovoluje 100 000 zapsaných řádků denně a každá
    nabídka stojí ~2 zápisy (řádek + index), proto strop max_radku — zbytek
    se pošle při dalším běhu. datum_posledni se neposílá, mění se každý den."""
    url = os.environ.get("OCENITO_SYNC_URL", "https://ocenito.pages.dev/api/sync")
    klic = os.environ.get("OCENITO_SYNC_KLIC")
    if not klic:
        print("Chybí OCENITO_SYNC_KLIC — synchronizaci přeskakuji.")
        return 1
    con = connect()
    con.execute("CREATE TABLE IF NOT EXISTS d1_stav (hash_id TEXT PRIMARY KEY, otisk TEXT)")
    odeslano = dict(con.execute("SELECT hash_id, otisk FROM d1_stav"))
    zmeny = []
    # pozemky napřed: do Ocenito se poprvé nahrály jen byty a domy (limit zápisů D1)
    for r in con.execute(f"SELECT {', '.join(SYNC_POLE)} FROM nabidky ORDER BY kategorie != 'pozemek'"):
        d = dict(r)
        otisk = hashlib.sha1(json.dumps(d, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
        if odeslano.get(d["hash_id"]) != otisk:
            zmeny.append((d, otisk))
    celkem = len(zmeny)
    zmeny = zmeny[:max_radku]
    print(f"Změněných nabídek {celkem}, posílám {len(zmeny)}.")
    poslano = 0
    for i in range(0, len(zmeny), SYNC_DAVKA):
        davka = zmeny[i:i + SYNC_DAVKA]
        for pokus in range(3):
            try:
                r = requests.post(url, json={"radky": [d for d, _ in davka]},
                                  headers={"Authorization": f"Bearer {klic}"}, timeout=60)
                if r.status_code == 200:
                    break
                print(f"  dávka {i}: HTTP {r.status_code} {r.text[:150]}")
            except requests.RequestException as ex:
                print(f"  dávka {i}: {ex}")
            time.sleep(5 * (pokus + 1))
        else:
            print("Synchronizace přerušena, zbytek se pošle příště.")
            break
        con.executemany("INSERT OR REPLACE INTO d1_stav VALUES (?, ?)",
                        [(d["hash_id"], o) for d, o in davka])
        con.commit()
        poslano += len(davka)
    con.close()
    print(f"Odesláno {poslano} nabídek, zbývá {celkem - poslano}.")
    try:
        sync_statistik(url, klic)
    except requests.RequestException as ex:
        print(f"Statistiky obcí se nepodařilo odeslat: {ex}")
    try:
        sync_detailu(url, klic)
    except (requests.RequestException, sqlite3.Error) as ex:
        print(f"Detaily nabídek se nepodařilo odeslat: {ex}")
    try:
        sync_modelu(url, klic)
    except (requests.RequestException, sqlite3.Error, ValueError) as ex:
        print(f"Model trhu se nepodařilo odeslat: {ex}")
    try:
        sync_prilezitosti(url, klic)
    except (requests.RequestException, sqlite3.Error) as ex:
        print(f"Příležitosti se nepodařilo odeslat: {ex}")
    try:
        sync_sazby(url, klic)
    except (requests.RequestException, OSError, ValueError) as ex:
        print(f"Sazbu hypoték se nepodařilo odeslat: {ex}")
    return 0 if poslano == len(zmeny) else 1


MIN_NABIDEK_OBCE = 25
# Čtvrti (část obce ze Sreality) dostanou vlastní stránku jen ve velkých městech.
MIN_NABIDEK_MESTA_S_CASTMI = 300
# Podtypy pozemků, které na stránce obce počítáme jako „stavební“.
STAVEBNI_POZEMEK = ("Bydlení",)


def _slug(text: str) -> str:
    import unicodedata
    t = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", t).strip("-")


def _kvantily(hodnoty):
    h = sorted(hodnoty)
    if not h:
        return None
    k = lambda q: h[min(len(h) - 1, max(0, round(q * (len(h) - 1))))]
    return {"p25": round(k(0.25)), "median": round(k(0.5)), "p75": round(k(0.75))}


REKREACNI_DOMY = ("Chata", "Chalupa", "Zemědělská usedlost", "Památka/jiné")


def _po_budovach(rows, hodnota):
    """Jedna budova = jeden hlas: nabídky se stejnou polohou (~10 m) se sloučí
    do mediánu. Jinak by jeden developerský projekt s desítkou bytů přehlasoval
    zbytek malé obce (Vejprty: 11 bytů za 120 tis. Kč/m² v jednom domě)."""
    budovy = {}
    for x in rows:
        h = hodnota(x)
        if h:
            klic = (round(x["lat"], 4), round(x["lon"], 4)) if x["lat"] and x["lon"] else id(x)
            budovy.setdefault(klic, []).append(h)
    return [statistics.median(v) for v in budovy.values()]


def _statistika(v: list, pred30: str) -> dict:
    """Ceny z nabídek jedné obce nebo kraje (aktivní + zmizelé za 30 dní)."""
    akt = [x for x in v if x["aktivni"]]
    def m2(rows, pole):
        return _po_budovach(rows, lambda x: x["cena_czk"] / x[pole] if x["cena_czk"] and x[pole] else None)
    def ceny(rows):
        return _po_budovach(rows, lambda x: x["cena_czk"])
    byty = [x for x in akt if x["kategorie"] == "byt"]
    domy = [x for x in akt if x["kategorie"] == "dum" and x["podtyp"] not in REKREACNI_DOMY]
    pozemky = [x for x in akt if x["kategorie"] == "pozemek" and x["podtyp"] in STAVEBNI_POZEMEK]
    dispozice = {}
    for x in byty:
        dispozice.setdefault(x["podtyp"] or "jiné", []).append(x)
    polohy = [x for x in akt if x["lat"] and x["lon"]]
    stred = None
    if polohy:  # medián souřadnic je odolný proti chybně umístěným inzerátům
        stred = [round(_kvantily([x["lat"] * 1e5 for x in polohy])["median"] / 1e5, 4),
                 round(_kvantily([x["lon"] * 1e5 for x in polohy])["median"] / 1e5, 4)]
    return {
        "byty": {"pocet": len(byty), "budov": len(m2(byty, "plocha_m2")), "cena_m2": _kvantily(m2(byty, "plocha_m2")),
                 "cena": _kvantily(ceny(byty)),
                 "dispozice": {d: {"pocet": len(r), "cena": _kvantily(ceny(r)),
                                   "cena_m2": _kvantily(m2(r, "plocha_m2"))}
                               for d, r in sorted(dispozice.items()) if len(r) >= 3}},
        "domy": {"pocet": len(domy), "budov": len(ceny(domy)), "cena": _kvantily(ceny(domy)),
                 "cena_m2": _kvantily(m2(domy, "plocha_m2")),
                 "pozemek": _kvantily([x["pozemek_m2"] for x in domy if x["pozemek_m2"]])},
        "pozemky": {"pocet": len(pozemky), "budov": len(m2(pozemky, "pozemek_m2")), "cena_m2": _kvantily(m2(pozemky, "pozemek_m2"))},
        "pohyb_30_dni": {"nove": sum(1 for x in v if x["datum_prvni"] >= pred30 and x["datum_prvni"] > "2026-10-31"),
                         "zmizele": sum(1 for x in v if not x["aktivni"])},
        "aktivnich": len(akt),
        "stred": stred,
    }


def statistiky_obci() -> list[dict]:
    """Ceny podle obcí a krajů pro ocenito.cz/ceny-nemovitosti (stránky obcí
    a mapa). Jen obce s aspoň MIN_NABIDEK_OBCE aktivními nabídkami. Praha se
    počítá jako celek (v datech je rozdělená po okresech Praha 1–10).
    Stejnojmenné obce dostanou do adresy i okres. Kraje mají slug „kraj-…“."""
    con = connect()
    pred30 = date.fromordinal(date.today().toordinal() - 30).isoformat()
    radky = con.execute(
        "SELECT obec, CASE WHEN obec = 'Praha' THEN 'Praha' ELSE okres END AS okres, kraj, kategorie, podtyp, "
        "cena_czk, plocha_m2, pozemek_m2, lat, lon, aktivni, datum_prvni, datum_zmizeni FROM nabidky "
        "WHERE obec IS NOT NULL AND (aktivni = 1 OR datum_zmizeni >= ?)", (pred30,)).fetchall()
    radky_casti = con.execute(
        "SELECT obec, okres, cast_obce, kraj, kategorie, podtyp, cena_czk, plocha_m2, pozemek_m2, lat, lon, aktivni, "
        "datum_prvni, datum_zmizeni FROM nabidky WHERE obec IS NOT NULL AND cast_obce IS NOT NULL AND cast_obce != '' "
        "AND cast_obce != obec AND (aktivni = 1 OR datum_zmizeni >= ?)", (pred30,)).fetchall()
    con.close()
    obce, kraje = {}, {}
    for r in radky:
        obce.setdefault((r["obec"], r["okres"]), []).append(r)
        kraje.setdefault(r["kraj"], []).append(r)
    vybrane = {k: v for k, v in obce.items() if sum(x["aktivni"] for x in v) >= MIN_NABIDEK_OBCE}
    jmena = [o for o, _ in vybrane]
    out = []
    for (obec, okres), v in vybrane.items():
        slug = _slug(obec) if jmena.count(obec) == 1 else _slug(f"{obec} {okres}")
        out.append({"slug": slug, "obec": obec, "okres": okres, "kraj": v[0]["kraj"], "data": _statistika(v, pred30)})
    # čtvrti velkých měst: slug „praha-smichov“, v datech odkaz na město
    velka = {}
    for (obec, okres), v in vybrane.items():
        if sum(x["aktivni"] for x in v) >= MIN_NABIDEK_MESTA_S_CASTMI and jmena.count(obec) == 1:
            velka[obec] = _slug(obec)
    casti = {}
    for r in radky_casti:
        if r["obec"] in velka:
            casti.setdefault((r["obec"], r["cast_obce"]), []).append(r)
    for (obec, cast), v in casti.items():
        if sum(x["aktivni"] for x in v) < MIN_NABIDEK_OBCE:
            continue
        okresy = [x["okres"] for x in v if x["okres"]]
        okres = max(set(okresy), key=okresy.count) if okresy else None
        nazev = (cast if cast.startswith(obec) else
                 f"{cast} ({okres})" if obec == "Praha" and okres else f"{obec}-{cast}")
        data = _statistika(v, pred30)
        data["nadrazena"] = {"slug": velka[obec], "obec": obec, "cast": cast}
        out.append({"slug": _slug(cast) if cast.startswith(obec) else f"{velka[obec]}-{_slug(cast)}", "obec": nazev, "okres": okres, "kraj": v[0]["kraj"], "data": data})
    for kraj, v in kraje.items():
        if kraj:
            out.append({"slug": "kraj-" + _slug(kraj), "obec": kraj, "okres": None, "kraj": kraj,
                        "data": _statistika(v, pred30)})
    return out


def sync_statistik(url: str, klic: str) -> None:
    stat = statistiky_obci()
    for i in range(0, len(stat), 100):
        r = requests.post(url, json={"statistiky": stat[i:i + 100]},
                          headers={"Authorization": f"Bearer {klic}"}, timeout=60)
        r.raise_for_status()
    print(f"Statistiky obcí odeslány: {len(stat)}.")


# Stav nabídky (novostavba, po rekonstrukci…) zná jen appka v src/ (detaily
# nabídek z Prahy a Středních Čech v data/nemovitosti.db). Do Ocenito ho
# spolu s dalšími údaji posílá sync_detailu níže.
APP_DB = ROOT / "data" / "nemovitosti.db"
STAV_OCENITO = {
    "Novostavba": "novostavba", "Ve výstavbě": "novostavba", "Projekt": "novostavba",
    "Po rekonstrukci": "po-rekonstrukci",
    "Velmi dobrý": "dobry", "Dobrý": "dobry", "Částečně po rekonstrukci": "dobry",
    "Před rekonstrukcí": "k-rekonstrukci", "Špatný": "k-rekonstrukci", "K demolici": "k-rekonstrukci",
}


def detaily_z_appky() -> dict:
    """Údaje z detailů nabídek (Praha, Střední Čechy): stav, doplatek k ceně
    (anuita družstva), neúplná cena, postoupení smlouvy, vlastnictví."""
    if not APP_DB.exists():
        return {}
    app = sqlite3.connect(APP_DB)
    out = {}
    for hid, s, dopl, neupl, post, vl in app.execute(
            "SELECT external_id, stav_sreality, doplatek_czk, cena_neuplna, postoupeni_stav, vlastnictvi "
            "FROM listings WHERE source='sreality' AND active=1"):
        out[str(hid)] = {"stav": STAV_OCENITO.get(s), "doplatek": dopl or 0, "neuplna": bool(neupl),
                         "postoupeni": post == "ano", "druzstevni": vl == "Družstevní"}
    app.close()
    return out


# ---------------------------------------------------------------- detaily nabídek
# Stav, typ stavby, energetická třída, patro, výtah a rok výstavby z detailu
# nabídky. Pro byty v Praze a Středních Čechách je už má appka (data/
# nemovitosti.db); domy v celé ČR a byty v ostatních krajích se stahují tady,
# každá nabídka jednou, nejvýš DETAIL_LIMIT za běh (šetrně, po 1 s). Text
# popisu se jen projde kvůli stavu a neukládá se.
DETAIL_API = "https://www.sreality.cz/api/v1/estates/{}"
DETAIL_HEADERS = {"User-Agent": "Ocenito/1.0 (+https://ocenito.cz)", "Accept": "application/json"}
DETAIL_LIMIT = 2000
DETAIL_PAUZA_S = 1.0
KONSTRUKCE = {"Cihlová": "cihla", "Kamenná": "cihla", "Panelová": "panel", "Skeletová": "skelet",
              "Smíšená": "smisena", "Dřevostavba": "drevostavba", "Montovaná": "montovana", "Modulární": "montovana"}
DETAIL_SCHEMA = """
CREATE TABLE IF NOT EXISTS detaily (
    hash_id TEXT PRIMARY KEY, datum TEXT NOT NULL,
    stav TEXT, stav_zdroj TEXT, konstrukce TEXT, penb TEXT, patro INTEGER, pater INTEGER,
    vytah INTEGER, rok INTEGER, rok_rekonstrukce INTEGER, vlastnictvi TEXT, druh TEXT,
    doplatek REAL, neuplna INTEGER,
    chyba TEXT
);
"""


def _jmeno(x):
    n = (x.get("name") if isinstance(x, dict) else x) or ""
    n = str(n).strip()
    return None if not n or n.startswith("-") or n.lower().startswith("nedefinov") else n


def _int(x, lo, hi):
    try:
        v = int(x)
    except (TypeError, ValueError):
        return None
    return v if lo <= v <= hi else None


_RE_K_REK = re.compile(r"(?:určen\w*|vhodn\w*|nutn\w*|vyžaduje|čeká)\s+(?:na\s+)?(?:celkov\w*\s+|kompletn\w*\s+)?rekonstrukc"
                       r"|k\s+(?:celkové\s+|kompletní\s+)?rekonstrukci|před\s+rekonstrukcí")
_RE_PO_REK = re.compile(r"po\s+(?:kompletní|celkové|generální|nákladné|citlivé|zdařilé)?\s*rekonstrukci"
                        r"|kompletně\s+(?:zrekonstruovan|zrenovovan)")


def stav_z_detailu(d: dict):
    """Stejná pravidla jako stavZInzeratu v Ocenito (lib/inzerat.js)."""
    s = (_jmeno(d.get("building_condition")) or "").lower()
    rok = date.today().year
    if re.search(r"novostavba|ve výstavbě|projekt", s):
        return "novostavba", "inzerat"
    if s.startswith("po rekonstrukci"):
        stara = d.get("reconstruction_year") and rok - int(d["reconstruction_year"]) > 15
        return ("dobry" if stara else "po-rekonstrukci"), "inzerat"
    if re.search(r"před rekonstrukcí|špatný|k demolici|v rekonstrukci", s):
        return "k-rekonstrukci", "inzerat"
    if re.search(r"dobrý|částečně", s):
        return "dobry", "inzerat"
    t = str(d.get("advert_description") or "").lower()
    if _RE_K_REK.search(t):
        return "k-rekonstrukci", "popis"
    if _RE_PO_REK.search(t):
        return "po-rekonstrukci", "popis"
    if "novostavb" in t:
        return "novostavba", "popis"
    return None, None


def detail_zaznam(d: dict) -> dict:
    penb = (_jmeno(d.get("energy_efficiency_rating_cb")) or "")[:1]
    vytah = (_jmeno(d.get("elevator")) or "").lower()
    stav, zdroj = stav_z_detailu(d)
    rok = None
    for k in ("object_age", "acceptance_year"):
        rok = rok or _int(d.get(k), 1700, date.today().year + 5)
    fn = d.get("floor_number")
    # doplatek k ceně (anuita družstva, „celková cena“ v poznámce) a neúplná
    # cena (jen záloha / první splátka): stejný rozbor textu jako appka
    try:
        from src.sreality_detail import _doplatek
        doplatek, neuplna = _doplatek(d)
    except Exception:  # noqa: BLE001 — rozbor textu je doplněk
        doplatek, neuplna = None, 0
    return {
        "stav": stav, "stav_zdroj": zdroj,
        "konstrukce": KONSTRUKCE.get(_jmeno(d.get("building_type")) or ""),
        "penb": penb if penb in "ABCDEFG" and penb else None,
        "patro": max(0, int(fn) - 1) if isinstance(fn, (int, float)) else None,
        "pater": _int(d.get("floors"), 1, 99),
        "vytah": 1 if vytah.startswith("ano") else 0 if vytah.startswith("ne") else None,
        "rok": rok, "rok_rekonstrukce": _int(d.get("reconstruction_year"), 1800, date.today().year + 5),
        "vlastnictvi": _jmeno(d.get("ownership")), "druh": _jmeno(d.get("object_kind")),
        "doplatek": doplatek, "neuplna": int(bool(neuplna)),
    }


def stahni_detaily(limit: int = DETAIL_LIMIT) -> int:
    """Stáhne detaily nabídek, které je ještě nemají: domy (celá ČR) napřed,
    pak byty mimo Prahu a Střední Čechy; nejnovější první."""
    con = connect()
    con.executescript(DETAIL_SCHEMA)
    rows = [r[0] for r in con.execute(
        "SELECT n.hash_id FROM nabidky n LEFT JOIN detaily d ON d.hash_id = n.hash_id "
        "WHERE n.aktivni = 1 AND d.hash_id IS NULL AND (n.kategorie = 'dum' OR "
        "(n.kategorie = 'byt' AND n.kraj NOT IN ('Praha', 'Středočeský'))) "
        "ORDER BY n.kategorie = 'byt', n.datum_prvni DESC LIMIT ?", (limit,))]
    print(f"Detaily: stahuji {len(rows)} nabídek.")
    dnes, ok, chyb, blok = date.today().isoformat(), 0, 0, 0
    for i, h in enumerate(rows):
        try:
            r = requests.get(DETAIL_API.format(h), headers=DETAIL_HEADERS, timeout=20)
        except requests.RequestException as ex:
            print(f"  {h}: {ex}")
            chyb += 1
            time.sleep(5)
            continue
        if r.status_code in (403, 429):
            blok += 1
            print(f"  {h}: HTTP {r.status_code}")
            if blok >= 5:
                print("Detaily: server odmítá dotazy, končím a zbytek zkusím příště.")
                break
            time.sleep(30 * blok)
            continue
        blok = 0
        if r.status_code in (404, 410):
            con.execute("INSERT OR REPLACE INTO detaily (hash_id, datum, chyba) VALUES (?, ?, 'neexistuje')", (h, dnes))
        elif r.status_code == 200:
            j = r.json()
            z = detail_zaznam(j.get("result", j))
            con.execute(f"INSERT OR REPLACE INTO detaily (hash_id, datum, {', '.join(z)}) VALUES (?, ?, "
                        f"{', '.join('?' for _ in z)})", (h, dnes, *z.values()))
            ok += 1
        else:
            chyb += 1
            print(f"  {h}: HTTP {r.status_code}")
        if i % 100 == 99:
            con.commit()
        time.sleep(DETAIL_PAUZA_S)
    con.commit()
    zbyva = con.execute(
        "SELECT COUNT(*) FROM nabidky n LEFT JOIN detaily d ON d.hash_id = n.hash_id WHERE n.aktivni = 1 "
        "AND d.hash_id IS NULL AND (n.kategorie = 'dum' OR (n.kategorie = 'byt' AND n.kraj NOT IN ('Praha', 'Středočeský')))"
    ).fetchone()[0]
    con.close()
    print(f"Detaily: uloženo {ok}, chyb {chyb}, zbývá {zbyva}.")
    return 0


def _konstrukce_appky(typ):
    return KONSTRUKCE.get((typ or "").strip())


def detaily_vsech() -> dict:
    """hash_id → (stav, konstrukce, penb, patro, vytah, rok, doplatek, neuplna)
    z appky a z tabulky detaily."""
    out = {}
    if APP_DB.exists():
        app = sqlite3.connect(APP_DB)
        for h, s, typ, penb, fn, vyt, rok, dopl, neupl in app.execute(
                "SELECT external_id, stav_sreality, typ_stavby, energeticky_stitek, patro, vytah, rok_vystavby, "
                "doplatek_czk, cena_neuplna FROM listings WHERE source='sreality'"):
            p = (penb or "")[:1]
            out[str(h)] = (STAV_OCENITO.get(s), _konstrukce_appky(typ), p if p and p in "ABCDEFG" else None,
                           max(0, int(fn) - 1) if isinstance(fn, (int, float)) else None,
                           1 if (vyt or "").lower().startswith("ano") else 0 if (vyt or "").lower().startswith("ne") else None,
                           rok if isinstance(rok, int) and 1700 <= rok <= 2100 else None,
                           round(dopl) if dopl and dopl > 0 else None, 1 if neupl else None)
        app.close()
    con = connect()
    con.executescript(DETAIL_SCHEMA)
    for r in con.execute("SELECT hash_id, stav, konstrukce, penb, patro, vytah, rok, doplatek, neuplna "
                         "FROM detaily WHERE chyba IS NULL"):
        out.setdefault(r[0], (*r[1:8], round(r[8]) if r[8] else None, 1 if r[9] else None))
    con.close()
    return out


def sync_detailu(url: str, klic: str, max_radku: int = 30_000) -> None:
    """Pošle kraj (všech nabídek) a známé detaily. Jen změny (tabulka d1_detaily),
    nejvýš max_radku za běh kvůli dennímu limitu zápisů D1."""
    det = detaily_vsech()
    con = connect()
    con.execute("CREATE TABLE IF NOT EXISTS d1_detaily (hash_id TEXT PRIMARY KEY, otisk TEXT)")
    odeslano = dict(con.execute("SELECT hash_id, otisk FROM d1_detaily"))
    zmeny = []
    # aktivní napřed, s detailem napřed
    for h, kraj, akt in con.execute("SELECT hash_id, kraj, aktivni FROM nabidky ORDER BY aktivni DESC"):
        radek = [h, kraj, *det.get(h, (None,) * 8)]
        otisk = hashlib.sha1(json.dumps(radek, ensure_ascii=False).encode()).hexdigest()[:12]
        if odeslano.get(h) != otisk:
            zmeny.append((radek, otisk, h in det))
    zmeny.sort(key=lambda z: not z[2])
    celkem = len(zmeny)
    zmeny = zmeny[:max_radku]
    for i in range(0, len(zmeny), 1000):
        davka = zmeny[i:i + 1000]
        r = requests.post(url, json={"detaily": [z[0] for z in davka]},
                          headers={"Authorization": f"Bearer {klic}"}, timeout=90)
        r.raise_for_status()
        con.executemany("INSERT OR REPLACE INTO d1_detaily VALUES (?, ?)", [(z[0][0], z[1]) for z in davka])
        con.commit()
    con.close()
    print(f"Detaily nabídek: známých {len(det)}, změn {celkem}, odesláno {len(zmeny)}.")


# ---------------------------------------------------------------- model pro Ocenito
# Dvě věci, které Ocenito dřív bralo jako pevná čísla, se počítají z dat:
#
# 1) Sleva z nabídkové na prodejní cenu podle kraje a typu. Nabídky během
#    inzerce zlevňují; z historie (udalosti) víme, jak často a o kolik, a jak
#    dlouho nabídka v průměru visí (mizení). Očekávané zlevnění za dobu inzerce
#    + vyjednávání při podpisu (VYJEDNAVANI, odhad z praxe) = sleva. Kraje
#    s málo daty se stahují k celostátní hodnotě.
# 2) Koeficienty stavu (novostavba, po rekonstrukci, k rekonstrukci) proti
#    „dobrému stavu“: srovnání cen za m² v okolí (~1 km) a podobné velikosti.
VYJEDNAVANI = 0.02
SLEVA_MEZE = (0.02, 0.15)
KOEF_VYCHOZI = {"byt": {"k-rekonstrukci": 0.85, "dobry": 1.0, "po-rekonstrukci": 1.08, "novostavba": 1.15},
                "dum": {"k-rekonstrukci": 0.85, "dobry": 1.0, "po-rekonstrukci": 1.08, "novostavba": 1.15}}
KOEF_MEZE = {"k-rekonstrukci": (0.6, 0.97), "po-rekonstrukci": (1.0, 1.25), "novostavba": (1.0, 1.45)}


def _slevy(con) -> dict:
    dnes = date.today()
    expo, zmizelo = {}, {}
    for kat, kraj, prvni, posl, zmiz in con.execute(
            "SELECT kategorie, kraj, datum_prvni, datum_posledni, datum_zmizeni FROM nabidky"):
        konec = zmiz or posl or dnes.isoformat()
        dni = max(1, (date.fromisoformat(konec[:10]) - date.fromisoformat(prvni[:10])).days)
        for k in ((kat, kraj), (kat, "_")):
            expo[k] = expo.get(k, 0) + dni
            if zmiz:
                zmizelo[k] = zmizelo.get(k, 0) + 1
    rezy = {}
    for kat, kraj, pred, po in con.execute(
            "SELECT n.kategorie, n.kraj, u.cena_pred, u.cena_po FROM udalosti u JOIN nabidky n USING (hash_id) "
            "WHERE u.typ = 'zmena_ceny' AND u.cena_po < u.cena_pred AND u.cena_pred > 0"):
        r = 1 - po / pred
        if 0.005 <= r <= 0.4:   # větší „slevy“ bývají oprava chyby v ceně
            for k in ((kat, kraj), (kat, "_")):
                rezy.setdefault(k, []).append(r)
    out = {}
    for k in sorted(expo, key=lambda k: k[1] != "_"):   # celostátní napřed
        e, z, rz = expo[k], zmizelo.get(k, 0), rezy.get(k, [])
        if not z or not rz:
            continue
        doba = min(365, max(45, e / z))               # průměrná doba inzerce (dní)
        rezu = min(2.0, len(rz) / e * doba)            # zlevnění za dobu inzerce
        sleva = VYJEDNAVANI + rezu * statistics.mean(rz)
        if k[1] != "_" and (k[0], "_") in out:
            w = e / (e + 20_000)                       # málo dat → blíž celostátní hodnotě
            sleva = w * sleva + (1 - w) * out[(k[0], "_")]["sleva"]
        sleva = min(SLEVA_MEZE[1], max(SLEVA_MEZE[0], sleva))
        out[k] = {"sleva": round(sleva, 4), "doba": round(doba), "zlevneni": round(rezu, 2),
                  "o_kolik": round(statistics.mean(rz), 4), "nabidek_dni": e}
    return {f"{k[0]}|{k[1]}": v for k, v in out.items()}


def _koeficienty(con, det: dict) -> dict:
    import math
    out = {}
    for kat in ("byt", "dum"):
        bunky = {}
        for h, lat, lon, pl, c in con.execute(
                "SELECT hash_id, lat, lon, plocha_m2, cena_czk FROM nabidky WHERE aktivni = 1 AND kategorie = ? "
                "AND plocha_m2 BETWEEN 20 AND 600 AND cena_czk > 300000 AND lat IS NOT NULL", (kat,)):
            d = det.get(h)
            if not d or not d[0] or d[7]:      # bez stavu nebo s neúplnou cenou
                continue
            cena = c + (d[6] or 0)
            k = (int(lat / 0.01), int(lon / 0.015), int(math.log(pl) / 0.3))
            bunky.setdefault(k, []).append((d[0], math.log(cena / pl)))
        rozdily = {}
        for v in bunky.values():
            dobre = [x for s_, x in v if s_ == "dobry"]
            if len(dobre) < 3:
                continue
            m = statistics.median(dobre)
            for s_, x in v:
                if s_ != "dobry":
                    rozdily.setdefault(s_, []).append(x - m)
        koef = dict(KOEF_VYCHOZI[kat])
        pocty = {}
        for s_, r in rozdily.items():
            n = len(r)
            w = n / (n + 200)                          # málo dat → blíž výchozí hodnotě
            k = math.exp(w * statistics.median(r) + (1 - w) * math.log(KOEF_VYCHOZI[kat][s_]))
            lo, hi = KOEF_MEZE[s_]
            koef[s_] = round(min(hi, max(lo, k)), 3)
            pocty[s_] = n
        out[kat] = {"koef": koef, "n": pocty}
    return out


def model_trhu() -> dict:
    det = detaily_vsech()
    con = connect()
    m = {"sleva": _slevy(con), "stav": _koeficienty(con, det), "datum": date.today().isoformat()}
    con.close()
    return m


def sync_modelu(url: str, klic: str) -> None:
    m = model_trhu()
    requests.post(url, json={"nastaveni": {"model": m}},
                  headers={"Authorization": f"Bearer {klic}"}, timeout=60).raise_for_status()
    s = m["sleva"]
    print("Model: sleva " + ", ".join(f"{k} {v['sleva'] * 100:.1f} %" for k, v in s.items() if k.endswith("|_"))
          + "; stav byt " + str(m["stav"]["byt"]["koef"]) + ", dům " + str(m["stav"]["dum"]["koef"]))


def presnost(url: str, klic: str, vzorku: int = 150, davka: int = 5) -> None:
    """Spustí měření přesnosti v Ocenito: po dávkách ocení náhodné nabídky,
    jako by je neviděl, a porovná s jejich cenou. Nakonec přepočítá kalibraci."""
    base = url.rsplit("/api/", 1)[0] + "/api/presnost"
    hotovo, chyb = 0, 0
    while hotovo < vzorku and chyb < 5:
        try:
            r = requests.post(base, json={"pocet": davka}, headers={"Authorization": f"Bearer {klic}"}, timeout=60)
            r.raise_for_status()
            hotovo += max(1, r.json().get("ulozeno", 0))
        except requests.RequestException as ex:
            chyb += 1
            print(f"  přesnost: {ex}")
            time.sleep(10)
    r = requests.post(base, json={"kalibrovat": True}, headers={"Authorization": f"Bearer {klic}"}, timeout=60)
    r.raise_for_status()
    print(f"Přesnost: změřeno {hotovo} nabídek, kalibrace {r.json().get('kalibrace')}.")


# ---------------------------------------------------------------- příležitosti
# Nabídky výrazně pod cenou podobných nabídek v okolí (pro admin Ocenito).
# Cena za m² se u nabídek se známým stavem přepočte na „dobrý stav" (stejné
# koeficienty jako lib/odhad.js v Ocenito), srovnává se s mediánem budov
# v okolí podobné velikosti (jedna budova = jeden hlas). Velké „slevy" bývají
# často chyba v inzerátu (podíl, dražba, plocha), proto filtry a strop slevy.
KOEF_STAVU = {"k-rekonstrukci": 0.85, "dobry": 1.0, "po-rekonstrukci": 1.08, "novostavba": 1.15}
PRIL_MAX = 500
PRIL_MIN_SLEVA, PRIL_MAX_SLEVA = 0.15, 0.45
PRIL_OVERIT = 0.35   # nad touto slevou bývá důvod v inzerátu (podíl, záloha, chyba plochy)
PRIL_MIN_BUDOV = 8
PRIL_PODEZRELE = re.compile(r"podíl|dražb|aukc|pronáj|rezervac|garáž|komerč|ateliér", re.I)
# (min., max. velikost, min. cena, okruh km)
PRIL_MEZE = {"byt": (15, 300, 300_000, 2.5), "dum": (40, 800, 500_000, 6), "pozemek": (300, 20_000, 150_000, 6)}
ZACATEK_HISTORIE = "2026-10-31"


def _km(a, b):
    import math
    dy = (a["lat"] - b["lat"]) * 111.32
    dx = (a["lon"] - b["lon"]) * 111.32 * math.cos(math.radians(a["lat"]))
    return math.hypot(dx, dy)


def prilezitosti(dnes: str | None = None) -> list[dict]:
    import math
    dnes = dnes or time.strftime("%Y-%m-%d")
    detaily = detaily_z_appky()
    con = connect()
    rows = [dict(r) for r in con.execute(
        "SELECT hash_id, kategorie, podtyp, nazev, url, kraj, okres, obec, cast_obce, ulice, lat, lon, "
        "plocha_m2, pozemek_m2, cena_czk, cena_prvni, pocet_zmen_ceny, datum_prvni FROM nabidky "
        "WHERE aktivni = 1 AND lat IS NOT NULL AND lon IS NOT NULL AND cena_czk IS NOT NULL")]
    con.close()
    # segmenty: rekreační domy zvlášť, pozemky jen stavební
    nabidky, videno = [], set()
    for x in rows:
        k = x["kategorie"]
        if k not in PRIL_MEZE:
            continue
        if k == "pozemek" and x["podtyp"] not in STAVEBNI_POZEMEK:
            continue
        vel = x["pozemek_m2"] if k == "pozemek" else x["plocha_m2"]
        lo, hi, min_cena, _ = PRIL_MEZE[k]
        if not vel or not (lo <= vel <= hi):
            continue
        klic = (k, round(vel), round(x["cena_czk"] / 1000))
        if klic in videno:  # tatáž nemovitost od více realitek
            continue
        videno.add(klic)
        d = detaily.get(x["hash_id"], {})
        x["segment"] = k + ("-rekreace" if k == "dum" and x["podtyp"] in REKREACNI_DOMY else "")
        x["vel"] = vel
        x["stav"] = d.get("stav")
        x["druzstevni"] = d.get("druzstevni")
        # u družstevních bytů je v ceně často jen záloha, zbytek je doplatek (anuita)
        x["cena_celkem"] = x["cena_czk"] + (d.get("doplatek") or 0)
        x["m2"] = x["cena_celkem"] / vel / KOEF_STAVU.get(x["stav"], 1.0)
        x["ok"] = (x["cena_czk"] >= min_cena and not PRIL_PODEZRELE.search(x["nazev"] or "")
                   and not d.get("neuplna") and not d.get("postoupeni"))
        nabidky.append(x)
    # mřížka ~2,2 km
    mrizka = {}
    for x in nabidky:
        mrizka.setdefault((x["segment"], int(x["lat"] / 0.02), int(x["lon"] / 0.03)), []).append(x)
    vysledek = []
    for x in nabidky:
        if not x["ok"]:
            continue
        r = PRIL_MEZE[x["kategorie"]][3]
        n = math.ceil(r / 2.2)
        ci, cj = int(x["lat"] / 0.02), int(x["lon"] / 0.03)
        budovy = {}
        for i in range(ci - n, ci + n + 1):
            for j in range(cj - n, cj + n + 1):
                for y in mrizka.get((x["segment"], i, j), ()):
                    if y is x or not (0.67 <= y["vel"] / x["vel"] <= 1.5):
                        continue
                    if abs(y["lat"] - x["lat"]) < 1e-4 and abs(y["lon"] - x["lon"]) < 1e-4:
                        continue  # stejný dům nebo projekt se nesrovnává sám se sebou
                    if _km(x, y) > r:
                        continue
                    budovy.setdefault((round(y["lat"], 4), round(y["lon"], 4)), []).append(y["m2"])
        if len(budovy) < PRIL_MIN_BUDOV:
            continue
        med = statistics.median(statistics.median(v) for v in budovy.values())
        sleva = 1 - x["m2"] / med
        if not (PRIL_MIN_SLEVA <= sleva <= PRIL_MAX_SLEVA):
            continue
        zlevneni = (1 - x["cena_czk"] / x["cena_prvni"]) if x["cena_prvni"] and x["cena_prvni"] > x["cena_czk"] else 0
        dni = None
        if x["datum_prvni"] and x["datum_prvni"] > ZACATEK_HISTORIE:
            dni = (time.mktime(time.strptime(dnes, "%Y-%m-%d")) - time.mktime(time.strptime(x["datum_prvni"][:10], "%Y-%m-%d"))) / 86400
        jistota = min(1.0, 0.7 + len(budovy) / 50)
        # nad PRIL_OVERIT skóre zase klesá: tak velká „sleva" bývá spíš chyba v inzerátu
        body_sleva = sleva * 100 if sleva <= PRIL_OVERIT else PRIL_OVERIT * 100 - (sleva - PRIL_OVERIT) * 150
        skore = (body_sleva + zlevneni * 60 + 2 * min(x["pocet_zmen_ceny"] or 0, 5)) * jistota
        odhad = med * KOEF_STAVU.get(x["stav"], 1.0) * x["vel"]
        vysledek.append({
            "hash_id": x["hash_id"], "kategorie": x["kategorie"], "podtyp": x["podtyp"], "nazev": x["nazev"], "url": x["url"],
            "kraj": x["kraj"], "okres": x["okres"], "obec": x["obec"], "cast_obce": x["cast_obce"], "ulice": x["ulice"],
            "lat": round(x["lat"], 5), "lon": round(x["lon"], 5), "plocha_m2": x["plocha_m2"], "pozemek_m2": x["pozemek_m2"],
            "cena": round(x["cena_czk"]), "doplatek": round(x["cena_celkem"] - x["cena_czk"]) or None,
            "druzstevni": x["druzstevni"], "overit": sleva > PRIL_OVERIT, "cena_prvni": round(x["cena_prvni"]) if x["cena_prvni"] else None,
            "zmen_ceny": x["pocet_zmen_ceny"] or 0, "datum_prvni": x["datum_prvni"], "dni": round(dni) if dni is not None else None,
            "stav": x["stav"], "odhad": round(odhad, -4), "sleva": round(sleva * 100, 1), "zlevneni": round(zlevneni * 100, 1),
            "budov": len(budovy), "skore": round(skore, 1),
        })
    vysledek.sort(key=lambda v: -v["skore"])
    return vysledek[:PRIL_MAX]


def sync_sazby(url: str, klic: str) -> None:
    """Poslední průměrná sazba nových hypoték (data/sazba_hypoteky.csv, měsíčně
    z ČBA Hypomonitoru) pro hypoteční kalkulačku Ocenito."""
    cesta = ROOT / "data" / "sazba_hypoteky.csv"
    if not cesta.exists():
        return
    radky = list(csv.DictReader(cesta.open(encoding="utf-8")))
    if not radky:
        return
    r = radky[-1]
    sazba = {"sazba": float(r["sazba_pct"]), "datum": r["datum_aktualizace"], "zdroj": r.get("zdroj")}
    requests.post(url, json={"nastaveni": {"sazba_hypoteky": sazba}},
                  headers={"Authorization": f"Bearer {klic}"}, timeout=30).raise_for_status()
    print(f"Sazba hypoték odeslána: {sazba['sazba']} % ({sazba['datum']}).")


def sync_prilezitosti(url: str, klic: str) -> None:
    p = prilezitosti()
    r = requests.post(url, json={"prilezitosti": p, "den": time.strftime("%Y-%m-%d")},
                      headers={"Authorization": f"Bearer {klic}"}, timeout=90)
    r.raise_for_status()
    print(f"Příležitosti odeslány: {len(p)}.")


def main():
    ap = argparse.ArgumentParser(prog="trh", description="Databáze trhu nemovitostí (Sreality, celá ČR)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("stahnout", help="stáhne aktuální nabídky a zapíše změny")
    p.add_argument("--kategorie", nargs="+", choices=list(KATEGORIE))
    p.add_argument("--kraje", nargs="+", type=int, choices=list(KRAJE),
                   help="ID krajů: " + ", ".join(f"{k}={v}" for k, v in KRAJE.items()))
    p.add_argument("--max-stranek", type=int, help="jen pro test: max. stránek na segment")
    sub.add_parser("prehled", help="vypíše stav databáze")
    sub.add_parser("souhrn", help="připíše dnešní agregát do data/trh_souhrn.csv")
    p = sub.add_parser("sync", help="pošle změněné nabídky do Ocenito (Cloudflare D1)")
    p.add_argument("--max", type=int, default=40_000, help="nejvýš nabídek za běh (limit zápisů D1)")
    p = sub.add_parser("detaily", help="stáhne detaily nabídek (stav, typ stavby…), které ještě chybí")
    p.add_argument("--limit", type=int, default=DETAIL_LIMIT)
    sub.add_parser("model", help="vypíše slevu podle krajů a koeficienty stavu, které jdou do Ocenito")
    p = sub.add_parser("presnost", help="spustí měření přesnosti ocenění v Ocenito")
    p.add_argument("--vzorku", type=int, default=150)
    p = sub.add_parser("prilezitosti", help="vypíše nebo uloží nabídky výrazně pod cenou okolí")
    p.add_argument("--json", help="uložit do souboru JSON")
    a = ap.parse_args()
    if a.cmd == "stahnout":
        sys.exit(stahnout(a.kategorie, a.kraje, a.max_stranek))
    elif a.cmd == "prehled":
        prehled()
    elif a.cmd == "souhrn":
        souhrn()
    elif a.cmd == "sync":
        sys.exit(sync(a.max))
    elif a.cmd == "detaily":
        sys.exit(stahni_detaily(a.limit))
    elif a.cmd == "model":
        print(json.dumps(model_trhu(), ensure_ascii=False, indent=1))
    elif a.cmd == "presnost":
        klic = os.environ.get("OCENITO_SYNC_KLIC")
        if not klic:
            print("Chybí OCENITO_SYNC_KLIC.")
            sys.exit(1)
        presnost(os.environ.get("OCENITO_SYNC_URL", "https://ocenito.pages.dev/api/sync"), klic, a.vzorku)
    elif a.cmd == "prilezitosti":
        p = prilezitosti()
        if a.json:
            Path(a.json).write_text(json.dumps(p, ensure_ascii=False), encoding="utf-8")
        for v in p[:25]:
            print(f"{v['skore']:6.1f} {v['sleva']:5.1f} % {v['kategorie']:7} {v['obec'] or '':22.22} {v['nazev'] or '':38.38} "
                  f"{v['cena']:>11,} (odhad {v['odhad']:>11,.0f}) budov {v['budov']}")
        print(f"celkem {len(p)}")


if __name__ == "__main__":
    main()
