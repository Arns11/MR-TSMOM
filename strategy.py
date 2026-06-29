"""
src/strategy.py
=================
Logique du combo XNDX MR LO + TSMOM LO sauf CL.

Toutes les fonctions de signal sont ici. Les scripts importeront depuis
ce fichier.

USAGE :
    from src.strategy import (
        load_config, load_wfo_state, save_wfo_state,
        build_xndx_mr_positions, build_tsmom_positions,
        compute_combo_returns,
    )
"""

import json
from pathlib import Path
import numpy as np
import pandas as pd

TRADING_DAYS = 252


# ====================================================================
# CONFIG & STATE
# ====================================================================
def load_config(config_path):
    with open(config_path, "r") as f:
        return json.load(f)


def load_wfo_state(state_path):
    if not Path(state_path).exists():
        return None
    with open(state_path, "r") as f:
        return json.load(f)


def save_wfo_state(state_path, state):
    with open(state_path, "w") as f:
        json.dump(state, f, indent=2)


# ====================================================================
# XNDX MR LO
# ====================================================================
def compute_zscore(prices, N):
    sma = prices.rolling(N, min_periods=N).mean()
    std = prices.rolling(N, min_periods=N).std()
    return (prices - sma) / std


def generate_positions_mr(z, threshold, time_stop, init_state=None):
    """Genere positions binaires (0 ou 1) selon z-score."""
    pos = np.zeros(len(z))
    if init_state is not None:
        cur, days = init_state["cur"], init_state["days"]
    else:
        cur, days = 0, 0
    zv = z.values
    for i in range(len(z)):
        zi = zv[i]
        if np.isnan(zi):
            pos[i] = 0; cur = 0; days = 0; continue
        if cur == 0:
            if zi <= -threshold:
                cur = 1; days = 1
        elif cur == 1:
            if zi >= 0 or days >= time_stop:
                cur = 0; days = 0
            else:
                days += 1
        pos[i] = cur
    return pd.Series(pos, index=z.index), {"cur": cur, "days": days}


def compute_eff_mr(prices_full, positions_full, vol_target, vol_window=60):
    """Applique le vol-targeting aux positions binaires."""
    rets = prices_full.pct_change()
    vol_real = rets.rolling(vol_window, min_periods=vol_window).std() * np.sqrt(TRADING_DAYS)
    lev = (vol_target / vol_real).replace([np.inf, -np.inf], np.nan)
    return (positions_full * lev).shift(1)


def select_best_mr_params(prices_full, is_start, is_end, vol_target,
                            N_grid, thr_grid, time_stop, vol_window=60):
    """Selectionne N et threshold qui maximisent Sharpe IS."""
    best_sharpe = -999
    best_params = None
    for N in N_grid:
        z_full = compute_zscore(prices_full, N)
        for thr in thr_grid:
            pos_full, _ = generate_positions_mr(z_full, thr, time_stop)
            eff = compute_eff_mr(prices_full, pos_full, vol_target, vol_window)
            rets = prices_full.pct_change()
            r = (eff * rets).fillna(0).loc[is_start:is_end]
            sharpe = (r.mean() / r.std()) * np.sqrt(TRADING_DAYS) if r.std() > 0 else 0
            if sharpe > best_sharpe:
                best_sharpe = sharpe
                best_params = (N, thr, sharpe)
    return best_params


def build_wfo_folds(wfo_start, end_date, is_years, oos_years):
    folds = []
    oos_start = pd.Timestamp(wfo_start)
    end = pd.Timestamp(end_date)
    while oos_start <= end:
        oos_end = min(oos_start + pd.DateOffset(years=oos_years) - pd.Timedelta(days=1), end)
        is_end = oos_start - pd.Timedelta(days=1)
        is_start = is_end - pd.DateOffset(years=is_years) + pd.Timedelta(days=1)
        folds.append({
            "is_start": is_start.strftime("%Y-%m-%d"),
            "is_end": is_end.strftime("%Y-%m-%d"),
            "oos_start": oos_start.strftime("%Y-%m-%d"),
            "oos_end": oos_end.strftime("%Y-%m-%d"),
        })
        oos_start = oos_start + pd.DateOffset(years=oos_years)
    return folds


def build_xndx_mr_positions(prices_xndx, config, end_date=None):
    """
    Construit positions effectives XNDX MR avec WFO complet.
    Retourne :
      - pos_binary : positions 0/1 (signal d'etat)
      - eff_xndx : exposition effective (binary * leverage_vol_target)
    """
    mr = config["xndx_mr"]
    if end_date is None:
        end_date = prices_xndx.index[-1].strftime("%Y-%m-%d")

    # Phase 1 : parametres figes
    z_p1 = compute_zscore(prices_xndx, mr["z_score"]["phase1_N"]).loc[
        mr["z_score"]["phase1_start"]:mr["z_score"]["phase1_end"]
    ]
    pos_p1, state = generate_positions_mr(
        z_p1, mr["z_score"]["phase1_threshold"], mr["time_stop_days"]
    )

    # Phase 2 : WFO
    folds = build_wfo_folds(
        mr["wfo"]["start"], end_date,
        mr["wfo"]["is_years"], mr["wfo"]["oos_years"]
    )
    pos_p2_segments = []
    for fold in folds:
        N_sel, thr_sel, _ = select_best_mr_params(
            prices_xndx, fold["is_start"], fold["is_end"],
            mr["vol_target"], mr["wfo"]["N_grid"], mr["wfo"]["thr_grid"],
            mr["time_stop_days"], mr["vol_window"]
        )
        z_oos = compute_zscore(prices_xndx, N_sel).loc[fold["oos_start"]:fold["oos_end"]]
        pos_oos, state = generate_positions_mr(
            z_oos, thr_sel, mr["time_stop_days"], init_state=state
        )
        pos_p2_segments.append(pos_oos)

    pos_p2 = pd.concat(pos_p2_segments).sort_index() if pos_p2_segments else pd.Series(dtype=float)
    pos_p2 = pos_p2[~pos_p2.index.duplicated(keep="last")]
    pos_complete = pd.concat([pos_p1, pos_p2]).sort_index()
    pos_complete = pos_complete[~pos_complete.index.duplicated(keep="last")]

    pos_binary = pd.Series(0.0, index=prices_xndx.index)
    pos_binary.loc[pos_complete.index] = pos_complete.values

    eff = compute_eff_mr(prices_xndx, pos_binary, mr["vol_target"], mr["vol_window"]).fillna(0)
    return pos_binary, eff


