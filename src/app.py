# Lokální webová aplikace — servíruje stejné UI jako GitHub Pages,
# jen data.json generuje živě z databáze. Oceňovací logika sem NEPATŘÍ.
from pathlib import Path

from flask import Flask, jsonify, send_file

from . import build_static, db, valuation

app = Flask(__name__)
STATIC = Path(__file__).resolve().parent / "static"


@app.get("/")
def index():
    return send_file(STATIC / "index.html")


@app.get("/data.json")
def data():
    return jsonify(build_static.dump_data())


@app.post("/api/prepocitat")
def prepocitat():
    return jsonify({"oceneno": valuation.ocenit_vse()})


def run(port=8000):
    print(f"Aplikace běží na http://localhost:{port}")
    app.run(port=port, debug=False)
