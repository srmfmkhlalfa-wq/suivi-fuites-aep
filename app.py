"""
SUIVI DES FUITES & INTERVENTIONS - Réseau AEP
=============================================
Application Streamlit (Python) utilisable sur PC et smartphone.

Pages :
  1. Nouvelle intervention  -> formulaire de saisie (opérateurs, mobile)
  2. Clôturer / compléter   -> ajouter début / fin de réparation à une fuite ouverte
  3. Tableau de bord        -> statistiques, tableau interactif, export Excel / CSV

Base de données :
  - Par défaut : SQLite local (fichier fuites.db)  -> pour tester
  - Production : PostgreSQL (ex. Supabase) via DATABASE_URL dans les secrets
    (SQLite sur Streamlit Cloud est EFFACÉ à chaque redémarrage !)

Notifications :
  - Liens WhatsApp pré-remplis (wa.me)  -> gratuit, 1 clic par destinataire
  - Envoi 100 % automatique via Twilio (optionnel) si les secrets TWILIO_* sont définis
"""

import io
import os
import urllib.parse
from datetime import datetime, date
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st
from sqlalchemy import (
    Column, DateTime, Float, Integer, MetaData, String, Table, Text,
    create_engine, insert, select, update,
)

# Géolocalisation GPS du smartphone (optionnelle)
try:
    from streamlit_geolocation import streamlit_geolocation
except ImportError:  # le champ texte reste utilisable
    streamlit_geolocation = None

# ----------------------------------------------------------------------------
# 1. CONFIGURATION (à adapter ici)
# ----------------------------------------------------------------------------
CENTRES = ["BOUHOUDA", "KHLALFA", "ZRIZER", "RGHIOUA", "BOUADEL", "BENI OULID"]

NATURES_CONDUITE = ["PVC", "PEHD", "Fonte", "AC (amiante-ciment)", "Acier", "Galvanisé", "Autre"]

DIAMETRES_MM = ["20", "25", "32", "40", "50", "63", "75", "90", "110", "125",
                "160", "200", "250", "315", "400", "Autre"]

# Destinataires des notifications (format international SANS le +)
DESTINATAIRES = {
    "AYOUB": "212666986132",
    "ABDELILAH": "212666384882",
}

FUSEAU = ZoneInfo("Africa/Casablanca")

st.set_page_config(page_title="Suivi des fuites AEP", page_icon="💧", layout="wide")


# ----------------------------------------------------------------------------
# 2. OUTILS : secrets, heure locale, formats
# ----------------------------------------------------------------------------
def secret(key, default=None):
    """Lit un secret Streamlit, sinon une variable d'environnement, sinon défaut."""
    try:
        return st.secrets[key]
    except Exception:
        return os.environ.get(key, default)


def maintenant():
    """Heure locale du Maroc, sans fuseau (naïve) pour simplifier le stockage."""
    return datetime.now(FUSEAU).replace(tzinfo=None, second=0, microsecond=0)


def fmt_dt(x):
    return "—" if x is None or pd.isna(x) else pd.Timestamp(x).strftime("%d/%m/%Y %H:%M")


def fmt_duree(heures):
    """Convertit des heures décimales en 'X h YY min'."""
    if heures is None or pd.isna(heures):
        return "—"
    total_min = int(round(heures * 60))
    return f"{total_min // 60} h {total_min % 60:02d} min"


# ----------------------------------------------------------------------------
# 3. BASE DE DONNÉES (SQLAlchemy : même code pour SQLite et PostgreSQL)
# ----------------------------------------------------------------------------
metadata = MetaData()
interventions = Table(
    "interventions", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("centre", String(50), nullable=False),
    Column("localisation", Text),
    Column("latitude", Float),
    Column("longitude", Float),
    Column("nature_conduite", String(50)),
    Column("diametre_mm", String(20)),
    Column("date_signalement", DateTime, nullable=False),
    Column("date_debut", DateTime),
    Column("date_fin", DateTime),
    Column("agent", String(100)),
    Column("observations", Text),
    Column("cree_le", DateTime),
)


