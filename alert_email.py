"""
scripts/alert_email.py — COMBO MR + TSMOM
==========================================
Alerte email quotidienne du combo XNDX MR + TSMOM.

GARDE-FOUS NIVEAUX 1, 2, 3 :
- Niveau 1 : Retry 3x à 15min si data pas fresh
- Niveau 2 : REFUS d'envoyer le signal si data du jour J non disponible
- Niveau 3 : Email alerte technique admin + email "pas de signal" abonnés

PRINCIPE FONDAMENTAL : mieux vaut PAS de signal qu'un MAUVAIS signal.

CONFIDENTIALITE : l'abonné voit ticker + direction + montant à exécuter,
mais PAS la logique interne (pas de multiplicateur vol-target, pas de
z-score, pas de momentum/lookback, pas de "pourquoi ce signal").

Prix actifs — cascade : EODHD → Yahoo → CSV local.
EUR/USD   — cascade : Yahoo → EODHD → cache (warning > 10j) → 1.08 (alerte).

Variables d'environnement :
  GMAIL_USER, GMAIL_APP_PASS, ALERT_RECIPIENTS, EMAIL_ADMIN,
  EODHD_API_KEY, SUBSCRIBER_CURRENCY (EUR/USD, défaut EUR)

USAGE :
    python scripts/alert_email.py
"""

import os
import sys
import json
import smtplib
import time
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import numpy as np

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from src.strategy import (
    load_config, build_xndx_mr_positions, build_tsmom_positions,
)

DATA_DIR          = ROOT / "data"
CONFIG_PATH       = ROOT / "config" / "parameters.json"
EURUSD_CACHE_PATH = ROOT / "config" / "eur_usd_cache.json"

EODHD_API_KEY       = os.environ.get("EODHD_API_KEY", "69fdc152a61830.85937256")
GMAIL_USER          = os.environ.get("GMAIL_USER", "")
GMAIL_APP_PASS      = os.environ.get("GMAIL_APP_PASS", "")
ALERT_RECIPIENTS    = os.environ.get("ALERT_RECIPIENTS", "")
EMAIL_ADMIN         = os.environ.get("EMAIL_ADMIN", GMAIL_USER)
SUBSCRIBER_CURRENCY = os.environ.get("SUBSCRIBER_CURRENCY", "EUR").upper()

CAPITAL_REF           = 10_000
EURUSD_MAX_CACHE_DAYS = 10
SMTP_SERVER, SMTP_PORT = "smtp.gmail.com", 587
FETCH_DAYS_BACK       = 600   # ~2 ans — suffisant pour TSMOM 252j + marge z-score

# Tickers EODHD par défaut (écrasés par cfg["data_source"]["tickers"] si présent)
_EODHD_TICKERS_DEFAULT = {
    "XNDX":  "XNDX.INDX",
    "SPXTR": "SPXTR.INDX",
    "TLT":   "TLT.US",
    "GLD":   "GLD.US",
    "CL":    "USO.US",
}

# Tickers Yahoo Finance (fallback niveau 2)
_YAHOO_TICKERS_FALLBACK = {
    "XNDX":  "^NDX",
    "SPXTR": "SPY",
    "TLT":   "TLT",
    "GLD":   "GLD",
    "CL":    "USO",
}


# ====================================================================
# EUR/USD — cascade Yahoo → EODHD → cache → hardcodé
# ====================================================================
def _load_cache():
    if EURUSD_CACHE_PATH.exists():
        with open(EURUSD_CACHE_PATH) as f:
            return json.load(f)
    return None


def _save_cache(rate):
    EURUSD_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(EURUSD_CACHE_PATH, "w") as f:
        json.dump({"rate": rate, "date": datetime.utcnow().isoformat()}, f)


