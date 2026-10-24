# Vygeneruje statickou verzi appky do docs/ (pro GitHub Pages):
# docs/data.json (nabídky + ocenění + cenová mapa) a docs/index.html (kopie UI).
import json
import shutil
from datetime import datetime
from pathlib import Path

from . import db

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"


def dump_data() -> dict:
    con = db.connect()
    listings = [dict(r) for r in con.execute(
        """SELECT l.*, v.koef_celkem, v.vysledna_cena_m2, v.cena_za_byt, v.priplatky_czk,
                  v.trzni_hodnota, v.rozdil_pct, v.sleva_pct, v.najem_rocni,
                  v.prosty_vynos_pct, v.celkovy_vynos_pct, v.splatka_mesicni,
                  v.najem_mesicni, v.pokryti_splatky_pct,
                  v.v_cena_mapy_m2, v.v_faktor_velikosti, v.v_zakladni_cena_m2,
                  v.v_zakladni_rucne, v.v_koef_lokalita_pct, v.v_koef_stav_pct,
                  v.v_koef_vek_pct, v.v_koef_dalsi_pct, v.v_vek_pouzity,
                  v.v_plocha_jadro, v.v_plocha_vedlejsi_m2, v.v_plocha_zahrada_m2,
                  v.v_plocha_efektivni, v.v_typ_parkovani, v.v_koef_lokalita_parkovani
           FROM listings l LEFT JOIN valuations v ON v.listing_id = l.id
           WHERE l.active = 1""")]
    price_map = [dict(r) for r in con.execute("SELECT * FROM price_map")]
    con.close()
    for r in listings:
        r["cena_m2"] = round(r["cena_czk"] / r["plocha_m2"]) \
            if (r.get("plocha_m2") and r.get("cena_czk")) else None
    return {"generated": datetime.now().isoformat(timespec="seconds"),
            "listings": listings, "price_map": price_map}


def build() -> None:
    DOCS.mkdir(exist_ok=True)
    (DOCS / "data.json").write_text(
        json.dumps(dump_data(), ensure_ascii=False), encoding="utf-8")
    shutil.copy(ROOT / "src" / "static" / "index.html", DOCS / "index.html")
    print(f"Statická appka vygenerována: {DOCS} (nabídek: {len(dump_data()['listings'])})")
