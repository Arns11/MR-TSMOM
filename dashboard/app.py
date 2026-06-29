"""
dashboard/app.py
==================
Dashboard Streamlit du combo XNDX MR LO + TSMOM LO sauf CL.

6 onglets : Live | Backtest | Composition | WFO | Présentation | Doc technique

USAGE : streamlit run dashboard/app.py
"""

import sys
import json
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import streamlit as st
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.strategy import (
    load_config, build_xndx_mr_positions, build_tsmom_positions,
    compute_combo_returns, compute_stats, yearly_breakdown,
    compute_zscore, generate_positions_mr, compute_eff_mr,
)

DATA_DIR = ROOT / "data"
RESULTS_DIR = ROOT / "results"
RESULTS_DIR.mkdir(exist_ok=True)
CONFIG_PATH = ROOT / "config" / "parameters.json"

st.set_page_config(page_title="Combo XNDX MR + TSMOM", page_icon="📈", layout="wide")

TRADING_DAYS = 252


# ====================================================================
# CACHE
# ====================================================================
@st.cache_data(ttl=3600)
def load_prices_cached(asset):
    csv_path = DATA_DIR / f"{asset}.csv"
    if not csv_path.exists():
        return None
    return pd.read_csv(csv_path, parse_dates=["date"], index_col="date")["close"]


@st.cache_data(ttl=3600)
def get_config_cached():
    return load_config(CONFIG_PATH)


@st.cache_data(ttl=3600)
def build_all_signals_cached(_prices_xndx, _prices_dict_keys):
    cfg = get_config_cached()
    prices_dict = {a: load_prices_cached(a) for a in _prices_dict_keys}
    prices_xndx = prices_dict["XNDX"]
    pos_mr, eff_xndx = build_xndx_mr_positions(prices_xndx, cfg)
    eff_tsmom = build_tsmom_positions(prices_dict, cfg)
    return pos_mr, eff_xndx, eff_tsmom, prices_dict


# ====================================================================
# UTILS
# ====================================================================
def get_eur_usd():
    return get_config_cached()["execution"]["eur_usd_default"]


def now_ny():
    return datetime.now(ZoneInfo("America/New_York"))


def compute_min_capital_etf(leverage):
    """ETF/CFD : granularite fine partout. Capital min faible."""
    base = 10_000
    return int(base / max(leverage, 1.0))


def compute_min_capital_mix(leverage, cfg):
    """
    Mix : MR en MNQ futures (50% capital) + TSMOM en ETF (granularite fine).
    Contrainte : avoir au moins 1 MNQ a la cible MR.
    MR cible = 50% × capital × vol_mult × leverage.
    1 MNQ ~= 46k EUR.
    capital_min = 46000 / (0.5 × 2 × leverage) ~ 46k a leverage 1×.
    On accepte 0.5 contrat min (arrondi a 1) -> capital_min = 23k a leverage 1×.
    Mais en pratique, on veut un peu de marge -> 50k base recommandé.
    """
    mnq_notional_eur = 46_000
    alloc_mr = cfg["allocation"]["mr"]
    vol_mult_avg = 2.0
    cap_min = 0.5 * mnq_notional_eur / (alloc_mr * vol_mult_avg * leverage)
    return int(cap_min)


def compute_min_capital_futures(leverage, cfg):
    """
    Futures purs : MNQ + MES + MGC + MCL + TLT ETF.
    Le plus contraignant : MNQ pour la jambe MR (50% capital).
    + il faut que TSMOM (10% par actif) ait granularite acceptable sur MES, MNQ, MGC, MCL.
    Le pire actif TSMOM = XNDX (10% × capital × 2 × leverage vs MNQ 46k EUR).
    cap_min_tsmom_xndx = 0.5 × 46000 / (0.1 × 2 × leverage) ~ 115k a leverage 1×
    Donc futures purs = 200k min a leverage 1x (avec marge).
    """
    mnq_notional_eur = 46_000
    alloc_ts_per_asset = 0.1
    vol_mult_avg = 2.0
    # Cible 0.5 contrat MNQ minimum sur la jambe TSMOM XNDX
    cap_min_tsmom = 0.5 * mnq_notional_eur / (alloc_ts_per_asset * vol_mult_avg * leverage)
    return int(cap_min_tsmom)


# ====================================================================
# SIMULATION REELLE (avec arrondis contrats entiers)
# ====================================================================
def get_unit_notional_funcs(vehicle_mode, cfg, prices_dict, common_idx):
    """
    Retourne 2 fonctions : unit_notional pour la jambe MR et pour la jambe TSMOM.
    
    3 modes :
    - etf : tout en ETF/CFD (QQQ, SPY, GLD, TLT, CFD_WTI)
    - mix : MR en futures MNQ, TSMOM en ETF/CFD
    - futures : MR en MNQ, TSMOM en MES/MNQ/MGC/MCL (TLT reste ETF, ZN trop gros)
    """
    contracts = cfg["vehicules"]["futures"]["instruments"]

    def etf_notional(asset, day_idx):
        price = prices_dict[asset].loc[common_idx[day_idx]]
        if asset == "XNDX":
            return price / 50  # QQQ ~ NDX/50
        elif asset == "SPXTR":
            return price / 100  # SPY ~ SPX/100 (approximation)
        else:
            return price  # GLD, TLT, CFD WTI

    def futures_notional(asset, day_idx):
        price = prices_dict[asset].loc[common_idx[day_idx]]
        if asset == "TLT":
            return price  # TLT reste ETF (ZN trop gros)
        return price * contracts[asset]["mult"]

    if vehicle_mode == "etf":
        return etf_notional, etf_notional
    elif vehicle_mode == "mix":
        # MR en futures, TSMOM en ETF
        return futures_notional, etf_notional
    else:  # futures
        return futures_notional, futures_notional


def simulate_realistic_returns(params, eff_xndx, eff_tsmom, prices_dict, cfg, start_date, strategy="combo"):
    """
    Simule rendements REELS avec arrondis aux unites entieres.
    
    APPROCHE CORRIGEE : on calcule les rendements theoriques par actif via
    (eff * rets), puis on applique un facteur correctif d'arrondi 
    (= position_arrondie / position_continue) basé sur la position cible.
    
    Cela evite de recalculer la boucle PnL et donc le bug de decalage temporel.
    """
    capital_eur = params["capital_eur"]
    eur_usd = get_eur_usd()
    capital_usd = capital_eur * eur_usd
    leverage = params["leverage"]
    vehicle_mode = params.get("vehicle_mode", "etf")
    panier = cfg["tsmom"]["panier"]

    if strategy == "xndx_mr_only":
        alloc_mr, alloc_ts_per = 1.0, 0.0
    elif strategy == "tsmom_only":
        alloc_mr, alloc_ts_per = 0.0, 1.0 / len(panier)
    else:
        alloc_mr = cfg["allocation"]["mr"]
        alloc_ts_per = cfg["allocation"]["tsmom"] / len(panier)

    common_idx = eff_xndx.index
    for a in panier:
        common_idx = common_idx.intersection(prices_dict[a].index)
        common_idx = common_idx.intersection(eff_tsmom[a].index)
    common_idx = common_idx[common_idx >= pd.Timestamp(start_date)].sort_values()
    if len(common_idx) < 10:
        return pd.Series(dtype=float)

    notional_mr_fn, notional_ts_fn = get_unit_notional_funcs(
        vehicle_mode, cfg, prices_dict, common_idx
    )

    # === Pour chaque actif, calculer le ratio "arrondi / continu" ===
    # Position continue (en unites) : eff * alloc * capital / notional_unit
    # Position arrondie : round(continue)
    # Ratio = arrondi / continu (1.0 si continu, plus eloigne si petit capital)
    
    # On utilise un capital fixe (pas compoundé) pour simplifier le calcul de ratio
    cap_usd_ref = capital_usd  # référence
    
    def compute_ratio_series(eff_series, asset, notional_fn, alloc_per):
        """Calcule pour chaque jour : ratio = round(target) / target"""
        ratios = []
        for i in range(len(common_idx)):
            day = common_idx[i]
            eff_today = eff_series.loc[day] if not np.isnan(eff_series.loc[day]) else 0
            if eff_today == 0:
                ratios.append(1.0)  # pas de position, pas d'écart
                continue
            target_usd = alloc_per * cap_usd_ref * eff_today * leverage
            notional_unit = notional_fn(asset, i)
            if notional_unit <= 0:
                ratios.append(1.0)
                continue
            target_units = target_usd / notional_unit
            actual_units = round(target_units)
            ratio = actual_units / target_units if target_units != 0 else 1.0
            ratios.append(ratio)
        return pd.Series(ratios, index=common_idx)

    # Ratios par actif
    ratio_mr = compute_ratio_series(eff_xndx, "XNDX", notional_mr_fn, alloc_mr)
    ratios_ts = {}
    for a in panier:
        ratios_ts[a] = compute_ratio_series(eff_tsmom[a], a, notional_ts_fn, alloc_ts_per)

    # === Rendements theoriques (baseline) ===
    rets_xndx = prices_dict["XNDX"].pct_change().reindex(common_idx).fillna(0)
    eff_xndx_aligned = eff_xndx.reindex(common_idx).fillna(0)
    r_mr_theo = (eff_xndx_aligned * rets_xndx) * leverage
    
    r_ts_theo_per_asset = {}
    for a in panier:
        rets_a = prices_dict[a].pct_change().reindex(common_idx).fillna(0)
        eff_a = eff_tsmom[a].reindex(common_idx).fillna(0)
        r_ts_theo_per_asset[a] = (eff_a * rets_a) * leverage

    # === Application des ratios (arrondi) ===
    # r_real_a = r_theo_a * ratio_a (ratio appliqué décalé d'un jour comme eff)
    r_mr_real = r_mr_theo * ratio_mr.shift(1).fillna(1.0)
    
    r_ts_real_total = pd.Series(0.0, index=common_idx)
    for a in panier:
        ratio_a_shifted = ratios_ts[a].shift(1).fillna(1.0)
        r_ts_real_a = r_ts_theo_per_asset[a] * ratio_a_shifted
        r_ts_real_total += r_ts_real_a * alloc_ts_per  # 1/N pondération

    # Combo final
    if strategy == "xndx_mr_only":
        return r_mr_real * 1.0  # alloc_mr = 1.0
    elif strategy == "tsmom_only":
        # r_ts_real_total deja pondéré par alloc_ts_per = 1/N
        return r_ts_real_total
    else:
        # alloc_mr fois r_mr + r_ts_real_total (déjà × alloc_ts/N par actif)
        return alloc_mr * r_mr_real + r_ts_real_total


