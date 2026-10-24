# Oceňovací logika — převzata 1:1 z Oceneni_Byt_InSheetTables.xlsx (list Praha).
# JEDINÉ místo v projektu, kde se počítá tržní hodnota, sleva, výnos a hypotéka.
# Písmena v komentářích = sloupce původního sheetu.
import csv
import re
from datetime import datetime

from . import db

# Nájemné z cenové mapy MFČR (Kč/m²/měsíc, po čtvrtích a dispozicích).
# Soubor data/najemne_mfcr.csv se extrahuje automaticky z mapy na mf.gov.cz.
# Schváleno uživatelem 2026-07-05: čtvrť + dispozice → hodnota MFČR;
# ručně zadané nájemné u nabídky má vždy přednost; bez dispozice se výnos nepočítá.
NAJEMNE_MFCR_CSV = db.ROOT / "data" / "najemne_mfcr.csv"
_SKUPINY = ["1+kk, 1+1", "2+kk, 2+1", "3+kk, 3+1", "4+kk, 4+1"]

ROK_OCENENI = 2025  # sheet: =min(2025-K3,80) — při aktualizaci modelu změnit

# BH5:BI7 — Koef: lokalita (%)
LOKALITA = {
    "u MHD / metro, u obchodu a služeb, tiché místo, parky v docházce": 5.0,
    "standardní dostupnost, běžná občanská vybavenost, žádné extrémy": 0.0,
    "daleko od MHD/služeb, hluk / bariéry (rušná silnice, železnice), horší pěší dostupnost": -5.0,
}

# BC4:BD6 — Koef: stav (%)
STAV = {
    "žádná / běžná údržba/novostavba": 0.0,
    "částečná rekonstrukce (kuchyň/koupelna ≤10 let)": 3.0,
    "generální rekonstrukce (≤10 let)": 6.0,
}

# AY4:AZ13 — pásma stáří: (start_věk, koef_cena %)
VEK_PASMA = [(0, 10.0), (10, 5.0), (20, 0.0), (30, -3.0), (40, -6.0),
             (50, -8.0), (60, -10.0), (70, -11.0), (80, -12.0), (90, -12.0)]

PARKOVANI_KC = 400000    # BM5 — příplatek parkování/garáž (Kč, absolutně)
# ^ Záložní hodnota, kdyby kraj nebo `typ_parkovani` chyběly (viz níže) —
#   od 2026-08-04 nahrazeno typově+lokalitně rozlišeným příplatkem pro
#   Prahu i Střední Čechy (PARKOVANI_ZAKLAD_KRAJ).

# Typově rozlišený příplatek parkování — schváleno uživatelem, zjištěno
# reálným trhem na Sreality (aktuální prodejní nabídky garáží/garážových
# stání), viz PREDAVACI.md:
#   Praha (2026-08-04): garáž medián ~990k Kč (n=86), stání medián ~710k Kč
#     (n=67) — zaokrouhleno na kulatá čísla.
#   Střední Čechy (2026-08-04): garáž medián ~699k Kč (n=55), stání medián
#     ~395k Kč (n=JEN 10 — malý vzorek, uživatel schválil zaokrouhlené 450k
#     přesto). Venkovní stání Sreality jako kategorii NEMÁ ani v jednom
#     kraji, žádná tržní data k dispozici — hodnota je uživatelův odhad
#     (Praha 500k dle uživatele; Střední Čechy 300k, taky uživatelovo číslo).
PARKOVANI_ZAKLAD_KRAJ = {
    "Praha": {"Garáž": 1_000_000, "Stání": 750_000, "Venkovní": 500_000},
    "Středočeský": {"Garáž": 700_000, "Stání": 450_000, "Venkovní": 300_000},
}

# Lokalitní koeficient příplatku parkování (schváleno uživatelem 2026-08-04,
# stejně pro oba kraje): parkování v drahé čtvrti/městě (např. Dejvice,
# Černošice) stojí víc než v levné (např. Roztoky), ale ne neomezeně —
# koeficient = cena_mapy dané čtvrti/města / průměrná cena_mapy kraje,
# OMEZENO na rozsah <0,8; 1,2>. Kraje mimo PARKOVANI_ZAKLAD_KRAJ koeficient
# nemají (zůstávají na 1,0).
PARKOVANI_LOKALITA_MIN = 0.8
PARKOVANI_LOKALITA_MAX = 1.2