@st.cache_resource
def get_engine():
    url = secret("DATABASE_URL", "sqlite:///fuites.db")
    # Supabase fournit 'postgres://' ou 'postgresql://' ; on force le pilote psycopg2
    # (celui installé via requirements.txt), sinon SQLAlchemy 2 cherche 'psycopg' v3.
    for prefixe in ("postgres://", "postgresql://"):
        if url.startswith(prefixe):
            url = "postgresql+psycopg2://" + url[len(prefixe):]
            break
    engine = create_engine(url, pool_pre_ping=True)
    metadata.create_all(engine)  # crée la table si elle n'existe pas
    return engine


def charger_donnees() -> pd.DataFrame:
    """Charge toutes les interventions et calcule statut + délai de réparation."""
    with get_engine().connect() as conn:
        df = pd.read_sql(
            select(interventions).order_by(interventions.c.date_signalement.desc()), conn
        )
    for c in ["date_signalement", "date_debut", "date_fin", "cree_le"]:
        df[c] = pd.to_datetime(df[c])
    df["statut"] = "Signalée"
    df.loc[df["date_debut"].notna(), "statut"] = "En cours"
    df.loc[df["date_fin"].notna(), "statut"] = "Réparée"
    # Délai = signalement -> fin de réparation (durée de coupure estimée)
    df["delai_h"] = (df["date_fin"] - df["date_signalement"]).dt.total_seconds() / 3600
    return df


def ajouter_intervention(data: dict) -> int:
    data["cree_le"] = maintenant()
    with get_engine().begin() as conn:
        res = conn.execute(insert(interventions).values(**data))
        return res.inserted_primary_key[0]


def modifier_intervention(id_: int, data: dict):
    with get_engine().begin() as conn:
        conn.execute(update(interventions).where(interventions.c.id == id_).values(**data))


# ----------------------------------------------------------------------------
# 4. NOTIFICATIONS
# ----------------------------------------------------------------------------
def construire_message(type_: str, r: dict) -> str:
    """Message récapitulatif. type_ = 'nouvelle' ou 'cloture'."""
    titre = "🚨 NOUVELLE FUITE SIGNALÉE" if type_ == "nouvelle" else "✅ INTERVENTION CLÔTURÉE"
    lignes = [
        titre,
        f"Centre : {r['centre']}",
        f"Lieu : {r.get('localisation') or '—'}",
        f"Conduite : {r.get('nature_conduite') or '—'} Ø{r.get('diametre_mm') or '—'} mm",
        f"Signalée le : {fmt_dt(r.get('date_signalement'))}",
    ]
    if r.get("date_debut") is not None and not pd.isna(r.get("date_debut")):
        lignes.append(f"Début intervention : {fmt_dt(r['date_debut'])}")
    if type_ == "cloture":
        lignes.append(f"Fin réparation : {fmt_dt(r.get('date_fin'))}")
        h = (pd.Timestamp(r["date_fin"]) - pd.Timestamp(r["date_signalement"])).total_seconds() / 3600
        lignes.append(f"Durée totale : {fmt_duree(h)}")
    lignes.append(f"Agent : {r.get('agent') or '—'}")
    if r.get("observations"):
        lignes.append(f"Obs. : {r['observations']}")
    if r.get("latitude") and r.get("longitude"):
        lignes.append(f"📍 https://maps.google.com/?q={r['latitude']},{r['longitude']}")
    return "\n".join(lignes)


def envoyer_twilio(message: str) -> str:
    """Envoi automatique via Twilio (WhatsApp ou SMS). Retourne un texte de statut."""
    sid, token = secret("TWILIO_SID"), secret("TWILIO_TOKEN")
    expediteur = secret("TWILIO_FROM")  # ex. +14155238886 (sandbox WhatsApp) ou numéro SMS
    if not (sid and token and expediteur):
        return ""  # Twilio non configuré -> on se rabat sur les liens wa.me
    canal = secret("TWILIO_CHANNEL", "whatsapp")  # 'whatsapp' ou 'sms'
    try:
        from twilio.rest import Client
        client = Client(sid, token)
        for nom, num in DESTINATAIRES.items():
            prefixe = "whatsapp:" if canal == "whatsapp" else ""
            client.messages.create(
                from_=f"{prefixe}{expediteur}", to=f"{prefixe}+{num}", body=message
            )
        return f"Message envoyé automatiquement ({canal}) à {', '.join(DESTINATAIRES)}."
    except Exception as e:  # ne jamais bloquer la saisie à cause d'un échec d'envoi
        return f"⚠️ Échec Twilio : {e}"


