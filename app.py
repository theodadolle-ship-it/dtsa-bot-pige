import re
import io
import os
import json
import sqlite3
from datetime import datetime
from urllib.parse import urlparse
import pandas as pd
import streamlit as st

APP_TITLE = "DTSA — Pige automobile"
DB_PATH = os.getenv("DTSA_DB_PATH", "dtsa_annonces.db")

st.set_page_config(page_title=APP_TITLE, page_icon="🚗", layout="wide")

# Filtrage volontairement prudent : les annonces douteuses sont mises en revue ou exclues.
NO_CONTACT_PATTERNS = [
    r"\bpas de démarchage téléphonique\b",
    r"\bpas de demarchage telephonique\b",
    r"\bpas de démarchage\b",
    r"\bpas de demarchage\b",
    r"\bpas d'appel\b",
    r"\bpas d appel\b",
    r"\bne pas appeler\b",
    r"\bpas de téléphone\b",
    r"\bpas de telephone\b",
    r"\bpas de professionnels?\b",
    r"\bpas de pro\b",
    r"\bprofessionnels? s'abstenir\b",
    r"\bprofessionnels? s abstenir\b",
    r"\bpro s'abstenir\b",
    r"\bpas d'agence\b",
    r"\bpas de garage\b",
]
PRO_PATTERNS = [
    r"\bprofessionnel\b", r"\bprofessionnels\b", r"\bgarage\b",
    r"\bconcessionnaire\b", r"\bnégoce automobile\b", r"\bnegoce automobile\b",
    r"\bauto-entrepreneur\b", r"\bSAS\b", r"\bSARL\b", r"\bSIRET\b",
    r"\bgarantie 3 mois\b", r"\bgarantie 6 mois\b", r"\bgarantie 12 mois\b",
    r"\bfinancement\b", r"\breprise possible\b", r"\bprix TTC\b",
]
SERIOUS_DEFECT_PATTERNS = [
    r"\bmoteur HS\b", r"\bmoteur à changer\b", r"\bmoteur a changer\b",
    r"\bboîte HS\b", r"\bboite HS\b", r"\bboîte à changer\b", r"\bboite a changer\b",
    r"\bnon roulante\b", r"\bne démarre pas\b", r"\bne demarre pas\b",
    r"\baccidentée\b", r"\baccidentee\b", r"\bvéhicule accidenté\b",
    r"\bpour pièces\b", r"\bpour pieces\b", r"\bjoint de culasse\b",
    r"\bproblème moteur\b", r"\bprobleme moteur\b",
]
PHONE_RE = re.compile(r"(?<!\d)(?:\+33\s?[1-9](?:[\s.-]?\d{2}){4}|0[1-9](?:[\s.-]?\d{2}){4})(?!\d)")

