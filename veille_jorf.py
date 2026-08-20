"""
═══════════════════════════════════════════════════════════════════
  AVS — Veille réglementaire JORF
  Backend Flask connecté à l'API officielle PISTE / Légifrance (DILA)
  Version FINALE — basée sur la structure réelle confirmée par debug
═══════════════════════════════════════════════════════════════════

Variables d'environnement sur Render :
    PISTE_CLIENT_ID, PISTE_CLIENT_SECRET, PISTE_ENV (=production)

Endpoints :
    GET /api/veille          → textes énergie du dernier JO
    GET /api/veille?q=ARRETE → filtre sur un mot précis
    GET /api/veille?tous=1   → TOUS les textes du JO (sans filtre)
    GET /api/health          → test authentification
"""

import os
import requests
from flask import Flask, jsonify, request
from flask_cors import CORS

app = Flask(__name__)
# CORS large : autorise l'app AVS (et tout navigateur) à appeler l'API
CORS(app, resources={r"/*": {"origins": "*"}})

@app.after_request
def ajouter_cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    return response

CLIENT_ID = os.environ.get("PISTE_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("PISTE_CLIENT_SECRET", "")
ENV = os.environ.get("PISTE_ENV", "sandbox").lower()

if ENV == "production":
    TOKEN_URL = "https://oauth.piste.gouv.fr/api/oauth/token"
    API_BASE = "https://api.piste.gouv.fr/dila/legifrance/lf-engine-app"
else:
    TOKEN_URL = "https://sandbox-oauth.piste.gouv.fr/api/oauth/token"
    API_BASE = "https://sandbox-api.piste.gouv.fr/dila/legifrance/lf-engine-app"

# Mots-clés transition énergétique / rénovation
MOTS_CLES = ["CEE", "BAR-TH", "BAR-EN", "RENOVATION", "RÉNOVATION",
             "PRIME RENOV", "MAPRIMERENOV", "POMPE A CHALEUR", "POMPE À CHALEUR",
             "ANAH", "RGE", "CUMAC", "RE2020", "ISOLATION", "ISOLANT",
             "ENERGETIQUE", "ÉNERGÉTIQUE", "ENERGIE", "ÉNERGIE",
             "CHAUDIERE", "CHAUDIÈRE", "PHOTOVOLTA", "SOLAIRE",
             "GEOTHERMIE", "GÉOTHERMIE", "BIOMASSE", "VENTILATION", "VMC",
             "DPE", "PERFORMANCE ENERGETIQUE", "TRANSITION ENERGETIQUE",
             "EFFICACITE ENERGETIQUE", "CERTIFICAT D'ECONOMIE",
             "ECONOMIE D'ENERGIE", "MENUISERIE", "FENETRE", "FENÊTRE"]


def get_token():
    resp = requests.post(TOKEN_URL, data={
        "grant_type": "client_credentials",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "scope": "openid",
    }, timeout=20)
    resp.raise_for_status()
    return resp.json()["access_token"]


def extraire_textes(noeud, sortie):
    """
    Parcourt récursivement TOUTE la réponse et récupère chaque texte.
    Structure confirmée : chaque texte est un dict avec 'id' (JORFTEXT...),
    'titre', et 'nature'.
    """
    if isinstance(noeud, dict):
        idv = noeud.get("id", "")
        titre = noeud.get("titre", "")
        if isinstance(idv, str) and "JORFTEXT" in idv and isinstance(titre, str) and titre:
            sortie.append({
                "id": idv,
                "titre": titre.strip(),
                "nature": noeud.get("nature", ""),
            })
        for v in noeud.values():
            extraire_textes(v, sortie)
    elif isinstance(noeud, list):
        for v in noeud:
            extraire_textes(v, sortie)


def nature_lisible(nat):
    return {"ARRETE": "Arrêté", "DECRET": "Décret", "LOI": "Loi",
            "DECISION": "Décision", "CIRCULAIRE": "Circulaire",
            "ORDONNANCE": "Ordonnance", "AVIS": "Avis"}.get(nat, nat or "Texte")


def recuperer_jo():
    """Récupère le dernier JO et en extrait tous les textes (dédoublonnés)."""
    token = get_token()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    r = requests.post(f"{API_BASE}/consult/lastNJo",
                      json={"nbElement": 1}, headers=headers, timeout=30)
    r.raise_for_status()
    reponse = r.json()

    containers = reponse.get("containers") or []
    jo_eli = containers[0].get("idEli", "") if containers else ""
    jo_date = containers[0].get("datePubli", "") if containers else ""

    bruts = []
    extraire_textes(reponse, bruts)

    # Dédoublonnage par id
    vus = {}
    for t in bruts:
        vus[t["id"]] = t
    textes = list(vus.values())

    return jo_eli, jo_date, textes


@app.route("/api/veille")
def api_veille():
    if not CLIENT_ID or not CLIENT_SECRET:
        return jsonify({"error": "Identifiants PISTE manquants", "textes": []}), 500
    try:
        q = request.args.get("q")
        tous = request.args.get("tous") == "1"
        cles = [q.upper()] if q else MOTS_CLES

        jo_eli, jo_date, textes = recuperer_jo()

        resultats = []
        for t in textes:
            titre_maj = t["titre"].upper()
            if tous:
                mots = []
                garder = True
            else:
                mots = [m for m in cles if m in titre_maj]
                garder = len(mots) > 0
            if garder:
                resultats.append({
                    "titre": t["titre"],
                    "type": nature_lisible(t["nature"]),
                    "nor": t["id"],
                    "url": f"https://www.legifrance.gouv.fr/jorf/id/{t['id']}",
                    "motsCles": mots,
                    "energie": not tous,
                })

        return jsonify({
            "env": ENV,
            "date": jo_eli,
            "datePubli": jo_date,
            "count": len(resultats),
            "total_textes_jo": len(textes),
            "source": f"API PISTE Légifrance ({ENV})",
            "textes": resultats,
        })
    except requests.HTTPError as e:
        return jsonify({"error": f"HTTP {e.response.status_code}",
                        "detail": e.response.text[:300], "textes": []}), 502
    except Exception as e:
        return jsonify({"error": str(e), "textes": []}), 500


@app.route("/api/health")
def health():
    ok = bool(CLIENT_ID and CLIENT_SECRET)
    token_ok = False
    erreur = None
    if ok:
        try:
            get_token()
            token_ok = True
        except Exception as e:
            erreur = str(e)
    return jsonify({"status": "ok", "env": ENV,
                    "credentials_presentes": ok,
                    "token_obtenu": token_ok, "erreur": erreur})


@app.route("/")
def home():
    return jsonify({"service": "AVS Veille JORF",
                    "endpoints": ["/api/veille", "/api/veille?tous=1",
                                  "/api/veille?q=ARRETE", "/api/health"]})


if __name__ == "__main__":
    app.run(debug=True, port=5000)