def fetch_eur_usd():
    try:
        import yfinance as yf
        val = yf.Ticker("EURUSD=X").fast_info["lastPrice"]
        if 0.8 < val < 1.5:
            print(f"  EUR/USD Yahoo : {val:.4f}")
            _save_cache(val)
            return val, None
    except Exception as e:
        print(f"  EUR/USD Yahoo echec : {e}")

    try:
        import urllib.request
        url = (f"https://eodhd.com/api/real-time/EUR.FOREX"
               f"?api_token={EODHD_API_KEY}&fmt=json")
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.loads(r.read())
        val = float(data["close"])
        if 0.8 < val < 1.5:
            print(f"  EUR/USD EODHD : {val:.4f}")
            _save_cache(val)
            return val, None
    except Exception as e:
        print(f"  EUR/USD EODHD echec : {e}")

    cache = _load_cache()
    if cache:
        age_days = (datetime.utcnow() - datetime.fromisoformat(cache["date"])).days
        val = cache["rate"]
        if age_days <= EURUSD_MAX_CACHE_DAYS:
            warn = f"Note : taux EUR/USD issu du cache ({age_days}j)."
        else:
            warn = f"Attention : taux EUR/USD cache ancien ({age_days}j)."
        print(f"  {warn}")
        return val, warn

    warn = "Attention : taux EUR/USD de secours utilise (1.08)."
    print(f"  {warn}")
    return 1.08, warn


# ====================================================================
# PRIX — cascade EODHD → Yahoo → CSV local
# ====================================================================
def _fetch_eodhd(eodhd_ticker: str) -> "pd.Series | None":
    """Fetch prix EOD depuis l'API EODHD."""
    start = (datetime.utcnow() - timedelta(days=FETCH_DAYS_BACK)).strftime("%Y-%m-%d")
    url = (
        f"https://eodhd.com/api/eod/{eodhd_ticker}"
        f"?api_token={EODHD_API_KEY}&from={start}&fmt=json&order=a"
    )
    try:
        import urllib.request
        with urllib.request.urlopen(url, timeout=20) as r:
            data = json.loads(r.read())
        if not data:
            return None
        df = pd.DataFrame(data)
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date")
        # adjusted_close pour ETF (dividendes inclus), close pour indices
        col = "adjusted_close" if "adjusted_close" in df.columns else "close"
        s = df[col].dropna().rename("close")
        if len(s) < 50:
            return None
        print(f"  EODHD {eodhd_ticker} ({col}): {len(s)} pts, dernier {s.index[-1].date()}")
        return s
    except Exception as e:
        print(f"  EODHD {eodhd_ticker} echec: {e}")
        return None


def _fetch_yahoo(yahoo_ticker: str) -> "pd.Series | None":
    """Fetch prix depuis Yahoo Finance."""
    try:
        import yfinance as yf
        start = (datetime.utcnow() - timedelta(days=FETCH_DAYS_BACK)).strftime("%Y-%m-%d")
        raw = yf.download(yahoo_ticker, start=start, progress=False, auto_adjust=True)
        if raw.empty:
            return None
        s = raw["Close"]
        if hasattr(s, "columns"):   # MultiIndex (yfinance >= 0.2.40)
            s = s.iloc[:, 0]
        s.index = pd.to_datetime(s.index).tz_localize(None)
        s.index.name = "date"
        s = s.dropna().rename("close")
        print(f"  Yahoo {yahoo_ticker}: {len(s)} pts, dernier {s.index[-1].date()}")
        return s
    except Exception as e:
        print(f"  Yahoo {yahoo_ticker} echec: {e}")
        return None


