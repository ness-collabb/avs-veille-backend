"""
═══════════════════════════════════════════════════════════════════
  AVS — Veille réglementaire JORF
  Backend Flask qui SCRAPE le sommaire officiel du Journal Officiel
  Source : legifrance.gouv.fr (pas de dépendance tierce, pas d'API key)
═══════════════════════════════════════════════════════════════════

Installation :
    pip install flask flask-cors requests beautifulsoup4 lxml

Lancement :
    python veille_jorf.py
    → serveur sur http://localhost:5000

Endpoints :
    GET /api/veille                  → JORF du jour, textes énergie
    GET /api/veille?date=2026-05-30  → JORF d'une date précise
    GET /api/veille?q=BAR-TH         → filtré sur un mot-clé
    GET /api/veille?tous=1           → tous les textes (pas seulement énergie)

⚠️ ROBUSTESSE :
Ce scraper repère les liens vers /jorf/id/JORFTEXT... et leur intitulé.
Cette approche résiste aux changements de design (on ne dépend pas de
classes CSS). Si Légifrance change radicalement sa structure, seule la
fonction parse_sommaire() est à adapter.
"""

import re
import requests
from datetime import datetime, date
from bs4 import BeautifulSoup
from flask import Flask, jsonify, request
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

BASE = "https://www.legifrance.gouv.fr"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; AVS-Veille/1.0)"}

# Mots-clés métier (repris de votre script d'origine)
MOTS_CLES = ["CEE", "BAR-TH", "BAR-EN", "RENOVATION", "RÉNOVATION", "ARRETE",
             "ARRÊTÉ", "PRIME RENOV", "MAPRIMERENOV", "POMPE A CHALEUR",
             "POMPE À CHALEUR", "TRER", "ANAH", "RGE", "CUMAC", "RE2020",
             "ISOLATION", "ENERGETIQUE", "ÉNERGÉTIQUE", "CHAUDIERE", "PHOTOVOLTA"]


def url_sommaire(d: date) -> str:
    """Construit l'URL du sommaire JORF. Le numéro de JO n'est pas connu
    à l'avance : on passe par la page /jorf/jo qui redirige vers le jour courant,
    ou on tente le format daté si une date est fournie."""
    # Légifrance accepte aussi /jorf/jo/AAAA/MM/JJ (sans numéro) qui redirige
    return f"{BASE}/jorf/jo/{d.year}/{d.month:02d}/{d.day:02d}"


def detecter_type(title: str) -> str:
    t = title.strip().lower()
    if t.startswith("décret") or t.startswith("decret"): return "Décret"
    if t.startswith("arrêté") or t.startswith("arrete"): return "Arrêté"
    if t.startswith("loi "): return "Loi"
    if t.startswith("ordonnance"): return "Ordonnance"
    if t.startswith("circulaire"): return "Circulaire"
    if t.startswith("avis"): return "Avis"
    if t.startswith("décision") or t.startswith("decision"): return "Décision"
    return "Texte"


def parse_sommaire(html: str):
    """
    Extrait tous les textes du sommaire.
    Stratégie robuste : on cherche tous les liens <a href="/jorf/id/JORFTEXT...">
    et on récupère leur texte comme titre.
    """
    soup = BeautifulSoup(html, "lxml")
    textes = []
    vus = set()

    for a in soup.find_all("a", href=True):
        href = a["href"]
        # On ne garde que les liens vers des textes JORF
        if "/jorf/id/JORFTEXT" not in href and "/jorf/article_jo/JORFARTI" not in href:
            continue
        titre = a.get_text(strip=True)
        if not titre or len(titre) < 12:   # ignore liens vides / "Voir"
            continue

        # Extraction de l'identifiant JORFTEXT
        m = re.search(r"(JORFTEXT\d+|JORFARTI\d+)", href)
        ident = m.group(1) if m else ""
        if ident in vus:
            continue
        vus.add(ident)

        lien = href if href.startswith("http") else BASE + href
        textes.append({
            "titre": titre,
            "type": detecter_type(titre),
            "ident": ident,
            "lien": lien,
        })
    return textes


def filtrer(textes, cles):
    """Garde les textes dont le titre contient au moins un mot-clé."""
    resultats = []
    for t in textes:
        haut = t["titre"].upper()
        mots = [m for m in cles if m in haut]
        if mots:
            resultats.append({**t, "motsCles": list(set(mots)), "energie": True})
    return resultats


def veille_journal_officiel(d=None, mot_cle=None, tous=False):
    d = d or date.today()
    cles = [mot_cle.upper()] if mot_cle else MOTS_CLES
    try:
        # On suit les redirections (le /jorf/jo/AAAA/MM/JJ redirige vers le bon n°)
        resp = requests.get(url_sommaire(d), headers=HEADERS, timeout=20, allow_redirects=True)
        resp.raise_for_status()
        textes = parse_sommaire(resp.text)

        if tous:
            return [{**t, "motsCles": [], "energie": False} for t in textes]
        return filtrer(textes, cles)
    except Exception as e:
        print(f"❌ Erreur récupération JO ({d}) : {e}")
        return []


# ─── ENDPOINTS API ──────────────────────────────────────────────
@app.route("/api/veille")
def api_veille():
    q = request.args.get("q")
    tous = request.args.get("tous") == "1"
    date_str = request.args.get("date")  # format AAAA-MM-JJ
    d = date.today()
    if date_str:
        try:
            d = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            pass

    textes = veille_journal_officiel(d, q, tous)
    return jsonify({
        "date": d.strftime("%d/%m/%Y"),
        "count": len(textes),
        "motsCles": MOTS_CLES,
        "source": "legifrance.gouv.fr (scraping sommaire)",
        "textes": textes,
    })


@app.route("/api/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    print(f"--- Veille réglementaire ({datetime.now().strftime('%d/%m/%Y')}) ---")
    resultats = veille_journal_officiel()
    if resultats:
        print(f"\n🎯 {len(resultats)} texte(s) énergie trouvé(s) :")
        for i, t in enumerate(resultats, 1):
            print(f"\n[{i}] [{t['type']}] {t['titre'][:90]}...")
            print(f"    👉 Mots-clés : {', '.join(t['motsCles'])}")
            print(f"    👉 Lien      : {t['lien']}")
    else:
        print("\n☕ Aucun texte énergie aujourd'hui (ou structure HTML à adapter).")

    print("\n🚀 Serveur API sur http://localhost:5000")
    app.run(debug=True, port=5000)
  