# ====================================================================
# SIDEBAR
# ====================================================================
def render_sidebar():
    st.sidebar.title("⚙️ Paramètres")

    # ===== TOGGLE STRATEGIE EN HAUT - TRES VISIBLE =====
    st.sidebar.markdown("### 🎯 Choisir la stratégie affichée")
    strategy_options = {
        "🟩 Combo (Tier 2 - 129€/mois)": "combo",
        "🟦 XNDX MR seul (Tier 1 - 39€/mois)": "xndx_mr_only",
        "⚙️ TSMOM seul (interne uniquement)": "tsmom_only",
    }
    strategy_choice = st.sidebar.radio(
        "Tous les onglets s'adaptent à ce choix.",
        options=list(strategy_options.keys()),
        index=0,
    )
    strategy_key = strategy_options[strategy_choice]
    # Label court pour affichage onglets
    strategy_label_short = {
        "combo": "Combo",
        "xndx_mr_only": "XNDX MR seul",
        "tsmom_only": "TSMOM seul",
    }[strategy_key]

    st.sidebar.markdown("---")
    st.sidebar.markdown("### 💰 Capital & Risque")

    capital_eur = st.sidebar.number_input(
        "Capital initial (EUR)", min_value=1000, max_value=10_000_000,
        value=100_000, step=5_000, format="%d"
    )

    leverage = st.sidebar.slider("Levier global", 1.0, 3.0, 1.0, 0.1)
    fees_bps = st.sidebar.slider("Frais transaction (bps)", 0.0, 20.0, 3.0, 0.5)
    borrow_short = st.sidebar.slider("Coût borrow shorts (% annuel)", 0.0, 5.0, 1.0, 0.25)

    cfg = get_config_cached()
    cap_min_etf = compute_min_capital_etf(leverage)
    cap_min_mix = compute_min_capital_mix(leverage, cfg)
    cap_min_futures = compute_min_capital_futures(leverage, cfg)

    st.sidebar.markdown("---")
    st.sidebar.markdown("### 📦 Véhicule")

    vehicule = st.sidebar.radio(
        f"Capital min recommandé à levier {leverage}× :",
        options=[
            f"ETF/CFD pur (min {cap_min_etf:,} EUR)",
            f"Mix : MR futures + TSMOM ETF/CFD (min {cap_min_mix:,} EUR)",
            f"Futures purs (min {cap_min_futures:,} EUR)"
        ],
        index=0,
    )
    if "ETF/CFD pur" in vehicule:
        vehicle_mode = "etf"
    elif "Mix" in vehicule:
        vehicle_mode = "mix"
    else:
        vehicle_mode = "futures"

    is_etf = vehicle_mode == "etf"  # gardé pour compat retro

    if vehicle_mode == "etf":
        cap_min = cap_min_etf
    elif vehicle_mode == "mix":
        cap_min = cap_min_mix
    else:
        cap_min = cap_min_futures

    if capital_eur < cap_min:
        st.sidebar.error(
            f"⚠️ Capital insuffisant\n\n"
            f"{capital_eur:,} EUR < {cap_min:,} EUR\n\n"
            f"Granularité dégradée → DD réel > backtest."
        )
    else:
        ratio = capital_eur / cap_min
        if ratio >= 2:
            st.sidebar.success(
                f"✓ Confortable ({ratio:.1f}× le min)",
                icon="✅",
            )
        else:
            st.sidebar.info(
                f"✓ Suffisant ({ratio:.1f}× le min)",
                icon="ℹ️",
            )
        
        # Explication du badge
        with st.sidebar.expander("ℹ️ Qu'est-ce que ce ratio ?"):
            st.markdown(f"""
Le ratio **{ratio:.1f}×** indique que votre capital ({capital_eur:,} EUR) est 
{ratio:.1f} fois supérieur au capital minimum recommandé ({cap_min:,} EUR) 
pour ce véhicule et ce levier.

**Interprétation** :
- < 1× : capital insuffisant, granularité dégradée
- 1× à 2× : **Suffisant** mais marge de manœuvre limitée
- ≥ 2× : **Confortable**, marge correcte pour absorber les fluctuations

Plus le ratio est élevé, plus la performance réelle se rapprochera 
de la performance théorique du backtest.
            """)

    return {
        "strategy": strategy_key,
        "strategy_label": strategy_label_short,
        "capital_eur": capital_eur,
        "leverage": leverage,
        "fees_bps": fees_bps,
        "borrow_short": borrow_short / 100,
        "vehicule": vehicule,
        "vehicle_mode": vehicle_mode,
        "is_etf": is_etf,  # retro-compat
        "cap_min": cap_min,
    }