def preparer_notification(type_: str, row: dict):
    """Stocke le message à afficher (liens wa.me) après enregistrement."""
    message = construire_message(type_, row)
    statut_auto = envoyer_twilio(message)
    st.session_state["notif"] = {"message": message, "auto": statut_auto}


def afficher_notification_en_attente():
    notif = st.session_state.get("notif")
    if not notif:
        return
    with st.container(border=True):
        st.success("Enregistrement effectué ✅")
        if notif["auto"]:
            st.info(notif["auto"])
        st.markdown("**Prévenir les chefs de centre (WhatsApp) :**")
        cols = st.columns(len(DESTINATAIRES))
        for col, (nom, num) in zip(cols, DESTINATAIRES.items()):
            url = f"https://wa.me/{num}?text={urllib.parse.quote(notif['message'])}"
            col.link_button(f"📲 Envoyer à {nom}", url, width="stretch")
        with st.expander("Voir le message"):
            st.code(notif["message"], language=None)
        if st.button("Fermer"):
            st.session_state.pop("notif")
            st.rerun()


# ----------------------------------------------------------------------------
# 5. AUTHENTIFICATION SIMPLE (mot de passe partagé, optionnel)
# ----------------------------------------------------------------------------
def verifier_acces() -> bool:
    mdp = secret("APP_PASSWORD")
    if not mdp or st.session_state.get("ok"):
        return True
    st.title("💧 Suivi des fuites AEP")
    saisi = st.text_input("Mot de passe", type="password")
    if st.button("Entrer", type="primary"):
        if saisi == str(mdp):
            st.session_state["ok"] = True
            st.rerun()
        else:
            st.error("Mot de passe incorrect.")
    return False


# ----------------------------------------------------------------------------
# 6. COMPOSANT : saisie date + heure
# ----------------------------------------------------------------------------
def saisie_datetime(label: str, cle: str, facultatif=False):
    """Retourne un datetime (ou None si facultatif et non renseigné)."""
    if facultatif and not st.checkbox(f"{label} : renseigner", key=f"{cle}_on"):
        return None
    c1, c2 = st.columns(2)
    now = maintenant()
    d = c1.date_input(f"{label} - date", value=now.date(), key=f"{cle}_d")
    t = c2.time_input(f"{label} - heure", value=now.time(), key=f"{cle}_t", step=300)
    return datetime.combine(d, t)


# ----------------------------------------------------------------------------
# 7. PAGE : NOUVELLE INTERVENTION
# ----------------------------------------------------------------------------
def page_saisie():
    st.header("➕ Nouvelle intervention")
    n = st.session_state.setdefault("form_n", 0)  # change à chaque envoi -> formulaire vidé
    k = lambda nom: f"{nom}_{n}"

    centre = st.selectbox("Centre concerné *", CENTRES, index=None,
                          placeholder="Choisir un centre…", key=k("centre"))
    localisation = st.text_input("Localisation (douar, lieu-dit, repère) *", key=k("loc"))

    lat = lon = None
    if streamlit_geolocation:
        st.caption("Ou utiliser la position GPS du téléphone :")
        pos = streamlit_geolocation()
        if pos and pos.get("latitude"):
            lat, lon = pos["latitude"], pos["longitude"]
            st.success(f"📍 Position captée : {lat:.5f}, {lon:.5f}")

    c1, c2 = st.columns(2)
    nature = c1.selectbox("Nature de la conduite", NATURES_CONDUITE, key=k("nat"))
    diam = c2.selectbox("Diamètre (mm)", DIAMETRES_MM, index=5, key=k("diam"))
    if diam == "Autre":
        diam = st.text_input("Préciser le diamètre (mm)", key=k("diam_autre"))

    st.subheader("Dates et heures")
    d_sig = saisie_datetime("Signalement / détection *", k("sig"))
    d_deb = saisie_datetime("Début d'intervention", k("deb"), facultatif=True)
    d_fin = saisie_datetime("Fin de réparation", k("fin"), facultatif=True)

    agent = st.text_input("Nom de l'agent intervenant", key=k("agent"))
    obs = st.text_area("Observations / notes", key=k("obs"))

    if st.button("💾 Enregistrer", type="primary", width="stretch"):
        # --- Validations ---
        erreurs = []
        if not centre:
            erreurs.append("Le centre est obligatoire.")
        if not localisation.strip() and lat is None:
            erreurs.append("Indiquez une localisation (texte ou GPS).")
        if d_deb and d_deb < d_sig:
            erreurs.append("Le début d'intervention est antérieur au signalement.")
        if d_fin and not d_deb:
            erreurs.append("Renseignez aussi le début d'intervention.")
        if d_fin and d_deb and d_fin < d_deb:
            erreurs.append("La fin de réparation est antérieure au début.")
        if erreurs:
            for e in erreurs:
                st.error(e)
            return

        row = dict(
            centre=centre, localisation=localisation.strip(), latitude=lat, longitude=lon,
            nature_conduite=nature, diametre_mm=str(diam), date_signalement=d_sig,
            date_debut=d_deb, date_fin=d_fin, agent=agent.strip(), observations=obs.strip(),
        )
        ajouter_intervention(dict(row))
        # Fuite déjà réparée à la saisie -> message de clôture, sinon nouvelle fuite
        preparer_notification("cloture" if d_fin else "nouvelle", row)
        st.session_state["form_n"] = n + 1
        st.rerun()


