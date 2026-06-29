"""
scripts/backtest_v2.py
========================
Backtest TRADE PAR TRADE conforme au protocole §1.2.

Etape A : levier 1x, validation de convergence avec backtest.py.
Doit retomber tres proche des chiffres valides (Sharpe 1.13, CAGR 11.76%).
Si convergence OK -> Etape B (levier + financement) -> Etape C (margin call).

PROTOCOLE §1.2 RESPECTE :
- capital initial explicite suivi
- cash trace en continu
- positions reelles (quantites entieres ou fractionnaires selon vehicule)
- ordres BUY/SELL avec prix d'execution explicites
- PnL par trade
- equity = cash + valorisation reelle
- aucun look-ahead bias (eff deja shifte de 1)
- frais de transaction 3 bps sur le notional change

USAGE :
    python scripts/backtest_v2.py [--capital 10000000] [--leverage 1.0] [--vehicle etf]
"""

import sys
import argparse
from pathlib import Path
from datetime import datetime

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.dates as mdates


def fetch_eur_usd(fallback=1.08):
    """Fetch taux EUR/USD live via Yahoo Finance. Fallback si indisponible."""
    try:
        import yfinance as yf
        val = yf.Ticker("EURUSD=X").fast_info["lastPrice"]
        if 0.8 < val < 1.5:
            print(f"  EUR/USD live : {val:.4f}")
            return val
    except Exception as e:
        print(f"  EUR/USD fetch echec ({e}), fallback {fallback}")
    return fallback

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.strategy import (
    load_config, build_xndx_mr_positions, build_tsmom_positions,
    compute_combo_returns, compute_stats, yearly_breakdown,
)

DATA_DIR = ROOT / "data"
RESULTS_DIR = ROOT / "results"
RESULTS_DIR.mkdir(exist_ok=True)
CONFIG_PATH = ROOT / "config" / "parameters.json"


def load_prices(asset):
    csv_path = DATA_DIR / f"{asset}.csv"
    return pd.read_csv(csv_path, parse_dates=["date"], index_col="date")["close"]


# ====================================================================
# UNIT NOTIONAL (selon vehicule)
# ====================================================================
def get_unit_notional(asset, price, vehicle_mode, cfg):
    """
    Retourne le notional d'une unite (1 share ETF, 1 contrat futures).
    """
    if vehicle_mode == "etf":
        # ETF / CFD
        if asset == "XNDX":
            return price / 50  # QQQ ~ NDX / 50
        elif asset == "SPXTR":
            return price / 100  # SPY ~ SPX / 100
        else:
            return price  # GLD, TLT, CL via CFD
    
    elif vehicle_mode == "futures":
        # Futures micros
        if asset == "TLT":
            return price  # TLT reste ETF (ZN trop gros)
        contracts = cfg["vehicules"]["futures"]["instruments"]
        return price * contracts[asset]["mult"]
    
    elif vehicle_mode == "mix":
        # MR en futures, TSMOM en ETF
        # Ce cas est gere par appel separe avec mode different
        # Pour simplifier on appelle cette fonction en specifiant le mode
        return get_unit_notional(asset, price, "etf", cfg)
    
    return price


def is_integer_quantity(asset, vehicle_mode):
    """
    Retourne True si l'instrument se trade en quantites entieres.
    ETF/CFD : non (on peut acheter des fractions chez certains brokers, 
    mais par defaut on prend des shares entieres)
    Futures : oui (toujours par contrat entier)
    """
    if vehicle_mode == "etf":
        return True  # ETF en shares entieres
    elif vehicle_mode == "futures":
        if asset == "TLT":
            return True  # TLT ETF en shares entieres
        return True  # Futures toujours entiers
    return True