# ====================================================================
# ONGLET LIVE
# ====================================================================
def render_live(params):
    st.header(f"🟢 Signal Live — {params['strategy_label']}")

    cfg = get_config_cached()
    panier = cfg["tsmom"]["panier"]
    prices_dict = {a: load_prices_cached(a) for a in panier}
    if any(p is None for p in prices_dict.values()):
        st.error("Données manquantes. Lancer `scripts/download_data.py`.")
        return

    last_dates = {a: p.index[-1] for a, p in prices_dict.items()}
    oldest_date = min(last_dates.values())
    today_ny = now_ny()
    days_old = (pd.Timestamp(today_ny.date()) - oldest_date).days
    
    # Compter uniquement les jours OUVRES depuis la dernière donnée
    business_days_old = pd.bdate_range(oldest_date + pd.Timedelta(days=1), today_ny.date())
    n_bdays = len(business_days_old)

    col1, col2, col3 = st.columns(3)
    col1.metric("Date NY", today_ny.strftime("%Y-%m-%d %H:%M ET"))
    col2.metric("Dernière donnée", oldest_date.strftime("%Y-%m-%d"))
    col3.metric("Jours ouvrés écoulés", f"{n_bdays}")

    # Logique alerte : > 1 jour ouvré sans donnée = anormal (apres 22h Paris)
    if n_bdays > 1:
        st.warning(
            f"⚠️ Données pas à jour : {n_bdays} jour(s) ouvré(s) écoulés depuis la dernière donnée.\n\n"
            f"Causes possibles : (1) téléchargement EODHD pas encore lancé aujourd'hui, "
            f"(2) marché fermé hier (jour férié US), (3) erreur API EODHD/Yahoo."
        )
        
        # Bouton de refresh automatique
        col_r1, col_r2 = st.columns([1, 3])
        with col_r1:
            if st.button("🔄 Rafraîchir données", type="primary"):
                with st.spinner("Téléchargement des données récentes via EODHD (fallback Yahoo)..."):
                    import subprocess
                    download_script = ROOT / "scripts" / "download_data.py"
                    try:
                        result = subprocess.run(
                            [sys.executable, str(download_script), "--refresh-only-recent"],
                            capture_output=True, text=True, timeout=180,
                            cwd=str(ROOT)
                        )
                        if result.returncode == 0:
                            st.success("✓ Données rafraîchies avec succès")
                            # Clear cache pour recharger les CSV
                            load_prices_cached.clear()
                            build_all_signals_cached.clear()
                            st.rerun()
                        else:
                            st.error(f"❌ Échec téléchargement :\n{result.stderr[-500:]}")
                    except subprocess.TimeoutExpired:
                        st.error("❌ Timeout téléchargement (>3 min)")
                    except Exception as e:
                        st.error(f"❌ Erreur : {e}")
        with col_r2:
            st.caption("Ce bouton lance le script `download_data.py --refresh-only-recent` "
                       "puis recharge le cache. À utiliser après un week-end ou si le téléchargement "
                       "automatique a échoué.")
    elif n_bdays == 1 and today_ny.hour < 17:
        # Marche ouvre ou pas encore close
        st.info(
            f"ℹ️ Marché en cours ou pas encore fermé (NY {today_ny.strftime('%H:%M')} ET). "
            f"Donnée du jour disponible après 16h ET / 22h Paris."
        )

    pos_mr, eff_xndx, eff_tsmom, _ = build_all_signals_cached(
        prices_dict["XNDX"], tuple(panier)
    )

    capital_usd = params["capital_eur"] * get_eur_usd()
    eur_usd = get_eur_usd()
    lev = params["leverage"]
    strategy = params["strategy"]

    # Allocations
    if strategy == "xndx_mr_only":
        alloc_mr, alloc_ts = 1.0, 0.0
    elif strategy == "tsmom_only":
        alloc_mr, alloc_ts = 0.0, 1.0
    else:
        alloc_mr = cfg["allocation"]["mr"]
        alloc_ts = cfg["allocation"]["tsmom"]

    st.subheader("Positions cibles")
    rows = []
    mr_exp = eff_xndx.iloc[-1]

    # XNDX MR (si applicable)
    if alloc_mr > 0:
        if not np.isnan(mr_exp) and mr_exp != 0:
            expo_usd = alloc_mr * capital_usd * mr_exp * lev
            rows.append({
                "Actif": "XNDX (MR)",
                "Position": "LONG",
                "Multiplicateur": f"{mr_exp * lev:.2f}×",
                "Exposition USD": f"{expo_usd:,.0f}",
                "Exposition EUR": f"{expo_usd/eur_usd:,.0f}",
                "% capital": f"{alloc_mr * 100 * mr_exp * lev:.0f}%",
            })
        else:
            rows.append({
                "Actif": "XNDX (MR)", "Position": "CASH",
                "Multiplicateur": "0", "Exposition USD": "0",
                "Exposition EUR": "0", "% capital": "0%",
            })

    # TSMOM (si applicable)
    if alloc_ts > 0:
        alloc_per_asset = alloc_ts / len(panier)
        for a in panier:
            ts_exp = eff_tsmom[a].iloc[-1]
            if np.isnan(ts_exp) or ts_exp == 0:
                position, expo_usd = "CASH", 0
            elif ts_exp > 0:
                position = "LONG"
                expo_usd = alloc_per_asset * capital_usd * ts_exp * lev
            else:
                position = "SHORT"
                expo_usd = alloc_per_asset * capital_usd * ts_exp * lev
            rows.append({
                "Actif": f"{a} (TSMOM)",
                "Position": position,
                "Multiplicateur": f"{ts_exp * lev:.2f}×" if ts_exp != 0 else "0",
                "Exposition USD": f"{expo_usd:,.0f}",
                "Exposition EUR": f"{expo_usd/eur_usd:,.0f}",
                "% capital": f"{alloc_per_asset * 100 * ts_exp * lev:.0f}%",
            })

    # Ligne TOTAL en bas
    total_expo_usd_signed = 0  # net (longs - shorts)
    total_expo_usd_abs = 0     # brut (|longs| + |shorts|)
    if alloc_mr > 0 and not np.isnan(mr_exp) and mr_exp != 0:
        e = alloc_mr * capital_usd * mr_exp * lev
        total_expo_usd_signed += e
        total_expo_usd_abs += abs(e)
    if alloc_ts > 0:
        alloc_per_asset = alloc_ts / len(panier)
        for a in panier:
            ts_exp = eff_tsmom[a].iloc[-1]
            if not np.isnan(ts_exp) and ts_exp != 0:
                e = alloc_per_asset * capital_usd * ts_exp * lev
                total_expo_usd_signed += e
                total_expo_usd_abs += abs(e)

    rows.append({
        "Actif": "TOTAL (net long-short)",
        "Position": "—",
        "Multiplicateur": "—",
        "Exposition USD": f"{total_expo_usd_signed:,.0f}",
        "Exposition EUR": f"{total_expo_usd_signed/eur_usd:,.0f}",
        "% capital": f"{total_expo_usd_signed/capital_usd*100:.0f}%",
    })
    rows.append({
        "Actif": "TOTAL (brut |long|+|short|)",
        "Position": "—",
        "Multiplicateur": "—",
        "Exposition USD": f"{total_expo_usd_abs:,.0f}",
        "Exposition EUR": f"{total_expo_usd_abs/eur_usd:,.0f}",
        "% capital": f"{total_expo_usd_abs/capital_usd*100:.0f}%",
    })

    df_positions = pd.DataFrame(rows)

    # Surlignage couleur selon position
    def highlight_position(row):
        pos = row.get("Position", "")
        if pos == "LONG":
            return ["background-color: #d4edda"] * len(row)  # vert clair
        elif pos == "SHORT":
            return ["background-color: #fff3cd"] * len(row)  # orange clair
        elif pos == "CASH":
            return ["background-color: #f8f9fa"] * len(row)  # gris très clair
        elif "TOTAL" in str(row.get("Actif", "")):
            return ["background-color: #e7e7ff; font-weight: bold"] * len(row)  # bleu lavande
        return [""] * len(row)

    styled = df_positions.style.apply(highlight_position, axis=1)
    st.dataframe(styled, hide_index=True, use_container_width=True)

    st.caption("🟢 LONG = vert clair | 🟠 SHORT = orange clair | ⚪ CASH = gris clair | 🟦 TOTAL = bleu lavande")

    # Calcul exposition totale
    total_expo_pct = 0
    if alloc_mr > 0 and not np.isnan(mr_exp) and mr_exp != 0:
        total_expo_pct += alloc_mr * 100 * abs(mr_exp) * lev
    if alloc_ts > 0:
        alloc_per_asset = alloc_ts / len(panier)
        for a in panier:
            ts_exp = eff_tsmom[a].iloc[-1]
            if not np.isnan(ts_exp) and ts_exp != 0:
                total_expo_pct += alloc_per_asset * 100 * abs(ts_exp) * lev

    if total_expo_pct > 100:
        st.info(
            f"💡 **Exposition totale brute : {total_expo_pct:.0f}% du capital** — "
            f"normal et voulu : la stratégie utilise du **vol-targeting** "
            f"(amplifie les positions quand la volatilité est basse). "
            f"Le multiplicateur affiché peut dépasser 1× sur chaque actif."
        )

    # Explications cash vs long et double XNDX
    with st.expander("ℹ️ Pourquoi telle position est en CASH / LONG / SHORT ?"):
        st.markdown("""
**XNDX (MR)** - Mean-Reversion court terme sur le Nasdaq :
- **CASH** si pas de signal d'entrée actif (le z-score n'est pas en zone de survente)
- **LONG** quand le z-score franchit le seuil bas (entrée déclenchée) et qu'on n'a pas encore atteint le z-score 0 ou le time-stop de 20 jours

**TSMOM (Trend-Following 12 mois)** sur les 5 actifs (TLT, SPXTR, XNDX, GLD, CL) :
- **LONG** quand le rendement 252 jours est positif
- **CASH** quand le rendement 252 jours est négatif (pour TLT, SPXTR, XNDX, GLD : on est long-only, donc pas de short)
- **SHORT** quand le rendement 252 jours est négatif sur CL uniquement (CL est l'unique actif en long/short)

**Pourquoi XNDX apparaît deux fois** (MR et TSMOM) :
- Le combo trade Nasdaq via **deux stratégies différentes** simultanément
- **MR** : tactique court terme, achète sur excès baissier, sort en quelques jours
- **TSMOM** : tactique long terme, achète si tendance 12 mois positive, garde 20 jours minimum
- Les deux signaux **peuvent être actifs en même temps** → cumul des positions long sur Nasdaq
- C'est voulu : la corrélation MR/TSMOM est faible (~0.4) malgré le sous-jacent commun
        """)

    # Quantités à passer
    st.subheader("Quantités à passer")
    vehicle_mode = params.get("vehicle_mode", "etf")

    if vehicle_mode == "etf":
        st.markdown("**Mode ETF/CFD pur** (granularité fine)")
        etf_map = {"XNDX": "QQQ", "SPXTR": "SPY", "GLD": "GLD", "TLT": "TLT", "CL": "CFD_WTI"}
        rows_q = []
        if alloc_mr > 0 and not np.isnan(mr_exp) and mr_exp != 0:
            expo_usd_mr = alloc_mr * capital_usd * mr_exp * lev
            qqq_price = prices_dict["XNDX"].iloc[-1] / 50
            n_qqq = int(round(expo_usd_mr / qqq_price))
            rows_q.append({"Instrument": "QQQ (MR)", "Quantité": n_qqq,
                          "Prix USD": f"{qqq_price:.2f}",
                          "Notional USD": f"{n_qqq * qqq_price:,.0f}"})
        if alloc_ts > 0:
            alloc_per = alloc_ts / len(panier)
            for a in panier:
                ts_exp = eff_tsmom[a].iloc[-1]
                if np.isnan(ts_exp) or ts_exp == 0: continue
                expo_usd = alloc_per * capital_usd * ts_exp * lev
                if a == "XNDX":
                    price = prices_dict[a].iloc[-1] / 50
                elif a == "SPXTR":
                    price = prices_dict[a].iloc[-1] / 100
                else:
                    price = prices_dict[a].iloc[-1]
                n = int(round(expo_usd / price))
                rows_q.append({"Instrument": etf_map.get(a, a), "Quantité": n,
                              "Prix USD": f"{price:.2f}",
                              "Notional USD": f"{n * price:,.0f}"})
        if rows_q:
            st.dataframe(pd.DataFrame(rows_q), hide_index=True, use_container_width=True)

    elif vehicle_mode == "mix":
        st.markdown("**Mode Mix** : MR en MNQ futures + TSMOM en ETF/CFD")
        contracts = cfg["vehicules"]["futures"]["instruments"]
        etf_map = {"XNDX": "QQQ", "SPXTR": "SPY", "GLD": "GLD", "TLT": "TLT", "CL": "CFD_WTI"}
        rows_q = []
        total_margin = 0
        # MR en MNQ futures
        if alloc_mr > 0 and not np.isnan(mr_exp) and mr_exp != 0:
            expo_usd_mr = alloc_mr * capital_usd * mr_exp * lev
            mnq = contracts["XNDX"]
            notional_per = prices_dict["XNDX"].iloc[-1] * mnq["mult"]
            n = int(round(expo_usd_mr / notional_per))
            margin = abs(n) * mnq["margin_usd"]
            total_margin += margin
            rows_q.append({"Instrument": f"{mnq['sym']} (MR future)", "Quantité": n,
                          "Notional/contrat": f"{notional_per:,.0f}",
                          "Marge USD": f"{margin:,.0f}"})
        # TSMOM en ETF
        if alloc_ts > 0:
            alloc_per = alloc_ts / len(panier)
            for a in panier:
                ts_exp = eff_tsmom[a].iloc[-1]
                if np.isnan(ts_exp) or ts_exp == 0: continue
                expo_usd = alloc_per * capital_usd * ts_exp * lev
                if a == "XNDX":
                    price = prices_dict[a].iloc[-1] / 50
                elif a == "SPXTR":
                    price = prices_dict[a].iloc[-1] / 100
                else:
                    price = prices_dict[a].iloc[-1]
                n = int(round(expo_usd / price))
                rows_q.append({"Instrument": f"{etf_map.get(a, a)} (ETF/CFD)", "Quantité": n,
                              "Notional/contrat": f"{price:.2f}",
                              "Marge USD": f"{n * price:,.0f}"})
        if rows_q:
            st.dataframe(pd.DataFrame(rows_q), hide_index=True, use_container_width=True)
            margin_pct = total_margin / capital_usd * 100
            c1, c2 = st.columns(2)
            c1.metric("Marge futures", f"{total_margin:,.0f} USD")
            c2.metric("% capital (marge futures)", f"{margin_pct:.1f}%")

    else:  # futures purs
        st.markdown("**Mode Futures purs** : tout en micros (sauf TLT en ETF)")
        contracts = cfg["vehicules"]["futures"]["instruments"]
        rows_q = []
        total_margin = 0
        if alloc_mr > 0 and not np.isnan(mr_exp) and mr_exp != 0:
            expo_usd_mr = alloc_mr * capital_usd * mr_exp * lev
            mnq = contracts["XNDX"]
            notional_per = prices_dict["XNDX"].iloc[-1] * mnq["mult"]
            n = int(round(expo_usd_mr / notional_per))
            margin = abs(n) * mnq["margin_usd"]
            total_margin += margin
            rows_q.append({"Instrument": f"{mnq['sym']} (MR)", "Quantité": n,
                          "Notional/contrat": f"{notional_per:,.0f}",
                          "Marge USD": f"{margin:,.0f}"})
        if alloc_ts > 0:
            alloc_per = alloc_ts / len(panier)
            for a in panier:
                ts_exp = eff_tsmom[a].iloc[-1]
                if np.isnan(ts_exp) or ts_exp == 0: continue
                expo_usd = alloc_per * capital_usd * ts_exp * lev
                if a == "TLT":
                    # TLT reste en ETF
                    price = prices_dict[a].iloc[-1]
                    n = int(round(expo_usd / price))
                    rows_q.append({"Instrument": "TLT (ETF)", "Quantité": n,
                                  "Notional/contrat": f"{price:.2f}",
                                  "Marge USD": f"{n * price:,.0f}"})
                else:
                    ctr = contracts[a]
                    notional_per = prices_dict[a].iloc[-1] * ctr["mult"]
                    n = int(round(expo_usd / notional_per))
                    margin = abs(n) * ctr["margin_usd"]
                    total_margin += margin
                    rows_q.append({"Instrument": f"{ctr['sym']} ({a})", "Quantité": n,
                                  "Notional/contrat": f"{notional_per:,.0f}",
                                  "Marge USD": f"{margin:,.0f}"})
        if rows_q:
            st.dataframe(pd.DataFrame(rows_q), hide_index=True, use_container_width=True)
            margin_pct = total_margin / capital_usd * 100
            c1, c2 = st.columns(2)
            c1.metric("Marge totale futures", f"{total_margin:,.0f} USD")
            c2.metric("% capital (marge)", f"{margin_pct:.1f}%")
            if margin_pct > 100:
                st.error(f"⚠️ MARGIN CALL : marge {margin_pct:.0f}% > capital")
            elif margin_pct > 80:
                st.warning(f"⚠️ Marge élevée ({margin_pct:.0f}%)")


