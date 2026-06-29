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

EUR/USD — cascade : Yahoo → EODHD → cache (warning > 10j) → 1.08 (alerte).

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
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
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
# PRIX
# ====================================================================
def load_prices(asset):
    csv_path = DATA_DIR / f"{asset}.csv"
    if not csv_path.exists():
        return None
    return pd.read_csv(csv_path, parse_dates=["date"], index_col="date")["close"]


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
def send_email(subject, body, recipients):
    if not GMAIL_USER or not GMAIL_APP_PASS:
        print("[WARN: GMAIL_USER / GMAIL_APP_PASS absents, email non envoye]")
        print(f"--- EMAIL ---\nTo: {recipients}\nSubject: {subject}\n\n{body}\n---")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = GMAIL_USER
    msg["To"]      = ", ".join(recipients) if isinstance(recipients, list) else recipients
    msg.attach(MIMEText(body, "plain", "utf-8"))

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

    lines = []
    lines.append(f"COMBO XNDX MR + TSMOM - Signal du {today_idx.strftime('%Y-%m-%d')}")
    lines.append("=" * 55)
    lines.append(f"Capital de reference : {fmt_amount(cap_p, cur_pri, cap_s, cur_sec)}")
    lines.append("")

    # --- XNDX MR (50% capital) ---
    lines.append("--- XNDX (50% du capital) ---")
    if np.isnan(mr_exp_today) or mr_exp_today == 0:
        lines.append("  CASH (pas en position)")
    else:
        expo_usd = alloc_mr_usd * mr_exp_today
        expo_eur = expo_usd / eur_usd
        expo_str = fmt_amount(expo_eur, "EUR", expo_usd, "USD") if cur_pri == "EUR" else fmt_amount(expo_usd, "USD", expo_eur, "EUR")
        lines.append("  LONG XNDX")
        lines.append(f"  Exposition : {expo_str}")
    lines.append("")

    # --- TSMOM (50% capital, 10% par actif) ---
    lines.append("--- TSMOM (50% du capital, 10% par actif) ---")
    for asset, exp in ts_exp_today.items():
        if np.isnan(exp) or exp == 0:
            lines.append(f"  {asset:<8}: CASH")
        else:
            expo_usd = alloc_per_usd * abs(exp)
            expo_eur = expo_usd / eur_usd
            direction = "LONG" if exp > 0 else "SHORT"
            expo_str = fmt_amount(expo_eur, "EUR", expo_usd, "USD") if cur_pri == "EUR" else fmt_amount(expo_usd, "USD", expo_eur, "EUR")
            lines.append(f"  {asset:<8}: {direction:<5}  expo {expo_str}")
    lines.append("")

    lines.append("--- EXECUTION ---")
    lines.append("Ordres MOC (Market On Close) le jour suivant.")
    lines.append("Quantites exactes : voir dashboard live abonne.")
    if eurusd_warning:
        lines.append("")
        lines.append(eurusd_warning)
    lines.append("")
    lines.append("---")
    lines.append("Information fournie a titre d'aide a la decision - ne constitue pas un conseil")
    lines.append("en investissement personnalise. Vous restez responsable de vos ordres.")

    return "\n".join(lines)


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
        prices_dict = {a: load_prices(a) for a in panier}
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
    if send_email(subject, body, get_recipients()):
        print("OK Signal envoye avec succes")
        sys.exit(0)
    else:
        print("Echec envoi - alerte admin")
        alert_admin_data_issue("Echec envoi email signal", 1)
        sys.exit(2)


if __name__ == "__main__":
    main()
