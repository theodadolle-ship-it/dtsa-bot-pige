 
import os
import re
import json
import sqlite3
from datetime import datetime

import pandas as pd
import streamlit as st

APP_TITLE = "DTSA — Pige automobile"
DB_PATH = os.getenv("DTSA_DB_PATH", "dtsa_annonces.db")

st.set_page_config(
    page_title=APP_TITLE,
    page_icon="🚗",
    layout="wide",
)

NO_CONTACT_PATTERNS = [
    r"\bpas de demarchage",
    r"\bpas d'appel",
    r"\bpas d appel",
    r"\bne pas appeler",
    r"\bpas de telephone",
    r"\bprofessionnels s'abstenir",
    r"\bprofessionnels s abstenir",
    r"\bpas de pro\b",
    r"\bpas de garage\b",
    r"\bpas d'agence\b",
]

PRO_PATTERNS = [
    r"\bprofessionnel\b",
    r"\bprofessionnels\b",
    r"\bgarage\b",
    r"\bconcessionnaire\b",
    r"\bnegoce automobile\b",
    r"\bauto-entrepreneur\b",
    r"\bSIRET\b",
    r"\bgarantie 3 mois\b",
    r"\bgarantie 6 mois\b",
    r"\bgarantie 12 mois\b",
    r"\breprise possible\b",
]

DEFECT_PATTERNS = [
    r"\bmoteur HS\b",
    r"\bmoteur a changer\b",
    r"\bboite HS\b",
    r"\bboite a changer\b",
    r"\bnon roulante\b",
    r"\bne demarre pas\b",
    r"\baccidentee\b",
    r"\bpour pieces\b",
    r"\bjoint de culasse\b",
    r"\bprobleme moteur\b",
]

PHONE_RE = re.compile(
    r"(?<!\d)(?:\+33\s?[1-9](?:[\s.-]?\d{2}){4}"
    r"|0[1-9](?:[\s.-]?\d{2}){4})(?!\d)"
)


def norm(value):
    if value is None or pd.isna(value):
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def clean_text(value):
    text = norm(value).lower()
    replacements = str.maketrans({
        "é": "e", "è": "e", "ê": "e", "ë": "e",
        "à": "a", "â": "a", "î": "i", "ï": "i",
        "ô": "o", "ù": "u", "û": "u", "ç": "c",
    })
    return text.translate(replacements)


def has_pattern(text, patterns):
    text = clean_text(text)
    return any(
        re.search(pattern, text, flags=re.I)
        for pattern in patterns
    )


def to_number(value):
    if value is None or pd.isna(value):
        return None
    try:
        result = float(str(value).replace(" ", "").replace(",", "."))
        return result if pd.notna(result) else None
    except (ValueError, TypeError):
        return None


def evaluate_listing(
    row,
    radius_km=30,
    min_price=3000,
    max_price=200000,
):
    title = norm(row.get("title"))
    description = norm(row.get("description"))
    combined = title + " " + description

    reasons = []

    no_contact = has_pattern(combined, NO_CONTACT_PATTERNS)
    professional = has_pattern(combined, PRO_PATTERNS)
    defect = has_pattern(combined, DEFECT_PATTERNS)

    seller = clean_text(row.get("seller_type", ""))

    price = to_number(row.get("price_eur"))
    distance = to_number(row.get("distance_km"))
    market = to_number(row.get("market_price_eur"))

    explicit_pro = seller in {
        "pro", "professionnel", "business", "dealer"
    }

    if explicit_pro:
        reasons.append("Vendeur professionnel")
    elif professional:
        reasons.append("Vendeur potentiellement professionnel")

    if no_contact:
        reasons.append("Restriction de contact détectée")

    if defect:
        reasons.append("Défaut important signalé")

    if price is None or not min_price <= price <= max_price:
        reasons.append("Prix absent ou hors plage")

    if distance is None:
        reasons.append("Distance non vérifiée")
    elif distance < 0 or distance > radius_km:
        reasons.append("Distance hors rayon")

    # Les exclusions sont appliquées avant l'estimation de marché.
    if no_contact or explicit_pro:
        status = "Exclue"
    elif price is None or not min_price <= price <= max_price:
        status = "Exclue"
    elif distance is not None and (
        distance < 0 or distance > radius_km
    ):
        status = "Exclue"
    elif professional or defect or distance is None:
        status = "À vérifier"
    else:
        status = "Retenue"

    deal = "Non évaluée"
    discount = None

    if price is not None and market is not None and market > 0:
        discount = round((market - price) / market * 100, 1)

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

    phone = norm(row.get("phone_public"))

    if not phone:
        match = PHONE_RE.search(combined)
        phone = match.group(0) if match else ""

    return {
        "status": status,
        "deal": deal,
        "discount_pct": discount,
        "reasons": "; ".join(reasons) or "Aucun filtre bloquant détecté",
        "phone_found": phone,
    }