# ----------------------------------------------------------------------------
# 8. PAGE : CLÔTURER / COMPLÉTER
# ----------------------------------------------------------------------------
def page_cloture():
    st.header("🔧 Clôturer / compléter une intervention")
    df = charger_donnees()
    ouvertes = df[df["date_fin"].isna()]
    if ouvertes.empty:
        st.info("Aucune fuite en attente de réparation. 🎉")
        return

    libelles = {
        int(r.id): f"#{r.id} · {r.centre} · {r.localisation or '—'} · signalée {fmt_dt(r.date_signalement)} · {r.statut}"
        for r in ouvertes.itertuples()
    }
    choix = st.selectbox("Fuite à mettre à jour", list(libelles), format_func=libelles.get)
    r = ouvertes[ouvertes["id"] == choix].iloc[0]

    st.caption(f"{r['nature_conduite']} Ø{r['diametre_mm']} mm")
    d_deb = r["date_debut"]
    if pd.isna(d_deb):
        d_deb = saisie_datetime("Début d'intervention", f"cl_deb_{choix}")
    else:
        st.write(f"Début d'intervention : **{fmt_dt(d_deb)}**")
    d_fin = saisie_datetime("Fin de réparation", f"cl_fin_{choix}")
    agent = st.text_input("Agent intervenant", value=r["agent"] or "", key=f"cl_ag_{choix}")
    obs = st.text_area("Observations", value=r["observations"] or "", key=f"cl_obs_{choix}")

    if st.button("✅ Clôturer l'intervention", type="primary", width="stretch"):
        d_deb = pd.Timestamp(d_deb).to_pydatetime()
        if d_deb < r["date_signalement"].to_pydatetime():
            return st.error("Le début est antérieur au signalement.")
        if d_fin < d_deb:
            return st.error("La fin de réparation est antérieure au début.")
        maj = dict(date_debut=d_deb, date_fin=d_fin, agent=agent.strip(), observations=obs.strip())
        modifier_intervention(int(choix), maj)
        complet = {**r.to_dict(), **maj}
        preparer_notification("cloture", complet)
        st.rerun()


# ----------------------------------------------------------------------------
# 9. EXPORTS
# ----------------------------------------------------------------------------
COLONNES_EXPORT = {
    "id": "N°", "centre": "Centre", "localisation": "Localisation",
    "latitude": "Latitude", "longitude": "Longitude",
    "nature_conduite": "Nature conduite", "diametre_mm": "Diamètre (mm)",
    "date_signalement": "Date signalement", "date_debut": "Début intervention",
    "date_fin": "Fin réparation", "delai_h": "Délai réparation (h)",
    "agent": "Agent", "observations": "Observations", "statut": "Statut",
}


def preparer_export(df: pd.DataFrame) -> pd.DataFrame:
    out = df[list(COLONNES_EXPORT)].rename(columns=COLONNES_EXPORT).copy()
    out["Délai réparation (h)"] = out["Délai réparation (h)"].round(2)
    return out


def vers_excel(df: pd.DataFrame) -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        df.to_excel(w, index=False, sheet_name="Interventions")
        ws = w.sheets["Interventions"]
        ws.freeze_panes = "A2"
        for col in ws.columns:  # largeur de colonne automatique
            largeur = max(len(str(c.value)) if c.value is not None else 0 for c in col)
            ws.column_dimensions[col[0].column_letter].width = min(max(12, largeur + 2), 50)
    return buf.getvalue()


