"""
diag_simulation_v2.py
======================
Test la nouvelle approche : utiliser baseline et appliquer ratio d'arrondi.
A 10M EUR + ETF, doit converger vers baseline.
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


def get_unit_notional_funcs_etf(prices_dict, common_idx):
    def etf_notional(asset, day_idx):
        price = prices_dict[asset].loc[common_idx[day_idx]]
        if asset == "XNDX":
            return price / 50
        elif asset == "SPXTR":
            return price / 100
        else:
            return price
    return etf_notional, etf_notional


def simulate_realistic_v2(capital_eur, leverage, eff_xndx, eff_tsmom, prices_dict, cfg, start_date):
    """
    Nouvelle approche : baseline x ratio d'arrondi.
    """
    eur_usd = 1.08
    capital_usd = capital_eur * eur_usd
    panier = cfg["tsmom"]["panier"]
    alloc_mr = cfg["allocation"]["mr"]
    alloc_ts_per = cfg["allocation"]["tsmom"] / len(panier)

    common_idx = eff_xndx.index
    for a in panier:
        common_idx = common_idx.intersection(prices_dict[a].index)
        common_idx = common_idx.intersection(eff_tsmom[a].index)
    common_idx = common_idx[common_idx >= pd.Timestamp(start_date)].sort_values()

    notional_mr_fn, notional_ts_fn = get_unit_notional_funcs_etf(prices_dict, common_idx)

    cap_usd_ref = capital_usd

    def compute_ratio(eff_series, asset, notional_fn, alloc_per):
        ratios = []
        for i in range(len(common_idx)):
            day = common_idx[i]
            eff_today = eff_series.loc[day] if not np.isnan(eff_series.loc[day]) else 0
            if eff_today == 0:
                ratios.append(1.0)
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

    ratio_mr = compute_ratio(eff_xndx, "XNDX", notional_mr_fn, alloc_mr)
    ratios_ts = {a: compute_ratio(eff_tsmom[a], a, notional_ts_fn, alloc_ts_per) for a in panier}

    # Baseline rendements
    rets_xndx = prices_dict["XNDX"].pct_change().reindex(common_idx).fillna(0)
    eff_xndx_aligned = eff_xndx.reindex(common_idx).fillna(0)
    r_mr_theo = (eff_xndx_aligned * rets_xndx) * leverage

    r_ts_theo_per = {}
    for a in panier:
        rets_a = prices_dict[a].pct_change().reindex(common_idx).fillna(0)
        eff_a = eff_tsmom[a].reindex(common_idx).fillna(0)
        r_ts_theo_per[a] = (eff_a * rets_a) * leverage

    # Application ratio
    r_mr_real = r_mr_theo * ratio_mr.shift(1).fillna(1.0)
    r_ts_real_total = pd.Series(0.0, index=common_idx)
    for a in panier:
        ratio_shifted = ratios_ts[a].shift(1).fillna(1.0)
        r_ts_real_a = r_ts_theo_per[a] * ratio_shifted
        r_ts_real_total += r_ts_real_a * alloc_ts_per

    return alloc_mr * r_mr_real + r_ts_real_total


def main():
    cfg = load_config(CONFIG_PATH)
    panier = cfg["tsmom"]["panier"]
    prices_dict = {a: load_prices(a) for a in panier}

    print("Construction signaux...")
    pos_mr, eff_xndx = build_xndx_mr_positions(prices_dict["XNDX"], cfg)
    eff_tsmom = build_tsmom_positions(prices_dict, cfg)

    print("\n[BASELINE] backtest.py compute_combo_returns()...")
    r_combo, _, _, _ = compute_combo_returns(
        prices_dict["XNDX"], eff_xndx, prices_dict, eff_tsmom, cfg,
        start_date="2005-11-17"
    )
    m_base = compute_stats(r_combo, "BASELINE")
    print(f"  Sharpe={m_base['sharpe']:.4f} CAGR={m_base['cagr_pct']:.3f}% MaxDD={m_base['maxdd_pct']:.3f}%")

    # Test à 3 capitaux différents
    for capital in [10_000_000, 1_000_000, 100_000, 30_000]:
        print(f"\n[NOUVEAU] Simulation à {capital:,} EUR + ETF + levier 1x")
        r_real = simulate_realistic_v2(capital, 1.0, eff_xndx, eff_tsmom, prices_dict, cfg, "2005-11-17")
        m_real = compute_stats(r_real, f"REAL_{capital}")
        print(f"  Sharpe={m_real['sharpe']:.4f} CAGR={m_real['cagr_pct']:.3f}% MaxDD={m_real['maxdd_pct']:.3f}%")
        print(f"  Δ vs baseline : Sharpe {m_real['sharpe']-m_base['sharpe']:+.4f}, "
              f"CAGR {m_real['cagr_pct']-m_base['cagr_pct']:+.3f}pp")


if __name__ == "__main__":
    main()
