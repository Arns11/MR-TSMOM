"""
diag_simulation.py
====================
Script de diagnostic isolé pour identifier la source du bug.

Compare 3 approches de simulation pour COMBO à 10M EUR + ETF :
1. THEORIQUE : allocations continues (= backtest.py)
2. ARRONDI QUOTIDIEN : positions arrondies chaque jour (mauvaise approche)
3. REBALANCE AUX CHANGEMENTS : positions arrondies uniquement quand le signal change

A gros capital, les 3 devraient converger. Si elles divergent → bug identifié.
"""

import sys
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.strategy import (
    load_config, build_xndx_mr_positions, build_tsmom_positions,
    compute_combo_returns, compute_stats,
)

DATA_DIR = ROOT / "data"
CONFIG_PATH = ROOT / "config" / "parameters.json"


def load_prices(asset):
    csv_path = DATA_DIR / f"{asset}.csv"
    return pd.read_csv(csv_path, parse_dates=["date"], index_col="date")["close"]


def simulate_approach(approach, capital_usd, eff_xndx, eff_tsmom, prices_dict, cfg, common_idx):
    """
    approach : "continuous" | "daily_round" | "trigger_only"
    """
    panier = cfg["tsmom"]["panier"]
    alloc_mr = cfg["allocation"]["mr"]
    alloc_ts_per = cfg["allocation"]["tsmom"] / len(panier)

    def notional_etf(asset, day_idx):
        price = prices_dict[asset].loc[common_idx[day_idx]]
        if asset == "XNDX":
            return price / 50
        elif asset == "SPXTR":
            return price / 100
        else:
            return price

    capital_t = 1.0
    daily_rets = [0.0]
    prev_positions = {"XNDX_MR": 0.0, **{a: 0.0 for a in panier}}

    eff_mr_values = eff_xndx.reindex(common_idx).fillna(0).values
    eff_ts_values = {a: eff_tsmom[a].reindex(common_idx).fillna(0).values for a in panier}

    for i in range(1, len(common_idx)):
        day = common_idx[i]
        cap_usd_t = capital_t * capital_usd

        # Cibles
        eff_mr_today = eff_mr_values[i]
        target_mr_usd = alloc_mr * cap_usd_t * eff_mr_today
        notional_unit_mr = notional_etf("XNDX", i)
        target_mr_units = target_mr_usd / notional_unit_mr if notional_unit_mr > 0 else 0

        # Decision selon approche
        if approach == "continuous":
            actual_mr_units = target_mr_units  # pas d'arrondi
        elif approach == "daily_round":
            actual_mr_units = round(target_mr_units)
        elif approach == "trigger_only":
            eff_mr_prev = eff_mr_values[i-1]
            if abs(eff_mr_today - eff_mr_prev) > 0.05:
                actual_mr_units = round(target_mr_units)
            else:
                actual_mr_units = prev_positions["XNDX_MR"]

        actual_ts = {}
        for a in panier:
            eff_a_today = eff_ts_values[a][i]
            target_a_usd = alloc_ts_per * cap_usd_t * eff_a_today
            notional_unit = notional_etf(a, i)
            target_a_units = target_a_usd / notional_unit if notional_unit > 0 else 0

            if approach == "continuous":
                actual_ts[a] = target_a_units
            elif approach == "daily_round":
                actual_ts[a] = round(target_a_units)
            elif approach == "trigger_only":
                eff_a_prev = eff_ts_values[a][i-1]
                if abs(eff_a_today - eff_a_prev) > 0.05:
                    actual_ts[a] = round(target_a_units)
                else:
                    actual_ts[a] = prev_positions[a]

        # PnL
        pnl_today = 0.0
        if prev_positions["XNDX_MR"] != 0:
            price_today = prices_dict["XNDX"].loc[day]
            price_prev = prices_dict["XNDX"].loc[common_idx[i-1]]
            ret = (price_today - price_prev) / price_prev
            position_notional_prev = prev_positions["XNDX_MR"] * notional_etf("XNDX", i-1)
            pnl_today += position_notional_prev * ret

        for a in panier:
            if prev_positions[a] != 0:
                price_today = prices_dict[a].loc[day]
                price_prev = prices_dict[a].loc[common_idx[i-1]]
                ret = (price_today - price_prev) / price_prev
                position_notional_prev = prev_positions[a] * notional_etf(a, i-1)
                pnl_today += position_notional_prev * ret

        daily_ret_pct = pnl_today / cap_usd_t if cap_usd_t > 0 else 0
        capital_t *= (1 + daily_ret_pct)
        daily_rets.append(daily_ret_pct)

        prev_positions["XNDX_MR"] = actual_mr_units
        for a in panier:
            prev_positions[a] = actual_ts[a]

    return pd.Series(daily_rets, index=common_idx)