def load_prices(asset: str, cfg=None) -> "pd.Series | None":
    """
    Cascade EODHD → Yahoo → CSV local.
    cfg (optionnel) : si cfg["data_source"]["tickers"][asset] existe, prioritaire
    sur _EODHD_TICKERS_DEFAULT.
    """
    # Ticker EODHD : config d'abord, défaut ensuite
    eodhd_ticker = None
    if cfg and "data_source" in cfg and "tickers" in cfg["data_source"]:
        eodhd_ticker = cfg["data_source"]["tickers"].get(asset)
    if eodhd_ticker is None:
        eodhd_ticker = _EODHD_TICKERS_DEFAULT.get(asset)

    yahoo_ticker = _YAHOO_TICKERS_FALLBACK.get(asset)

    # 1. EODHD (primaire)
    if eodhd_ticker:
        s = _fetch_eodhd(eodhd_ticker)
        if s is not None:
            return s

    # 2. Yahoo (fallback)
    if yahoo_ticker:
        print(f"  [{asset}] EODHD KO → Yahoo...")
        s = _fetch_yahoo(yahoo_ticker)
        if s is not None:
            return s

    # 3. CSV local (dernier recours)
    csv_path = DATA_DIR / f"{asset}.csv"
    if csv_path.exists():
        print(f"  [{asset}] Yahoo KO → CSV local...")
        return pd.read_csv(csv_path, parse_dates=["date"], index_col="date")["close"]

    print(f"  [{asset}] TOUTES SOURCES KO — aucune donnee disponible")
    return None


def is_data_fresh_strict(prices_dict):
    now_ny_dt   = datetime.now(ZoneInfo("America/New_York"))
    now_ny_date = now_ny_dt.date()

    if now_ny_dt.hour >= 16:
        expected_date = pd.Timestamp(now_ny_date)
    else:
        expected_date = pd.Timestamp(now_ny_date) - pd.tseries.offsets.BDay(1)

    while expected_date.dayofweek >= 5:
        expected_date -= pd.tseries.offsets.BDay(1)

    for asset, prices in prices_dict.items():
        if prices is None or len(prices) == 0:
            return False, f"Pas de donnees pour {asset}", None, expected_date
        last = prices.index[-1]
        if last < expected_date:
            days_late = len(pd.bdate_range(last + pd.Timedelta(days=1),
                                           expected_date.strftime("%Y-%m-%d")))
            return False, (f"{asset}: dernier prix {last.date()} "
                           f"(attendu {expected_date.date()}, retard {days_late}j ouvre(s))"), last, expected_date

    return True, "OK", prices_dict[list(prices_dict.keys())[0]].index[-1], expected_date


# ====================================================================
# EMAIL (Gmail SMTP)
# ====================================================================
def send_email(subject, body, recipients, is_html=False):
    if not GMAIL_USER or not GMAIL_APP_PASS:
        print("[WARN: GMAIL_USER / GMAIL_APP_PASS absents, email non envoye]")
        print(f"--- EMAIL ---\nTo: {recipients}\nSubject: {subject}\n\n{body}\n---")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = GMAIL_USER
    msg["To"]      = ", ".join(recipients) if isinstance(recipients, list) else recipients
    msg.attach(MIMEText(body, "html" if is_html else "plain", "utf-8"))

    try:
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as srv:
            srv.starttls()
            srv.login(GMAIL_USER, GMAIL_APP_PASS)
            srv.sendmail(GMAIL_USER,
                         recipients if isinstance(recipients, list) else [recipients],
                         msg.as_string())
        print(f"  [Email envoye a {recipients}]")
        return True
    except Exception as e:
        print(f"  [ERREUR envoi email : {e}]")
        return False


def get_recipients():
    raw = ALERT_RECIPIENTS.strip()
    if not raw:
        return [GMAIL_USER]
    return [r.strip() for r in raw.split(",") if r.strip()]


def alert_admin_data_issue(msg, attempts):
    subject = "[COMBO MR+TSMOM] ALERTE DATA - Signal NON envoye"
    body = f"""ALERTE TECHNIQUE - Combo MR + TSMOM

Le signal du jour N'A PAS pu etre envoye aux abonnes car les donnees ne sont pas a jour.

Detail : {msg}
Tentatives effectuees : {attempts}

ACTION REQUISE :
1. Verifier le telechargement EODHD
2. Verifier disponibilite Yahoo fallback
3. Verifier les logs GitHub Actions
4. Une fois data recuperee, relancer : python scripts/alert_email.py
"""
    send_email(subject, body, [EMAIL_ADMIN])


