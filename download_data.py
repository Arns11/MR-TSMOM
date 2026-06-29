"""
scripts/download_data.py
==========================
Telecharge les donnees pour tous les actifs du combo.

ARCHITECTURE RESILIENTE :
- Source primaire : EODHD (eodhistoricaldata.com)
- Source fallback : Yahoo Finance (via yfinance)
- En cas d'echec d'une source pour un actif, bascule auto sur l'autre
- Email d'erreur IMMEDIAT a l'admin si echec total (les 2 sources KO)

USAGE :
    python scripts/download_data.py [--refresh-only-recent]
"""

import sys
import os
import json
import argparse
import smtplib
from email.message import EmailMessage
from pathlib import Path
import urllib.request
import urllib.error
import urllib.parse
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "parameters.json"
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)

# Mapping EODHD -> Yahoo Finance (fallback)
YAHOO_FALLBACK = {
    "XNDX.INDX": "^NDX",        # Nasdaq 100 prix nu (Yahoo ne fait pas le TR)
    "SPXTR.INDX": "^SP500TR",   # SP500 Total Return
    "GLD.US": "GLD",            # ETF GLD
    "TLT.US": "TLT",            # ETF TLT
    "CL.COMM": "CL=F",          # Future continu WTI
}

# Email admin pour alertes techniques
EMAIL_ADMIN = os.environ.get("EMAIL_FROM", "arnaud.tfam@gmail.com")
SENDGRID_API_KEY = os.environ.get("SENDGRID_API_KEY", "")


def load_config():
    with open(CONFIG_PATH, "r") as f:
        return json.load(f)


def send_admin_alert(subject, body):
    """Envoie email d'erreur a l'admin immediatement."""
    if not SENDGRID_API_KEY:
        print(f"\n[ADMIN ALERT - email non envoye, SENDGRID_API_KEY absent]")
        print(f"Subject: {subject}\n{body}")
        return False
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = EMAIL_ADMIN
    msg["To"] = EMAIL_ADMIN
    msg.set_content(body)
    try:
        with smtplib.SMTP("smtp.sendgrid.net", 587) as s:
            s.starttls()
            s.login("apikey", SENDGRID_API_KEY)
            s.send_message(msg)
        print(f"  [ADMIN ALERT envoye: {subject}]")
        return True
    except Exception as e:
        print(f"  [ERREUR envoi admin alert: {e}]")
        return False


def fetch_eodhd(ticker, api_key, from_date, to_date=None):
    """Source primaire."""
    if to_date is None:
        to_date = "2030-12-31"
    params = {
        "api_token": api_key,
        "from": from_date,
        "to": to_date,
        "period": "d",
        "fmt": "json",
    }
    qs = urllib.parse.urlencode(params)
    url = f"https://eodhistoricaldata.com/api/eod/{ticker}?{qs}"
    try:
        with urllib.request.urlopen(url, timeout=60) as resp:
            data = pd.read_json(resp)
    except urllib.error.HTTPError as e:
        return None, f"HTTPError {e.code}"
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"

    if data is None or len(data) == 0 or "date" not in data.columns:
        return None, "Reponse vide ou format invalide"

    data["date"] = pd.to_datetime(data["date"])
    data = data.set_index("date").sort_index()
    col = "adjusted_close" if "adjusted_close" in data.columns else "close"
    s = data[col].astype(float)
    s = s.where(s > 0).ffill()
    return s, None


def fetch_yahoo(yahoo_ticker, from_date, to_date=None):
    """Source fallback - utilise yfinance si dispo, sinon urllib direct."""
    try:
        import yfinance as yf
        df = yf.download(yahoo_ticker, start=from_date, end=to_date,
                         progress=False, auto_adjust=False)
        if df is None or len(df) == 0:
            return None, "Yahoo : reponse vide"
        # Adjusted Close pour ETF/Indices (= TR pour ETF)
        col = "Adj Close" if "Adj Close" in df.columns else "Close"
        if isinstance(df.columns, pd.MultiIndex):
            # yfinance >=0.2 retourne MultiIndex parfois
            s = df[(col, yahoo_ticker)] if (col, yahoo_ticker) in df.columns else df[col].iloc[:, 0]
        else:
            s = df[col]
        s = s.astype(float).dropna()
        s.index = pd.to_datetime(s.index)
        return s, None
    except ImportError:
        return None, "yfinance non installe (pip install yfinance)"
    except Exception as e:
        return None, f"Yahoo {type(e).__name__}: {e}"