# ====================================================================
# ONGLET BACKTEST
# ====================================================================
def render_backtest(params):
    st.header(f"📊 Backtest — {params['strategy_label']}")

    cfg = get_config_cached()
    panier = cfg["tsmom"]["panier"]
    prices_dict = {a: load_prices_cached(a) for a in panier}
    if any(p is None for p in prices_dict.values()):
        st.error("Données manquantes.")
        return

    pos_mr, eff_xndx, eff_tsmom, _ = build_all_signals_cached(
        prices_dict["XNDX"], tuple(panier)
    )

    # === Contrôles + Tableau perf côte à côte (tableau à gauche, contrôles à droite) ===
    st.markdown("---")
    col_left, col_right = st.columns([1.7, 1])
    
    with col_right:
        st.markdown("**📅 Période d'analyse**")
        period = st.selectbox("Période", ["Total", "10 ans", "5 ans", "3 ans", "1 an"],
                              label_visibility="collapsed")
        st.markdown("**📊 Échelle axe Y**")
        echelle = st.radio("Échelle Y", ["Linéaire (vertical)", "Log (compressé)"],
                            horizontal=False, label_visibility="collapsed", index=0)
        echelle = "Linéaire" if "Linéaire" in echelle else "Log"

    today = prices_dict["XNDX"].index[-1]
    period_starts = {
        "Total": "2005-11-17",
        "10 ans": (today - pd.DateOffset(years=10)).strftime("%Y-%m-%d"),
        "5 ans": (today - pd.DateOffset(years=5)).strftime("%Y-%m-%d"),
        "3 ans": (today - pd.DateOffset(years=3)).strftime("%Y-%m-%d"),
        "1 an": (today - pd.DateOffset(years=1)).strftime("%Y-%m-%d"),
    }
    start_date = period_starts[period]

    r_combo, r_mr, r_ts, df_ts = compute_combo_returns(
        prices_dict["XNDX"], eff_xndx, prices_dict, eff_tsmom, cfg, start_date=start_date
    )

    # Selon stratégie, on garde la bonne courbe
    strategy = params["strategy"]
    lev = params["leverage"]
    if strategy == "xndx_mr_only":
        r_theo = r_mr * lev
        label_strat = "XNDX MR seul"
    elif strategy == "tsmom_only":
        r_theo = r_ts * lev
        label_strat = "TSMOM seul"
    else:
        r_theo = r_combo * lev
        label_strat = "COMBO"

    # Coût roll futures (CL uniquement) + borrow shorts (ETF uniquement)
    pos_cl = eff_tsmom["CL"].reindex(r_theo.index).fillna(0)
    if strategy != "xndx_mr_only":
        if params["is_etf"]:
            short_exp_cl = pos_cl.where(pos_cl < 0, 0).abs() * 0.1
            daily_cost = (params["borrow_short"] / 252) * short_exp_cl
        else:
            cl_exp = pos_cl.abs() * 0.1
            daily_cost = (0.03 / 252) * cl_exp  # roll 3%/an
        if strategy == "combo":
            r_theo = r_theo - daily_cost * lev
        elif strategy == "tsmom_only":
            r_theo = r_theo - daily_cost * lev * 2

    # Simulation reelle
    r_real = simulate_realistic_returns(
        params, eff_xndx, eff_tsmom, prices_dict, cfg, start_date, strategy
    )

    m_theo = compute_stats(r_theo, "THEO")
    m_real = compute_stats(r_real, "REEL")

    # === Tableau compact unifié théo + réel + perf période ===
    period_label = period
    eq_theo = (1 + r_theo).cumprod()
    perf_periode_theo = (eq_theo.iloc[-1] - 1) * 100 if len(eq_theo) > 0 else 0
    capital_final_theo = params["capital_eur"] * eq_theo.iloc[-1] if len(eq_theo) > 0 else params["capital_eur"]
    
    perf_periode_real = None
    capital_final_real = None
    if m_real and len(r_real) > 0:
        eq_real = (1 + r_real).cumprod()
        perf_periode_real = (eq_real.iloc[-1] - 1) * 100
        capital_final_real = params["capital_eur"] * eq_real.iloc[-1]
    
    vmode = params.get("vehicle_mode", "etf")
    vehic_lbl = {"etf": "ETF/CFD pur", "mix": "Mix (MR futures + TSMOM ETF)",
                 "futures": "Futures purs (micros)"}.get(vmode, "?")
    
    # Tableau dans la colonne de gauche
    with col_left:
        st.markdown(f"**Performances comparées — {label_strat}**")
        st.caption(f"{vehic_lbl} · levier {lev}× · période {period_label} · capital initial {params['capital_eur']:,} EUR")

        # === TABLEAU HTML PUR avec header gold ===
        # On utilise du markdown HTML pour avoir un controle total du style.
        
        def fmt_perf(val_str, color_pos_neg=False, force_red=False, force_green=False):
            """Retourne le HTML formate pour une valeur."""
            if val_str == "—":
                return '<span style="color:#999;">—</span>'
            color = "#1a1a1a"  # noir par defaut
            if force_red:
                color = "#c0392b"
            elif force_green:
                color = "#1e7e34"
            elif color_pos_neg:
                try:
                    clean = val_str.replace("%", "").replace("+", "").replace(",", "").replace(" EUR", "").strip()
                    num = float(clean)
                    if num > 0:
                        color = "#1e7e34"
                    elif num < 0:
                        color = "#c0392b"
                except (ValueError, AttributeError):
                    pass
            return f'<span style="color:{color}; font-weight:600;">{val_str}</span>'

        # Construire le tableau HTML
        cagr_theo_html = fmt_perf(f"{m_theo['cagr_pct']:.2f}%", color_pos_neg=True)
        cagr_real_html = fmt_perf(f"{m_real['cagr_pct']:.2f}%" if m_real else "—", color_pos_neg=True)
        cagr_ecart = f"{m_real['cagr_pct'] - m_theo['cagr_pct']:+.2f}%" if m_real else "—"
        cagr_ecart_html = fmt_perf(cagr_ecart, color_pos_neg=True)

        perf_theo_html = fmt_perf(f"{perf_periode_theo:+.2f}%", color_pos_neg=True)
        perf_real_html = fmt_perf(f"{perf_periode_real:+.2f}%" if perf_periode_real is not None else "—", color_pos_neg=True)
        perf_ecart = f"{perf_periode_real - perf_periode_theo:+.2f}%" if perf_periode_real is not None else "—"
        perf_ecart_html = fmt_perf(perf_ecart, color_pos_neg=True)

        cap_theo_html = fmt_perf(f"{capital_final_theo:,.0f} EUR".replace(",", " "), color_pos_neg=False,
                                  force_green=(capital_final_theo >= params["capital_eur"]),
                                  force_red=(capital_final_theo < params["capital_eur"]))
        cap_real_html = fmt_perf(
            f"{capital_final_real:,.0f} EUR".replace(",", " ") if capital_final_real is not None else "—",
            color_pos_neg=False,
            force_green=(capital_final_real is not None and capital_final_real >= params["capital_eur"]),
            force_red=(capital_final_real is not None and capital_final_real < params["capital_eur"])
        )
        cap_ecart = f"{capital_final_real - capital_final_theo:+,.0f} EUR".replace(",", " ") if capital_final_real is not None else "—"
        cap_ecart_html = fmt_perf(cap_ecart, color_pos_neg=True)

        dd_theo_html = fmt_perf(f"{m_theo['maxdd_pct']:.2f}%", force_red=True)
        dd_real_html = fmt_perf(f"{m_real['maxdd_pct']:.2f}%" if m_real else "—", force_red=True)
        dd_ecart = f"{m_real['maxdd_pct'] - m_theo['maxdd_pct']:+.2f}%" if m_real else "—"
        dd_ecart_html = fmt_perf(dd_ecart, color_pos_neg=True)

        sharpe_theo_html = f'<span style="color:#1a1a1a; font-weight:600;">{m_theo["sharpe"]:.3f}</span>'
        sharpe_real_html = f'<span style="color:#1a1a1a; font-weight:600;">{m_real["sharpe"]:.3f}</span>' if m_real else fmt_perf("—")
        sharpe_ecart = f"{m_real['sharpe'] - m_theo['sharpe']:+.3f}" if m_real else "—"
        sharpe_ecart_html = fmt_perf(sharpe_ecart, color_pos_neg=True)

        vol_theo_html = f'<span style="color:#1a1a1a; font-weight:600;">{m_theo["vol_pct"]:.2f}%</span>'
        vol_real_html = f'<span style="color:#1a1a1a; font-weight:600;">{m_real["vol_pct"]:.2f}%</span>' if m_real else fmt_perf("—")

        # Tableau HTML
        html_table = f"""
<style>
.perf-table {{
    width: 100%;
    border-collapse: collapse;
    font-size: 15px;
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
}}
.perf-table thead th {{
    background-color: #fff8dc;
    color: #1a1a1a;
    font-weight: bold;
    padding: 12px 14px;
    text-align: left;
    border-bottom: 2px solid #d4af37;
    font-size: 15px;
}}
.perf-table tbody td {{
    padding: 10px 14px;
    border-bottom: 1px solid #eee;
}}
.perf-table tbody td:first-child {{
    font-weight: 600;
    color: #333;
}}
.perf-table tbody tr:hover {{
    background-color: #fafafa;
}}
</style>
<table class="perf-table">
    <thead>
        <tr>
            <th>Métrique</th>
            <th>Théorique</th>
            <th>Réel ({params['capital_eur']:,} EUR)</th>
            <th>Écart</th>
        </tr>
    </thead>
    <tbody>
        <tr><td>CAGR annualisé</td><td>{cagr_theo_html}</td><td>{cagr_real_html}</td><td>{cagr_ecart_html}</td></tr>
        <tr><td>Perf totale période</td><td>{perf_theo_html}</td><td>{perf_real_html}</td><td>{perf_ecart_html}</td></tr>
        <tr><td>Capital final</td><td>{cap_theo_html}</td><td>{cap_real_html}</td><td>{cap_ecart_html}</td></tr>
        <tr><td>MaxDD</td><td>{dd_theo_html}</td><td>{dd_real_html}</td><td>{dd_ecart_html}</td></tr>
        <tr><td>Sharpe</td><td>{sharpe_theo_html}</td><td>{sharpe_real_html}</td><td>{sharpe_ecart_html}</td></tr>
        <tr><td>Volatilité</td><td>{vol_theo_html}</td><td>{vol_real_html}</td><td>—</td></tr>
    </tbody>
</table>
""".replace(",", "&nbsp;")  # remplacer les virgules de milliers par espace insecable
        st.markdown(html_table, unsafe_allow_html=True)

    # ========================================================================
    # TODO BACKLOG : Onglet "Live vs Backtest" (réconciliation automatique)
    # ------------------------------------------------------------------------
    # Une fois la stratégie en live trading via IBKR :
    # - Capter automatiquement les fills via ib_insync (TradeReport / ExecDetails)
    # - Stocker dans un fichier results/live_trades_log.csv :
    #   date | asset | side | qty_target_theo | qty_executed_real | 
    #   price_close_theo | price_exec_real | slippage_bps | pnl_diff
    # - Comparer chaque jour : signal du backtest vs ordre exécuté chez IBKR
    # - Afficher un onglet "🔄 Réconciliation" avec :
    #   * Tracking error cumulé (perf live vs perf backtest)
    #   * Histogramme des slippages
    #   * Liste des écarts > 5 bps à investiguer
    #   * Alerte si dérive > 1% sur 1 mois
    # - PAS de saisie manuelle : tout doit être automatique via API IBKR
    # ========================================================================
    
    st.markdown("---")

    # Warning si DD réel élevé
    if m_real:
        infl = abs(m_real["maxdd_pct"]) - abs(m_theo["maxdd_pct"])
        if infl > 3:
            st.warning(
                f"⚠️ MaxDD réel {infl:.1f}pp supérieur au backtest. "
                f"Cause : arrondis aux contrats entiers à ce niveau de capital. "
                f"Augmenter le capital ou passer en ETF/CFD réduirait l'écart."
            )

        # Encadre explicatif
        with st.expander("ℹ️ Explication : Théorique vs Réel — pourquoi l'écart ?"):
            st.markdown(f"""
**Théorique** = backtest avec **allocations continues**. À chaque jour, la position 
prend exactement la valeur cible calculée (par exemple 1.73 contrat MNQ, ou 4.5 shares QQQ).
C'est un idéal mathématique qui ne reflète pas la réalité d'exécution.

**Réel** = simulation avec **arrondis aux unités entières** selon le véhicule choisi 
({vehic_lbl}). À chaque jour, on arrondit la position cible à l'unité entière 
(par exemple 2 contrats MNQ au lieu de 1.73, ou 5 shares QQQ au lieu de 4.5).

**Conséquences pratiques** :
- À petit capital : les arrondis créent des **surexpositions** ou **sous-expositions** 
  importantes par rapport à la cible théorique → vol et DD réels plus élevés
- En mode Futures (micros) : granularité grossière (1 contrat MNQ = ~50k USD notional). 
  À petit capital, on est forcé d'être à 0 ou 1 contrat même si la cible serait 0.3
- En mode ETF/CFD : granularité fine (1 share QQQ = ~600 USD). 
  L'écart théo vs réel reste faible même à petit capital
- Plus le capital augmente, plus l'écart se réduit (granularité relative meilleure)

**Distinction CAGR annualisé vs Perf période** :
- **CAGR annualisé** = taux de croissance composé moyen par an. Indépendant de la durée.
- **Perf période** = rendement total cumulé sur la période sélectionnée. Augmente avec la durée.

**Lecture du graphique** :
- Courbe **noire** = théorique (allocations continues, idéal)
- Courbe **orange** = réelle (arrondis, ce qui se passera vraiment en live)
- Si elles divergent fortement : ton capital n'est pas suffisant pour répliquer 
  fidèlement la stratégie avec ce véhicule
            """)

    # EC chart
    fig, axes = plt.subplots(2, 1, figsize=(14, 9))
    eq_theo = (1 + r_theo).cumprod()
    ax = axes[0]
    ax.plot(eq_theo.index, eq_theo.values, color="black", linewidth=2.2,
            label=f"Théorique (Sharpe {m_theo['sharpe']:.2f}, CAGR {m_theo['cagr_pct']:.1f}%)")
    if m_real and len(r_real) > 10:
        eq_real = (1 + r_real).cumprod()
        ax.plot(eq_real.index, eq_real.values, color="orange", linewidth=1.5,
                label=f"Réel à {params['capital_eur']:,} EUR (Sharpe {m_real['sharpe']:.2f}, CAGR {m_real['cagr_pct']:.1f}%)")
    if echelle == "Log":
        ax.set_yscale("log")
    ax.set_title(f"Equity {label_strat} (levier {lev}×, {'ETF/CFD' if params['is_etf'] else 'Futures'})")
    ax.legend(fontsize=10); ax.grid(True, alpha=0.3)
    ax.xaxis.set_major_locator(mdates.YearLocator(2))

    ax = axes[1]
    dd_theo = (eq_theo - eq_theo.cummax()) / eq_theo.cummax() * 100
    ax.fill_between(dd_theo.index, dd_theo.values, 0, alpha=0.4, color="red", label="Théorique")
    if m_real and len(r_real) > 10:
        eq_real = (1 + r_real).cumprod()
        dd_real = (eq_real - eq_real.cummax()) / eq_real.cummax() * 100
        ax.plot(dd_real.index, dd_real.values, color="darkred", linewidth=1.5, label="Réel")
    ax.set_title("Drawdown"); ax.set_ylabel("%")
    ax.legend(); ax.grid(True, alpha=0.3)
    ax.xaxis.set_major_locator(mdates.YearLocator(2))
    fig.tight_layout()
    st.pyplot(fig)
    plt.close(fig)

    # Perf annuelle - theo vs reel
    st.subheader("Performance annuelle")
    df_theo = yearly_breakdown(r_theo)
    df_real = yearly_breakdown(r_real) if len(r_real) > 0 else None

    if len(df_theo) > 0:
        fig2, ax = plt.subplots(figsize=(14, 4.5))
        years = df_theo["year"].values
        x = np.arange(len(years))
        width = 0.4
        ax.bar(x - width/2, df_theo["cagr_pct"], width,
               label="Théorique",
               color=["#7fc97f" if v >= 0 else "#fdc086" for v in df_theo["cagr_pct"]],
               edgecolor="black", linewidth=0.5)
        if df_real is not None and len(df_real) > 0:
            real_dict = dict(zip(df_real["year"], df_real["cagr_pct"]))
            real_vals = [real_dict.get(y, 0) for y in years]
            ax.bar(x + width/2, real_vals, width,
                   label=f"Réel ({params['capital_eur']:,} EUR, {'ETF' if params['is_etf'] else 'Futures'})",
                   color=["#0e7c0e" if v >= 0 else "#c0392b" for v in real_vals],
                   edgecolor="black", linewidth=0.5)
        ax.set_xticks(x); ax.set_xticklabels(years, rotation=45)
        ax.axhline(0, color="black", linewidth=0.5)
        ax.set_ylabel("Rendement annuel %")
        ax.legend(); ax.grid(True, alpha=0.3, axis="y")
        fig2.tight_layout()
        st.pyplot(fig2)
        plt.close(fig2)

        # Tableau
        df_table = df_theo.copy().rename(columns={
            "cagr_pct": "CAGR théo %", "maxdd_pct": "MaxDD théo %", "sharpe": "Sharpe théo"
        })
        if df_real is not None and len(df_real) > 0:
            real_cagr = dict(zip(df_real["year"], df_real["cagr_pct"]))
            real_dd = dict(zip(df_real["year"], df_real["maxdd_pct"]))
            df_table["CAGR réel %"] = df_table["year"].map(real_cagr).round(2)
            df_table["MaxDD réel %"] = df_table["year"].map(real_dd).round(2)
            df_table["Δ CAGR pp"] = (df_table["CAGR réel %"] - df_table["CAGR théo %"]).round(2)
        st.dataframe(df_table, hide_index=True)