def alert_subscribers_no_signal(expected_date):
    subject = "[COMBO] Pas de signal aujourd'hui - Conserver positions actuelles"
    body = f"""Bonjour,

Le signal du combo n'a pas pu etre calcule aujourd'hui ({expected_date}).

CAUSE TECHNIQUE : nos sources de donnees ne sont pas a jour.
Principe "mieux vaut pas de signal qu'un mauvais signal" applique.

ACTION RECOMMANDEE :
- CONSERVER vos positions actuelles
- NE PAS placer d'ordres MOC aujourd'hui

Le signal sera envoye des que les donnees seront disponibles.

L'equipe Combo
"""
    send_email(subject, body, get_recipients())


# ====================================================================
# GENERATION DU MESSAGE SIGNAL
# (ticker + direction + montant visibles ; logique interne masquee)
# ====================================================================
def fmt_amount(amount_primary, currency_primary, amount_secondary, currency_secondary):
    sym = {"EUR": "EUR ", "USD": "USD "}
    p = sym.get(currency_primary, currency_primary)
    s = sym.get(currency_secondary, currency_secondary)
    return f"{p}{amount_primary:,.0f}  ({s}{amount_secondary:,.0f})"


def _badge(direction):
    colors = {"LONG": "#15803d", "SHORT": "#b91c1c", "CASH": "#64748b"}
    c = colors.get(direction, "#64748b")
    return f'<span style="display:inline-block;background:{c};color:#fff;font-size:12px;font-weight:700;padding:2px 10px;border-radius:12px;letter-spacing:.5px;">{direction}</span>'


def _expo_html(expo_eur, expo_usd, cur_pri):
    if cur_pri == "EUR":
        big, small = f"{expo_eur:,.0f} EUR", f"{expo_usd:,.0f} USD"
    else:
        big, small = f"{expo_usd:,.0f} USD", f"{expo_eur:,.0f} EUR"
    return f'<span style="font-size:15px;font-weight:700;color:#0f172a;">{big}</span> <span style="font-size:12px;color:#94a3b8;">({small})</span>'