# Vedlejší plochy — terasa/lodžie/balkon/zahrada (schváleno uživatelem
# 2026-07-27, viz PREDAVACI.md). NAHRAZUJE původní plochý bonus BALKON_PCT
# (+1,01 % za pouhou přítomnost balkonu bez ohledu na velikost) — ten dělal
# u bytů s velkou terasou (např. 60+ m²) tržní hodnotu absurdně nadhodnocenou,
# protože se plocha terasy počítala stejnou sazbou jako obytný prostor.
# Nově: když Sreality dá číselnou plochu (pole floor_area/terrace_area/
# loggia_area/balcony_area — ověřeno v REST API), použije se jádrová plocha
# bytu (floor_area) + vedlejší plochy se sníženou váhou. Zahrada má vlastní,
# nižší váhu (jiný typ hodnoty — pozemek, ne stavba). Sklep se do vzorce
# NEZAPOČÍTÁVÁ (zůstává jen informační pole, viz sklep_m2).
# Když strukturovaná data chybí (realitka je nevyplnila), plocha se
# NEMĚNÍ — žádné odhadování z volného textu popisu. Viz
# jadro_a_vedlejsi_plocha().
VAHA_TERASA_LODZIE_BALKON = 0.25
VAHA_ZAHRADA = 0.15

# Výnosový model (sloupce AF–AL)
RUST_NAJMU = 0.05        # AF
RUST_CENY = 0.05         # AG
OBSAZENOST_MESICU = 10   # AB = nájem × 10 („roční 10/12")
AMORTIZACE = 0.3         # AH = 0.3 × tržní × (1+AF)^10

# Hypotéka (sloupce AN–AS)
LTV = 0.8                # AO
SAZBA_ZALOZNI = 0.042    # AP — záložní hodnota, kdyby chybělo/bylo neplatné data/sazba_hypoteky.csv
SPLATNOST_MESICU = 360

# Průměrná sazba hypoték — automaticky 1× měsíčně z ČBA Hypomonitoru
# (cbamonitor.cz), schváleno uživatelem 2026-07-14 (viz scripts/cba_hypomonitor.py
# a PREDAVACI.md sekce 10). Bez souboru/při neplatné hodnotě se použije SAZBA_ZALOZNI.
SAZBA_HYPOTEKY_CSV = db.ROOT / "data" / "sazba_hypoteky.csv"


def nacti_sazbu_hypoteky():
    if SAZBA_HYPOTEKY_CSV.exists():
        try:
            with open(SAZBA_HYPOTEKY_CSV, encoding="utf-8-sig") as f:
                radek = next(csv.DictReader(f))
                sazba = float(radek["sazba_pct"]) / 100
                if 0.005 <= sazba <= 0.20:
                    return sazba
        except Exception:
            pass
    return SAZBA_ZALOZNI


SAZBA = nacti_sazbu_hypoteky()

# List „Cenová mapa", sloupce G–I: úprava základní ceny o velikost bytu
# (lineární křivka faktorů vůči průměru: 40 m2 malý, 57 střední, 75 velký)
_VELIKOST_BODY = [(40, 160150 / 145431), (57, 140682 / 145431), (75, 137215 / 145431)]


def koef_vek(vek):
    """M: lineární interpolace mezi dekádovými pásmy (vzorec ze sheetu).
    Věk < 0 (kolaudace v budoucnu) se bere jako 0 = novostavba (+10 %)."""
    vek = min(max(vek, 0), 80) + 0.00001
    dolni = max(s for s, _ in VEK_PASMA if s <= vek)
    horni = dolni + 10
    k = dict(VEK_PASMA)
    return k[dolni] * (horni - vek) / 10 + k[horni] * (vek - dolni) / 10


def faktor_velikosti(plocha):
    """Úprava základní ceny/m2 o velikost bytu (interpolace křivky ze sheetu)."""
    b = _VELIKOST_BODY
    if plocha <= b[0][0]:
        return b[0][1]
    if plocha >= b[-1][0]:
        return b[-1][1]
    for (x1, y1), (x2, y2) in zip(b, b[1:]):
        if x1 <= plocha <= x2:
            return y1 + (y2 - y1) * (plocha - x1) / (x2 - x1)
    return 1.0


def jadro_a_vedlejsi_plocha(l):
    """Rozdělí plochu nabídky na (jádrová plocha bytu, vedlejší plocha
    terasa+lodžie+balkon v m²) podle strukturovaných polí Sreality.
    `plocha_cista_m2` (floor_area) se použije jen když je MENŠÍ než
    `plocha_m2` (Užitná plocha) A zároveň je vyplněná aspoň jedna vedlejší
    plocha — to je signál, že Užitná plocha vedlejší prostory opravdu
    zahrnuje. Jinak vrátí (Užitná plocha, 0) beze změny — chybí-li
    strukturovaná data, nic se neodhaduje."""
    plocha_uzitna = l.get("plocha_m2")
    jadro = l.get("plocha_cista_m2")
    vedlejsi = (l.get("terasa_m2") or 0.0) + (l.get("lodzie_m2") or 0.0) + (l.get("balkon_m2") or 0.0)
    if jadro and plocha_uzitna and jadro < plocha_uzitna and vedlejsi > 0:
        return jadro, vedlejsi
    return plocha_uzitna, 0.0