# ====================================================================
# BACKTEST TRADE PAR TRADE
# ====================================================================
def run_backtest_trade_by_trade(
    capital_initial_eur,
    leverage,
    vehicle_mode,
    fees_bps,
    borrow_cost_annual,
    cfg,
    eff_xndx,
    eff_tsmom,
    prices_dict,
    start_date,
    eur_usd=1.08,
    rebal_tolerance=0.01,  # 1% : seuil de declenchement d'un rebalance
):
    """
    Execute le backtest trade par trade conforme au protocole §1.2 et §9.3.
    
    REGLE §9.3 : un trade ne se declenche QUE lorsque le signal change.
    Entre deux signaux, la position en unites reste FIGEE et derive avec le marche.
    
    Detection de "changement de signal" : eff(J) - eff(J-1) | > rebal_tolerance.
    Cela couvre :
    - Entree de position (eff passe de 0 a non-zero)
    - Sortie de position (eff passe de non-zero a 0)
    - Rebalance TSMOM tous les 20j (eff change discretement)
    - Adjustement vol-target significatif (> tolerance)
    """
    panier = cfg["tsmom"]["panier"]
    alloc_mr = cfg["allocation"]["mr"]
    alloc_ts_per = cfg["allocation"]["tsmom"] / len(panier)
    fees_rate = fees_bps / 10_000
    
    # Index commun
    common_idx = eff_xndx.index
    for a in panier:
        common_idx = common_idx.intersection(prices_dict[a].index)
        common_idx = common_idx.intersection(eff_tsmom[a].index)
    common_idx = common_idx[common_idx >= pd.Timestamp(start_date)].sort_values()
    
    if len(common_idx) < 10:
        return None
    
    # Aligner les signaux sur common_idx (valeurs numpy pour acces rapide)
    eff_mr_arr = eff_xndx.reindex(common_idx).fillna(0).values
    eff_ts_arr = {a: eff_tsmom[a].reindex(common_idx).fillna(0).values for a in panier}
    
    # Initialisation
    capital_initial_usd = capital_initial_eur * eur_usd
    cash_usd = capital_initial_usd
    positions = {"XNDX_MR": 0, **{a: 0 for a in panier}}
    
    # Historique
    equity_history = []
    cash_history = []
    positions_history = []
    trade_log = []
    margin_calls = []
    
    for i, day in enumerate(common_idx):
        # === 1. CALCULER POSITIONS_VALUE (close du jour) ===
        positions_value = 0
        if positions["XNDX_MR"] != 0:
            price_xndx = prices_dict["XNDX"].loc[day]
            unit_not = get_unit_notional("XNDX", price_xndx, vehicle_mode, cfg)
            positions_value += positions["XNDX_MR"] * unit_not
        for a in panier:
            if positions[a] != 0:
                price_a = prices_dict[a].loc[day]
                unit_not_a = get_unit_notional(a, price_a, vehicle_mode, cfg)
                positions_value += positions[a] * unit_not_a
        
        equity_usd = cash_usd + positions_value
        
        # Securite equity negative
        if equity_usd <= 0:
            margin_calls.append(day)
            cash_usd = 0
            positions = {"XNDX_MR": 0, **{a: 0 for a in panier}}
            equity_history.append({"date": day, "equity_eur": 0})
            cash_history.append({"date": day, "cash_eur": 0})
            positions_history.append({"date": day, **positions.copy()})
            continue
        
        # Detection appel de marge (si levier > 1)
        if leverage > 1.0:
            critical_threshold = capital_initial_usd * (1 - 0.5 / leverage)
            if equity_usd < critical_threshold:
                margin_calls.append(day)
        
        # === 2. DETECTER CHANGEMENTS DE SIGNAL (regle §9.3) ===
        # Au jour 0, on initialise les positions (entree initiale).
        # Apres : on rebalance uniquement si eff change > tolerance.
        signal_changed = {}
        
        eff_mr_today = eff_mr_arr[i]
        if i == 0:
            signal_changed["XNDX_MR"] = (eff_mr_today != 0)  # entree initiale si signal actif
        else:
            eff_mr_prev = eff_mr_arr[i-1]
            signal_changed["XNDX_MR"] = abs(eff_mr_today - eff_mr_prev) > rebal_tolerance
        
        for a in panier:
            eff_a_today = eff_ts_arr[a][i]
            if i == 0:
                signal_changed[a] = (eff_a_today != 0)
            else:
                eff_a_prev = eff_ts_arr[a][i-1]
                signal_changed[a] = abs(eff_a_today - eff_a_prev) > rebal_tolerance
        
        # === 3. CALCULER POSITIONS CIBLES uniquement pour ceux qui changent ===
        target_positions = dict(positions)  # par defaut, on garde les positions existantes
        
        # MR : recalcul cible uniquement si signal change
        if signal_changed["XNDX_MR"]:
            target_mr_usd = alloc_mr * equity_usd * eff_mr_today * leverage
            price_xndx = prices_dict["XNDX"].loc[day]
            unit_not_mr = get_unit_notional("XNDX", price_xndx, vehicle_mode, cfg)
            if unit_not_mr > 0:
                target_qty_mr = target_mr_usd / unit_not_mr
                if is_integer_quantity("XNDX", vehicle_mode):
                    target_positions["XNDX_MR"] = int(round(target_qty_mr))
                else:
                    target_positions["XNDX_MR"] = target_qty_mr
            else:
                target_positions["XNDX_MR"] = 0
        
        # TSMOM
        for a in panier:
            if signal_changed[a]:
                eff_a_today = eff_ts_arr[a][i]
                target_a_usd = alloc_ts_per * equity_usd * eff_a_today * leverage
                price_a = prices_dict[a].loc[day]
                unit_not_a = get_unit_notional(a, price_a, vehicle_mode, cfg)
                if unit_not_a > 0:
                    target_qty_a = target_a_usd / unit_not_a
                    if is_integer_quantity(a, vehicle_mode):
                        target_positions[a] = int(round(target_qty_a))
                    else:
                        target_positions[a] = target_qty_a
                else:
                    target_positions[a] = 0
        
        # === 4. EXECUTER LES ORDRES (uniquement si delta != 0) ===
        for asset_key, target_qty in target_positions.items():
            current_qty = positions[asset_key]
            delta = target_qty - current_qty
            
            if delta == 0:
                continue
            
            if asset_key == "XNDX_MR":
                actual_asset = "XNDX"
            else:
                actual_asset = asset_key
            price = prices_dict[actual_asset].loc[day]
            unit_not = get_unit_notional(actual_asset, price, vehicle_mode, cfg)
            
            notional_change = abs(delta) * unit_not
            fees = notional_change * fees_rate
            
            cash_usd -= delta * unit_not
            cash_usd -= fees
            
            trade_log.append({
                "date": day,
                "asset": asset_key,
                "side": "BUY" if delta > 0 else "SELL",
                "qty_delta": delta,
                "qty_new": target_qty,
                "price": price,
                "unit_notional": unit_not,
                "notional": notional_change,
                "fees": fees,
                "cash_after": cash_usd,
            })
            
            positions[asset_key] = target_qty
        
        # === 5. COUT DE FINANCEMENT (si levier > 1) ===
        if leverage > 1.0 and borrow_cost_annual > 0:
            positions_value_new = 0
            for asset_key, qty in positions.items():
                if qty != 0:
                    if asset_key == "XNDX_MR":
                        actual_asset = "XNDX"
                    else:
                        actual_asset = asset_key
                    price = prices_dict[actual_asset].loc[day]
                    unit_not = get_unit_notional(actual_asset, price, vehicle_mode, cfg)
                    positions_value_new += qty * unit_not
            
            cash_borrowed = max(0, positions_value_new - equity_usd)
            daily_borrow_fee = cash_borrowed * borrow_cost_annual / 252
            cash_usd -= daily_borrow_fee
        
        # === 6. ENREGISTRER L'ETAT ===
        positions_value_final = 0
        for asset_key, qty in positions.items():
            if qty != 0:
                if asset_key == "XNDX_MR":
                    actual_asset = "XNDX"
                else:
                    actual_asset = asset_key
                price = prices_dict[actual_asset].loc[day]
                unit_not = get_unit_notional(actual_asset, price, vehicle_mode, cfg)
                positions_value_final += qty * unit_not
        
        equity_usd_final = cash_usd + positions_value_final
        
        equity_history.append({"date": day, "equity_eur": equity_usd_final / eur_usd})
        cash_history.append({"date": day, "cash_eur": cash_usd / eur_usd})
        positions_history.append({"date": day, **positions.copy()})
    
    equity_series = pd.DataFrame(equity_history).set_index("date")["equity_eur"]
    cash_series = pd.DataFrame(cash_history).set_index("date")["cash_eur"]
    positions_df = pd.DataFrame(positions_history).set_index("date")
    trade_log_df = pd.DataFrame(trade_log) if trade_log else pd.DataFrame()
    
    return {
        "equity": equity_series,
        "cash": cash_series,
        "positions": positions_df,
        "trade_log": trade_log_df,
        "margin_calls": margin_calls,
        "capital_initial_eur": capital_initial_eur,
    }