def generate_signal_message(mr_exp_today, ts_exp_today, today_idx,
                             eur_usd, eurusd_warning,
                             subscriber_currency, capital_ref):
    if subscriber_currency == "EUR":
        cap_eur, cap_usd = capital_ref, capital_ref * eur_usd
        cur_pri, cur_sec = "EUR", "USD"
    else:
        cap_usd, cap_eur = capital_ref, capital_ref / eur_usd
        cur_pri, cur_sec = "USD", "EUR"

    alloc_mr_usd  = cap_usd * 0.50
    alloc_ts_usd  = cap_usd * 0.50
    n_actifs_ts   = len(ts_exp_today)
    alloc_per_usd = alloc_ts_usd / n_actifs_ts if n_actifs_ts else 0

    cap_p = cap_eur if cur_pri == "EUR" else cap_usd
    cap_s = cap_usd if cur_pri == "EUR" else cap_eur
    cap_str = f"{cap_p:,.0f} {cur_pri}  ({cap_s:,.0f} {cur_sec})"

    next_bday = (today_idx + pd.tseries.offsets.BDay(1)).date()

    # --- POSITION 1 : XNDX MR ---
    if np.isnan(mr_exp_today) or mr_exp_today == 0:
        p1_rows = f'<tr><td style="padding:10px 14px;">{_badge("CASH")} &nbsp;XNDX</td><td style="padding:10px 14px;text-align:right;color:#64748b;">Aucune position</td></tr>'
    else:
        expo_usd = alloc_mr_usd * mr_exp_today
        expo_eur = expo_usd / eur_usd
        p1_rows = f'<tr><td style="padding:10px 14px;">{_badge("LONG")} &nbsp;XNDX</td><td style="padding:10px 14px;text-align:right;">{_expo_html(expo_eur, expo_usd, cur_pri)}</td></tr>'

    # --- POSITION 2 : TSMOM ---
    p2_rows = ""
    for asset, exp in ts_exp_today.items():
        if np.isnan(exp) or exp == 0:
            p2_rows += f'<tr><td style="padding:8px 14px;border-top:1px solid #f1f5f9;">{_badge("CASH")} &nbsp;{asset}</td><td style="padding:8px 14px;border-top:1px solid #f1f5f9;text-align:right;color:#64748b;">Aucune position</td></tr>'
        else:
            expo_usd = alloc_per_usd * abs(exp)
            expo_eur = expo_usd / eur_usd
            direction = "LONG" if exp > 0 else "SHORT"
            p2_rows += f'<tr><td style="padding:8px 14px;border-top:1px solid #f1f5f9;">{_badge(direction)} &nbsp;{asset}</td><td style="padding:8px 14px;border-top:1px solid #f1f5f9;text-align:right;">{_expo_html(expo_eur, expo_usd, cur_pri)}</td></tr>'

    warn_html = ""
    if eurusd_warning:
        warn_html = f'<tr><td style="padding:8px 28px;"><p style="margin:0;color:#92400e;background:#fef3c7;padding:8px 12px;border-radius:6px;font-size:12px;">{eurusd_warning}</p></td></tr>'

    html = f"""<!DOCTYPE html><html><body style="margin:0;padding:0;background:#f1f5f9;font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f1f5f9;padding:24px 0;"><tr><td align="center">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:14px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.1);">

  <tr><td style="background:#0f172a;padding:22px 28px;">
    <p style="margin:0;color:#fff;font-size:18px;font-weight:700;">COMBO XNDX MR + TSMOM</p>
    <p style="margin:4px 0 0;color:#94a3b8;font-size:13px;">Signal du {today_idx.strftime('%Y-%m-%d')}</p>
  </td></tr>

  <tr><td style="padding:18px 28px 6px;">
    <table role="presentation" width="100%" style="background:#eff6ff;border:1px solid #bfdbfe;border-radius:10px;"><tr><td style="padding:14px 16px;">
      <p style="margin:0;color:#1e40af;font-size:14px;font-weight:700;">2 positions a prendre &mdash; chacune = 50% du capital</p>
      <p style="margin:6px 0 0;color:#475569;font-size:13px;">Capital de reference : <b>{cap_str}</b></p>
    </td></tr></table>
  </td></tr>

  <tr><td style="padding:14px 28px 4px;">
    <p style="margin:0 0 8px;color:#0f172a;font-size:13px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;">Position 1 &mdash; 50% du capital</p>
    <table role="presentation" width="100%" style="border:1px solid #e2e8f0;border-radius:10px;overflow:hidden;">{p1_rows}</table>
  </td></tr>

  <tr><td style="padding:14px 28px 4px;">
    <p style="margin:0 0 8px;color:#0f172a;font-size:13px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;">Position 2 &mdash; 50% du capital (reparti egalement)</p>
    <table role="presentation" width="100%" style="border:1px solid #e2e8f0;border-radius:10px;overflow:hidden;">{p2_rows}</table>
  </td></tr>

  <tr><td style="padding:18px 28px 6px;">
    <table role="presentation" width="100%" style="background:#f8fafc;border-left:4px solid #0f172a;border-radius:0 8px 8px 0;"><tr><td style="padding:14px 16px;">
      <p style="margin:0 0 8px;color:#0f172a;font-size:13px;font-weight:700;">Execution</p>
      <table role="presentation" width="100%" style="font-size:13px;color:#475569;">
        <tr><td style="padding:3px 0;">Dernier signal calcule le</td><td style="padding:3px 0;text-align:right;font-weight:600;color:#0f172a;">{today_idx.strftime('%Y-%m-%d')} (cloture)</td></tr>
        <tr><td style="padding:3px 0;">Ordre a executer le</td><td style="padding:3px 0;text-align:right;font-weight:600;color:#0f172a;">{next_bday} a la cloture (MOC)</td></tr>
        <tr><td style="padding:3px 0;">Prochain signal possible le</td><td style="padding:3px 0;text-align:right;font-weight:600;color:#0f172a;">{next_bday} (jour ouvre suivant)</td></tr>
      </table>
      <p style="margin:8px 0 0;color:#64748b;font-size:12px;">Le systeme verifie chaque jour ouvre. Vous ne recevrez un nouvel email qu'en cas de changement de position.</p>
    </td></tr></table>
  </td></tr>
  {warn_html}

  <tr><td style="padding:18px 28px 26px;border-top:1px solid #e2e8f0;">
    <p style="margin:0;color:#94a3b8;font-size:10px;line-height:1.5;">Information fournie a titre d'aide a la decision &mdash; ne constitue pas un conseil en investissement personnalise. Les performances passees ne prejugent pas des performances futures. Vous restez responsable de vos ordres.</p>
  </td></tr>

</table></td></tr></table></body></html>"""
    return html


