import sys, os, json, argparse, smtplib
from email.message import EmailMessage
from pathlib import Path
import urllib.request, urllib.error, urllib.parse
import pandas as pd

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config" / "parameters.json"
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)

YAHOO_FALLBACK = {
    "XNDX.INDX": "^NDX",
    "SPXTR.INDX": "^SP500TR",
    "GLD.US": "GLD",
    "TLT.US": "TLT",
    "CL.COMM": "CL=F",
}

EMAIL_ADMIN = os.environ.get("EMAIL_ADMIN", os.environ.get("GMAIL_USER", "arnaud.tfam@gmail.com"))
GMAIL_USER = os.environ.get("GMAIL_USER", "")
GMAIL_APP_PASS = os.environ.get("GMAIL_APP_PASS", "")


def load_config():
    with open(CONFIG_PATH, "r") as f:
        return json.load(f)


def send_admin_alert(subject, body):
    if not GMAIL_USER or not GMAIL_APP_PASS:
        print(f"\n[ADMIN ALERT non envoye - GMAIL absent]\nSubject: {subject}\n{body}")
        return False
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = GMAIL_USER
    msg["To"] = EMAIL_ADMIN
    msg.set_content(body)
    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as s:
            s.starttls()
            s.login(GMAIL_USER, GMAIL_APP_PASS)
            s.send_message(msg)
        return True
    except Exception as e:
        print(f"  Erreur envoi: {e}")
        return False


def fetch_eodhd(ticker, api_key, from_date):
    params = {"api_token": api_key, "from": from_date, "to": "2030-12-31",
              "period": "d", "fmt": "json"}
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
        return None, "Reponse vide"
    data["date"] = pd.to_datetime(data["date"])
    data = data.set_index("date").sort_index()
    col = "adjusted_close" if "adjusted_close" in data.columns else "close"
    s = data[col].astype(float).where(lambda x: x > 0).ffill()
    return s, None


def fetch_yahoo(yahoo_ticker, from_date):
    try:
        import yfinance as yf
        df = yf.download(yahoo_ticker, start=from_date, progress=False, auto_adjust=False)
        if df is None or len(df) == 0:
            return None, "Yahoo vide"
        col = "Adj Close" if "Adj Close" in df.columns else "Close"
        if isinstance(df.columns, pd.MultiIndex):
            s = df[col].iloc[:, 0]
        else:
            s = df[col]
        s = s.astype(float).dropna()
        s.index = pd.to_datetime(s.index)
        return s, None
    except ImportError:
        return None, "yfinance non installe"
    except Exception as e:
        return None, f"Yahoo {type(e).__name__}: {e}"


def fetch_with_fallback(asset, eodhd_ticker, api_key, from_date):
    s, err = fetch_eodhd(eodhd_ticker, api_key, from_date)
    if s is not None and len(s) > 0:
        return s, "EODHD", None
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
    print("DOWNLOAD - Combo MR+TSMOM (EODHD primaire + Yahoo fallback)")
    print("=" * 60)
    errors_critical = []
    fallback_used = []
    for asset, ticker in tickers.items():
        print(f"\n[{asset}] ticker = {ticker}")
        if args.refresh_only_recent:
            existing = load_existing(asset)
            from_date = (existing.index[-1] - pd.Timedelta(days=70)).strftime("%Y-%m-%d") if existing is not None else "1999-01-01"
        else:
            from_date = "1999-01-01"
            existing = None
        s_new, source, warning = fetch_with_fallback(asset, ticker, api_key, from_date)
        if s_new is None:
            print(f"  ECHEC TOTAL : {warning}")
            errors_critical.append((asset, ticker, warning))
            continue
        if "Yahoo" in (source or ""):
            print(f"  BASCULE YAHOO : {warning}")
            fallback_used.append((asset, ticker, source))
        else:
            print(f"  Source : {source}")
        if args.refresh_only_recent and existing is not None:
            s_merged = pd.concat([existing, s_new]).sort_index()
            s_merged = s_merged[~s_merged.index.duplicated(keep="last")]
            save_csv(s_merged, asset)
        else:
            save_csv(s_new, asset)
    print("\n" + "=" * 60)
    if errors_critical:
        body = "ECHEC CRITIQUE :\n" + "\n".join(f"  {a} ({t}): {w}" for a, t, w in errors_critical)
        print(body)
        send_admin_alert(f"[COMBO] ECHEC DONNEES {len(errors_critical)} actif(s)", body)
        sys.exit(1)
    if fallback_used:
        body = "FALLBACK YAHOO :\n" + "\n".join(f"  {a} ({t}) -> {s}" for a, t, s in fallback_used)
        print(body)
        send_admin_alert(f"[COMBO] FALLBACK YAHOO {len(fallback_used)} actif(s)", body)
    if not errors_critical and not fallback_used:
        print("OK : tous via EODHD")


if __name__ == "__main__":
    main()