# ====================================================================
# ONGLET COMPOSITION
# ====================================================================
def render_composition(params):
    st.header(f"📊 Composition — {params['strategy_label']}")

    cfg = get_config_cached()
    panier = cfg["tsmom"]["panier"]
    prices_dict = {a: load_prices_cached(a) for a in panier}
    if any(p is None for p in prices_dict.values()):
        st.error("Données manquantes.")
        return

    pos_mr, eff_xndx, eff_tsmom, _ = build_all_signals_cached(
        prices_dict["XNDX"], tuple(panier)
    )
    r_combo, r_mr, r_ts, df_ts = compute_combo_returns(
        prices_dict["XNDX"], eff_xndx, prices_dict, eff_tsmom, cfg
    )
    lev = params["leverage"]

    strategy = params["strategy"]

    if strategy == "xndx_mr_only":
        st.info("Stratégie XNDX MR seule : pas de décomposition par actif (un seul actif).")
        r_mr_lev = r_mr * lev
        m = compute_stats(r_mr_lev, "XNDX MR")
        eq = (1 + r_mr_lev).cumprod()
        fig, ax = plt.subplots(figsize=(14, 5))
        ax.plot(eq.index, eq.values, color="blue", linewidth=2)
        ax.set_title(f"XNDX MR LO (Sharpe {m['sharpe']:.2f}, CAGR {m['cagr_pct']:.1f}%, levier {lev}×)")
        ax.grid(True, alpha=0.3)
        ax.xaxis.set_major_locator(mdates.YearLocator(2))
        st.pyplot(fig)
        plt.close(fig)
        return

    # Sinon : afficher EC par actif TSMOM (avec levier)
    st.subheader(f"Equity curves par actif au sein de TSMOM (levier {lev}×)")
    fig, ax = plt.subplots(figsize=(14, 6))
    colors = {"TLT": "purple", "SPXTR": "blue", "XNDX": "green", "GLD": "gold", "CL": "red"}
    for a in panier:
        rets_a = df_ts[a] * lev  # AVEC LEVIER
        eq_a = (1 + rets_a).cumprod()
        m_a = compute_stats(rets_a, a)
        if not m_a: continue
        mode = "L/S" if a == "CL" else "LO"
        ax.plot(eq_a.index, eq_a.values, color=colors.get(a, "black"),
                label=f"{a} ({mode}) - Sharpe {m_a['sharpe']:.2f}, CAGR {m_a['cagr_pct']:.1f}%")
    ax.set_yscale("log")
    ax.set_ylabel("Equity (base 1, log)")
    ax.set_title(f"EC par actif TSMOM (chacun = 1/5 = 10% capital, levier {lev}×)")
    ax.legend(fontsize=9, loc="upper left")
    ax.grid(True, alpha=0.3, which="both")
    ax.xaxis.set_major_locator(mdates.YearLocator(2))
    fig.tight_layout()
    st.pyplot(fig)
    plt.close(fig)

    # Matrice corrélations dans expander
    df_rets_full = pd.DataFrame({a: prices_dict[a].pct_change() for a in panier}).dropna()
    corr_start_full = df_rets_full.index[0].strftime("%Y-%m-%d")
    corr_end_full = df_rets_full.index[-1].strftime("%Y-%m-%d")
    n_obs_full = len(df_rets_full)

    # Sous-période 3 ans
    cutoff_3y = df_rets_full.index[-1] - pd.DateOffset(years=3)
    df_rets_3y = df_rets_full[df_rets_full.index >= cutoff_3y]
    corr_start_3y = df_rets_3y.index[0].strftime("%Y-%m-%d") if len(df_rets_3y) > 0 else "?"
    corr_end_3y = df_rets_3y.index[-1].strftime("%Y-%m-%d") if len(df_rets_3y) > 0 else "?"

    with st.expander("📊 Matrices de corrélations (cliquez pour afficher)"):
        st.markdown("""
**Lecture** : corrélation entre les rendements quotidiens des prix sous-jacents 
(pas les rendements de la stratégie). 1.0 = parfaitement corrélé, 0 = décorrélé, 
-1.0 = parfaitement opposé.
        """)
        
        col_long, col_3y = st.columns(2)
        with col_long:
            st.markdown(f"##### Long terme")
            st.caption(f"{corr_start_full} → {corr_end_full} ({n_obs_full:,} obs)")
            corr_full = df_rets_full.corr()
            fig_l, ax_l = plt.subplots(figsize=(3, 2.5))
            im_l = ax_l.imshow(corr_full.values, cmap="RdBu_r", vmin=-1, vmax=1, aspect="auto")
            ax_l.set_xticks(range(len(panier))); ax_l.set_xticklabels(panier, fontsize=7)
            ax_l.set_yticks(range(len(panier))); ax_l.set_yticklabels(panier, fontsize=7)
            for i in range(len(panier)):
                for j in range(len(panier)):
                    ax_l.text(j, i, f"{corr_full.values[i,j]:.2f}", ha="center", va="center",
                            color="white" if abs(corr_full.values[i,j]) > 0.5 else "black", fontsize=6)
            fig_l.tight_layout()
            st.pyplot(fig_l)
            plt.close(fig_l)

        with col_3y:
            st.markdown(f"##### 3 ans glissants")
            st.caption(f"{corr_start_3y} → {corr_end_3y} ({len(df_rets_3y):,} obs)")
            corr_3y = df_rets_3y.corr()
            fig_s, ax_s = plt.subplots(figsize=(3, 2.5))
            im_s = ax_s.imshow(corr_3y.values, cmap="RdBu_r", vmin=-1, vmax=1, aspect="auto")
            ax_s.set_xticks(range(len(panier))); ax_s.set_xticklabels(panier, fontsize=7)
            ax_s.set_yticks(range(len(panier))); ax_s.set_yticklabels(panier, fontsize=7)
            for i in range(len(panier)):
                for j in range(len(panier)):
                    ax_s.text(j, i, f"{corr_3y.values[i,j]:.2f}", ha="center", va="center",
                            color="white" if abs(corr_3y.values[i,j]) > 0.5 else "black", fontsize=6)
            fig_s.tight_layout()
            st.pyplot(fig_s)
            plt.close(fig_s)
        
        st.caption(
            "💡 Comparer les deux périodes permet de voir si les corrélations actuelles "
            "diffèrent de leur moyenne historique long terme (changement de régime de marché)."
        )