# ----------------------------------------------------------------------------
# 10. PAGE : TABLEAU DE BORD
# ----------------------------------------------------------------------------
def page_dashboard():
    st.header("📊 Tableau de bord")
    df = charger_donnees()
    if st.button("🔄 Actualiser"):
        st.rerun()
    if df.empty:
        st.info("Aucune intervention enregistrée pour le moment.")
        return

    # --- Filtres (appliqués aux stats, au tableau ET à l'export) ---
    with st.container(border=True):
        f1, f2, f3 = st.columns([2, 2, 2])
        centres_sel = f1.multiselect("Centre(s)", CENTRES, placeholder="Tous les centres")
        dmin = df["date_signalement"].min().date()
        periode = f2.date_input("Période (signalement)", value=(dmin, date.today()))
        statuts = f3.multiselect("Statut", ["Signalée", "En cours", "Réparée"],
                                 placeholder="Tous les statuts")

    vue = df.copy()
    if centres_sel:
        vue = vue[vue["centre"].isin(centres_sel)]
    if statuts:
        vue = vue[vue["statut"].isin(statuts)]
    if isinstance(periode, (tuple, list)) and len(periode) == 2:
        vue = vue[(vue["date_signalement"].dt.date >= periode[0])
                  & (vue["date_signalement"].dt.date <= periode[1])]

    # --- Export (bouton bien visible) ---
    export = preparer_export(vue)
    e1, e2, _ = st.columns([2, 2, 3])
    nom = f"interventions_{maintenant():%Y%m%d_%H%M}"
    e1.download_button("📥 Télécharger en Excel", vers_excel(export), f"{nom}.xlsx",
                       "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                       type="primary", width="stretch")
    e2.download_button("📄 Exporter en CSV", export.to_csv(index=False, sep=";").encode("utf-8-sig"),
                       f"{nom}.csv", "text/csv", width="stretch")

    # --- Indicateurs clés ---
    reparees = vue[vue["statut"] == "Réparée"]
    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Fuites signalées", len(vue))
    k2.metric("Réparées", len(reparees))
    k3.metric("En attente / en cours", len(vue) - len(reparees))
    k4.metric("Délai moyen de réparation", fmt_duree(reparees["delai_h"].mean()))

    # --- Répartition par centre ---
    st.subheader("Répartition par centre")
    par_centre = (
        vue.groupby("centre")
        .agg(Fuites=("id", "count"),
             Réparées=("statut", lambda s: (s == "Réparée").sum()),
             delai=("delai_h", "mean"))
        .reindex(CENTRES, fill_value=0)
    )
    g1, g2 = st.columns([3, 2])
    g1.bar_chart(par_centre[["Fuites", "Réparées"]])
    tab = par_centre.assign(**{"Délai moyen": par_centre["delai"].map(fmt_duree)})
    g2.dataframe(tab[["Fuites", "Réparées", "Délai moyen"]], width="stretch")

    # --- Tableau interactif (tri, recherche, plein écran via l'icône du tableau) ---
    st.subheader(f"Liste des interventions ({len(vue)})")
    st.dataframe(
        export, width="stretch", hide_index=True,
        column_config={
            "Date signalement": st.column_config.DatetimeColumn(format="DD/MM/YYYY HH:mm"),
            "Début intervention": st.column_config.DatetimeColumn(format="DD/MM/YYYY HH:mm"),
            "Fin réparation": st.column_config.DatetimeColumn(format="DD/MM/YYYY HH:mm"),
        },
    )


# ----------------------------------------------------------------------------
# 11. ROUTAGE
# ----------------------------------------------------------------------------
def main():
    if not verifier_acces():
        return
    st.sidebar.title("💧 Suivi des fuites AEP")
    page = st.sidebar.radio(
        "Menu", ["➕ Nouvelle intervention", "🔧 Clôturer une intervention", "📊 Tableau de bord"],
        label_visibility="collapsed",
    )
    afficher_notification_en_attente()
    if page.startswith("➕"):
        page_saisie()
    elif page.startswith("🔧"):
        page_cloture()
    else:
        page_dashboard()


main()
