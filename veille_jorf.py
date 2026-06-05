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
    PISTE_ENV            = "production"   (ou "sandbox" pour tester)

Endpoints :
    GET /api/veille      → textes énergie du dernier JORF
    GET /api/veille?q=mot → filtre sur un mot précis
    GET /api/health      → test de vie + état de connexion PISTE
"""

import os
import re
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

# ─── MOTS-CLÉS MÉTIER (ciblés rénovation énergétique) ───────────────
MOTS_CLES = [
    "CEE", "BAR-TH", "BAR-EN", "BAR-EQ",
    "MAPRIMERENOV", "MAPRIMERÉNOV", "PRIME RENOV", "PRIME RÉNOV",
    "ANAH", "RGE", "CUMAC", "RE2020",
    "ISOLATION", "ISOLANT",
    "POMPE A CHALEUR", "POMPE À CHALEUR",
    "CHAUDIERE", "CHAUDIÈRE",
    "PHOTOVOLTA", "PANNEAU SOLAIRE", "SOLAIRE THERMIQUE",
    "RENOVATION ENERGETIQUE", "RÉNOVATION ÉNERGÉTIQUE",
    "PERFORMANCE ENERGETIQUE", "PERFORMANCE ÉNERGÉTIQUE",
    "TRANSITION ENERGETIQUE", "TRANSITION ÉNERGÉTIQUE",
    "CERTIFICAT D'ECONOMIE", "CERTIFICAT D'ÉCONOMIE",
    "ECONOMIE D'ENERGIE", "ÉCONOMIE D'ÉNERGIE",
    "VENTILATION", "VMC",
    "DPE", "DIAGNOSTIC DE PERFORMANCE",
    "EFFICACITE ENERGETIQUE", "EFFICACITÉ ÉNERGÉTIQUE",
    "MENUISERIE", "FENETRE", "FENÊTRE",
    "BIOMASSE", "GEOTHERMIE", "GÉOTHERMIE",
]


# Pré-compilation : chaque mot-clé devient un motif "mot entier"
# (frontières de mot) pour éviter les faux positifs comme
# "RGE" dans "chaRGE" / "concieRGE", ou "CEE" dans un fragment.
def _construire_motifs(cles):
    motifs = []
    for m in cles:
        pattern = r"(?<![A-Za-zÀ-ÿ0-9])" + re.escape(m) + r"(?![A-Za-zÀ-ÿ0-9])"
        motifs.append((m, re.compile(pattern, re.IGNORECASE)))
    return motifs


MOTIFS = _construire_motifs(MOTS_CLES)


def trouver_mots_cles(titre, cles=None):
    """Retourne la liste des mots-clés trouvés comme MOTS ENTIERS dans le titre."""
    if cles is None:
        return [m for m, rx in MOTIFS if rx.search(titre)]
    trouves = []
    for m in cles:
        pattern = r"(?<![A-Za-zÀ-ÿ0-9])" + re.escape(m) + r"(?![A-Za-zÀ-ÿ0-9])"
        if re.search(pattern, titre, re.IGNORECASE):
            trouves.append(m)
    return trouves


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


def detecter_type(nature, titre):
    """Détermine le type lisible à partir du champ 'nature' (ou du titre)."""
    n = (nature or "").strip().upper()
    correspondances = {
        "LOI": "Loi",
        "DECRET": "Décret",
        "ARRETE": "Arrêté",
        "ORDONNANCE": "Ordonnance",
        "CIRCULAIRE": "Circulaire",
        "AVIS": "Avis",
        "DECISION": "Décision",
        "DELIBERATION": "Délibération",
        "ARRET": "Arrêt",
        "RECOMMANDATION": "Recommandation",
        "INFORMATIONS_PARLEMENTAIRES": "Info parlementaire",
        "ANNONCES": "Annonce",
    }
    if n in correspondances:
        return correspondances[n]
    t = (titre or "").strip().lower()
    if t.startswith("décret") or t.startswith("decret"): return "Décret"
    if t.startswith("arrêté") or t.startswith("arrete"): return "Arrêté"
    if t.startswith("loi "): return "Loi"
    return "Texte"


def collecter_textes(noeud, sortie):
    """
    Parcourt récursivement le sommaire JORF.
    Structure réelle Légifrance :
      containers[].structure.tms[]  (rubriques, récursif via 'tms')
      chaque rubrique a un tableau 'liensTxt[]' contenant les textes :
        { "id": "JORFTEXT...", "titre": "...", "nature": "ARRETE", ... }
    """
    if isinstance(noeud, dict):
        for txt in noeud.get("liensTxt", []) or []:
            ident = txt.get("id") or ""
            titre = txt.get("titre") or ""
            if ident and "JORFTEXT" in str(ident) and titre:
                sortie.append({
                    "titre": titre.strip(),
                    "type": detecter_type(txt.get("nature"), titre),
                    "nature": txt.get("nature") or "",
                    "ministere": txt.get("ministere") or txt.get("emetteur") or "",
                    "nor": ident,
                    "ident": ident,
                    "lien": f"https://www.legifrance.gouv.fr/jorf/id/{ident}",
                })
        for v in noeud.values():
            collecter_textes(v, sortie)
    elif isinstance(noeud, list):
        for v in noeud:
            collecter_textes(v, sortie)


def veille_journal_officiel(mot_cle=None):
    token = get_token()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    # 1) Récupérer le dernier JO publié
    r = requests.post(f"{API_BASE}/consult/lastNJo",
                      json={"nbElement": 1}, headers=headers, timeout=30)
    r.raise_for_status()
    payload = r.json()
    containers = payload.get("containers") or payload.get("results") or payload.get("jo") or []
    if not containers:
        return {"date": "", "textes": [], "total_textes_jo": 0,
                "info": "Aucun JO retourné par l'API"}

    dernier = containers[0]
    jo_id = dernier.get("id") or dernier.get("cid")

    # Date : datePubli est un timestamp en millisecondes
    jo_date = ""
    ts = dernier.get("datePubli")
    if ts:
        try:
            jo_date = datetime.fromtimestamp(ts / 1000).strftime("%d/%m/%Y")
        except Exception:
            jo_date = str(ts)

    # 2) Récupérer le sommaire complet de ce JO
    r2 = requests.post(f"{API_BASE}/consult/jorfCont",
                       json={"id": jo_id}, headers=headers, timeout=30)
    r2.raise_for_status()
    sommaire = r2.json()

    if not jo_date:
        som_containers = sommaire.get("containers") or []
        if som_containers:
            ts2 = som_containers[0].get("datePubli")
            if ts2:
                try:
                    jo_date = datetime.fromtimestamp(ts2 / 1000).strftime("%d/%m/%Y")
                except Exception:
                    jo_date = str(ts2)

    # 3) Extraire tous les textes, puis filtrer par mots-clés (mot entier)
    tous = []
    collecter_textes(sommaire, tous)
    resultats = []
    vus = set()
    custom = [mot_cle.upper()] if mot_cle else None
    for t in tous:
        if t["ident"] in vus:
            continue
        vus.add(t["ident"])
        mots = trouver_mots_cles(t["titre"], custom)
        if mots:
            resultats.append({**t, "motsCles": list(set(mots)), "energie": True})

    return {"date": jo_date, "jo_id": jo_id,
            "total_textes_jo": len(vus), "textes": resultats}


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
            "total_textes_jo": data.get("total_textes_jo", 0),
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
            print(f"JO du {data.get('date')} — {len(data['textes'])} texte(s) énergie "
                  f"sur {data.get('total_textes_jo')} au total :")
            for i, t in enumerate(data["textes"], 1):
                print(f"  [{i}] {t['type']} : {t['titre'][:80]}")
                print(f"      {t['lien']}")
        except Exception as e:
            print(f"Erreur : {e}")
    else:
        print("⚠️ Définis PISTE_CLIENT_ID et PISTE_CLIENT_SECRET")
    app.run(debug=True, port=5000)
