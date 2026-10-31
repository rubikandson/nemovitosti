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
    m = re.search(r"(\d[\d ]*(?:[.,]\d+)?)\s*m²", s)
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
    for r in con.execute(f"SELECT {', '.join(SYNC_POLE)} FROM nabidky"):
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
    return 0 if poslano == len(zmeny) else 1


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
    a = ap.parse_args()
    if a.cmd == "stahnout":
        sys.exit(stahnout(a.kategorie, a.kraje, a.max_stranek))
    elif a.cmd == "prehled":
        prehled()
    elif a.cmd == "souhrn":
        souhrn()
    elif a.cmd == "sync":
        sys.exit(sync(a.max))


if __name__ == "__main__":
    main()