def ensure_db():
    with sqlite3.connect(DB_PATH) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS listings (
                listing_id TEXT PRIMARY KEY,
                title TEXT,
                price_eur REAL,
                make TEXT,
                model TEXT,
                year INTEGER,
                mileage_km REAL,
                fuel TEXT,
                transmission TEXT,
                location TEXT,
                distance_km REAL,
                seller_type TEXT,
                description TEXT,
                url TEXT,
                phone_public TEXT,
                market_price_eur REAL,
                first_seen TEXT,
                source TEXT,
                raw_json TEXT
            )
        """)


def safe_value(value):
    if value is None or pd.isna(value):
        return None
    return value


def load_file(uploaded):
    if uploaded.name.lower().endswith(".csv"):
        return pd.read_csv(uploaded, sep=None, engine="python")

    data = json.load(uploaded)
    if isinstance(data, dict):
        data = data.get("listings", [])
    if not isinstance(data, list):
        raise ValueError("Le JSON doit contenir une liste d'annonces.")
    return pd.DataFrame(data)


def save_listings(imported, source):
    saved = 0

    columns = [
        "listing_id", "title", "price_eur", "make", "model",
        "year", "mileage_km", "fuel", "transmission", "location",
        "distance_km", "seller_type", "description", "url",
        "phone_public", "market_price_eur", "first_seen",
        "source", "raw_json",
    ]

    with sqlite3.connect(DB_PATH) as con:
        for _, series in imported.iterrows():
            row = series.to_dict()

            url = norm(row.get("url"))
            title = norm(row.get("title"))
            price = to_number(row.get("price_eur"))

            listing_id = (
                norm(row.get("listing_id"))
                or url
                or f"{title}-{price}"
            )

            if not listing_id.strip("-"):
                continue

            analysis = evaluate_listing(
                row,
                radius_km=radius,
                min_price=min_price,
                max_price=max_price,
            )

            payload = {
                "listing_id": listing_id,
                "title": title,
                "price_eur": price,
                "make": norm(row.get("make")),
                "model": norm(row.get("model")),
                "year": to_number(row.get("year")),
                "mileage_km": to_number(row.get("mileage_km")),
                "fuel": norm(row.get("fuel")),
                "transmission": norm(row.get("transmission")),
                "location": norm(row.get("location")),
                "distance_km": to_number(row.get("distance_km")),
                "seller_type": norm(row.get("seller_type")),
                "description": norm(row.get("description")),
                "url": url,
                "phone_public": analysis["phone_found"],
                "market_price_eur": to_number(
                    row.get("market_price_eur")
                ),
                "first_seen": norm(row.get("first_seen"))
                    or datetime.now().isoformat(timespec="seconds"),
                "source": source,
                "raw_json": json.dumps(
                    row, ensure_ascii=False, default=str
                ),
            }

            values = tuple(
                safe_value(payload[key]) for key in columns
            )

            con.execute(
                """
                INSERT OR REPLACE INTO listings
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?)
                """,
                values,
            )
            saved += 1

    return saved


ensure_db()

st.title(APP_TITLE)
st.caption(
    "Écully (69130) · annonces de particuliers · "
    "rayon et budget personnalisables"
)

st.info(
    "Importe des annonces provenant d'une source autorisée. "
    "La collecte automatique nécessite une API ou un flux autorisé."
)

with st.sidebar:
    st.header("Paramètres")
    center = st.text_input("Centre de recherche", "Écully, 69130")
    radius = st.slider("Rayon (km)", 5, 100, 30, 5)
    min_price = st.number_input(
        "Prix minimum (€)", 0, 200000, 3000, 500
    )
    max_price = st.number_input(
        "Prix maximum (€)", 3000, 500000, 200000, 5000
    )

    if min_price > max_price:
        st.error("Le prix minimum doit être inférieur au maximum.")

st.subheader("1. Importer les annonces")

uploaded = st.file_uploader(
    "Fichier CSV ou JSON",
    type=["csv", "json"],
)

if uploaded and min_price <= max_price:
    try:
        imported = load_file(uploaded)

        st.success(f"{len(imported)} annonce(s) importée(s).")
        st.dataframe(imported.head(10), use_container_width=True)

        if st.button("Analyser et enregistrer", type="primary"):
            count = save_listings(imported, uploaded.name)
            st.session_state["import_done"] = count
            st.success(f"{count} annonce(s) enregistrée(s).")
            st.rerun()

    except Exception as error:
        st.error(f"Erreur pendant l'import : {error}")

st.subheader("2. Tableau de bord")

with sqlite3.connect(DB_PATH) as con:
    df = pd.read_sql_query(
        "SELECT * FROM listings ORDER BY first_seen DESC",
        con,
    )

if df.empty:
    st.info("Aucune annonce enregistrée. Importe un fichier CSV.")
else:
    analyses = [
        evaluate_listing(
            row,
            radius_km=radius,
            min_price=min_price,
            max_price=max_price,
        )
        for row in df.to_dict("records")
    ]

    df["status"] = [a["status"] for a in analyses]
    df["deal"] = [a["deal"] for a in analyses]
    df["reasons"] = [a["reasons"] for a in analyses]
    df["discount_pct"] = [a["discount_pct"] for a in analyses]

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Annonces en base", len(df))
    c2.metric(
        "Retenues / à vérifier",
        int(df["status"].isin(["Retenue", "À vérifier"]).sum()),
    )
    c3.metric(
        "Bonnes affaires",
        int(df["deal"].eq("Bonne affaire").sum()),
    )
    c4.metric(
        "Très bonnes affaires",
        int(df["deal"].eq("Très bonne affaire").sum()),
    )

    statuses = st.multiselect(
        "Afficher les statuts",
        ["Retenue", "À vérifier", "Exclue"],
        default=["Retenue", "À vérifier"],
    )

    deals = st.multiselect(
        "Catégories de prix",
        [
            "Très bonne affaire",
            "Bonne affaire",
            "Prix équitable",
            "Au-dessus du marché",
            "Estimation marché nécessaire",
            "Non évaluée",
        ],
        default=[
            "Très bonne affaire",
            "Bonne affaire",
            "Prix équitable",
            "Estimation marché nécessaire",
        ],
    )

    view = df[
        df["status"].isin(statuses) & df["deal"].isin(deals)
    ].copy()

    st.dataframe(
        view[
            [
                "title", "price_eur", "make", "model", "year",
                "mileage_km", "location", "distance_km",
                "seller_type", "deal", "status", "reasons",
                "url",
            ]
        ],
        use_container_width=True,
        hide_index=True,
    )

    st.download_button(
        "Exporter les résultats CSV",
        view.to_csv(index=False).encode("utf-8-sig"),
        "dtsa_resultats.csv",
        "text/csv",
    )

st.subheader("3. Limites et sécurité")
st.markdown("""
- Les annonces douteuses doivent être vérifiées manuellement.
- Le bot n'essaie pas de contourner les protections des sites.
- Il n'invente pas d'estimation de prix de marché.
- Le téléphone est lu uniquement dans les données fournies.
- La distance doit être fournie par la source importée.
- Une base SQLite locale peut être temporaire sur certains hébergeurs.
- Vérifie les conditions d'utilisation des sources et les obligations RGPD.
""")