def norm(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()

def text_for_match(value):
    s = norm(value).lower()
    replacements = str.maketrans({"é":"e","è":"e","ê":"e","ë":"e","à":"a","â":"a","î":"i","ï":"i","ô":"o","ù":"u","û":"u","ç":"c"})
    return s.translate(replacements)

def first_matches(text, patterns):
    t = text_for_match(text)
    found = []
    for p in patterns:
        if re.search(text_for_match(p), t, flags=re.I):
            found.append(p.replace(r"\b", "").replace("\\", ""))
    return found

def extract_phone(text):
    # Ne recherche que dans le texte explicitement fourni/importé. Ne contourne aucune protection.
    m = PHONE_RE.search(str(text or ""))
    return m.group(0) if m else ""

def evaluate_listing(
    row,
    center_lat=None,
    center_lon=None,
    radius_km=30,
    min_price=3000,
    max_price=200000
):
    title = norm(row.get("title", ""))
    desc = norm(row.get("description", ""))
    combined = title + " " + desc
    reasons = []
    no_contact = first_matches(combined, NO_CONTACT_PATTERNS)
    pro = first_matches(combined, PRO_PATTERNS)
    defects = first_matches(combined, SERIOUS_DEFECT_PATTERNS)

    seller_type = text_for_match(row.get("seller_type", ""))
    if seller_type in {"professionnel", "pro", "business", "dealer"}:
        reasons.append("Vendeur identifié comme professionnel")
    elif seller_type in {"particulier", "private", "individual"}:
        pass
    elif pro:
        reasons.append("Indices de vendeur professionnel à vérifier")
    if no_contact:
        reasons.append("Restriction de contact détectée : " + ", ".join(no_contact[:3]))
    if defects:
        reasons.append("Défaut important signalé : " + ", ".join(defects[:3]))

    price = pd.to_numeric(row.get("price_eur", None), errors="coerce")
    if pd.isna(price) or price < min_price or price > max_price:
    reasons.append(
        f"Prix hors plage ou absent ({min_price}–{max_price} €)"
    )

    # Distance : utilise la distance déjà fournie par la source/import. Pas de géocodage supposé.
    dist = pd.to_numeric(row.get("distance_km", None), errors="coerce")
    if not pd.isna(dist) and dist > radius_km:
        reasons.append(f"Hors rayon ({dist:g} km > {radius_km} km)")
    if pd.isna(dist):
        reasons.append("Distance non vérifiée")

     status = "À vérifier"

    explicit_pro = seller_type in {
        "professionnel", "pro", "business", "dealer"
    }
    invalid_price = (
        pd.isna(price) or price < min_price or price > max_price
    )
    invalid_distance = (
        pd.isna(dist) or dist < 0 or dist > radius_km
    )

    if no_contact or explicit_pro or invalid_price:
        status = "Exclue"
    elif pro or defects or invalid_distance:
        status = "À vérifier"
    else:
        status = "À vérifier"

    # Estimation marché optionnelle : fournie par l'utilisateur ou par un futur fournisseur de données autorisé.
    market = pd.to_numeric(row.get("market_price_eur", None), errors="coerce")
    deal = "Non évaluée"
    discount = None
    if not pd.isna(price) and not pd.isna(market) and market > 0:
        discount = (market - price) / market * 100
        if discount >= 15:
            deal = "Très bonne affaire"
        elif discount >= 8:
            deal = "Bonne affaire"
        elif discount >= -5:
            deal = "Prix équitable"
        else:
            deal = "Au-dessus du marché"
    elif status != "Exclue":
        deal = "Estimation marché nécessaire"

    # L'absence de téléphone n'est pas un motif d'exclusion.
    phone = norm(row.get("phone_public", "")) or extract_phone(combined)
    if status == "Exclue":
        keep = False
    else:
        keep = True

    return {
        "status": status, "deal": deal, "discount_pct": discount,
        "reasons": "; ".join(reasons) if reasons else "Aucun filtre bloquant détecté",
        "phone_found": phone, "keep": keep
    }

def ensure_db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""CREATE TABLE IF NOT EXISTS listings (
        listing_id TEXT PRIMARY KEY, title TEXT, price_eur REAL, make TEXT, model TEXT,
        year INTEGER, mileage_km REAL, fuel TEXT, transmission TEXT, location TEXT,
        distance_km REAL, seller_type TEXT, description TEXT, url TEXT, phone_public TEXT,
        market_price_eur REAL, first_seen TEXT, source TEXT, raw_json TEXT
    )""")
    con.commit()
    con.close()

ensure_db()

st.title("🚗 DTSA — Pige automobile")
st.caption("Écully (69130) · rayon 30 km · 3 000 à 200 000 € · vendeurs particuliers")
st.info("Prototype prêt à tester : import CSV/JSON et analyse des descriptions. La collecte automatique horaire nécessite une source autorisée (flux/API/alertes exportées) et sa configuration.")

with st.sidebar:
    st.header("Paramètres")
    center = st.text_input("Centre de recherche", "Écully, 69130")
    radius = st.slider("Rayon (km)", 5, 100, 30, 5)
    min_price = st.number_input("Prix minimum (€)", min_value=0, max_value=200000, value=3000, step=500)
    max_price = st.number_input("Prix maximum (€)", min_value=3000, max_value=500000, value=200000, step=5000)
    st.caption("La distance doit être fournie par la source d'annonces ou calculée avec un géocodage autorisé.")

st.subheader("1. Importer les annonces")
st.markdown("Importe un CSV exporté depuis une source autorisée, ou un JSON préparé. Colonnes recommandées : `listing_id`, `title`, `price_eur`, `make`, `model`, `year`, `mileage_km`, `fuel`, `location`, `distance_km`, `seller_type`, `description`, `url`, `phone_public`, `market_price_eur`.")
uploaded = st.file_uploader("Fichier CSV ou JSON", type=["csv", "json"])
if uploaded:
    try:
        if uploaded.name.lower().endswith(".csv"):
            imported = pd.read_csv(uploaded)
        else:
            raw = json.load(uploaded)
            imported = pd.DataFrame(raw if isinstance(raw, list) else raw.get("listings", []))
        st.success(f"{len(imported)} annonce(s) importée(s) pour analyse.")
        st.dataframe(imported.head(10), use_container_width=True)
        if st.button("Analyser et enregistrer les annonces", type="primary"):
            results = []
            con = sqlite3.connect(DB_PATH)
            for _, r in imported.iterrows():
                row = r.to_dict()
                row.setdefault("price_eur", None)
                row.setdefault("description", "")
                row.setdefault("title", "")
                row.setdefault("seller_type", "")
                row.setdefault("distance_km", None)
                row.setdefault("market_price_eur", None)
                row.setdefault("phone_public", "")
                url = norm(row.get("url", ""))
                lid = norm(row.get("listing_id", "")) or url or f"{norm(row.get('title'))}-{row.get('price_eur','')}"
                analysis = evaluate_listing(
    row,
    radius_km=radius,
    min_price=min_price,
    max_price=max_price
)
                payload = {
                    "listing_id": lid, "title": norm(row.get("title")), "price_eur": pd.to_numeric(row.get("price_eur"), errors="coerce"),
                    "make": norm(row.get("make")), "model": norm(row.get("model")), "year": pd.to_numeric(row.get("year"), errors="coerce"),
                    "mileage_km": pd.to_numeric(row.get("mileage_km"), errors="coerce"), "fuel": norm(row.get("fuel")),
                    "transmission": norm(row.get("transmission")), "location": norm(row.get("location")),
                    "distance_km": pd.to_numeric(row.get("distance_km"), errors="coerce"), "seller_type": norm(row.get("seller_type")),
                    "description": norm(row.get("description")), "url": url, "phone_public": analysis["phone_found"],
                    "market_price_eur": pd.to_numeric(row.get("market_price_eur"), errors="coerce"),
                    "first_seen": norm(row.get("first_seen")) or datetime.now().isoformat(timespec="seconds"),
                    "source": norm(row.get("source")) or uploaded.name, "raw_json": json.dumps(row, ensure_ascii=False, default=str)
                }
                con.execute("""INSERT OR REPLACE INTO listings VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            tuple(payload[k] for k in ["listing_id","title","price_eur","make","model","year","mileage_km","fuel","transmission","location","distance_km","seller_type","description","url","phone_public","market_price_eur","first_seen","source","raw_json"]))
                results.append({**payload, **analysis})
            con.commit()
            con.close()
            st.session_state["last_results"] = pd.DataFrame(results)
            st.success("Analyse terminée et données enregistrées.")

    except Exception as e:
        st.error(f"Impossible de lire/analyser le fichier : {e}")