# ====================================================================
# ONGLET WFO
# ====================================================================
def render_wfo(params):
    st.header("🔄 WFO - Walk-Forward Optimization")

    # Explication claire en haut
    with st.expander("ℹ️ Qu'est-ce que le WFO et que recalibre-t-on ?", expanded=True):
        st.markdown("""
        ### Stratégie concernée : **XNDX MR LO uniquement**
        
        Le combo se compose de deux stratégies :
        - **XNDX MR LO** (50% du capital) : utilise un WFO **car les paramètres bougent dans le temps**
        - **TSMOM LO sauf CL** (50% du capital) : **paramètres figés** (momentum 252j, rebal 20j, vol cible 21%), 
          pas de WFO car cette stratégie est par nature stable.
        
        ### Paramètres optimisés par le WFO
        
        | Paramètre | Description | Grille testée |
        |---|---|---|
        | **N** | Fenêtre du z-score (jours) | 5, 7, 9, 11, 13, 15 |
        | **threshold** | Seuil d'entrée (z-score négatif déclencheur) | -0.8, -0.9, -1.0, -1.1, -1.2, -1.3 |
        
        Les autres paramètres restent **figés** : time-stop 20j, vol cible 20%, long-only.
        
        ### Méthodologie
        
        - **IS (in-sample)** : 8 ans glissants pour optimiser
        - **OOS (out-of-sample)** : 2 ans suivants pour appliquer le couple choisi
        - **Critère de sélection** : Sharpe IS maximal
        - **Recalibration** : tous les 2 ans
        - **Validation manuelle obligatoire** : on ne fige jamais sans double-check
        """)

    # Explication detection plateau
    with st.expander("ℹ️ Détection du plateau — pourquoi c'est essentiel"):
        st.markdown("""
        ### Pourquoi un "plateau" et pas un "pic"
        
        Quand on teste 36 combinaisons (6 valeurs de N × 6 valeurs de threshold), il y a 
        forcément **une cellule** avec le Sharpe IS le plus élevé. Mais ce maximum peut être :
        
        - **Un vrai plateau** : la cellule gagnante ET ses voisines ont des Sharpe très proches.
          → Le résultat est **robuste**, on peut faire confiance.
        - **Un pic isolé** : la cellule gagnante a un Sharpe élevé mais ses voisines ont des Sharpe 
          bien plus bas.
          → Le résultat est probablement **dû au hasard** (overfit), on ne peut pas faire confiance 
          en out-of-sample.
        
        ### Comment l'algo détecte automatiquement
        
        Pour la proposition retenue (cellule avec Sharpe IS max), l'algo regarde les **8 voisins** 
        (4 cellules en croix + 4 cellules en diagonale) dans la heatmap.
        
        Pour chaque voisin, il vérifie si son **Sharpe IS ≥ 90%** du Sharpe IS max.
        
        Selon le pourcentage de voisins qui sont "proches" du max, l'algo classifie :
        
        | Catégorie | Critère | Décision |
        |---|---|---|
        | ✓ **Plateau robuste** | ≥ 60% des voisins (≥ 5 sur 8) ont Sharpe ≥ 90% du max | Validation simple |
        | ⚠️ **Plateau moyen** | Entre 30% et 60% (3-4 voisins) | Vérifier visuellement avant validation |
        | 🔴 **Pic isolé** | < 30% des voisins (≤ 2 sur 8) | Validation déconseillée (risque overfit) |
        
        ### Cas "Pic isolé" — protection supplémentaire
        
        Si l'algo détecte un pic isolé, on ne peut pas valider en un seul clic. Il faut :
        1. Cocher "J'ai vérifié les éléments"
        2. Cocher EN PLUS "Je confirme malgré le pic isolé (overfit possible)"
        
        Si tu vois ce cas, **regarde la heatmap** et préfère un couple voisin avec plateau plus 
        robuste, même si son Sharpe IS est légèrement inférieur (-5 à -10%). C'est plus prudent en live.
        
        ### Référence protocole
        
        Cette logique applique la **règle §9.6** du protocole : 
        *"Privilégier un plateau large à un pic isolé. Le pic isolé est souvent du hasard / overfit. 
        Choisir le centre du plateau, pas la case la plus verte."*
        """)

    cfg = get_config_cached()
    state_path = ROOT / "config" / "wfo_state_xndx_mr.json"
    if not state_path.exists():
        st.error("Fichier état WFO manquant.")
        return
    with open(state_path, "r") as f:
        state = json.load(f)

    st.subheader("État courant")
    
    # ALERTE PROCHAINE RECALIBRATION
    try:
        next_recal_date = pd.Timestamp(state.get("next_recalibration_date"))
        today = pd.Timestamp(datetime.now().date())
        days_remaining = (next_recal_date - today).days
        
        if days_remaining < 0:
            # EN RETARD - rouge
            st.error(
                f"🔴 **WFO EN RETARD** : la recalibration aurait dû avoir lieu il y a "
                f"{abs(days_remaining)} jour(s). À EFFECTUER IMMÉDIATEMENT.\n\n"
                f"Date de recalibration prévue : {next_recal_date.strftime('%Y-%m-%d')}\n"
                f"Aujourd'hui : {today.strftime('%Y-%m-%d')}"
            )
        elif days_remaining < 3:
            # URGENT - rouge
            st.error(
                f"🔴 **WFO URGENT** : recalibration dans seulement **{days_remaining} jour(s)** "
                f"({next_recal_date.strftime('%Y-%m-%d')}).\n\n"
                f"⚠️ Lancer la recalibration dès maintenant pour éviter le retard."
            )
        elif days_remaining <= 15:
            # ORANGE - imminent
            st.warning(
                f"🟠 **WFO imminent** : recalibration dans **{days_remaining} jour(s)** "
                f"({next_recal_date.strftime('%Y-%m-%d')}).\n\n"
                f"Planifier la recalibration cette semaine."
            )
        elif days_remaining <= 30:
            # JAUNE - approche
            st.info(
                f"🟡 **WFO proche** : prochaine recalibration dans **{days_remaining} jour(s)** "
                f"({next_recal_date.strftime('%Y-%m-%d')}).",
                icon="🟡",
            )
        # Sinon (>30j) : pas d'alerte
    except Exception:
        pass
    
    c1, c2, c3 = st.columns(3)
    c1.metric("N (fenêtre actuelle)", state.get("current_N"))
    c2.metric("threshold actuel", state.get("current_threshold"))
    c3.metric("Prochaine recalibration", state.get("next_recalibration_date"))

    st.write(f"**Fenêtre IS courante** : {state.get('current_fold_is_start')} → {state.get('current_fold_is_end')}")
    st.write(f"**Fenêtre OOS courante** : {state.get('current_fold_oos_start')} → {state.get('current_fold_oos_end')}")
    st.write(f"*Dernière mise à jour : {state.get('last_update', '?')}*")

    st.subheader("Historique des recalibrations")
    hist = state.get("history", [])
    if hist:
        df_hist = pd.DataFrame(hist)
        st.dataframe(df_hist, hide_index=True, use_container_width=True)
    else:
        st.info("Aucun historique pour le moment.")

    # === RECALIBRATION AVEC WORKFLOW VALIDATION ===
    st.subheader("🔧 Lancer une recalibration")
    st.markdown("""
    La recalibration teste la grille N × threshold sur la fenêtre IS de 8 ans qui se termine aujourd'hui.
    Elle propose les paramètres optimaux selon Sharpe IS. **La validation est manuelle** : 
    rien n'est figé sans confirmation explicite.
    """)

    # Initialiser session_state pour stocker le résultat
    if "wfo_proposal" not in st.session_state:
        st.session_state.wfo_proposal = None

    if st.button("📈 Étape 1 — Calculer la recalibration"):
        with st.spinner("Calcul de la grille N × threshold sur la fenêtre IS 8 ans..."):
            prices_xndx = load_prices_cached("XNDX")
            mr = cfg["xndx_mr"]
            today = prices_xndx.index[-1]
            is_end_dt = today
            is_start_dt = today - pd.DateOffset(years=mr["wfo"]["is_years"])
            is_end = is_end_dt.strftime("%Y-%m-%d")
            is_start = is_start_dt.strftime("%Y-%m-%d")

            results = []
            for N in mr["wfo"]["N_grid"]:
                z = compute_zscore(prices_xndx, N)
                for thr in mr["wfo"]["thr_grid"]:
                    pos, _ = generate_positions_mr(z, thr, mr["time_stop_days"])
                    eff = compute_eff_mr(prices_xndx, pos, mr["vol_target"], mr["vol_window"])
                    rets = prices_xndx.pct_change()
                    r = (eff * rets).fillna(0).loc[is_start:is_end]
                    sharpe = (r.mean() / r.std()) * np.sqrt(TRADING_DAYS) if r.std() > 0 else 0
                    eq = (1 + r).cumprod()
                    cagr = ((eq.iloc[-1] ** (252/len(r)) - 1) * 100) if len(r) > 0 and eq.iloc[-1] > 0 else 0
                    dd = ((eq - eq.cummax()) / eq.cummax()).min() * 100
                    results.append({
                        "N": N, "threshold": thr,
                        "Sharpe IS": round(sharpe, 3),
                        "CAGR IS %": round(cagr, 2),
                        "MaxDD IS %": round(dd, 2),
                    })
            df_res = pd.DataFrame(results)
            best = df_res.loc[df_res["Sharpe IS"].idxmax()]

            # Stocker en session_state
            st.session_state.wfo_proposal = {
                "N": int(best["N"]),
                "threshold": float(best["threshold"]),
                "sharpe_is": float(best["Sharpe IS"]),
                "is_start": is_start,
                "is_end": is_end,
                "df_res": df_res.to_dict(),
            }

    # Afficher la proposition si elle existe
    if st.session_state.wfo_proposal is not None:
        prop = st.session_state.wfo_proposal
        df_res = pd.DataFrame(prop["df_res"])

        st.markdown("---")
        st.subheader("📋 Étape 2 — Proposition + analyse plateau")

        # === DETECTION PLATEAU ===
        # Logique : la proposition est dans un plateau si les voisins (case ±1 en N et ±1 en thr)
        # ont un Sharpe IS proche du max (écart < 10% du max)
        pivot = df_res.pivot(index="N", columns="threshold", values="Sharpe IS")
        N_grid = list(pivot.index)
        thr_grid = list(pivot.columns)
        prop_N_idx = N_grid.index(prop["N"])
        prop_thr_idx = thr_grid.index(prop["threshold"])

        # Récupérer les voisins (8 voisins en croix + diagonales)
        neighbors = []
        for dN in [-1, 0, 1]:
            for dthr in [-1, 0, 1]:
                if dN == 0 and dthr == 0:
                    continue
                ni, ti = prop_N_idx + dN, prop_thr_idx + dthr
                if 0 <= ni < len(N_grid) and 0 <= ti < len(thr_grid):
                    neighbors.append(pivot.values[ni, ti])

        best_sharpe = prop["sharpe_is"]
        if neighbors and best_sharpe != 0:
            # Tolerance : voisins acceptables si Sharpe >= 90% du best
            tolerance = 0.90
            n_in_plateau = sum(1 for v in neighbors if v >= tolerance * best_sharpe)
            ratio_plateau = n_in_plateau / len(neighbors)

            if ratio_plateau >= 0.6:
                plateau_status = "robuste"
                plateau_msg = (f"✓ **Plateau robuste** : {n_in_plateau}/{len(neighbors)} voisins "
                               f"ont un Sharpe ≥ 90% du max. La proposition est dans une zone stable.")
                plateau_color = "success"
            elif ratio_plateau >= 0.3:
                plateau_status = "moyen"
                plateau_msg = (f"⚠️ **Plateau moyen** : {n_in_plateau}/{len(neighbors)} voisins "
                               f"ont un Sharpe ≥ 90% du max. À analyser visuellement avant validation.")
                plateau_color = "warning"
            else:
                plateau_status = "isolé"
                plateau_msg = (f"🔴 **Pic isolé** : seulement {n_in_plateau}/{len(neighbors)} voisins "
                               f"ont un Sharpe ≥ 90% du max. **RISQUE D'OVERFIT** : ne pas valider sans "
                               f"vérification approfondie. Préférer un couple voisin avec plateau plus robuste.")
                plateau_color = "error"
        else:
            plateau_status = "inconnu"
            plateau_msg = "ℹ️ Impossible d'évaluer le plateau (pas assez de voisins)."
            plateau_color = "info"

        # Comparaison side-by-side
        col1, col2 = st.columns(2)
        with col1:
            st.markdown("##### 📌 Actuel (figé)")
            st.metric("N", state.get("current_N"))
            st.metric("threshold", state.get("current_threshold"))
            st.caption(f"Figé depuis {state.get('current_fold_oos_start')}")
        with col2:
            st.markdown("##### 🆕 Proposé")
            same_N = prop["N"] == state.get("current_N")
            same_thr = abs(prop["threshold"] - state.get("current_threshold", 0)) < 0.01
            st.metric("N", prop["N"], delta="inchangé" if same_N else f"vs {state.get('current_N')}")
            st.metric("threshold", f"{prop['threshold']:.2f}",
                     delta="inchangé" if same_thr else f"vs {state.get('current_threshold')}")
            st.caption(f"Sharpe IS = {prop['sharpe_is']:.3f}")

        # Affichage status plateau
        if plateau_color == "success":
            st.success(plateau_msg)
        elif plateau_color == "warning":
            st.warning(plateau_msg)
        elif plateau_color == "error":
            st.error(plateau_msg)
        else:
            st.info(plateau_msg)

        if same_N and same_thr:
            st.info("✓ Les paramètres optimaux n'ont pas changé. Pas de recalibration nécessaire.")
        else:
            st.warning("⚠️ Les paramètres optimaux ont changé. Vérifier le plateau ci-dessus avant validation.")

        # Heatmap
        st.markdown("##### Heatmap Sharpe IS sur la grille N × threshold")
        pivot = df_res.pivot(index="N", columns="threshold", values="Sharpe IS")
        fig, ax = plt.subplots(figsize=(6, 4))
        im = ax.imshow(pivot.values, cmap="RdYlGn", aspect="auto")
        ax.set_xticks(range(len(pivot.columns)))
        ax.set_xticklabels([f"{t:.1f}" for t in pivot.columns])
        ax.set_yticks(range(len(pivot.index)))
        ax.set_yticklabels(pivot.index)
        ax.set_xlabel("threshold"); ax.set_ylabel("N")
        for i in range(pivot.shape[0]):
            for j in range(pivot.shape[1]):
                ax.text(j, i, f"{pivot.values[i,j]:.2f}", ha="center", va="center",
                        color="white" if abs(pivot.values[i,j]) > 0.5 else "black", fontsize=9)
        fig.colorbar(im, ax=ax, fraction=0.04)
        fig.tight_layout()
        st.pyplot(fig)
        plt.close(fig)

        with st.expander("📊 Détail complet des résultats"):
            st.dataframe(df_res.sort_values("Sharpe IS", ascending=False), hide_index=True)

        st.markdown("---")
        st.subheader("✅ Étape 3 — Validation manuelle")
        st.markdown("""
        **Avant de valider**, vérifier :
        - Le statut du plateau ci-dessus (✓ robuste, ⚠️ moyen, ou 🔴 isolé)
        - La heatmap : zone stable autour de la valeur choisie
        - Le Sharpe IS proposé n'est pas trop éloigné de l'actuel (sauf changement clair de régime)
        - Les valeurs proposées ne sont pas en bord de grille extrême
        """)

        if plateau_status == "isolé":
            st.error(
                "🔴 **Validation déconseillée** : la proposition correspond à un pic isolé. "
                "Risque élevé d'overfit. Forcer la validation reste possible mais à vos risques."
            )

        col_a, col_b = st.columns(2)
        with col_a:
            confirm = st.checkbox("J'ai vérifié les éléments ci-dessus et je valide la proposition")
            if plateau_status == "isolé":
                force = st.checkbox("⚠️ Je confirme malgré le pic isolé (overfit possible)")
                can_validate = confirm and force
            else:
                can_validate = confirm
        with col_b:
            if st.button("💾 Figer ces paramètres dans wfo_state_xndx_mr.json",
                         disabled=not can_validate,
                         type="primary"):
                # Sauvegarde JSON
                new_state = state.copy()
                new_state["current_N"] = prop["N"]
                new_state["current_threshold"] = prop["threshold"]
                new_state["current_fold_is_start"] = prop["is_start"]
                new_state["current_fold_is_end"] = prop["is_end"]
                new_state["current_fold_oos_start"] = (pd.Timestamp(prop["is_end"]) +
                                                       pd.Timedelta(days=1)).strftime("%Y-%m-%d")
                next_oos_end = (pd.Timestamp(prop["is_end"]) +
                                pd.DateOffset(years=2)).strftime("%Y-%m-%d")
                new_state["current_fold_oos_end"] = next_oos_end
                new_state["next_recalibration_date"] = (pd.Timestamp(next_oos_end) +
                                                        pd.Timedelta(days=1)).strftime("%Y-%m-%d")
                new_state["last_update"] = datetime.now().isoformat()

                # Ajouter à l'historique
                history_entry = {
                    "date_recal": datetime.now().strftime("%Y-%m-%d"),
                    "N_chosen": prop["N"],
                    "thr_chosen": prop["threshold"],
                    "sharpe_is": round(prop["sharpe_is"], 3),
                    "is_period": f"{prop['is_start']} -> {prop['is_end']}",
                    "oos_period": f"{new_state['current_fold_oos_start']} -> {new_state['current_fold_oos_end']}",
                    "forced": False,
                    "N_previous": state.get("current_N"),
                    "thr_previous": state.get("current_threshold"),
                }
                new_state.setdefault("history", []).append(history_entry)

                with open(state_path, "w") as f:
                    json.dump(new_state, f, indent=2)

                st.success(f"✅ Sauvegardé ! Nouveau couple (N={prop['N']}, "
                           f"threshold={prop['threshold']:.2f}) actif jusqu'au "
                           f"{new_state['next_recalibration_date']}.")
                st.session_state.wfo_proposal = None
                st.rerun()