def fetch_with_fallback(asset, eodhd_ticker, api_key, from_date):
    """Tente EODHD puis Yahoo en fallback."""
    # Source 1 : EODHD
    s, err = fetch_eodhd(eodhd_ticker, api_key, from_date)
    if s is not None and len(s) > 0:
        return s, "EODHD", None

    # Source 2 : Yahoo fallback
    yahoo_ticker = YAHOO_FALLBACK.get(eodhd_ticker)
    if yahoo_ticker is None:
        return None, None, f"EODHD echec ({err}), pas de mapping Yahoo"

    s_y, err_y = fetch_yahoo(yahoo_ticker, from_date)
    if s_y is not None and len(s_y) > 0:
        return s_y, f"Yahoo ({yahoo_ticker})", f"EODHD echec ({err}) - bascule Yahoo"

    return None, None, f"EODHD echec ({err}) ET Yahoo echec ({err_y})"


def save_csv(prices, asset):
    csv_path = DATA_DIR / f"{asset}.csv"
    df = pd.DataFrame({"date": prices.index, "close": prices.values})
    df.to_csv(csv_path, index=False)
    print(f"  Sauvegarde : {csv_path} ({len(prices)} lignes, "
          f"{prices.index[0].date()} -> {prices.index[-1].date()})")


def load_existing(asset):
    csv_path = DATA_DIR / f"{asset}.csv"
    if not csv_path.exists():
        return None
    return pd.read_csv(csv_path, parse_dates=["date"], index_col="date")["close"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--refresh-only-recent", action="store_true")
    args = parser.parse_args()

    cfg = load_config()
    api_key = cfg["data_source"]["api_key_default"]
    tickers = cfg["data_source"]["tickers"]

    print("=" * 60)
    print(f"DOWNLOAD DATA - Combo MR + TSMOM (resilient EODHD + Yahoo)")
    print(f"Refresh only recent : {args.refresh_only_recent}")
    print("=" * 60)

    errors_critical = []  # Echec total (les 2 sources KO)
    fallback_used = []    # Bascule sur Yahoo

    for asset, ticker in tickers.items():
        print(f"\n[{asset}] ticker EODHD = {ticker}")
        if args.refresh_only_recent:
            existing = load_existing(asset)
            from_date = (existing.index[-1] - pd.Timedelta(days=70)).strftime("%Y-%m-%d") \
                        if existing is not None else "1999-01-01"
        else:
            from_date = "1999-01-01"
            existing = None

        s_new, source, warning = fetch_with_fallback(asset, ticker, api_key, from_date)

        if s_new is None:
            print(f"  ECHEC TOTAL : {warning}")
            errors_critical.append((asset, ticker, warning))
            continue

        if "Yahoo" in (source or ""):
            print(f"  ATTENTION : bascule Yahoo - {warning}")
            fallback_used.append((asset, ticker, source))
        else:
            print(f"  Source : {source}")

        if args.refresh_only_recent and existing is not None:
            s_merged = pd.concat([existing, s_new]).sort_index()
            s_merged = s_merged[~s_merged.index.duplicated(keep="last")]
            save_csv(s_merged, asset)
        else:
            save_csv(s_new, asset)

    # Bilan
    print("\n" + "=" * 60)
    print("BILAN")
    print("=" * 60)

    if errors_critical:
        body = f"ECHEC CRITIQUE telechargement donnees combo MR+TSMOM :\n\n"
        for asset, ticker, w in errors_critical:
            body += f"  - {asset} ({ticker}) : {w}\n"
        body += "\nLe combo ne peut pas envoyer son signal aujourd'hui.\n"
        body += "ACTION IMMEDIATE REQUISE."
        print(body)
        send_admin_alert(
            f"[COMBO MR+TSMOM] ECHEC DONNEES - {len(errors_critical)} actif(s) KO",
            body
        )
        sys.exit(1)

    if fallback_used:
        body = f"AVERTISSEMENT : bascule sur Yahoo pour {len(fallback_used)} actif(s) :\n\n"
        for asset, ticker, source in fallback_used:
            body += f"  - {asset} ({ticker}) -> {source}\n"
        body += "\nEODHD est indisponible pour ces actifs. Verifier au plus vite.\n"
        body += "Le signal du jour est calcule sur donnees Yahoo (qualite degradee possible)."
        print(body)
        send_admin_alert(
            f"[COMBO MR+TSMOM] FALLBACK YAHOO - {len(fallback_used)} actif(s)",
            body
        )

    if not errors_critical and not fallback_used:
        print("OK : tous les actifs telecharges via EODHD primaire")


if __name__ == "__main__":
    main()