if "last_results" in st.session_state:
    st.subheader("2. Résultats du dernier import")
    res = st.session_state["last_results"]
    st.dataframe(res[["title","price_eur","make","model","year","mileage_km","distance_km","status","deal","discount_pct","reasons","phone_found","url"]], use_container_width=True, hide_index=True)
    st.download_button("Télécharger les résultats CSV", res.to_csv(index=False).encode("utf-8-sig"), "dtsa_resultats.csv", "text/csv")

st.subheader("3. Tableau de bord")
con = sqlite3.connect(DB_PATH)
df = pd.read_sql_query("SELECT * FROM listings ORDER BY first_seen DESC", con)
con.close()
if df.empty:
    st.write("Aucune annonce enregistrée pour le moment. Importe un premier fichier pour commencer.")
else:
    analyses = []
    for _, r in df.iterrows():
        a = evaluate_listing(
    r.to_dict(),
    radius_km=radius,
    min_price=min_price,
    max_price=max_price
)
        analyses.append(a)
    df["status"] = [a["status"] for a in analyses]
    df["deal"] = [a["deal"] for a in analyses]
    df["reasons"] = [a["reasons"] for a in analyses]
    df["discount_pct"] = [a["discount_pct"] for a in analyses]
    a,b,c,d = st.columns(4)

a.metric("Annonces en base", len(df))

b.metric(
    "À vérifier / retenues",
    int((df.status != "Exclue").sum())
)

c.metric(
    "Bonnes affaires (total)",
    int(df.deal.isin(["Très bonne affaire", "Bonne affaire"]).sum())
)

d.metric(
    "Dont très bonnes affaires",
    int((df.deal == "Très bonne affaire").sum())
)
    filter_status = st.multiselect("Afficher les statuts", ["À vérifier","Exclue"], default=["À vérifier"])
    filter_deal = st.multiselect("Catégories de prix", ["Très bonne affaire","Bonne affaire","Prix équitable","Au-dessus du marché","Estimation marché nécessaire","Non évaluée"], default=["Très bonne affaire","Bonne affaire","Prix équitable","Estimation marché nécessaire"])
    view = df[df.status.isin(filter_status) & df.deal.isin(filter_deal)].copy()
    st.dataframe(view[["title","price_eur","make","model","year","mileage_km","location","distance_km","seller_type","deal","status","reasons","phone_public","url","first_seen"]], use_container_width=True, hide_index=True)
    st.download_button("Exporter le tableau complet", view.to_csv(index=False).encode("utf-8-sig"), "dtsa_tableau.csv", "text/csv")

st.subheader("Règles de sécurité et limites")
st.markdown("""
- Le bot ne contourne pas les protections de Leboncoin et ne récupère pas de téléphone masqué.
- Les annonces mentionnant un refus de démarchage ou de contact téléphonique sont exclues.
- Les annonces dont le statut vendeur est ambigu sont marquées « À vérifier », pas automatiquement considérées comme particulières.
- Une estimation fiable de bonne affaire nécessite une valeur de marché issue d'une source fiable. Sans cette donnée, le bot ne prétend pas connaître le prix du marché.
- Avant une mise en production, vérifier les conditions d'utilisation de la source d'annonces, les droits de réutilisation des données et les obligations RGPD applicables.
""")