# ====================================================================
# ONGLET PRESENTATION & ABONNEMENTS
# ====================================================================
def render_pedago(params):
    st.header("📚 Présentation & Abonnements")

    st.markdown("""
    ## Le combo en bref

    Cette stratégie combine **deux approches indépendantes et complémentaires** :
    
    - **Mean-Reversion (50% du capital)** : achat opportuniste du Nasdaq sur excès baissier court terme
    - **Trend-Following (50% du capital)** : positions long sur 5 classes d'actifs selon tendance 12 mois

    Ensemble, ils performent à travers les régimes de marché grâce à une corrélation faible (~0.4).
    
    ## Profil de performance (2005-2026)
    """)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Sharpe", "1.13")
    c2.metric("CAGR", "12.3%")
    c3.metric("MaxDD", "-15.4%")
    c4.metric("Années +", "86%")

    st.markdown("---")
    st.markdown("## 💼 Abonnements")

    col_t1, col_t2 = st.columns(2)
    with col_t1:
        st.markdown("""
        ### 🟦 Tier 1 - XNDX MR seul
        **39 EUR/mois** ou 390 EUR/an
        
        - **1 actif unique** : Nasdaq 100 (QQQ ou MNQ)
        - **Capital min** : 5 000 EUR
        - **Fréquence** : ~20 alertes/an
        - **Profil** : régularité, mean-reversion
        
        **Performances** :
        - Sharpe : 0.90
        - CAGR : 10%
        - MaxDD : -19%
        
        **Cible** : retail débutant, simplicité d'exécution
        """)

    with col_t2:
        st.markdown("""
        ### 🟩 Tier 2 - Combo complet
        **129 EUR/mois** ou 1 290 EUR/an
        
        - **6 actifs** : QQQ + 5 actifs TSMOM (TLT/SPY/QQQ/GLD/CL)
        - **Capital min** : 10 000 EUR (ETF) ou 30 000 EUR (futures)
        - **Fréquence** : ~32 alertes/an
        - **Profil** : diversifié, robuste
        
        **Performances** :
        - Sharpe : 1.13
        - CAGR : 12.3%
        - MaxDD : -15.4%
        
        **Cible** : patrimoine moyen, recherche de Sharpe élevé
        """)

    st.markdown("---")
    st.markdown("""
    ## 🎯 Pourquoi 2 tiers

    - **Tier 1** = porte d'entrée accessible, 1 actif simple, capital bas
    - **Tier 2** = montée en gamme avec Sharpe +25% et MaxDD réduit
    - **Pas de Tier "TSMOM seul"** : trop complexe en standalone (5 actifs, CL en CFD/futures), 
      ne se justifie qu'avec XNDX MR à côté
    
    ## 📦 Distribution
    
    - Email automatique chaque soir après close NY (17h ET = 23h Paris été / 22h hiver)
    - Dashboard live abonné (filtré selon Tier)
    - Garde-fou données : retry 3× + bascule Yahoo si EODHD KO
    
    ## ⚖️ Cadre légal
    
    - Signaux **éducationnels** uniquement (pas de recommandation personnalisée)
    - Statut CIF à préparer pour Q3 2026 si croissance significative
    - Disclaimer complet dans CGV
    """)