# ====================================================================
# MAIN
# ====================================================================
def main():
    cfg    = load_config(CONFIG_PATH)
    panier = cfg["tsmom"]["panier"]

    print("=" * 55)
    print(f"ALERT EMAIL COMBO - {datetime.now().isoformat()}")
    print(f"Devise abonne : {SUBSCRIBER_CURRENCY}  |  Capital ref : {CAPITAL_REF:,}")
    print("=" * 55)

    print("\n-> Fetch EUR/USD...")
    eur_usd, eurusd_warning = fetch_eur_usd()

    max_retries     = 3
    retry_delay_sec = 15 * 60
    last_error_msg  = ""
    expected        = None

    for attempt in range(max_retries):
        print(f"\n[Tentative {attempt+1}/{max_retries}] Verification fraicheur...")
        prices_dict = {a: load_prices(a, cfg) for a in panier}
        is_ok, msg, last_data, expected = is_data_fresh_strict(prices_dict)

        if is_ok:
            print(f"  OK Data fresh - derniere : {last_data.date()}")
            break

        last_error_msg = msg
        print(f"  Data PAS fresh : {msg}")
        if attempt < max_retries - 1:
            print(f"  -> Retry dans {retry_delay_sec//60} min...")
            time.sleep(retry_delay_sec)
    else:
        print("\n!!! ECHEC DEFINITIF - SIGNAL NON ENVOYE !!!")
        alert_admin_data_issue(last_error_msg, max_retries)
        alert_subscribers_no_signal(expected.date() if expected else "aujourd'hui")
        sys.exit(1)

    print("\n-> Construction des signaux...")
    pos_mr, eff_xndx = build_xndx_mr_positions(prices_dict["XNDX"], cfg)
    eff_tsmom        = build_tsmom_positions(prices_dict, cfg)

    today_idx    = eff_xndx.index[-1]
    mr_exp_today = eff_xndx.iloc[-1]
    ts_exp_today = {a: eff_tsmom[a].iloc[-1] for a in panier}

    body = generate_signal_message(
        mr_exp_today, ts_exp_today, today_idx,
        eur_usd, eurusd_warning,
        SUBSCRIBER_CURRENCY, CAPITAL_REF,
    )

    subject = f"[COMBO] Signal du {today_idx.strftime('%Y-%m-%d')}"

    print(f"\n-> Envoi signal ({SUBSCRIBER_CURRENCY}) a {get_recipients()}...")
    if send_email(subject, body, get_recipients(), is_html=True):
        print("OK Signal envoye avec succes")
        sys.exit(0)
    else:
        print("Echec envoi - alerte admin")
        alert_admin_data_issue("Echec envoi email signal", 1)
        sys.exit(2)


if __name__ == "__main__":
    main()