# ====================================================================
# STATS A PARTIR DE L'EQUITY REELLE
# ====================================================================
def compute_stats_from_equity(equity_series, label=""):
    """Calcule Sharpe, CAGR, MaxDD a partir de l'equity reelle en EUR."""
    if equity_series is None or len(equity_series) < 30:
        return {}
    rets = equity_series.pct_change().dropna()
    n_y = (equity_series.index[-1] - equity_series.index[0]).days / 365.25
    if n_y <= 0:
        return {}
    
    capital_initial = equity_series.iloc[0]
    capital_final = equity_series.iloc[-1]
    
    cagr = (capital_final / capital_initial) ** (1 / n_y) - 1
    vol = rets.std() * np.sqrt(252)
    sharpe = (rets.mean() / rets.std()) * np.sqrt(252) if rets.std() > 0 else 0
    dd = ((equity_series - equity_series.cummax()) / equity_series.cummax()).min()
    
    return {
        "label": label,
        "capital_initial_eur": capital_initial,
        "capital_final_eur": capital_final,
        "cagr_pct": cagr * 100,
        "vol_pct": vol * 100,
        "sharpe": sharpe,
        "maxdd_pct": dd * 100,
        "n_years": n_y,
    }


# ====================================================================
# MAIN
# ====================================================================
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--capital", type=float, default=10_000_000, help="Capital initial EUR")
    parser.add_argument("--leverage", type=float, default=1.0, help="Levier")
    parser.add_argument("--vehicle", choices=["etf", "futures"], default="etf")
    parser.add_argument("--fees-bps", type=float, default=3.0)
    parser.add_argument("--borrow-cost", type=float, default=0.03, help="Cout annuel financement levier (3%)")
    parser.add_argument("--start", default="2005-11-17")
    parser.add_argument("--eur-usd", type=float, default=None,
                        help="Taux EUR/USD (defaut: fetch live Yahoo, fallback 1.08)")
    args = parser.parse_args()
    
    cfg = load_config(CONFIG_PATH)
    panier = cfg["tsmom"]["panier"]

    # EUR/USD : fetch live si pas fourni en CLI
    eur_usd = args.eur_usd if args.eur_usd is not None else fetch_eur_usd(fallback=1.08)

    prices_dict = {a: load_prices(a) for a in panier}
    
    print("=" * 70)
    print("BACKTEST V2 - TRADE PAR TRADE (conforme protocole §1.2)")
    print("=" * 70)
    print(f"Capital initial   : {args.capital:,.0f} EUR")
    print(f"Levier            : {args.leverage}x")
    print(f"Vehicule          : {args.vehicle}")
    print(f"Frais transaction : {args.fees_bps} bps")
    print(f"Cout financement  : {args.borrow_cost*100:.1f}% annuel (si levier > 1)")
    print(f"EUR/USD            : {eur_usd:.4f}")
    print(f"Periode           : {args.start} -> aujourd'hui")
    print("=" * 70)
    
    print("\nConstruction signaux...")
    pos_mr, eff_xndx = build_xndx_mr_positions(prices_dict["XNDX"], cfg)
    eff_tsmom = build_tsmom_positions(prices_dict, cfg)
    
    # === BACKTEST V1 (référence simplifiée) ===
    print("\n[REFERENCE] backtest.py compute_combo_returns()...")
    r_combo, _, _, _ = compute_combo_returns(
        prices_dict["XNDX"], eff_xndx, prices_dict, eff_tsmom, cfg, start_date=args.start
    )
    if args.leverage > 1.0:
        # On compare brut sans financement pour comprendre la divergence
        r_v1 = r_combo * args.leverage
    else:
        r_v1 = r_combo
    m_v1 = compute_stats(r_v1, "V1")
    print(f"  Sharpe={m_v1['sharpe']:.4f}  CAGR={m_v1['cagr_pct']:.3f}%  MaxDD={m_v1['maxdd_pct']:.3f}%")
    print(f"  (= ancien backtest simplifie, formule r × leverage)")
    
    # === BACKTEST V2 (trade par trade) ===
    print(f"\n[V2 TRADE PAR TRADE] avec {args.vehicle.upper()}, levier {args.leverage}x...")
    print("  Execution en cours (peut prendre 30-60s)...")
    
    result = run_backtest_trade_by_trade(
        capital_initial_eur=args.capital,
        leverage=args.leverage,
        vehicle_mode=args.vehicle,
        fees_bps=args.fees_bps,
        borrow_cost_annual=args.borrow_cost,
        cfg=cfg,
        eff_xndx=eff_xndx,
        eff_tsmom=eff_tsmom,
        prices_dict=prices_dict,
        start_date=args.start,
        eur_usd=eur_usd,
    )
    
    if result is None:
        print("  ERREUR : pas assez de donnees")
        sys.exit(1)
    
    m_v2 = compute_stats_from_equity(result["equity"], "V2")
    print(f"  Sharpe={m_v2['sharpe']:.4f}  CAGR={m_v2['cagr_pct']:.3f}%  MaxDD={m_v2['maxdd_pct']:.3f}%")
    print(f"  Capital final : {m_v2['capital_final_eur']:,.0f} EUR")
    print(f"  Multiple capital initial : ×{m_v2['capital_final_eur']/m_v2['capital_initial_eur']:.2f}")
    print(f"  Nombre de trades : {len(result['trade_log'])}")
    print(f"  Appels de marge potentiels : {len(result['margin_calls'])}")
    
    if result['margin_calls']:
        print(f"  ⚠️ ALERTE : {len(result['margin_calls'])} jours en appel de marge")
        print(f"  Premier : {result['margin_calls'][0].date()}")
        print(f"  Dernier : {result['margin_calls'][-1].date()}")
    
    # === COMPARAISON V1 vs V2 ===
    print("\n" + "=" * 70)
    print("COMPARAISON V1 vs V2")
    print("=" * 70)
    print(f"  Sharpe   :  V1 {m_v1['sharpe']:.4f}  vs  V2 {m_v2['sharpe']:.4f}  "
          f"(delta {m_v2['sharpe']-m_v1['sharpe']:+.4f})")
    print(f"  CAGR     :  V1 {m_v1['cagr_pct']:.3f}%  vs  V2 {m_v2['cagr_pct']:.3f}%  "
          f"(delta {m_v2['cagr_pct']-m_v1['cagr_pct']:+.3f}pp)")
    print(f"  MaxDD    :  V1 {m_v1['maxdd_pct']:.3f}%  vs  V2 {m_v2['maxdd_pct']:.3f}%  "
          f"(delta {m_v2['maxdd_pct']-m_v1['maxdd_pct']:+.3f}pp)")
    
    # Sauvegarde resultats
    out_dir = RESULTS_DIR / f"v2_lev{args.leverage}_{args.vehicle}"
    out_dir.mkdir(parents=True, exist_ok=True)
    result["equity"].to_csv(out_dir / "equity.csv")
    result["cash"].to_csv(out_dir / "cash.csv")
    result["positions"].to_csv(out_dir / "positions.csv")
    if not result["trade_log"].empty:
        result["trade_log"].to_csv(out_dir / "trade_log.csv", index=False)
    
    print(f"\nResultats sauvegardes : {out_dir}")
    
    # Echantillon trades
    if not result["trade_log"].empty:
        print(f"\n--- Echantillon : 10 premiers trades ---")
        print(result["trade_log"].head(10).to_string())
    
    # Chart
    fig, axes = plt.subplots(2, 1, figsize=(14, 8))
    
    ax = axes[0]
    eq_v1 = (1 + r_v1).cumprod() * args.capital
    ax.plot(eq_v1.index, eq_v1.values, color="gray", linestyle="--", linewidth=1.5,
            label=f"V1 simplifie (Sharpe {m_v1['sharpe']:.2f}, CAGR {m_v1['cagr_pct']:.1f}%)")
    ax.plot(result["equity"].index, result["equity"].values, color="blue", linewidth=2,
            label=f"V2 trade-par-trade (Sharpe {m_v2['sharpe']:.2f}, CAGR {m_v2['cagr_pct']:.1f}%)")
    ax.set_yscale("log")
    ax.set_ylabel("Equity EUR (log)")
    ax.set_title(f"Backtest V1 vs V2 - Capital {args.capital:,.0f} EUR - levier {args.leverage}x - {args.vehicle.upper()}")
    ax.legend()
    ax.grid(True, alpha=0.3, which="both")
    ax.xaxis.set_major_locator(mdates.YearLocator(2))
    
    ax = axes[1]
    dd_v2 = (result["equity"] - result["equity"].cummax()) / result["equity"].cummax() * 100
    ax.fill_between(dd_v2.index, dd_v2.values, 0, alpha=0.4, color="red")
    ax.set_title(f"Drawdown V2 (MaxDD {m_v2['maxdd_pct']:.2f}%)")
    ax.set_ylabel("%")
    ax.grid(True, alpha=0.3)
    ax.xaxis.set_major_locator(mdates.YearLocator(2))
    
    fig.tight_layout()
    fig.savefig(out_dir / "comparison_v1_v2.png", dpi=100, bbox_inches="tight")
    plt.close(fig)
    
    print(f"Chart : {out_dir / 'comparison_v1_v2.png'}")
    print("=" * 70)


if __name__ == "__main__":
    main()
