import csv
import sqlite3
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "nemovitosti.db"
PRICE_MAP_CSV = ROOT / "data" / "price_map.csv"

LISTING_SLOUPCE = [
    "source", "external_id", "url", "nazev", "dispozice", "ctvrt", "plocha_m2",
    "cena_czk", "zakladni_cena_m2", "lokalita", "stav", "rok_vystavby",
    "balkon", "parkovani", "dalsi_koef_pct", "najem_m2_mesic", "najem_priplatky_rocni",
    "lokalita_auto", "lokalita_skore", "lokalita_detail", "kraj",
]


def migruj(con):
    """Doplní nové sloupce do starších databází."""
    for sql in ("ALTER TABLE listings ADD COLUMN detail_at TEXT",
                "ALTER TABLE listings ADD COLUMN lokalita_auto TEXT",
                "ALTER TABLE listings ADD COLUMN lokalita_skore REAL",
                "ALTER TABLE listings ADD COLUMN lokalita_detail TEXT",
                # Rozpad výpočtu tržní hodnoty pro zobrazení v appce (2026-07-10)
                "ALTER TABLE valuations ADD COLUMN v_cena_mapy_m2 REAL",
                "ALTER TABLE valuations ADD COLUMN v_faktor_velikosti REAL",
                "ALTER TABLE valuations ADD COLUMN v_zakladni_cena_m2 REAL",
                "ALTER TABLE valuations ADD COLUMN v_zakladni_rucne INTEGER",
                "ALTER TABLE valuations ADD COLUMN v_koef_lokalita_pct REAL",
                "ALTER TABLE valuations ADD COLUMN v_koef_stav_pct REAL",
                "ALTER TABLE valuations ADD COLUMN v_koef_vek_pct REAL",
                "ALTER TABLE valuations ADD COLUMN v_koef_balkon_pct REAL",
                "ALTER TABLE valuations ADD COLUMN v_koef_dalsi_pct REAL",
                "ALTER TABLE valuations ADD COLUMN v_vek_pouzity REAL",
                # Watchlist (hvězdička) a skrytí nabídky z appky (2026-07-13,
                # na žádost uživatele) — čistě ruční stav, import ho NIKDY
                # nepřepíše (není v LISTING_SLOUPCE), mění se jen přes
                # scripts/aplikuj_upravu.py (GitHub Issue).
                "ALTER TABLE listings ADD COLUMN watchlist INTEGER DEFAULT 0",
                "ALTER TABLE listings ADD COLUMN skryto INTEGER DEFAULT 0",
                # Vlastnictví (Osobní/Družstevní/Státní-obecní) a stav anuity
                # u družstevních bytů (2026-07-15, na žádost uživatele) —
                # dotahuje se v import_detaily z pole `ownership` a textovým
                # rozborem `advert_description` (pole `annuity` ze Sreality
                # samo o sobě není spolehlivé, viz PREDAVACI.md).
                "ALTER TABLE listings ADD COLUMN vlastnictvi TEXT",
                "ALTER TABLE listings ADD COLUMN anuita_stav TEXT",
                # Doplňkové informační sloupce z detailu Sreality (2026-07-15,
                # na žádost uživatele) — jen zobrazení/filtr v appce, NEVSTUPUJÍ
                # do oceňovacího vzorce (ten je cornerstone, viz valuation.py).
                "ALTER TABLE listings ADD COLUMN energeticky_stitek TEXT",
                "ALTER TABLE listings ADD COLUMN patro INTEGER",
                "ALTER TABLE listings ADD COLUMN pater_celkem INTEGER",
                "ALTER TABLE listings ADD COLUMN vytah TEXT",
                "ALTER TABLE listings ADD COLUMN sklep INTEGER",
                "ALTER TABLE listings ADD COLUMN sklep_m2 REAL",
                "ALTER TABLE listings ADD COLUMN zahrada_m2 REAL",
                "ALTER TABLE listings ADD COLUMN typ_stavby TEXT",
                # datum_vlozeni = skutečné datum zveřejnění na Sreality (pole
                # `since`), na rozdíl od first_seen, což je jen okamžik, kdy si
                # toho poprvé všiml náš vlastní import.
                "ALTER TABLE listings ADD COLUMN datum_vlozeni TEXT",
                # Vedlejší plochy (2026-07-27, na žádost uživatele) — ze
                # strukturovaných polí Sreality floor_area/terrace_area/
                # loggia_area/balcony_area, KDYŽ je realitka vyplnila (jinak
                # zůstávají NULL, žádné odhadování z volného textu popisu).
                # plocha_cista_m2 = floor_area, používá se jen když je MENŠÍ
                # než plocha_m2 (Užitná plocha) — signál, že Užitná plocha
                # v sobě má i vedlejší prostory. Viz valuation.py
                # (jadro_a_vedlejsi_plocha, VAHA_TERASA_LODZIE_BALKON).
                "ALTER TABLE listings ADD COLUMN plocha_cista_m2 REAL",
                "ALTER TABLE listings ADD COLUMN terasa_m2 REAL",
                "ALTER TABLE listings ADD COLUMN lodzie_m2 REAL",
                "ALTER TABLE listings ADD COLUMN balkon_m2 REAL",
                # Postoupení smlouvy s developerem na splátky (2026-07-27,
                # schváleno uživatelem) — textová heuristika na "postoupen*"
                # v popisu inzerátu. Inzerovaná cena bývá jen 1. splátka,
                # NE celková cena — čistě informační red-flag pole, do
                # oceňovacího vzorce nevstupuje (cena_czk se tiše nemění).
                "ALTER TABLE listings ADD COLUMN postoupeni_stav TEXT",
                # Surová hodnota stavu ze Sreality (Novostavba/Projekt/
                # Ve výstavbě/Velmi dobrý/Dobrý/Špatný/Před rekonstrukcí/
                # Po rekonstrukci...) — na žádost uživatele 2026-07-28, jako
                # filtr na Sreality.cz. Čistě informační/filtrovací, NEVSTUPUJE
                # do vzorce — ten dál používá jen zjednodušené `stav` (viz
                # valuation.py, cornerstone beze změny).
                "ALTER TABLE listings ADD COLUMN stav_sreality TEXT",
                # Rozpad efektivní plochy pro zobrazení v appce (2026-07-27,
                # nahrazuje v_koef_balkon_pct — viz valuation.py).
                "ALTER TABLE valuations ADD COLUMN v_plocha_jadro REAL",
                "ALTER TABLE valuations ADD COLUMN v_plocha_vedlejsi_m2 REAL",
                "ALTER TABLE valuations ADD COLUMN v_plocha_zahrada_m2 REAL",
                "ALTER TABLE valuations ADD COLUMN v_plocha_efektivni REAL",
                # Rozpad příplatku parkování pro zobrazení v appce (2026-08-04,
                # viz valuation.py — typově a lokalitně rozlišený příplatek).
                "ALTER TABLE valuations ADD COLUMN v_typ_parkovani TEXT",
                "ALTER TABLE valuations ADD COLUMN v_koef_lokalita_parkovani REAL",
                # Rozšíření mimo Prahu (2026-08, na žádost uživatele) — cenová
                # mapa teď obsahuje i velká města Středočeského kraje (nad
                # 5000 obyvatel, viz scripts/stredocesky_cenova_mapa.py).
                # kraj rozlišuje řádky cenové mapy, aby si regionální scrapery
                # navzájem nepřepisovaly řádky (každý smí měnit jen svůj kraj).
                "ALTER TABLE price_map ADD COLUMN kraj TEXT",
                # listings.kraj (2026-08): ze kterého regionu import_sreality()
                # nabídku stáhl (Praha/Středočeský/...). KRITICKÉ pro bezpečnost
                # "deaktivace zmizelých nabídek" v import_sreality() — bez téhle
                # informace by import jednoho kraje mohl omylem deaktivovat
                # VŠECHNY nabídky jiného kraje (ty se logicky nemůžou objevit
                # ve výsledcích vyhledávání jiného kraje, takže by vypadaly
                # jako "zmizelé"). Viz src/sreality.py, import_sreality().
                "ALTER TABLE listings ADD COLUMN kraj TEXT",
                # Typ parkování — Garáž/Stání/Venkovní (2026-08-04, na žádost
                # uživatele, JEN PRAHA): reálný trh na Sreality má výrazně
                # jinou cenu pro samostatnou garáž (medián ~990k Kč) než pro
                # garážové stání (medián ~710k Kč) — dosavadní jediné
                # PARKOVANI_KC=400k Kč tohle nerozlišovalo. Garáž/Stání se
                # pozná ze strukturovaných polí Sreality (garage/parking_lots),
                # Venkovní JEN textovou heuristikou v popisu (Sreality to jako
                # vlastní pole nemá) — viz src/sreality_detail.py, _typ_parkovani().
                # Střední Čechy zůstávají beze změny (PARKOVANI_KC), dokud
                # uživatel neschválí totéž rozlišení i pro ten kraj.
                "ALTER TABLE listings ADD COLUMN typ_parkovani TEXT"):
        try:
            con.execute(sql)
        except sqlite3.OperationalError:
            pass

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    id INTEGER PRIMARY KEY, source TEXT NOT NULL, external_id TEXT, url TEXT,
    nazev TEXT, dispozice TEXT, ctvrt TEXT, plocha_m2 REAL, cena_czk REAL,
    zakladni_cena_m2 REAL, lokalita TEXT, stav TEXT, rok_vystavby INTEGER,
    balkon TEXT DEFAULT 'Ne', parkovani TEXT DEFAULT 'Ne', dalsi_koef_pct REAL DEFAULT 0,
    najem_m2_mesic REAL, najem_priplatky_rocni REAL DEFAULT 0,
    active INTEGER DEFAULT 1, first_seen TEXT, last_seen TEXT, kraj TEXT,
    watchlist INTEGER DEFAULT 0, skryto INTEGER DEFAULT 0,
    vlastnictvi TEXT, anuita_stav TEXT,
    energeticky_stitek TEXT, patro INTEGER, pater_celkem INTEGER, vytah TEXT,
    sklep INTEGER, sklep_m2 REAL, zahrada_m2 REAL, typ_stavby TEXT, datum_vlozeni TEXT,
    plocha_cista_m2 REAL, terasa_m2 REAL, lodzie_m2 REAL, balkon_m2 REAL,
    postoupeni_stav TEXT, stav_sreality TEXT, typ_parkovani TEXT,
    UNIQUE (source, external_id)
);
CREATE TABLE IF NOT EXISTS price_map (
    klic TEXT PRIMARY KEY, ctvrt TEXT NOT NULL, cena_za_m2_czk REAL NOT NULL,
    pocet_transakci INTEGER, najem_m2_mesic REAL, kraj TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS valuations (
    listing_id INTEGER PRIMARY KEY REFERENCES listings(id),
    koef_celkem REAL, vysledna_cena_m2 REAL, cena_za_byt REAL, priplatky_czk REAL,
    trzni_hodnota REAL, rozdil_pct REAL, sleva_pct REAL, najem_rocni REAL,
    prosty_vynos_pct REAL, celkovy_vynos_pct REAL, splatka_mesicni REAL,
    najem_mesicni REAL, pokryti_splatky_pct REAL,
    v_cena_mapy_m2 REAL, v_faktor_velikosti REAL, v_zakladni_cena_m2 REAL,
    v_zakladni_rucne INTEGER, v_koef_lokalita_pct REAL, v_koef_stav_pct REAL,
    v_koef_vek_pct REAL, v_koef_balkon_pct REAL, v_koef_dalsi_pct REAL,
    v_vek_pouzity REAL, computed_at TEXT,
    v_plocha_jadro REAL, v_plocha_vedlejsi_m2 REAL, v_plocha_zahrada_m2 REAL,
    v_plocha_efektivni REAL, v_typ_parkovani TEXT, v_koef_lokalita_parkovani REAL
);
"""

def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    con = connect()
    con.executescript(SCHEMA)
    migruj(con)
    con.commit()
    con.close()
    load_price_map()
    print(f"Databáze připravena: {DB_PATH}")

def load_price_map():
    con = connect()
    con.executescript(SCHEMA)
    try:
        con.execute("ALTER TABLE price_map ADD COLUMN najem_m2_mesic REAL")
    except sqlite3.OperationalError:
        pass  # sloupec už existuje
    try:
        con.execute("ALTER TABLE price_map ADD COLUMN kraj TEXT")
    except sqlite3.OperationalError:
        pass  # sloupec už existuje
    now = datetime.now().isoformat(timespec="seconds")
    n = 0
    with open(PRICE_MAP_CSV, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            con.execute(
                "INSERT INTO price_map (klic, ctvrt, cena_za_m2_czk, pocet_transakci, najem_m2_mesic, kraj, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(klic) DO UPDATE SET ctvrt=excluded.ctvrt, "
                "cena_za_m2_czk=excluded.cena_za_m2_czk, pocet_transakci=excluded.pocet_transakci, "
                "najem_m2_mesic=excluded.najem_m2_mesic, kraj=excluded.kraj, updated_at=excluded.updated_at",
                (row["klic"].strip(), row["ctvrt"].strip(),
                 float(row["cena_za_m2_czk"]), int(row.get("pocet_transakci") or 0),
                 float(row["najem_m2_mesic"]) if row.get("najem_m2_mesic") else None,
                 (row.get("kraj") or "Praha").strip(), now))
            n += 1
    con.commit()
    con.close()
    print(f"Cenová mapa nahrána: {n} čtvrtí.")
    return n

# Pole, která import vždy přepíše (jsou v každém výpisu čerstvá).
# Ostatní (stav, rok, balkon, parkování, nájemné…) se při UPDATE zachovají,
# pokud nová hodnota chybí — jinak by denní import mazal dotažené detaily.
PREPSAT_VZDY = {"url", "nazev", "dispozice", "ctvrt", "plocha_m2", "cena_czk",
                "lokalita_auto", "lokalita_skore", "lokalita_detail"}


def upsert_listing(con, d):
    now = datetime.now().isoformat(timespec="seconds")
    data = {k: d.get(k) for k in LISTING_SLOUPCE}
    cols = ", ".join(LISTING_SLOUPCE)
    ph = ", ".join(":" + c for c in LISTING_SLOUPCE)
    upd = ", ".join(
        f"{c}=excluded.{c}" if c in PREPSAT_VZDY else f"{c}=COALESCE(excluded.{c}, {c})"
        for c in LISTING_SLOUPCE if c not in ("source", "external_id"))
    con.execute(
        f"INSERT INTO listings ({cols}, active, first_seen, last_seen) VALUES ({ph}, 1, :now, :now) "
        f"ON CONFLICT(source, external_id) DO UPDATE SET {upd}, active=1, last_seen=excluded.last_seen",
        {**data, "now": now})
