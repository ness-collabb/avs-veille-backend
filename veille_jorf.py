"""
═══════════════════════════════════════════════════════════════════
  AVS — Veille réglementaire JORF
  Backend Flask connecté à l'API officielle PISTE / Légifrance (DILA)
═══════════════════════════════════════════════════════════════════

Installation :
    pip install flask flask-cors requests gunicorn

Variables d'environnement à définir sur Render (onglet Environment) :
    PISTE_CLIENT_ID      = ton client_id
    PISTE_CLIENT_SECRET  = ton client_secret
    PISTE_ENV            = "sandbox"  (ou "production" plus tard)

Endpoints :
    GET /api/veille      → textes énergie du dernier JORF
    GET /api/health      → test de vie + état de connexion PISTE
"""

import os
import requests
from datetime import datetime, date, timedelta
from flask import Flask, jsonify, request
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# ─── CONFIG PISTE (lue depuis les variables d'environnement Render) ──
CLIENT_ID = os.environ.get("PISTE_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("PISTE_CLIENT_SECRET", "")
ENV = os.environ.get("PISTE_ENV", "sandbox").lower()

if ENV == "production":
    TOKEN_URL = "https://oauth.piste.gouv.fr/api/oauth/token"
    API_BASE = "https://api.piste.gouv.fr/dila/legifrance/lf-engine-app"
else:
    TOKEN_URL = "https://sandbox-oauth.piste.gouv.fr/api/oauth/token"
    API_BASE = "https://sandbox-api.piste.gouv.fr/dila/legifrance/lf-engine-app"

# Mots-clés métier (repris de ton script d'origine)
MOTS_CLES = ["CEE", "BAR-TH", "BAR-EN", "RENOVATION", "RÉNOVATION", "ARRETE",
             "ARRÊTÉ", "PRIME RENOV", "MAPRIMERENOV", "POMPE A CHALEUR",
             "POMPE À CHALEUR", "TRER", "ANAH", "RGE", "CUMAC", "RE2020",
             "ISOLATION", "ENERGETIQUE", "ÉNERGÉTIQUE", "CHAUDIERE", "PHOTOVOLTA"]


def get_token():
    """Récupère un token OAuth via le flux client_credentials (sans login)."""
    resp = requests.post(TOKEN_URL, data={
        "grant_type": "client_credentials",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "scope": "openid",
    }, timeout=20)
    resp.raise_for_status()
    return resp.json()["access_token"]


def detecter_type(title):
    t = (title or "").strip().lower()
    if t.startswith("décret") or t.startswith("decret"): return "Décret"
    if t.startswith("arrêté") or t.startswith("arrete"): return "Arrêté"
    if t.startswith("loi "): return "Loi"
    if t.startswith("ordonnance"): return "Ordonnance"
    if t.startswith("circulaire"): return "Circulaire"
    if t.startswith("avis"): return "Avis"
    if t.startswith("décision") or t.startswith("decision"): return "Décision"
    return "Texte"


def collecter_textes(noeud, sortie):
    """Parcourt récursivement le sommaire JORF et récupère chaque texte."""
    if isinstance(noeud, dict):
        # Un texte a généralement un 'id' (JORFTEXT...) et un 'title' / 'pathTitle'
        ident = noeud.get("id") or noeud.get("cid") or ""
        titre = noeud.get("title") or noeud.get("pathTitle") or ""
        if isinstance(titre, list):
            titre = " ".join(str(x) for x in titre)
        if ident and "JORFTEXT" in str(ident) and titre:
            sortie.append({
                "titre": titre.strip(),
                "type": detecter_type(titre),
                "nor": noeud.get("nor") or ident,
                "ident": ident,
                "lien": f"https://www.legifrance.gouv.fr/jorf/id/{ident}",
            })
        # On descend dans tous les sous-éléments
        for v in noeud.values():
            collecter_textes(v, sortie)
    elif isinstance(noeud, list):
        for v in noeud:
            collecter_textes(v, sortie)


def veille_journal_officiel(mot_cle=None):
    cles = [mot_cle.upper()] if mot_cle else MOTS_CLES
    token = get_token()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    # 1) Récupérer le dernier JO publié
    r = requests.post(f"{API_BASE}/consult/lastNJo",
                      json={"nbElement": 1}, headers=headers, timeout=30)
    r.raise_for_status()
    jos = r.json().get("results") or r.json().get("jo") or []
    if not jos:
        return {"date": "", "textes": [], "info": "Aucun JO retourné par l'API"}

    dernier = jos[0]
    jo_id = dernier.get("id") or dernier.get("cid")
    jo_date = dernier.get("dateJo") or dernier.get("date") or ""

    # 2) Récupérer le sommaire de ce JO
    r2 = requests.post(f"{API_BASE}/consult/jorfCont",
                       json={"id": jo_id}, headers=headers, timeout=30)
    r2.raise_for_status()
    sommaire = r2.json()

    # 3) Extraire tous les textes, puis filtrer par mots-clés
    tous = []
    collecter_textes(sommaire, tous)
    resultats = []
    vus = set()
    for t in tous:
        if t["ident"] in vus:
            continue
        vus.add(t["ident"])
        haut = t["titre"].upper()
        mots = [m for m in cles if m in haut]
        if mots:
            resultats.append({**t, "motsCles": list(set(mots)), "energie": True})

    return {"date": jo_date, "jo_id": jo_id, "textes": resultats}


# ─── ENDPOINTS ──────────────────────────────────────────────────
@app.route("/api/veille")
def api_veille():
    if not CLIENT_ID or not CLIENT_SECRET:
        return jsonify({"error": "Identifiants PISTE manquants (variables d'environnement)",
                        "textes": []}), 500
    try:
        q = request.args.get("q")
        data = veille_journal_officiel(q)
        return jsonify({
            "env": ENV,
            "date": data.get("date", ""),
            "count": len(data["textes"]),
            "motsCles": MOTS_CLES,
            "source": f"API PISTE Légifrance ({ENV})",
            "textes": data["textes"],
        })
    except requests.HTTPError as e:
        return jsonify({"error": f"Erreur API PISTE : {e.response.status_code}",
                        "detail": e.response.text[:300], "textes": []}), 502
    except Exception as e:
        return jsonify({"error": str(e), "textes": []}), 500


@app.route("/api/health")
def health():
    ok_creds = bool(CLIENT_ID and CLIENT_SECRET)
    token_ok = False
    erreur = None
    detail = None
    if ok_creds:
        try:
            get_token()
            token_ok = True
        except requests.HTTPError as e:
            erreur = f"HTTP {e.response.status_code}"
            detail = e.response.text[:300]
        except Exception as e:
            erreur = str(e)
    return jsonify({"status": "ok", "env": ENV,
                    "token_url": TOKEN_URL,
                    "client_id_longueur": len(CLIENT_ID),
                    "secret_longueur": len(CLIENT_SECRET),
                    "credentials_presentes": ok_creds,
                    "token_obtenu": token_ok,
                    "erreur": erreur,
                    "detail": detail})


if __name__ == "__main__":
    print(f"--- Veille JORF via API PISTE ({ENV}) ---")
    if CLIENT_ID and CLIENT_SECRET:
        try:
            data = veille_journal_officiel()
            print(f"JO du {data.get('date')} — {len(data['textes'])} texte(s) énergie :")
            for i, t in enumerate(data["textes"], 1):
                print(f"  [{i}] {t['type']} : {t['titre'][:80]}")
                print(f"      {t['lien']}")
        except Exception as e:
            print(f"Erreur : {e}")
    else:
        print("⚠️ Définis PISTE_CLIENT_ID et PISTE_CLIENT_SECRET")
    app.run(debug=True, port=5000)