# ====================================================================
# ONGLET DOC TECHNIQUE
# ====================================================================
def render_doc_tech(params):
    st.header("🔧 Documentation technique")
    cfg = get_config_cached()

    # Métadonnées
    st.subheader("Identité stratégie")
    md = cfg["metadata"]
    c1, c2, c3 = st.columns(3)
    c1.metric("Nom", md["name"][:20] + "...")
    c2.metric("Version", md["version"])
    c3.metric("Statut", md["statut"])
    st.caption(f"Validée le {md['date_validation']}")

    # Allocation
    st.subheader("Allocation")
    c1, c2 = st.columns(2)
    c1.metric("XNDX MR", f"{cfg['allocation']['mr']*100:.0f}%")
    c2.metric("TSMOM", f"{cfg['allocation']['tsmom']*100:.0f}%")
    st.caption(f"Rebalance : {cfg['allocation']['rebalance']}")

    # XNDX MR
    st.subheader("XNDX MR LO - Paramètres figés")
    mr = cfg["xndx_mr"]
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Phase 1 N", mr["z_score"]["phase1_N"])
    c2.metric("Phase 1 threshold", mr["z_score"]["phase1_threshold"])
    c3.metric("Time-stop", f"{mr['time_stop_days']} j")
    c4.metric("Vol cible", f"{mr['vol_target']*100:.0f}%")
    st.markdown(f"""
    **WFO** : IS {mr['wfo']['is_years']} ans, OOS {mr['wfo']['oos_years']} ans, 
    critère = {mr['wfo']['criterion']}, exécution = {mr['execution']}
    
    **Grilles WFO** :
    - `N` (fenêtre z-score) : {mr['wfo']['N_grid']}
    - `threshold` : {mr['wfo']['thr_grid']}
    """)

    # TSMOM
    st.subheader("TSMOM LO sauf CL - Paramètres figés")
    ts = cfg["tsmom"]
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Panier", f"{len(ts['panier'])} actifs")
    c2.metric("Lookback", f"{ts['signal']['momentum_lookback_days']} j")
    c3.metric("Rebal", f"{ts['rebalance_days']} j")
    c4.metric("Vol cible", f"{ts['vol_target']*100:.0f}%")
    st.markdown(f"""
    **Long-only** : {', '.join(ts['long_only_actifs'])}
    
    **Long/Short** : {', '.join(ts['long_short_actifs'])} (uniquement)
    
    **Cap levier** : {ts['leverage_cap']}× | **Fenêtre vol** : {ts['vol_window']} j | **Exécution** : {ts['execution']}
    """)

    # Coûts
    st.subheader("Coûts opérationnels")
    ex = cfg["execution"]
    c1, c2, c3 = st.columns(3)
    c1.metric("Frais", f"{ex['fees_bps']} bps")
    c2.metric("Borrow short", f"{ex['borrow_cost_short_annual']*100:.1f}%/an")
    c3.metric("EUR/USD", ex['eur_usd_default'])

    # Performances validées
    st.subheader("Performances validées (hors-échantillon inclus)")
    perf = cfg["performances_validees"]
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Sharpe", f"{perf['sharpe']}")
    c2.metric("CAGR", f"{perf['cagr_pct']}%")
    c3.metric("MaxDD", f"{perf['maxdd_pct']}%")
    c4.metric("Vol", f"{perf['vol_pct']}%")
    st.caption(f"Période : {perf['periode']} | Années + : {perf['annees_positives']}")

    if "hold_out_2024_2026" in perf:
        st.markdown("**Hold-out 2024-2026 (truly out-of-sample)** :")
        h = perf["hold_out_2024_2026"]
        c1, c2, c3 = st.columns(3)
        c1.metric("Sharpe HO", f"{h['sharpe']}")
        c2.metric("CAGR HO", f"{h['cagr_pct']}%")
        c3.metric("MaxDD HO", f"{h['maxdd_pct']}%")

    # Véhicules
    st.subheader("Véhicules d'exécution")
    veh = cfg["vehicules"]
    st.markdown(f"""
    **Version ETF/CFD** (capital min recommandé : {veh['etf_cfd']['capital_min_eur']:,} EUR)
    """)
    st.json(veh["etf_cfd"]["instruments"])
    st.markdown(f"""
    **Version Futures** (capital min recommandé : {veh['futures']['capital_min_eur']:,} EUR)
    """)
    df_fut = pd.DataFrame([
        {"Actif": k, **v} for k, v in veh["futures"]["instruments"].items()
    ])
    st.dataframe(df_fut, hide_index=True)

    # Data source
    st.subheader("Sources de données")
    ds = cfg["data_source"]
    st.markdown(f"**Primaire** : {ds['primary']}")
    df_tickers = pd.DataFrame([{"Actif": k, "Ticker EODHD": v} for k, v in ds["tickers"].items()])
    st.dataframe(df_tickers, hide_index=True)


# ====================================================================
# MAIN
# ====================================================================
def main():
    st.title("📈 Combo XNDX MR LO + TSMOM LO sauf CL")
    st.caption(f"Validé §8.3 le 2026-05-15 | Live depuis 2026-05-15")

    params = render_sidebar()

    tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs([
        "🟢 Live", "📊 Backtest", "📊 Composition", "🔄 WFO", "📚 Présentation", "🔧 Doc tech"
    ])

    with tab1: render_live(params)
    with tab2: render_backtest(params)
    with tab3: render_composition(params)
    with tab4: render_wfo(params)
    with tab5: render_pedago(params)
    with tab6: render_doc_tech(params)


if __name__ == "__main__":
    main()