def _irr(cena, celkem_najem, opravy, zhodnoceni):
    """AL: hledá r tak, aby AJ/(1+r)^20 + AI/(1+r)^10 − AH/(1+r)^10 − B = 0."""
    def npv(r):
        return zhodnoceni / (1 + r) ** 20 + celkem_najem / (1 + r) ** 10 \
            - opravy / (1 + r) ** 10 - cena
    lo, hi = 0.0001, 1.0
    if npv(lo) < 0:
        return None
    for _ in range(80):
        mid = (lo + hi) / 2
        if npv(mid) > 0:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def _norm(s):
    return s.strip().lower().replace(" ", "_").replace("-", "_")


def nacti_najemne_mfcr():
    """{norm(čtvrť): {skupina: Kč/m²}} z data/najemne_mfcr.csv (MFČR, beze změn)."""
    tab = {}
    if not NAJEMNE_MFCR_CSV.exists():
        return tab
    with open(NAJEMNE_MFCR_CSV, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            tab[_norm(row["ctvrt"])] = {
                sk: float(row[sk]) for sk in _SKUPINY if row.get(sk)}
    return tab


def _skupina_dispozice(dispozice):
    """Dispozice → sloupec MFČR: 1+kk/1+1→1, 2+…→2, 3+…→3, 4+ a větší→4."""
    m = re.match(r"(\d)\s*\+", str(dispozice or ""))
    if not m:
        return None
    return _SKUPINY[min(int(m.group(1)), 4) - 1]


def ocenit_nabidku(l, mapa, najemne_mfcr=None, prumer_cena_m2_kraj=None):
    """Ocení jednu nabídku dle modelu. `mapa` = {klic: cena_za_m2} z price_map,
    `najemne_mfcr` = tabulka MFČR {čtvrť: {skupina: Kč/m²}} (viz nacti_najemne_mfcr),
    `prumer_cena_m2_kraj` = {kraj: průměrná cena_za_m2_czk z price_map} — pro
    lokalitní koeficient příplatku parkování (jen Praha, viz níže)."""
    plocha_uzitna, cena = l.get("plocha_m2"), l.get("cena_czk")
    if not plocha_uzitna or not cena:
        return None

    # Efektivní plocha (schváleno 2026-07-27): jádro bytu + terasa/lodžie/
    # balkon a zahrada se sníženou váhou — viz VAHA_TERASA_LODZIE_BALKON.
    jadro, vedlejsi_m2 = jadro_a_vedlejsi_plocha(l)
    zahrada_m2 = l.get("zahrada_m2") or 0.0
    plocha = jadro + VAHA_TERASA_LODZIE_BALKON * vedlejsi_m2 + VAHA_ZAHRADA * zahrada_m2

    # F: základní cena/m2 — ručně zadaná, jinak cenová mapa × faktor velikosti
    zakladni_rucne = l.get("zakladni_cena_m2")
    cena_mapy = None
    velikost_faktor = faktor_velikosti(plocha)
    if zakladni_rucne:
        zakladni = zakladni_rucne
    else:
        cena_mapy = mapa.get(_norm(l.get("ctvrt") or ""))
        if cena_mapy is None:
            return None
        zakladni = cena_mapy * velikost_faktor

    # H: ruční lokalita má přednost; jinak kategorie z POI matice (lokalita_auto)
    k_lok = LOKALITA.get(l.get("lokalita") or l.get("lokalita_auto") or "", 0.0)
    k_stav = STAV.get((l.get("stav") or "").strip().lower(), 0.0)             # J×100
    vek_pouzity = min(max(ROK_OCENENI - l["rok_vystavby"], 0), 80) if l.get("rok_vystavby") else None
    k_vek = koef_vek(ROK_OCENENI - l["rok_vystavby"]) if l.get("rok_vystavby") else 0.0  # M

    # Příplatek parkování (P/Q) — typově+lokalitně rozlišený pro kraje v
    # PARKOVANI_ZAKLAD_KRAJ (Praha, Střední Čechy), jinde fixní PARKOVANI_KC
    # a koeficient lokality 1,0 (žádná tržní data).
    park = (l.get("parkovani") or "Ne").strip().lower()
    pocet_parkovani = 1 if park == "ano" else (0 if park == "ne" else 2)
    kraj = l.get("kraj")
    # cena_mapy dané čtvrti/města — použije se i když je zadaná
    # zakladni_cena_m2 ručně (cena_mapy výše by v tom případě byla None),
    # čistě pro lokalitní koeficient parkování, nemění se tím základní
    # cena/m2 bytu.
    cena_mapy_ctvrt = cena_mapy if cena_mapy is not None else mapa.get(_norm(l.get("ctvrt") or ""))
    typ_park = None
    koef_lok_park = 1.0
    zaklady_kraj = PARKOVANI_ZAKLAD_KRAJ.get(kraj)
    if zaklady_kraj:
        typ_park = l.get("typ_parkovani")
        zaklad_park = zaklady_kraj.get(typ_park, PARKOVANI_KC)
        prumer_kraj = (prumer_cena_m2_kraj or {}).get(kraj)
        if cena_mapy_ctvrt and prumer_kraj:
            koef_lok_park = min(max(cena_mapy_ctvrt / prumer_kraj,
                                     PARKOVANI_LOKALITA_MIN), PARKOVANI_LOKALITA_MAX)
    else:
        zaklad_park = PARKOVANI_KC
    priplatky = round(pocet_parkovani * zaklad_park * koef_lok_park)
    k_dalsi = float(l.get("dalsi_koef_pct") or 0.0)                           # R

    # S, T, U, X — přesně dle sheetu (koeficient balkonu nahrazen efektivní
    # plochou výše — viz VAHA_TERASA_LODZIE_BALKON)
    koef = (1 + k_lok / 100) * (1 + k_stav / 100) * (1 + k_vek / 100) \
        * (1 + k_dalsi / 100)
    vysledna_m2 = zakladni * koef
    cena_za_byt = plocha * vysledna_m2
    trzni = cena_za_byt + priplatky
    rozdil = (cena / trzni - 1) * 100        # X: záporný = nabídka pod tržní hodnotou

    v = {
        "koef_celkem": round(koef, 4), "vysledna_cena_m2": round(vysledna_m2),
        "cena_za_byt": round(cena_za_byt), "priplatky_czk": priplatky,
        "trzni_hodnota": round(trzni), "rozdil_pct": round(rozdil, 2),
        "sleva_pct": round(-rozdil, 2), "najem_rocni": None, "prosty_vynos_pct": None,
        "celkovy_vynos_pct": None, "splatka_mesicni": None, "najem_mesicni": None,
        "pokryti_splatky_pct": None,
        # Rozpad výpočtu pro zobrazení v appce (krok za krokem) — doplněno
        # 2026-07-10 na žádost uživatele („chci vidět cenu, koeficienty za
        # sebou, výsledek"), nic nepočítá jinak, jen ukládá mezivýsledky.
        "v_cena_mapy_m2": round(cena_mapy) if cena_mapy is not None else None,
        "v_faktor_velikosti": round(velikost_faktor, 4),
        "v_zakladni_cena_m2": round(zakladni),
        "v_zakladni_rucne": bool(zakladni_rucne),
        "v_plocha_jadro": round(jadro, 2) if jadro else None,
        "v_plocha_vedlejsi_m2": round(vedlejsi_m2, 2),
        "v_plocha_zahrada_m2": round(zahrada_m2, 2),
        "v_plocha_efektivni": round(plocha, 2),
        "v_koef_lokalita_pct": k_lok, "v_koef_stav_pct": k_stav,
        "v_koef_vek_pct": round(k_vek, 2),
        "v_koef_dalsi_pct": k_dalsi, "v_vek_pouzity": vek_pouzity,
        "v_typ_parkovani": typ_park if pocet_parkovani else None,
        "v_koef_lokalita_parkovani": round(koef_lok_park, 3) if pocet_parkovani and zaklady_kraj else None,
    }

    # Nájem a výnos (AA–AL) — ruční nájemné má přednost, jinak MFČR (čtvrť + dispozice)
    najem_m2 = l.get("najem_m2_mesic")
    if not najem_m2 and najemne_mfcr:
        skupina = _skupina_dispozice(l.get("dispozice"))
        if skupina:
            najem_m2 = najemne_mfcr.get(_norm(l.get("ctvrt") or ""), {}).get(skupina)
    if najem_m2:
        rocni_m2 = najem_m2 * OBSAZENOST_MESICU                               # AB
        najem_rocni = rocni_m2 * koef * plocha + float(l.get("najem_priplatky_rocni") or 0)  # AD
        opravy = trzni * AMORTIZACE * (1 + RUST_NAJMU) ** 10                  # AH
        celkem_najem = ((1 + RUST_NAJMU) ** 20 - 1) / RUST_NAJMU * najem_rocni  # AI
        zhodnoceni = (1 + RUST_CENY) ** 20 * trzni                            # AJ
        irr = _irr(cena, celkem_najem, opravy, zhodnoceni)                    # AL
        splatka = cena * LTV * (1 + SAZBA / 12) ** SPLATNOST_MESICU * (SAZBA / 12) \
            / ((1 + SAZBA / 12) ** SPLATNOST_MESICU - 1)                      # AQ
        najem_mesicni = najem_rocni / 12                                      # AR
        v.update({
            "najem_rocni": round(najem_rocni),
            "prosty_vynos_pct": round(najem_rocni / cena * 100, 2),           # AE
            "celkovy_vynos_pct": round(irr * 100, 2) if irr else None,
            "splatka_mesicni": round(splatka),
            "najem_mesicni": round(najem_mesicni),
            "pokryti_splatky_pct": round(najem_mesicni / splatka * 100, 1),   # AS
        })
    return v


def ocenit_vse():
    """Ocení všechny aktivní nabídky a uloží do tabulky valuations.

    Nabídky, které mají cenu i plochu, ale jejichž čtvrť není v cenové mapě
    a nemají ručně zadanou zakladni_cena_m2, se deaktivují (active=0) —
    schváleno uživatelem 2026-07-07: šlo o malý počet (typicky nabídky
    s obecným „Praha 5" bez konkrétní čtvrti, nebo okrajové čtvrti mimo
    99 čtvrtí ze sheetu), nestojí za budování náhradního mapování a v appce
    jen zbytečně visely bez ocenění. Nabídky bez CENY („cena na vyžádání")
    se NEDEAKTIVUJÍ — to je samostatná, úmyslně zachovaná kategorie."""
    con = db.connect()
    pm = [dict(r) for r in con.execute("SELECT * FROM price_map")]
    mapa = {_norm(r["klic"]): r["cena_za_m2_czk"] for r in pm}
    mapa.update({_norm(r["ctvrt"]): r["cena_za_m2_czk"] for r in pm})
    # Průměrná cena_za_m2_czk za kraj (2026-08-04) — pro lokalitní koeficient
    # příplatku parkování (jen Praha, viz GARAZ_KC/STANI_KC/VENKOVNI_KC výše).
    # Prostý (nevážený) průměr přes čtvrti cenové mapy.
    prumer_cena_m2_kraj = {}
    for kraj_nazev in {r.get("kraj") for r in pm if r.get("kraj")}:
        hodnoty = [r["cena_za_m2_czk"] for r in pm
                   if r.get("kraj") == kraj_nazev and r.get("cena_za_m2_czk")]
        if hodnoty:
            prumer_cena_m2_kraj[kraj_nazev] = sum(hodnoty) / len(hodnoty)
    najemne_mfcr = nacti_najemne_mfcr()
    now = datetime.now().isoformat(timespec="seconds")
    n, bez_mapy, deaktivovano = 0, set(), 0
    for l in con.execute("SELECT * FROM listings WHERE active=1"):
        l = dict(l)
        v = ocenit_nabidku(l, mapa, najemne_mfcr, prumer_cena_m2_kraj)
        if v is None:
            ma_cenu_a_plochu = bool(l.get("plocha_m2") and l.get("cena_czk"))
            nema_mapu = (not l.get("zakladni_cena_m2") and l.get("ctvrt")
                         and _norm(l["ctvrt"]) not in mapa)
            if ma_cenu_a_plochu and nema_mapu:
                con.execute("UPDATE listings SET active=0 WHERE id=?", (l["id"],))
                deaktivovano += 1
                bez_mapy.add(l["ctvrt"])
            continue
        cols = ", ".join(v)
        ph = ", ".join(":" + c for c in v)
        upd = ", ".join(f"{c}=excluded.{c}" for c in v)
        con.execute(
            f"INSERT INTO valuations (listing_id, {cols}, computed_at) VALUES (:lid, {ph}, :now) "
            f"ON CONFLICT(listing_id) DO UPDATE SET {upd}, computed_at=excluded.computed_at",
            {**v, "lid": l["id"], "now": now})
        n += 1
    con.commit()
    con.close()
    print(f"Oceněno {n} nabídek.")
    if deaktivovano:
        print(f"Deaktivováno {deaktivovano} nabídek bez shody v cenové mapě "
              "(čtvrti: " + ", ".join(sorted(bez_mapy)) + ").")
    return n
