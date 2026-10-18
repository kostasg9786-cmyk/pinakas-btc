#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Πίνακας BTC — συλλέκτης δεδομένων.

Τρέχει σε διακομιστή (GitHub Actions), όχι σε browser. Τραβάει όλες τις πηγές,
βαθμολογεί με τους κανόνες του κεφαλαίου 9 του εγχειριδίου «Η Βαλβίδα», και γράφει:
  data.json    — η τρέχουσα ανάγνωση, σκορ, σημαίες, κελί, ημερολόγιο πηγών
  history.csv  — μία γραμμή ανά ημέρα, για εκατοστημόρια και έλεγχο ανά κελί
  alert.json   — τι άλλαξε από την προηγούμενη ανάγνωση

Κάθε πηγή είναι ανεξάρτητη: αν μία αποτύχει, η ανάγνωση μένει κενή (μετρά 0) και
το ημερολόγιο πηγών λέει ποια. Τίποτα δεν σταματά το σύνολο.
"""
import csv, datetime as dt, json, os, re
import requests
from zoneinfo import ZoneInfo

LOCAL_TZ = "Europe/Athens"   # η ώρα του χρήστη στα μηνύματα· αν αλλάξει χώρα, αλλάζει μόνο εδώ
PAGE_URL = "https://kostasg9786-cmyk.github.io/pinakas-btc/"

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data.json")
HIST = os.path.join(ROOT, "history.csv")
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
LOG = []

def log(m, ok=True):
    LOG.append(("✓ " if ok else "✗ ") + m)

def get(url, label, text=False, timeout=25):
    try:
        r = requests.get(url, headers=UA, timeout=timeout)
        r.raise_for_status()
        log(label)
        return r.text if text else r.json()
    except Exception as e:
        log(f"{label} — {type(e).__name__} {str(e)[:70]}", False)
        return None

def band(lo, hi, lo_strict=False):
    def f(x):
        if x >= hi: return 1
        if (x < lo) if lo_strict else (x <= lo): return -1
        return 0
    return f
def band_inv(lo, hi):
    def f(x):
        if x <= lo: return 1
        if x >= hi: return -1
        return 0
    return f

READINGS = [
    ("c1", "Γ", "Γ1 Funding ετησιοποιημένο", "%", "+1 ≥25 · −1 ≤0", band(0, 25), 1),
    ("c2", "Γ", "Γ2 OI σε BTC, 7 ημέρες", "%", "+1 ≥+8 με τιμή πάνω · −1 ≥+8 με τιμή κάτω", None, 1),
    ("c2p", "Γ", "Γ2β Τιμή, 7 ημέρες", "%", "μόνο το πρόσημο", None, 1),
    ("c3", "Γ", "Γ3 Skew 30 ημερών (call−put)", "μον.", "+1 ≥+3 · −1 ≤−5", band(-5, 3), 1),
    ("c4", "Γ", "Γ4 Λόγος OI put/call", "", "+1 ≤0,5 · −1 ≥0,9", band_inv(0.5, 0.9), 2),
    ("c5", "Γ", "Γ5 Basis τριμηνιαίου, ετησιοποιημένο", "%", "+1 ≥12 · −1 <3", band(3, 12, True), 1),
    ("f1", "Σ", "OI σε BTC, 2 ημέρες", "%", "Ξέπλυμα αν ≤ −15", None, 1),
    ("f2", "Σ", "IV 7 ημερών", "%", "Γεγονός αν IV7 ≥ IV30 + 5", None, 1),
    ("f3", "Σ", "IV 30 ημερών", "%", "", None, 1),
    ("d2", "Δ", "Δ2 Premium ΗΠΑ έναντι offshore", "%", "+1 >0 · −1 <0", lambda x: 1 if x > 0 else (-1 if x < 0 else 0), 3),
    ("d3", "Δ", "Δ3 Προσφορά stablecoins, 30 ημέρες", "%", "+1 ≥+2 · −1 ≤−2", band(-2, 2), 2),
    ("a1", "Α", "Α1 Πραγματικό επιτόκιο 10ετίας, 4 εβδ.", "μ.β.", "+1 ≤−10 · −1 ≥+10", band_inv(-10, 10), 1),
    ("a2", "Α", "Α2 Καθαρή ρευστότητα, 4 εβδ.", "%", "+1 ≥+1 · −1 ≤−1", band(-1, 1), 2),
    ("a4", "Α", "Α4 Δείκτης δολαρίου, 4 εβδ.", "%", "+1 ≤−1,5 · −1 ≥+1,5", band_inv(-1.5, 1.5), 2),
    ("a5", "Α", "Α5 Spread υψηλού κινδύνου, 4 εβδ.", "μ.β.", "+1 ≤−25 · −1 ≥+25", band_inv(-25, 25), 1),
    ("a6", "Α", "Α6 Απόδοση 2ετούς, 4 εβδ.", "μ.β.", "+1 ≤−10 · −1 ≥+10", band_inv(-10, 10), 1),
    ("a7", "Α", "Α7 Πληθωριστικό καθεστώς", "", "+1 πληθ. πάνω & πραγματικό δεν ανεβαίνει · −1 πληθ. πάνω & πραγματικό ανεβαίνει", lambda x: int(x), 0),
    ("b1", "Β", "Β1 Μικρές έναντι μεγάλων, 4 εβδ.", "%", "+1 ≥+2 · −1 ≤−2", band(-2, 2), 2),
    ("b2", "Β", "Β2 Υψηλό beta έναντι χαμηλής μετ., 4 εβδ.", "%", "+1 ≥+2 · −1 ≤−2", band(-2, 2), 2),
    ("b3", "Β", "Β3 Δείκτης μεταβλητότητας", "επίπ.", "+1 <18 με contango · −1 >25 ή backwardation", None, 2),
    ("b3t", "Β", "Β3β VIX 3μηνών μείον VIX", "μον.", "θετικό = contango", None, 2),
    ("b4", "Β", "Β4 Νομίσματα αναδυόμενων έναντι δολαρίου, 4 εβδ.", "%", "+1 ≥+1,5 · −1 ≤−1,5", band(-1.5, 1.5), 2),
    ("b5", "Β", "Β5 Κυριαρχία BTC, 4 εβδ.", "μον.", "+1 ≤−2 · −1 ≥+2 · Όψιμη φάση ≤−5", band_inv(-2, 2), 2),
    ("b5l", "Β", "Β5β Κυριαρχία BTC, επίπεδο", "%", "", None, 2),
    ("t1", "Τ", "Τ1 Τιμή BTC", "$", "", None, 0),
    ("t2", "Τ", "Τ2 Απόσταση από τον μέσο όρο 50 ημερών", "%", "", None, 1),
    ("t3", "Τ", "Τ3 Απόσταση από τον μέσο όρο 200 ημερών", "%", "", None, 1),
    ("t4", "Τ", "Τ4 Απόσταση από τον μέσο όρο 200 εβδομάδων", "%", "", None, 1),
    ("t5", "Τ", "Τ5 Απόσταση από το υψηλό 12 μηνών", "%", "", None, 1),
    ("t6", "Τ", "Τ6 MVRV (αγοραία προς πραγματοποιημένη αξία)", "", "", None, 2),
    ("t7", "Τ", "Τ7 Μήνες από το τελευταίο halving", "μήνες", "", None, 0),
    ("e1", "Ε", "Ε1 Συσχέτιση 30 ημερών BTC–τεχνολογικού δείκτη", "", "", None, 2),
    ("e2", "Ε", "Ε2 Εταιρεία-ταμείο μείον BTC, 1 ημέρα", "μον.%", "", None, 2),
    ("e3", "Ε", "Ε3 Ανταλλακτήριο μείον BTC, 1 ημέρα", "μον.%", "", None, 2),
    ("e4", "Ε", "Ε4 Ασφάλιστρο μεταβλητότητας (IV30 − πραγματοποιημένη)", "μον.", "", None, 1),
    ("e5", "Ε", "Ε5 Πραγματοποιημένη μεταβλητότητα 30 ημερών", "%", "", None, 1),
]
IDS = [r[0] for r in READINGS]
V = {i: None for i in IDS}
SRC = {i: "" for i in IDS}
EXTRA = {"oi_level": None, "price": None, "events": []}

def setv(i, val, src):
    try:
        if val is None or val != val or abs(val) == float("inf"):
            return
        V[i] = float(val); SRC[i] = src
    except Exception:
        pass

DER = "https://www.deribit.com/api/v2/public"

def derivs():
    c = get("https://api.exchange.coinbase.com/products/BTC-USD/candles?granularity=86400", "τιμή 7ημ (spot ΗΠΑ)")
    try:
        c = sorted(c, key=lambda x: x[0])
        CLOSES[:] = [float(x[4]) for x in c]
        p0, p1 = float(c[-8][4]), float(c[-1][4])
        if p0 > 0: setv("c2p", (p1 / p0 - 1) * 100, "spot ΗΠΑ"); EXTRA["price"] = p1
        import math
        cl = [float(x[4]) for x in c[-31:]]
        rets = [math.log(cl[i] / cl[i-1]) for i in range(1, len(cl)) if cl[i-1] > 0]
        if len(rets) >= 20:
            mu = sum(rets) / len(rets)
            sd = (sum((r - mu) ** 2 for r in rets) / (len(rets) - 1)) ** 0.5
            setv("e5", sd * (365 ** 0.5) * 100, "spot ΗΠΑ")
    except Exception:
        pass
    d = get("https://api.coingecko.com/api/v3/derivatives", "funding/OI (συγκεντρωτική)")
    if d:
        num = den = 0.0
        for x in d:
            try:
                if x.get("index_id") != "BTC" or x.get("contract_type") != "perpetual": continue
                o = float(x.get("open_interest") or 0); fr = float(x.get("funding_rate") or 0)
                if o > 0: num += fr * o; den += o
            except Exception:
                continue
        if den > 0:
            setv("c1", (num / den) * 3 * 365, "συγκεντρωτική")
            if EXTRA["price"]: EXTRA["oi_level"] = den / EXTRA["price"]
    if V["c1"] is None or EXTRA["oi_level"] is None:
        t = get(f"{DER}/ticker?instrument_name=BTC-PERPETUAL", "funding/OI (αγορά options)")
        try:
            r = t["result"]
            if V["c1"] is None and r.get("funding_8h") is not None: setv("c1", float(r["funding_8h"]) * 3 * 365 * 100, "αγορά options")
            if EXTRA["oi_level"] is None and r.get("open_interest") and r.get("index_price"):
                EXTRA["oi_level"] = float(r["open_interest"]) / float(r["index_price"])
            if EXTRA["price"] is None and r.get("index_price"): EXTRA["price"] = float(r["index_price"])
        except Exception:
            pass

MON = {m: i for i, m in enumerate(["JAN","FEB","MAR","APR","MAY","JUN","JUL","AUG","SEP","OCT","NOV","DEC"], 1)}

def options():
    j = get(f"{DER}/get_book_summary_by_currency?currency=BTC&kind=option", "options")
    if not j or not j.get("result"): return
    now = dt.datetime.now(dt.timezone.utc)
    rows, U, pOI, cOI = [], 0.0, 0.0, 0.0
    for o in j["result"]:
        m = re.match(r"BTC-(\d{1,2})([A-Z]{3})(\d{2})-(\d+)-([CP])$", o.get("instrument_name", ""))
        if not m: continue
        d, mon, y, k, t = m.groups()
        try:
            exp = dt.datetime(2000 + int(y), MON[mon], int(d), 8, tzinfo=dt.timezone.utc)
        except Exception:
            continue
        days = (exp - now).total_seconds() / 86400
        if days < 0.5: continue
        if o.get("underlying_price"): U = float(o["underlying_price"])
        oi = float(o.get("open_interest") or 0)
        if t == "P": pOI += oi
        else: cOI += oi
        rows.append((days, float(k), t, o.get("mark_iv")))
    if cOI > 0: setv("c4", pOI / cOI, "options")
    if not rows or U <= 0: return
    def nearest(target):
        return min({r[0] for r in rows}, key=lambda x: abs(x - target))
    def iv_at(dexp, strike, t):
        c = [r for r in rows if abs(r[0] - dexp) < 0.01 and r[2] == t and r[3]]
        return min(c, key=lambda r: abs(r[1] - strike))[3] if c else None
    e30, e7 = nearest(30), nearest(7)
    ivc, ivp, atm30, atm7 = iv_at(e30, U * 1.10, "C"), iv_at(e30, U * 0.90, "P"), iv_at(e30, U, "C"), iv_at(e7, U, "C")
    if ivc and ivp: setv("c3", ivc - ivp, "options")
    if atm30: setv("f3", atm30, "options")
    if atm7: setv("f2", atm7, "options")

def basis():
    j = get(f"{DER}/get_instruments?currency=BTC&kind=future&expired=false", "futures με λήξη")
    if not j or not j.get("result"): return
    # Τριμηνιαίο = λήξη Μαρ/Ιουν/Σεπ/Δεκ. Παίρνουμε το πλησιέστερο με τουλάχιστον 30 μέρες ζωής
    now_ms = dt.datetime.now(dt.timezone.utc).timestamp() * 1000
    fut = [f for f in j["result"] if f.get("settlement_period") != "perpetual"
           and dt.datetime.fromtimestamp(f["expiration_timestamp"] / 1000, dt.timezone.utc).month in (3, 6, 9, 12)
           and f["expiration_timestamp"] - now_ms >= 30 * 86400000]
    if not fut: return
    far = min(fut, key=lambda f: f["expiration_timestamp"])
    t = get(f"{DER}/ticker?instrument_name={far['instrument_name']}", f"basis {far['instrument_name']}")
    try:
        r = t["result"]; mk = r.get("mark_price") or r.get("last_price"); ix = r["index_price"]
        days = (far["expiration_timestamp"] / 1000 - dt.datetime.now(dt.timezone.utc).timestamp()) / 86400
        if mk and ix and days > 1: setv("c5", (mk / ix - 1) * 100 * 365 / days, "futures")
    except Exception:
        pass

def demand():
    us = get("https://api.exchange.coinbase.com/products/BTC-USD/ticker", "spot ΗΠΑ")
    off = get("https://api-pub.bitfinex.com/v2/ticker/tBTCUSD", "spot offshore (πηγή 1)")
    try: off = {"price": off[6]}
    except Exception: off = None
    if not off:
        o2 = get("https://www.okx.com/api/v5/market/ticker?instId=BTC-USDT", "spot offshore (πηγή 2)")
        try: off = {"price": o2["data"][0]["last"]}
        except Exception: off = None
    try:
        setv("d2", (float(us["price"]) / float(off["price"]) - 1) * 100, "spot")
    except Exception:
        pass
    tot0 = tot1 = 0.0
    for coin, lab in (("tether", "stablecoin 1"), ("usd-coin", "stablecoin 2")):
        j = get(f"https://api.coingecko.com/api/v3/coins/{coin}/market_chart?vs_currency=usd&days=31&interval=daily", lab)
        try:
            mc = j["market_caps"]; tot0 += mc[0][1]; tot1 += mc[-1][1]
        except Exception:
            pass
    if tot0 > 0: setv("d3", (tot1 / tot0 - 1) * 100, "stablecoins")
    g = get("https://api.coingecko.com/api/v3/global", "κυριαρχία BTC")
    try: setv("b5l", float(g["data"]["market_cap_percentage"]["btc"]), "κυριαρχία")
    except Exception: pass

FRED = {"real": "DFII10", "bs": "WALCL", "tga": "WTREGEN", "rrp": "RRPONTSYD",
        "dxy": "DTWEXBGS", "hy": "BAMLH0A0HYM2", "y2": "DGS2", "be": "T10YIE", "emfx": "DTWEXEMEGS"}

def fred_series(code, label):
    key = os.environ.get("FRED_API_KEY", "").strip()
    out = []
    if key:
        j = get(f"https://api.stlouisfed.org/fred/series/observations?series_id={code}&api_key={key}"
                f"&file_type=json&sort_order=asc&observation_start={(dt.date.today()-dt.timedelta(days=120)).isoformat()}", f"μακρο {label}")
        for o in (j or {}).get("observations", []):
            try: out.append((dt.date.fromisoformat(o["date"]), float(o["value"])))
            except Exception: pass
    if not out:
        t = get(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={code}&cosd={(dt.date.today()-dt.timedelta(days=120)).isoformat()}", f"μακρο {label} (csv)", text=True, timeout=60)
        for line in (t or "").splitlines()[1:]:
            c = line.split(",")
            try: out.append((dt.date.fromisoformat(c[0]), float(c[1])))
            except Exception: pass
    return out

def value_at(series, days_back):
    if not series: return None
    target = series[-1][0] - dt.timedelta(days=days_back)
    cand = [v for d, v in series if d <= target]
    return cand[-1] if cand else None

def macro():
    S = {k: fred_series(code, k) for k, code in FRED.items()}
    def chg(k, mult=1.0):
        s = S[k]; prev = value_at(s, 28)
        return None if (not s or prev is None) else (s[-1][1] - prev) * mult
    def pct(k):
        s = S[k]; prev = value_at(s, 28)
        return None if (not s or not prev) else (s[-1][1] / prev - 1) * 100
    setv("a1", chg("real", 100), "μακρο")
    if S["bs"] and S["tga"] and S["rrp"]:
        # WALCL και WTREGEN σε εκατομμύρια USD, RRPONTSYD σε δισεκατομμύρια: μετατροπή σε εκατομμύρια
        n0 = S["bs"][-1][1] - S["tga"][-1][1] - S["rrp"][-1][1] * 1000
        p = [value_at(S[k], 28) for k in ("bs", "tga", "rrp")]
        if None not in p and (p[0] - p[1] - p[2] * 1000) != 0:
            setv("a2", (n0 / (p[0] - p[1] - p[2] * 1000) - 1) * 100, "μακρο")
    setv("a4", pct("dxy"), "μακρο")
    setv("a5", chg("hy", 100), "μακρο")
    setv("a6", chg("y2", 100), "μακρο")
    em = pct("emfx")
    if em is not None: setv("b4", -em, "μακρο")
    be, rl = chg("be", 100), chg("real", 100)
    if be is not None and rl is not None:
        setv("a7", (-1 if rl >= 10 else 1) if be > 5 else 0, "μακρο")

def equities():
    try:
        import yfinance as yf
        px = yf.download(["IWM", "SPY", "SPHB", "SPLV", "^VIX", "^VIX3M", "QQQ", "BTC-USD", "MSTR", "COIN"],
                         period="4mo", interval="1d", progress=False, auto_adjust=True, group_by="column")["Close"].dropna(how="all")
        log("μετοχές και proxy (10 σειρές)")
    except Exception as e:
        log(f"μετοχές — {type(e).__name__} {str(e)[:60]}", False); return
    def rel(a, b):
        s = (px[a] / px[b]).dropna()
        return (s.iloc[-1] / s.iloc[-21] - 1) * 100 if len(s) >= 21 else None
    try: setv("b1", rel("IWM", "SPY"), "μετοχές")
    except Exception: pass
    try: setv("b2", rel("SPHB", "SPLV"), "μετοχές")
    except Exception: pass
    try:
        v, v3 = px["^VIX"].dropna().iloc[-1], px["^VIX3M"].dropna().iloc[-1]
        setv("b3", v, "μετοχές"); setv("b3t", v3 - v, "μετοχές")
        for k, sym in (("vix_yf", "^VIX"), ("vix3_yf", "^VIX3M")):
            EXTRA[k] = {d.date().isoformat(): float(x) for d, x in px[sym].dropna().tail(10).items()}
    except Exception: pass

def vix_check():
    """Β3: εφεδρεία και διασταύρωση από τις επίσημες σειρές της Cboe στη FRED
    (VIXCLS, VXVCLS — κλείσιμο ημέρας, συνήθως με καθυστέρηση μίας μέρας).
    Αν η κύρια πηγή έπεσε, η FRED γίνεται πηγή. Αν δουλεύουν και οι δύο, συγκρίνονται
    στην πιο πρόσφατη κοινή ημέρα και η ασυμφωνία δηλώνεται, δεν διορθώνεται σιωπηλά."""
    fv = {d.isoformat(): x for d, x in fred_series("VIXCLS", "VIX")}
    f3 = {d.isoformat(): x for d, x in fred_series("VXVCLS", "VIX 3 μηνών")}
    common = sorted(set(fv) & set(f3))
    if not common:
        log("VIX στη FRED — καμία τιμή", False); return
    last = common[-1]; tag = f"{last[8:10]}/{last[5:7]}"
    if V["b3"] is None:
        setv("b3", fv[last], f"FRED, κλείσιμο {tag}"); setv("b3t", f3[last] - fv[last], f"FRED, κλείσιμο {tag}")
        log(f"Β3 από FRED (κλείσιμο {tag}), επειδή η κύρια πηγή δεν απάντησε"); return
    yv, y3 = EXTRA.get("vix_yf") or {}, EXTRA.get("vix3_yf") or {}
    both = sorted(set(yv) & set(y3) & set(common))
    if not both:
        log("VIX — καμία κοινή ημέρα για διασταύρωση με FRED", False); return
    d = both[-1]; dtag = f"{d[8:10]}/{d[5:7]}"
    gap, gap3 = abs(yv[d] - fv[d]), abs(y3[d] - f3[d])
    if gap > 0.25 or gap3 > 0.25:
        EXTRA["vix_mismatch"] = {"date": d, "yahoo": [yv[d], y3[d]], "fred": [fv[d], f3[d]]}
        SRC["b3"] = SRC["b3t"] = f"μετοχές · ΑΣΥΜΦΩΝΙΑ με FRED {dtag}"
        log(f"VIX {dtag}: Yahoo {yv[d]:.2f}/{y3[d]:.2f} έναντι FRED {fv[d]:.2f}/{f3[d]:.2f}", False)
    else:
        SRC["b3"] = SRC["b3t"] = f"μετοχές · ✓ FRED {dtag}"
        log(f"VIX διασταυρώθηκε με FRED ({dtag}, διαφορά {gap:.2f})")
    try:
        r = px[["BTC-USD", "QQQ"]].dropna().pct_change().dropna().tail(30)
        if len(r) >= 20: setv("e1", float(r["BTC-USD"].corr(r["QQQ"])), "μετοχές")
    except Exception: pass
    def day(sym):
        s2 = px[sym].dropna()
        return (s2.iloc[-1] / s2.iloc[-2] - 1) * 100 if len(s2) >= 2 else None
    try:
        b = day("BTC-USD")
        if b is not None:
            m, cn = day("MSTR"), day("COIN")
            if m is not None: setv("e2", m - b, "μετοχές")
            if cn is not None: setv("e3", cn - b, "μετοχές")
    except Exception: pass

CLOSES = []
HALVING = dt.date(2024, 4, 20)

def trend():
    """Τάση και κύκλος. Πηγή: ημερήσια τιμή και MVRV από την ανοιχτή βάση on-chain δεδομένων
    (≈4 χρόνια, για τον μέσο όρο 200 εβδομάδων). Εφεδρεία για τους μέσους όρους 50/200 ημερών:
    τα 300 ημερήσια κλεισίματα του spot ΗΠΑ που τραβάει ήδη ο συλλέκτης."""
    start = (dt.date.today() - dt.timedelta(days=1500)).isoformat()
    j = get("https://community-api.coinmetrics.io/v4/timeseries/asset-metrics?assets=btc"
            f"&metrics=PriceUSD,CapMVRVCur&frequency=1d&page_size=2000&start_time={start}", "τάση και αποτίμηση (on-chain)")
    px, mv = [], None
    for r in sorted((j or {}).get("data", []), key=lambda r: r.get("time", "")):
        try: px.append(float(r["PriceUSD"]))
        except Exception: continue
        try: mv = float(r["CapMVRVCur"])
        except Exception: pass
    src = "on-chain"
    if len(px) < 200 and len(CLOSES) >= 200:
        px, src = list(CLOSES), "spot ΗΠΑ"
    p = EXTRA.get("price") or (px[-1] if px else None)
    if p: setv("t1", p, "spot ΗΠΑ" if EXTRA.get("price") else src)
    def dist(n):
        return None if (not p or len(px) < n) else (p / (sum(px[-n:]) / n) - 1) * 100
    setv("t2", dist(50), src)
    setv("t3", dist(200), src)
    if len(px) >= 1400: setv("t4", dist(1400), src)
    if p and len(px) >= 365: setv("t5", (p / max(px[-365:]) - 1) * 100, src)
    if mv is not None: setv("t6", mv, "on-chain")
    setv("t7", (dt.date.today() - HALVING).days / 30.44, "ημερολόγιο")

def tnotes(V):
    """Μία φράση ερμηνείας για κάθε ανάγνωση του Τ — για τη σελίδα."""
    n = {}
    t2, t3, t4, t5, t6, t7 = (V.get(k) for k in ("t2", "t3", "t4", "t5", "t6", "t7"))
    if t2 is not None: n["t2"] = "πάνω από τον μέσο όρο: βραχυπρόθεσμη τάση ανοδική" if t2 > 0 else "κάτω από τον μέσο όρο: βραχυπρόθεσμη τάση πτωτική"
    if t3 is not None: n["t3"] = "πάνω: η κύρια τάση είναι ανοδική" if t3 > 0 else "κάτω: η κύρια τάση είναι πτωτική"
    if t4 is not None: n["t4"] = ("κάτω από τον μέσο όρο 200 εβδομάδων: ιστορικά σπάνιο, συνέπεσε με πάτους κύκλων" if t4 < 0
                                  else "πάνω από τη γραμμή που ιστορικά λειτούργησε ως πάτος κύκλου")
    if t5 is not None: n["t5"] = "πόσο απέχει η τιμή από το υψηλότερο κλείσιμο των τελευταίων 12 μηνών"
    if t6 is not None: n["t6"] = mvrv_zone(t6)
    if t7 is not None: n["t7"] = halving_phase(t7)
    return n

def mvrv_zone(v):
    if v < 1: return "κάτω από 1: η αγορά συνολικά κάτω από το κόστος κτήσης — ιστορικά ζώνη πάτων"
    if v < 2.4: return "μεσαία ζώνη: ούτε φθηνό ούτε ακριβό με ιστορικούς όρους"
    if v < 3.5: return "ακριβό: πολλοί κάτοχοι σε μεγάλο κέρδος, αυξημένη πίεση πώλησης"
    return "πάνω από 3,5: ιστορικά ζώνη κορυφών κύκλου"

def halving_phase(m):
    if m < 12: return "πρώιμη φάση μετά το halving"
    if m < 18: return "το ιστορικό παράθυρο κορυφών (12–18 μήνες μετά)"
    if m < 25: return "μετά το ιστορικό παράθυρο κορυφών"
    if m < 32: return "το ιστορικό παράθυρο πάτων (25–30 μήνες μετά)"
    return "πριν από το επόμενο halving· ιστορικά φάση ανάκαμψης"

def calendar():
    """Γεγονότα υψηλής επίδρασης, επόμενες 48 ώρες. Η πηγή δημοσιεύει μόνο την τρέχουσα εβδομάδα·
    το αρχείο της επόμενης δίνει 404 σε κάθε έλεγχο και εφεδρικός διακομιστής δεν υπάρχει. Γι' αυτό
    καταγράφεται μέχρι πότε καλύπτει η πηγή, ώστε η σελίδα να λέει «άγνωστο» και όχι «κανένα γεγονός»
    για το διάστημα μετά."""
    now = dt.datetime.now(dt.timezone.utc); out = []; last = None
    j = get("https://nfs.faireconomy.media/ff_calendar_thisweek.json", "ημερολόγιο γεγονότων")
    EXTRA["events_ok"] = j is not None
    for e in (j or []):
        try:
            t = dt.datetime.fromisoformat(str(e["date"]).replace("Z", "+00:00")).astimezone(dt.timezone.utc)
        except Exception:
            continue
        if last is None or t > last: last = t
        try:
            if e.get("impact") != "High" or e.get("country") not in ("USD", "EUR"): continue
            dh = (t - now).total_seconds() / 3600
            if -3 <= dh <= 48:
                out.append({"ts": t.timestamp(), "t": t.strftime("%a %d/%m %H:%M UTC"), "title": e.get("title"),
                            "ccy": e.get("country"), "forecast": e.get("forecast") or "", "previous": e.get("previous") or ""})
        except Exception:
            continue
    EXTRA["events_until"] = last.timestamp() if last else None
    EXTRA["events"] = sorted(out, key=lambda x: x["ts"])[:14]

def read_history():
    if not os.path.exists(HIST): return []
    with open(HIST, encoding="utf-8") as f:
        return list(csv.DictReader(f))

def from_history(rows, col, days_back):
    today = dt.date.today(); best = None
    for r in rows:
        try:
            d = dt.date.fromisoformat(r["date"][:10]); val = float(r[col])
        except Exception:
            continue
        age = (today - d).days
        if age >= days_back and (best is None or age < best[0]): best = (age, val)
    return best[1] if best and best[0] <= days_back + 5 else None

MULT = {"Επέκταση": {"Συνωστισμός short": 1.0, "Ισορροπία": 1.0, "Συνωστισμός long": 0.5},
        "Ουδέτερο": {"Συνωστισμός short": 0.5, "Ισορροπία": 0.5, "Συνωστισμός long": 0.25},
        "Συρρίκνωση": {"Συνωστισμός short": 0.25, "Ισορροπία": 0.5, "Συνωστισμός long": 1.0}}
STANCE = {"Επέκταση": {"Συνωστισμός short": "Long με πλήρες ρίσκο· squeeze και μακρο συμφωνούν.",
                       "Ισορροπία": "Long· η υποχώρηση είναι ευκαιρία.",
                       "Συνωστισμός long": "Long μόνο μετά από Ξέπλυμα· χωρίς κυνήγι."},
          "Ουδέτερο": {"Συνωστισμός short": "Long τακτικά, με στόχο κοντά.",
                       "Ισορροπία": "Μόνο η δική σου δομή αποφασίζει.",
                       "Συνωστισμός long": "Χωρίς νέα long· short μόνο ενδοημερήσια σε εξάντληση."},
          "Συρρίκνωση": {"Συνωστισμός short": "Χωρίς short· περιμένεις να εξαντληθεί η κάλυψη.",
                         "Ισορροπία": "Short στα ράλι.",
                         "Συνωστισμός long": "Short στα ράλι με πλήρες ρίσκο· ο καταρράκτης είναι σύμμαχος."}}

def flip_point(fn, v):
    """Πόσο πρέπει να κινηθεί μια ανάγνωση για να αλλάξει το σκορ της."""
    if fn is None or v is None: return None
    try: base = fn(v)
    except Exception: return None
    step = max(abs(v) * 0.002, 0.01)
    best = None
    for d in (1, -1):
        x = v
        for _ in range(3000):
            x += step * d
            try: sc = fn(x)
            except Exception: break
            if sc != base:
                cand = (x, sc, abs(x - v))
                if best is None or cand[2] < best[2]: best = cand
                break
    return best

def flips(V, S, readings_meta):
    """Γράφει μόνο του τη γραμμή «τι το ανατρέπει», με αριθμό."""
    meta = {m[0]: m for m in readings_meta}
    def near(ids, need_dir):
        out = []
        for i in ids:
            if i == "a7": continue
            m = meta[i]; fp = flip_point(m[5], V[i])
            if not fp: continue
            new, cur = fp[1], S["sc"][i]
            if (new - cur) * need_dir <= 0: continue
            word = "ανέβει πάνω από" if fp[0] > V[i] else "πέσει κάτω από"
            name = re.sub(r"^\S+\s", "", m[2]).lower()
            out.append((fp[2] / max(abs(V[i]), 1), f"{name} {word} {gnum(fp[0], m[6])} {m[3]} (τώρα {gnum(V[i], m[6])})".replace("  ", " ")))
        out.sort(key=lambda x: x[0])
        return [t for _, t in out[:2]]

    res = {}
    aids = ("a1", "a2", "a4", "a5", "a6", "a7")
    up, dn = 3 - S["sA"], S["sA"] + 3
    if up <= dn: need, target, d = up, "Επέκταση", 1
    else: need, target, d = dn, "Συρρίκνωση", -1
    cands = near(aids, d)
    if need <= 0:
        back = near(aids, -d)
        res["A"] = (f"Το Α είναι ήδη {S['stA']}· φεύγει από εκεί αν {back[0]}." if back else f"Το Α είναι ήδη {S['stA']}.")
    elif need == 1 and cands:
        res["A"] = f"Το Α γίνεται {target} αν {cands[0]}."
    elif cands:
        res["A"] = f"Το Α θέλει {need} βαθμούς για {target}· πιο κοντά: " + " · ".join(cands) + "."
    else:
        res["A"] = f"Το Α θέλει {need} βαθμούς για {target}."
    bids = ("b1", "b2", "b3", "b4", "b5")
    def tail(need, cands):
        if not cands: return "."
        if need == 1: return f"· αρκεί αν {cands[0]}."
        return f"· θέλει {need} βαθμούς, πιο κοντά: " + " · ".join(cands) + "."
    if S["eff"] != S["stA"]:
        res["B"] = (f"Η όρεξη για ρίσκο (Β {S['sB']:+d}) ήδη ουδετεροποιεί το Α ({S['stA']}). "
                    f"Αν το Β επιστρέψει μέσα στο ±1, το Α ξαναγίνεται ενεργό.")
    elif S["stA"] == "Συρρίκνωση":
        nB = 2 - S["sB"]
        res["B"] = f"Το ενεργό καθεστώς γίνεται Ουδέτερο αν η όρεξη για ρίσκο (Β, τώρα {S['sB']:+d}) φτάσει +2" + tail(nB, near(bids, 1))
    elif S["stA"] == "Επέκταση":
        nB = S["sB"] + 2
        res["B"] = f"Το ενεργό καθεστώς γίνεται Ουδέτερο αν η όρεξη για ρίσκο (Β, τώρα {S['sB']:+d}) πέσει στο −2" + tail(nB, near(bids, -1))
    cids = ("c1", "c2", "c3", "c4", "c5")
    upC, dnC = 3 - S["sC"], S["sC"] + 3
    if upC <= dnC: needC, targetC, dC = upC, "Συνωστισμός long", 1
    else: needC, targetC, dC = dnC, "Συνωστισμός short", -1
    cC = near(cids, dC)
    extra = "· ανάβει Ξέπλυμα αν το OI 2 ημερών πέσει κάτω από −15%"
    if needC <= 0:
        backC = near(cids, -dC)
        res["C"] = (f"Το Γ είναι ήδη {S['stC']}· φεύγει αν {backC[0]}{extra}." if backC else f"Το Γ είναι ήδη {S['stC']}{extra}.")
    elif needC == 1 and cC:
        res["C"] = f"Το Γ γίνεται {targetC} αν {cC[0]}{extra}."
    elif cC:
        res["C"] = f"Το Γ θέλει {needC} βαθμούς για {targetC}· πιο κοντά: " + " · ".join(cC) + extra + "."
    else:
        res["C"] = f"Το Γ θέλει {needC} βαθμούς για {targetC}{extra}."
    return res

def score(V):
    sc = {}
    for i, layer, label, unit, rule, fn, dec in READINGS:
        sc[i] = fn(V[i]) if (fn and V[i] is not None) else 0
    if V["c2"] is not None and V["c2p"] is not None and V["c2"] >= 8:
        sc["c2"] = 1 if V["c2p"] > 0 else (-1 if V["c2p"] < 0 else 0)
    if V["b3"] is not None:
        term = V["b3t"]
        if V["b3"] < 18 and (term is None or term > 0): sc["b3"] = 1
        elif V["b3"] > 25 or (term is not None and term < 0): sc["b3"] = -1
    sA = sum(sc[i] for i in ("a1", "a2", "a4", "a5", "a6", "a7"))
    sB = sum(sc[i] for i in ("b1", "b2", "b3", "b4", "b5"))
    sC = sum(sc[i] for i in ("c1", "c2", "c3", "c4", "c5"))
    sD = sum(sc[i] for i in ("d2", "d3"))
    stA = "Επέκταση" if sA >= 3 else ("Συρρίκνωση" if sA <= -3 else "Ουδέτερο")
    stB = "Προς το ρίσκο" if sB >= 2 else ("Μακριά από το ρίσκο" if sB <= -2 else "Μικτό")
    eff = stA
    if stA == "Επέκταση" and sB <= -2: eff = "Ουδέτερο"
    if stA == "Συρρίκνωση" and sB >= 2: eff = "Ουδέτερο"
    stC = "Συνωστισμός long" if sC >= 3 else ("Συνωστισμός short" if sC <= -3 else "Ισορροπία")
    stD = "Στήριξη παρούσα" if sD >= 2 else ("Στήριξη απούσα" if sD <= -2 else "Ουδέτερο")
    flags = []
    flush = V["f1"] is not None and V["f1"] <= -15
    if flush: flags.append("Ξέπλυμα"); stC = "Ισορροπία"
    if V["f2"] is not None and V["f3"] is not None and V["f2"] >= V["f3"] + 5: flags.append("Γεγονός")
    if V["b5"] is not None and V["b5"] <= -5: flags.append("Όψιμη φάση")
    if V["c5"] is not None and V["c5"] >= 12: flags.append("Ζήτηση carry")
    return {"sc": sc, "sA": sA, "sB": sB, "sC": sC, "sD": sD, "stA": stA, "stB": stB, "eff": eff,
            "stC": stC, "stD": stD, "flags": flags, "flush": flush,
            "cell": f"{eff} × {stC}", "mult": MULT[eff][stC], "stance": STANCE[eff][stC]}

GR_DAYS = ["Δευ", "Τρί", "Τετ", "Πέμ", "Παρ", "Σάβ", "Κυρ"]

def lt(ts):
    d = dt.datetime.fromtimestamp(ts, ZoneInfo(LOCAL_TZ))
    return f"{GR_DAYS[d.weekday()]} {d:%d/%m %H:%M}"

def gnum(x, d=1, sign=False):
    s = f"{x:+.{d}f}" if sign else f"{x:.{d}f}"
    return s.replace(".", ",").replace("-", "−")

def gm(m):
    return f"{m:g}".replace(".", ",")

def us_open_local(now):
    o = now.astimezone(ZoneInfo("America/New_York")).replace(hour=9, minute=30, second=0, microsecond=0)
    return o.astimezone(ZoneInfo(LOCAL_TZ)).strftime("%H:%M")

PLAIN_A = {"Επέκταση": "Το χρήμα είναι άφθονο και φθηνό: ο άνεμος φυσάει υπέρ των ανόδων.",
           "Ουδέτερο": "Το μακρο δεν δίνει καθαρή κατεύθυνση.",
           "Συρρίκνωση": "Το χρήμα ακριβαίνει ή λιγοστεύει: ο άνεμος φυσάει κόντρα στις ανόδους."}
PLAIN_C = {"Ισορροπία": "Κανείς δεν είναι φορτωμένος με μόχλευση στη μία πλευρά, άρα δεν υπάρχει έτοιμο καύσιμο για απότομη κίνηση από αναγκαστικά κλεισίματα.",
           "Συνωστισμός long": "Πολλοί είναι ήδη long με μόχλευση: μια πτώση μπορεί να επιταχυνθεί από ρευστοποιήσεις.",
           "Συνωστισμός short": "Πολλοί είναι short με μόχλευση: μια άνοδος μπορεί να επιταχυνθεί από κάλυψη (squeeze)."}
A_NAMES = {"a1": ("πραγματικό επιτόκιο 10ετίας", "μ.β.", 0), "a2": ("καθαρή ρευστότητα", "%", 2),
           "a4": ("δολάριο", "%", 2), "a5": ("spread υψηλού κινδύνου", "μ.β.", 0), "a6": ("απόδοση 2ετούς", "μ.β.", 0)}
FLAG_TEXT = {"Ξέπλυμα": "Ξέπλυμα: η μόχλευση μόλις καθάρισε απότομα. Μετά από τέτοιο γεγονός η αγορά συνήθως ηρεμεί ή γυρίζει.",
             "Γεγονός": "Τα options τιμολογούν ταραχή τις επόμενες μέρες: η κίνηση θα είναι μεγαλύτερη από τη συνηθισμένη.",
             "Ζήτηση carry": "Μεγάλο μέρος της ζήτησης είναι carry (αντισταθμισμένο), όχι πεποίθηση· φεύγει γρήγορα αν πέσει το basis.",
             "Όψιμη φάση": "Τα μικρότερα coins τρέχουν πολύ μπροστά: όψιμη φάση όρεξης για ρίσκο, η άνοδος γίνεται εύθραυστη."}

def interpret(V, S, FL, hist, now):
    eff, stA, stC = S["eff"], S["stA"], S["stC"]
    if eff != stA:
        plain = f"Το μακρο λέει «{stA}», αλλά η όρεξη για ρίσκο το αντικρούει, άρα καθαρή κατεύθυνση δεν υπάρχει. " + PLAIN_C[stC]
    else:
        plain = PLAIN_A[eff] + " " + PLAIN_C[stC]

    short = [FLAG_TEXT[f] for f in S["flags"] if f in FLAG_TEXT]
    c1 = V.get("c1")
    if c1 is not None:
        if c1 >= 25: short.append(f"Funding {gnum(c1)}% ετησίως: οι long πληρώνουν ακριβά για να μείνουν μέσα.")
        elif c1 <= 0: short.append(f"Funding {gnum(c1)}% ετησίως: οι short πληρώνουν τους long, η αγορά γέρνει προς τα κάτω.")
        else: short.append(f"Funding {gnum(c1)}% ετησίως (ουδέτερο ≈11%): κανείς δεν πληρώνει υπερβολικά για τη θέση του.")
    e5 = V.get("e5")
    if e5:
        short.append(f"Τυπική ημερήσια κίνηση τώρα ≈{gnum(e5 / 365 ** 0.5)}%: κίνηση μέσα σε αυτό το εύρος είναι συνηθισμένη, και ένα stop πιο κοντά θα χτυπιέται από θόρυβο.")
    e4 = V.get("e4")
    if e4 is not None and e4 <= -5: short.append("Τα options υποτιμούν την κίνηση που ήδη γίνεται: η προστασία είναι φθηνή.")
    elif e4 is not None and e4 >= 5: short.append("Η προστασία είναι ακριβή: η αγορά φοβάται περισσότερο απ' όσο κινείται.")
    e1 = V.get("e1")
    if e1 is not None and e1 > 0.6:
        short.append(f"Από τις {us_open_local(now)} (άνοιγμα ΗΠΑ) το BTC κινείται μαζί με τις τεχνολογικές μετοχές: αν γυρίσουν εκείνες, συνήθως ακολουθεί.")
    elif e1 is not None and e1 < 0.3:
        short.append("Το BTC κινείται με δικούς του όρους· οι μετοχές λένε λίγα αυτές τις μέρες.")
    e2, e3 = V.get("e2"), V.get("e3")
    if e2 is not None and e3 is not None:
        if e2 > 2 and e3 > 2: short.append("Οι μετοχές που συνδέονται με το BTC κινήθηκαν την τελευταία μέρα πιο θετικά από το ίδιο: η αγορά μετοχών το βλέπει καλύτερα.")
        elif e2 < -2 and e3 < -2: short.append("Οι μετοχές που συνδέονται με το BTC κινήθηκαν την τελευταία μέρα πιο αρνητικά από το ίδιο: η αγορά μετοχών το βλέπει χειρότερα.")
    nowts = now.timestamp(); groups = {}
    for e in EXTRA.get("events", []):
        if e["ts"] >= nowts: groups.setdefault(e["ts"], []).append(f"{e['ccy']} {e['title']}")
    for ts in sorted(groups):
        short.append(f"{lt(ts)}: " + ", ".join(groups[ts]) + ". Γύρω από αυτή την ώρα η κίνηση μπορεί να είναι απότομη.")
    until = EXTRA.get("events_until")
    if EXTRA.get("events_ok") is False:
        short.append("Το ημερολόγιο γεγονότων δεν διαβάστηκε: τα γεγονότα είναι άγνωστα, όχι ανύπαρκτα.")
    elif until and until < nowts + 48 * 3600:
        short.append(f"Μετά τις {lt(until)} το ημερολόγιο δεν καλύπτει· έλεγξε τη δική σου ροή.")

    medium = [f"Κατεύθυνση: {S['stance']} Ρίσκο ×{gm(S['mult'])} του βασικού σου R."]
    against, pro = [], []
    for i, (name, unit, d) in A_NAMES.items():
        v, sc = V.get(i), S["sc"].get(i, 0)
        if v is None or sc == 0: continue
        (pro if sc > 0 else against).append(f"{name} {gnum(v, d, True)} {unit}")
    if S["sc"].get("a7") == -1: against.append("πληθωρισμός που ανεβαίνει με σύσφιξη")
    if S["sc"].get("a7") == 1: pro.append("πληθωρισμός που ανεβαίνει χωρίς σύσφιξη")
    end = lambda t: t if t.endswith(".") else t + "."
    if against: medium.append(end("Κόντρα στο BTC, σε 4 εβδομάδες: " + ", ".join(against)))
    if pro: medium.append(end("Υπέρ του BTC, σε 4 εβδομάδες: " + ", ".join(pro)))
    medium.append(f"Όρεξη για ρίσκο στις άλλες αγορές: {S['stB']} ({S['sB']:+d}).")
    if FL.get("A"): medium.append(FL["A"])
    if FL.get("B"): medium.append(FL["B"])
    t2, t3 = V.get("t2"), V.get("t3")
    if t2 is not None and t3 is not None:
        tr = "ανοδική" if (t2 > 0 and t3 > 0) else ("πτωτική" if (t2 < 0 and t3 < 0) else "μικτή")
        medium.insert(1, f"Τάση τιμής: {tr} — {gnum(abs(t2))}% {'πάνω' if t2 > 0 else 'κάτω'} από τον μέσο όρο 50 ημερών, "
                         f"{gnum(abs(t3))}% {'πάνω' if t3 > 0 else 'κάτω'} από τον μέσο όρο 200 ημερών.")
        if eff == "Συρρίκνωση" and tr == "ανοδική":
            medium.insert(2, "Σύγκρουση: το μακρο πιέζει, αλλά η τιμή είναι ακόμη σε ανοδική τάση. Το short θέλει επιβεβαίωση από τη δική σου δομή· μην το ανοίγεις μόνο επειδή το λέει το καθεστώς.")
        elif eff == "Επέκταση" and tr == "πτωτική":
            medium.insert(2, "Σύγκρουση: το μακρο στηρίζει, αλλά η τιμή είναι ακόμη σε πτωτική τάση. Το long θέλει επιβεβαίωση από τη δική σου δομή· μην το ανοίγεις μόνο επειδή το λέει το καθεστώς.")
        elif (eff == "Συρρίκνωση" and tr == "πτωτική") or (eff == "Επέκταση" and tr == "ανοδική"):
            medium.insert(2, "Η τάση της τιμής συμφωνεί με το μακρο: καθεστώς και κίνηση δείχνουν προς την ίδια κατεύθυνση.")
        elif tr == "μικτή" and eff != "Ουδέτερο":
            medium.insert(2, "Η τάση της τιμής είναι μικτή: ούτε επιβεβαιώνει ούτε αντικρούει το μακρο. Η κατεύθυνση ισχύει, αλλά χωρίς τη στήριξη της κίνησης.")

    long = [f"Ρευστότητα: {stA} (σκορ {S['sA']:+d}, κλίμακα −6 έως +6). Για ορίζοντα μηνών μετράει αυτή και όχι η τοποθέτηση."]
    t4, t5, t6, t7 = (V.get(k) for k in ("t4", "t5", "t6", "t7"))
    if t6 is not None: long.append(f"Αποτίμηση: MVRV {gnum(t6, 2)} — {mvrv_zone(t6)}.")
    if t4 is not None:
        long.append(f"Τιμή {gnum(abs(t4))}% {'πάνω από' if t4 >= 0 else 'κάτω από'} τον μέσο όρο 200 εβδομάδων"
                    + (", τη γραμμή που ιστορικά λειτούργησε ως πάτος κύκλου." if t4 >= 0 else ": ιστορικά σπάνιο, συνέπεσε με πάτους κύκλων."))
    if t5 is not None: long.append(f"Η τιμή είναι {gnum(abs(t5))}% κάτω από το υψηλό των τελευταίων 12 μηνών.")
    if t7 is not None: long.append(f"Κύκλος: {gnum(t7, 0)} μήνες από το τελευταίο halving — {halving_phase(t7)}. Τρεις κύκλοι μόνο: μοτίβο, όχι νόμος, και αδυνατίζει όσο μεγαλώνει η αγορά.")
    m = from_history(hist, "sA", 30)
    if m is not None:
        was = "Επέκταση" if m >= 3 else ("Συρρίκνωση" if m <= -3 else "Ουδέτερο")
        if was == stA:
            long.append(f"Η ρευστότητα ήταν «{was}» και πριν από έναν μήνα: δεύτερη συνεχόμενη μηνιαία ανάγνωση, άρα για μακροπρόθεσμη θέση η εικόνα θεωρείται επιβεβαιωμένη.")
        else:
            long.append(f"Πριν από έναν μήνα η ρευστότητα ήταν «{was}»: πρώτη μηνιαία ανάγνωση σε «{stA}». Για μακροπρόθεσμη θέση περίμενε και τη δεύτερη πριν αλλάξεις κάτι.")
    return {"plain": plain, "short": short, "medium": medium, "long": long}

def tg_ready():
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN", "").strip() and os.environ.get("TELEGRAM_CHAT_ID", "").strip())

def tg_send(text, label):
    tok = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(); cid = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    try:
        r = requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                          json={"chat_id": cid, "text": text[:4000], "disable_web_page_preview": True}, timeout=20)
        if r.status_code == 200:
            log(label); return True
        log(f"{label} — HTTP {r.status_code}", False); return False
    except Exception as e:
        log(f"{label} — {type(e).__name__}", False)   # ποτέ το κείμενο του σφάλματος: περιέχει το κλειδί
        return False

def brief_text(S, I, now):
    p = [f"Πίνακας BTC — {lt(now.timestamp())}", f"{S['cell']} · ρίσκο ×{gm(S['mult'])}", S["stance"], "", "Με απλά λόγια: " + I["plain"]]
    for t, k in (("ΤΙΣ ΕΠΟΜΕΝΕΣ ΩΡΕΣ – 2 ΜΕΡΕΣ", "short"), ("ΗΜΕΡΕΣ – ΕΒΔΟΜΑΔΕΣ", "medium"), ("ΜΗΝΕΣ", "long")):
        if I[k]: p += ["", t] + ["• " + l for l in I[k]]
    return "\n".join(p + ["", PAGE_URL])

def alert_text(changes, S, I, now):
    p = [f"Πίνακας BTC — ΑΛΛΑΓΗ ({lt(now.timestamp())})"] + ["• " + c for c in changes]
    p += ["", f"Νέα εικόνα: {S['cell']} · ρίσκο ×{gm(S['mult'])}", S["stance"], "", "Με απλά λόγια: " + I["plain"], "", PAGE_URL]
    return "\n".join(p)

def main():
    for step in (derivs, options, basis, demand, macro, equities, vix_check, trend, calendar):
        try: step()
        except Exception as e: log(f"{step.__name__} — {type(e).__name__} {str(e)[:60]}", False)
    hist = read_history()
    if V["b5l"] is not None:
        prev5 = from_history(hist, "b5l", 28)
        if prev5 is not None: setv("b5", V["b5l"] - prev5, "ιστορικό")
    if EXTRA["oi_level"]:
        p7 = from_history(hist, "oi_level", 7)
        if p7: setv("c2", (EXTRA["oi_level"] / p7 - 1) * 100, "ιστορικό")
        p2 = from_history(hist, "oi_level", 2)
        if p2: setv("f1", (EXTRA["oi_level"] / p2 - 1) * 100, "ιστορικό")

    # Ανάγνωση που απέτυχε σε αυτή την εκτέλεση κρατά την τελευταία γνωστή τιμή (σημερινή ή χθεσινή),
    # δηλωμένη ως προηγούμενη. Μια πηγή που πέφτει δεν πρέπει να αλλάζει το καθεστώς.
    STALE = []
    for i in IDS:
        if V[i] is not None or i == "e4": continue   # το e4 υπολογίζεται από f3 και e5
        for r in reversed(hist):
            try:
                age = (dt.date.today() - dt.date.fromisoformat(r["date"][:10])).days
            except Exception:
                continue
            if age > 1: break
            try:
                val = float(r.get(i, ""))
            except Exception:
                continue
            setv(i, val, f"προηγ. τιμή {r['date'][8:10]}/{r['date'][5:7]}"); STALE.append(i); break

    if V["f3"] is not None and V["e5"] is not None:
        setv("e4", V["f3"] - V["e5"], "υπολογισμός")

    prev = {}
    try:
        with open(DATA, encoding="utf-8") as f:
            pj = json.load(f); prev = {"cell": pj["scores"]["cell"], "flags": pj.get("flags", []), "brief_date": pj.get("brief_date")}
    except Exception:
        pass

    S = score(V)
    FL = flips(V, S, READINGS)
    now = dt.datetime.now(dt.timezone.utc)

    changes = []
    if prev:
        if prev["cell"] != S["cell"]:
            changes.append(f"Κελί: {prev['cell']} → {S['cell']} (ρίσκο ×{S['mult']})")
        for fl in S["flags"]:
            if fl not in prev["flags"]: changes.append(f"Νέα σημαία: {fl}")
        for fl in prev["flags"]:
            if fl not in S["flags"]: changes.append(f"Έσβησε η σημαία: {fl}")
    if changes and STALE:
        changes.append("Σημείωση: με προηγούμενη τιμή, επειδή η πηγή τους δεν απάντησε: " + ", ".join(STALE) + ".")
    with open(os.path.join(ROOT, "alert.json"), "w", encoding="utf-8") as f:
        json.dump({"changed": bool(changes), "changes": changes, "cell": S["cell"], "mult": S["mult"],
                   "stance": S["stance"], "flags": S["flags"], "when": now.isoformat(timespec="seconds")}, f, ensure_ascii=False, indent=1)

    try:
        INTERP = interpret(V, S, FL, hist, now)
    except Exception as e:
        INTERP = {"plain": "", "short": [], "medium": [], "long": []}
        log(f"ερμηνεία — {type(e).__name__} {str(e)[:60]}", False)
    BRIEF = prev.get("brief_date") if prev else None
    local_now = now.astimezone(ZoneInfo(LOCAL_TZ))
    if tg_ready() and INTERP["plain"]:
        if changes:
            tg_send(alert_text(changes, S, INTERP, now), "telegram αλλαγή")
        if local_now.hour >= 7 and BRIEF != local_now.date().isoformat():
            if tg_send(brief_text(S, INTERP, now), "telegram πρωινή σύνοψη"):
                BRIEF = local_now.date().isoformat()

    readings = [{"id": i, "layer": L, "label": lab, "unit": u, "rule": rule, "dec": dec,
                 "v": V[i], "src": SRC[i], "score": S["sc"][i]} for i, L, lab, u, rule, fn, dec in READINGS]
    out = {"generated": now.isoformat(timespec="seconds"), "readings": readings,
           "scores": {k: S[k] for k in ("sA", "sB", "sC", "sD", "stA", "stB", "eff", "stC", "stD", "cell", "mult", "stance", "flush")},
           "flags": S["flags"], "extra": EXTRA, "events": EXTRA.get("events", []), "log": LOG, "changes": changes, "flips": FL, "interp": INTERP, "brief_date": BRIEF, "stale": STALE, "tnotes": tnotes(V),
           "missing": [i for i in IDS if V[i] is None and i not in ("b3t", "b5l") and not i.startswith("t")]}
    with open(DATA, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    cols = ["date"] + IDS + ["oi_level", "price", "sA", "sB", "sC", "sD", "eff", "stC", "cell", "mult", "flags"]
    row = {"date": now.date().isoformat(), **{i: ("" if V[i] is None else round(V[i], 4)) for i in IDS},
           "oi_level": EXTRA["oi_level"] or "", "price": EXTRA["price"] or "",
           "sA": S["sA"], "sB": S["sB"], "sC": S["sC"], "sD": S["sD"], "eff": S["eff"], "stC": S["stC"],
           "cell": S["cell"], "mult": S["mult"], "flags": "|".join(S["flags"])}
    rows = [r for r in hist if r.get("date") != row["date"]] + [row]
    with open(HIST, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore"); w.writeheader(); w.writerows(rows)
    print(f"{S['cell']} ×{S['mult']} | αλλαγές: {changes or '—'} | κενά: {out['missing'] or '—'}")
    print("\n".join(LOG))

if __name__ == "__main__":
    main()