# ====================================================================
# TSMOM LO sauf CL
# ====================================================================
def compute_pos_tsmom(prices, vt, cap, n_momentum=252, vol_window=60, long_only=False):
    """Calcule position TSMOM avec vol-targeting."""
    rets = prices.pct_change()
    sig = np.sign(prices.pct_change(n_momentum))
    if long_only:
        sig = sig.where(sig > 0, 0)
    vol = rets.rolling(vol_window).std() * np.sqrt(TRADING_DAYS)
    return (sig * (vt / vol).clip(upper=cap)).shift(1)


def rebalance_tsmom(pos, M):
    """Garde la position constante pendant M jours, puis met a jour."""
    out = pd.Series(np.nan, index=pos.index)
    last = 0.0
    counter = M
    for i, (_, p) in enumerate(pos.items()):
        if np.isnan(p):
            out.iloc[i] = last
            continue
        if counter >= M:
            last = p
            counter = 1
        else:
            counter += 1
        out.iloc[i] = last
    return out


def build_tsmom_positions(prices_dict, config):
    """
    Retourne dict {actif: effective_position}.
    Applique long-only ou long/short selon config.
    """
    ts = config["tsmom"]
    eff_dict = {}
    for asset, prices in prices_dict.items():
        is_lo = asset in ts["long_only_actifs"]
        pos = compute_pos_tsmom(
            prices, ts["vol_target"], ts["leverage_cap"],
            n_momentum=ts["signal"]["momentum_lookback_days"],
            vol_window=ts["vol_window"], long_only=is_lo
        )
        eff_dict[asset] = rebalance_tsmom(pos, ts["rebalance_days"]).fillna(0.0)
    return eff_dict


# ====================================================================
# COMBO
# ====================================================================
def compute_combo_returns(prices_xndx, eff_xndx, prices_dict, eff_tsmom, config, start_date=None):
    """Calcule le rendement du combo 50/50."""
    panier = config["tsmom"]["panier"]
    alloc_mr = config["allocation"]["mr"]
    alloc_ts = config["allocation"]["tsmom"]

    common_idx = prices_xndx.index.intersection(eff_xndx.index)
    for a in panier:
        common_idx = common_idx.intersection(prices_dict[a].index)
        common_idx = common_idx.intersection(eff_tsmom[a].index)
    if start_date is not None:
        common_idx = common_idx[common_idx >= pd.Timestamp(start_date)]
    common_idx = common_idx.sort_values()

    rets_xndx = prices_xndx.pct_change()
    r_mr = (eff_xndx * rets_xndx).fillna(0).reindex(common_idx).fillna(0)

    r_ts_per = {}
    for a in panier:
        rets_a = prices_dict[a].pct_change()
        r_ts_per[a] = (eff_tsmom[a] * rets_a).fillna(0)
    df_ts = pd.DataFrame(r_ts_per).reindex(common_idx).fillna(0)
    r_ts = df_ts.mean(axis=1)

    r_combo = alloc_mr * r_mr + alloc_ts * r_ts
    return r_combo, r_mr, r_ts, df_ts


# ====================================================================
# STATS
# ====================================================================
def compute_stats(returns, label=""):
    r = returns.dropna()
    if len(r) < 30:
        return {}
    eq = (1 + r).cumprod()
    n_y = (eq.index[-1] - eq.index[0]).days / 365.25
    cagr = (eq.iloc[-1] ** (1/n_y) - 1) * 100 if eq.iloc[-1] > 0 else -100
    sharpe = (r.mean() / r.std()) * np.sqrt(TRADING_DAYS) if r.std() > 0 else 0
    rm = eq.cummax()
    dd = (eq - rm) / rm
    return {
        "label": label,
        "sharpe": sharpe,
        "cagr_pct": cagr,
        "maxdd_pct": dd.min() * 100,
        "vol_pct": r.std() * np.sqrt(TRADING_DAYS) * 100,
        "start": eq.index[0],
        "end": eq.index[-1],
        "n_years": n_y,
    }


def yearly_breakdown(returns):
    """Retourne DataFrame avec perfs annuelles."""
    df = pd.DataFrame({"ret": returns})
    df["year"] = df.index.year
    rows = []
    for year, group in df.groupby("year"):
        if len(group) < 20: continue
        cagr = ((1 + group["ret"]).prod() - 1) * 100
        eq = (1 + group["ret"]).cumprod()
        dd = ((eq - eq.cummax()) / eq.cummax()).min() * 100
        sh = (group["ret"].mean() / group["ret"].std()) * np.sqrt(TRADING_DAYS) if group["ret"].std() > 0 else 0
        rows.append({"year": year, "cagr_pct": cagr, "maxdd_pct": dd, "sharpe": sh})
    return pd.DataFrame(rows)