def main():
    cfg = load_config(CONFIG_PATH)
    panier = cfg["tsmom"]["panier"]
    prices_dict = {a: load_prices(a) for a in panier}
    capital_usd = 10_000_000 * 1.08  # 10M EUR

    print("=" * 70)
    print("DIAGNOSTIC SIMULATION - 3 approches à 10M EUR + ETF")
    print("=" * 70)

    print("\nConstruction signaux...")
    pos_mr, eff_xndx = build_xndx_mr_positions(prices_dict["XNDX"], cfg)
    eff_tsmom = build_tsmom_positions(prices_dict, cfg)

    # === BASELINE : backtest.py (compute_combo_returns) ===
    print("\n[BASELINE] backtest.py compute_combo_returns()...")
    r_combo, _, _, _ = compute_combo_returns(
        prices_dict["XNDX"], eff_xndx, prices_dict, eff_tsmom, cfg,
        start_date="2005-11-17"
    )
    m_base = compute_stats(r_combo, "BASELINE")
    print(f"  Sharpe={m_base['sharpe']:.4f} CAGR={m_base['cagr_pct']:.3f}% MaxDD={m_base['maxdd_pct']:.3f}%")

    # Index commun
    common_idx = eff_xndx.index
    for a in panier:
        common_idx = common_idx.intersection(prices_dict[a].index)
        common_idx = common_idx.intersection(eff_tsmom[a].index)
    common_idx = common_idx[common_idx >= pd.Timestamp("2005-11-17")].sort_values()

    # === Approche 1 : CONTINUOUS ===
    print("\n[1] CONTINUOUS (pas d'arrondi, juste avec ma sim)...")
    r1 = simulate_approach("continuous", capital_usd, eff_xndx, eff_tsmom,
                            prices_dict, cfg, common_idx)
    m1 = compute_stats(r1, "CONTINUOUS")
    print(f"  Sharpe={m1['sharpe']:.4f} CAGR={m1['cagr_pct']:.3f}% MaxDD={m1['maxdd_pct']:.3f}%")
    print(f"  Δ vs baseline : Sharpe {m1['sharpe']-m_base['sharpe']:+.4f}, "
          f"CAGR {m1['cagr_pct']-m_base['cagr_pct']:+.3f}pp")

    # === Approche 2 : DAILY ROUND ===
    print("\n[2] DAILY ROUND (arrondi chaque jour)...")
    r2 = simulate_approach("daily_round", capital_usd, eff_xndx, eff_tsmom,
                            prices_dict, cfg, common_idx)
    m2 = compute_stats(r2, "DAILY ROUND")
    print(f"  Sharpe={m2['sharpe']:.4f} CAGR={m2['cagr_pct']:.3f}% MaxDD={m2['maxdd_pct']:.3f}%")
    print(f"  Δ vs baseline : Sharpe {m2['sharpe']-m_base['sharpe']:+.4f}, "
          f"CAGR {m2['cagr_pct']-m_base['cagr_pct']:+.3f}pp")

    # === Approche 3 : TRIGGER ONLY ===
    print("\n[3] TRIGGER ONLY (rebalance uniquement aux changements)...")
    r3 = simulate_approach("trigger_only", capital_usd, eff_xndx, eff_tsmom,
                            prices_dict, cfg, common_idx)
    m3 = compute_stats(r3, "TRIGGER ONLY")
    print(f"  Sharpe={m3['sharpe']:.4f} CAGR={m3['cagr_pct']:.3f}% MaxDD={m3['maxdd_pct']:.3f}%")
    print(f"  Δ vs baseline : Sharpe {m3['sharpe']-m_base['sharpe']:+.4f}, "
          f"CAGR {m3['cagr_pct']-m_base['cagr_pct']:+.3f}pp")

    # Conclusion
    print("\n" + "=" * 70)
    print("DIAGNOSTIC")
    print("=" * 70)
    if abs(m1['cagr_pct'] - m_base['cagr_pct']) < 0.1:
        print("✓ Approche CONTINUOUS = baseline → la sim est juste à gros capital")
    else:
        print(f"✗ Approche CONTINUOUS diverge de {m1['cagr_pct']-m_base['cagr_pct']:+.2f}pp")
        print("  → BUG dans la simulation elle-même (pas dans l'arrondi)")

    if abs(m2['cagr_pct'] - m_base['cagr_pct']) < 0.1:
        print("✓ Approche DAILY ROUND = baseline → l'arrondi quotidien à gros capital est OK")
    else:
        print(f"✗ Approche DAILY ROUND diverge de {m2['cagr_pct']-m_base['cagr_pct']:+.2f}pp")

    if abs(m3['cagr_pct'] - m_base['cagr_pct']) < 0.1:
        print("✓ Approche TRIGGER ONLY ≈ baseline → la logique trigger est correcte")
    else:
        print(f"✗ Approche TRIGGER ONLY diverge de {m3['cagr_pct']-m_base['cagr_pct']:+.2f}pp")
        print("  → BUG dans la logique de rebalance trigger")


if __name__ == "__main__":
    main()
