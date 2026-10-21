#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Φρουρός BTC — ειδοποίηση μόνο όταν υπάρχει ισχυρή ένδειξη ή ακραίο γεγονός.

Συμπληρώνει τον Πίνακα BTC χωρίς να τον αλλάζει: διαβάζει μόνο το alert.json του
Πίνακα για να γράφει στο μήνυμα την εικόνα καθεστώτος.

Δύο είδη ειδοποίησης (κανόνες και πηγές: phylakas_kanones.md)
  ΙΣΧΥΡΗ ΕΝΔΕΙΞΗ  η τιμή βρίσκεται σε επίπεδο που βγαίνει από τη δομή ΚΑΙ τουλάχιστον
                  δύο ανεξάρτητες οικογένειες στοιχείων (order flow, μόχλευση, ρευστότητα
                  βιβλίου) έδωσαν στοιχείο τα τελευταία 30 λεπτά, χωρίς καμία να δείχνει
                  αντίθετα
  ΑΚΡΑΙΟ ΓΕΓΟΝΟΣ  κασκάδα ρευστοποιήσεων, πέρασμα zero gamma, απότομη κίνηση

Πηγές: Basis (Binance order flow, footprint, παράγωγα — δωρεάν, χωρίς κλειδί)
       OKX perp (κεριά, εφεδρικό order flow, open interest, ρευστοποιήσεις)
       Coinbase spot (βιβλίο εντολών) · Deribit (options)
Αρχή: αριθμός και κατεύθυνση, ποτέ είσοδος.
"""
import datetime as dt, json, math, os, re, statistics
import requests
from zoneinfo import ZoneInfo

LOCAL_TZ = "Europe/Athens"
PAGE_URL = "https://kostasg9786-cmyk.github.io/pinakas-btc/"
ROOT = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(ROOT, "phylakas_state.json")   # μόνο στη μνήμη cache του workflow, ποτέ στο αποθετήριο
ALERT = os.path.join(ROOT, "alert.json")             # το καθεστώς του Πίνακα BTC (μόνο ανάγνωση)
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
BASIS = "https://www.basischarts.com/api/mcp"
OKX = "https://www.okx.com/api/v5"
DER = "https://www.deribit.com/api/v2/public"
CB = "https://api.exchange.coinbase.com"
INST = "BTC-USDT-SWAP"
MON = {m: i + 1 for i, m in enumerate("JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split())}

# ── Παράμετροι. Αρχικές τιμές· αλλαγή μόνο σε τριμηνιαία επανεξέταση, με γραπτό λόγο. ──
P = {
    "near_level_pct": 0.3,    # «στο επίπεδο»: απόσταση ≤ 0,3% ή πέρασμα στα 3 τελευταία κεριά 15m
    "window_min": 30,         # παράθυρο συρροής στοιχείων
    "z_absorb": 2.5,          # delta κεριού 15m σε τυπικές αποκλίσεις για absorption
    "z_aggr": 3.0,            # για επιθετική ροή
    "fp_absorb_min": 5,       # ελάχιστα σήματα absorption στο footprint (Basis) της ίδιας πλευράς
    "fp_stack_rows": 3,       # γειτονικές γραμμές με imbalance ≥3:1 για stacked
    "oi_1h_ev": 1.5,          # % μεταβολή OI σε 1 ώρα = στοιχείο μόχλευσης
    "liq_ev_usd": 1e6,        # ρευστοποιήσεις 15' = στοιχείο μόχλευσης
    "liq_extreme_usd": 3e6,   # ρευστοποιήσεις 15' = ακραίο γεγονός
    "move_1h_pct": 2.0,       # ακραίο: μεταβολή 1 ώρας
    "range_atr": 3.0,         # ακραίο: εύρος κεριού 15m σε πολλαπλάσια ATR14
    "wall_btc": 10.0, "wall_mult": 5.0, "wall_near_pct": 0.6, "wall_seen": 3,
    "zg_hyst_pct": 0.2,
}
COOL = {"strong": 120, "liq": 30, "zg": 120, "move": 60, "fail": 360}   # λεπτά

LOG = []
def log(m, ok=True): LOG.append(("✓ " if ok else "✗ ") + m)

def get(url, label, timeout=20):
    try:
        r = requests.get(url, headers=UA, timeout=timeout); r.raise_for_status()
        log(label); return r.json()
    except Exception as e:
        log(f"{label} — {type(e).__name__} {str(e)[:70]}", False); return None

def n(x, d=0):
    s = f"{x:,.{d}f}"
    return s.replace(",", "§").replace(".", ",").replace("§", ".")

def sgn(x, d=1): return ("+" if x >= 0 else "") + n(x, d)
def pct(a, b): return (a / b - 1) * 100 if b else 0.0
def lt(now): return now.astimezone(ZoneInfo(LOCAL_TZ)).strftime("%d/%m %H:%M")

# ── Basis: δωρεάν MCP, Streamable HTTP (JSON-RPC μέσω POST) ───────────────────

class Basis:
    def __init__(self): self.sid = None; self.ok = False; self.i = 0

    def _post(self, payload, timeout=45):
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream", **UA}
        if self.sid: h["Mcp-Session-Id"] = self.sid
        r = requests.post(BASIS, json=payload, headers=h, timeout=timeout); r.raise_for_status()
        if r.headers.get("Mcp-Session-Id"): self.sid = r.headers["Mcp-Session-Id"]
        if r.status_code == 202 or not r.content: return None
        if "text/event-stream" in r.headers.get("Content-Type", ""):
            msg = None
            for line in r.text.splitlines():
                if line.startswith("data:"):
                    try: m = json.loads(line[5:].strip())
                    except Exception: continue
                    if isinstance(m, dict) and m.get("id") == payload.get("id"): msg = m
            return msg
        return r.json()

    def start(self):
        try:
            self.i += 1
            res = self._post({"jsonrpc": "2.0", "id": self.i, "method": "initialize",
                              "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                                         "clientInfo": {"name": "frouros-btc", "version": "1.0"}}})
            if not res or "result" not in res: raise RuntimeError("χωρίς απάντηση")
            self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
            self.ok = True; log("Basis: σύνδεση")
        except Exception as e:
            log(f"Basis: σύνδεση — {type(e).__name__} {str(e)[:70]}", False)
        return self.ok

    def call(self, tool, args, label):
        if not self.ok: return None
        try:
            self.i += 1
            res = self._post({"jsonrpc": "2.0", "id": self.i, "method": "tools/call",
                              "params": {"name": tool, "arguments": args}})
            if not res: raise RuntimeError("κενή απάντηση")
            if "error" in res: raise RuntimeError(str(res["error"])[:80])
            r = res.get("result") or {}
            txt = [c.get("text", "") for c in r.get("content", []) if c.get("type") == "text"]
            if r.get("isError"): raise RuntimeError(" ".join(txt)[:80])
            out = r.get("structuredContent") if isinstance(r.get("structuredContent"), dict) else None
            for t in txt:
                if out is not None: break
                try: out = json.loads(t)
                except Exception: pass
            if not isinstance(out, dict): raise RuntimeError("μη αναγνώσιμη απάντηση")
            log(label); return out
        except Exception as e:
            log(f"{label} — {type(e).__name__} {str(e)[:70]}", False); return None

def basis_read(B, of_bar_ms):
    """Binance perp μέσω Basis: παράγωγα, delta ανά κερί 15m, footprint του τελευταίου κλειστού κεριού."""
    out = {}
    c = B.call("market_candles", {"exchange": "binance", "market": "futures", "symbol": "BTCUSDT",
                                  "timeframe": "15m", "bars": 200, "withDerivatives": True}, "Basis: κεριά 15m (Binance)")
    if not c or not c.get("ref"): return out
    ref = c["ref"]
    d = B.call("market_derivatives", {"ref": ref}, "Basis: παράγωγα (Binance)")
    if d: out["deriv"] = d
    cv = B.call("indicator_compute", {"studyId": "cvd", "ref": ref, "reduce": "tail"}, "Basis: delta 15m (Binance)")
    try:
        s = cv["series"]["delta"]
        out["delta"] = {int(t) * 1000: float(v) for t, v in zip(s["tailTimes"], s["tail"])}
    except Exception:
        pass
    if of_bar_ms:
        of = B.call("orderflow_read", {"exchange": "binance", "market": "futures", "symbol": "BTCUSDT",
                                       "timeframe": "15m", "candleTime": of_bar_ms // 1000, "levels": 20},
                    "Basis: footprint τελευταίου κεριού (Binance)")
        if of: out["of"] = of
    return out

# ── OKX, Coinbase, Deribit ────────────────────────────────────────────────────

def okx_candles():
    j = get(f"{OKX}/market/candles?instId={INST}&bar=15m&limit=300", "OKX: κεριά 15m")
    out = [{"t": int(r[0]), "o": float(r[1]), "h": float(r[2]), "l": float(r[3]), "c": float(r[4]), "closed": r[8] == "1"}
           for r in reversed((j or {}).get("data") or [])]
    closed = [b for b in out if b["closed"]]
    live = out[-1] if out and not out[-1]["closed"] else None
    return closed, live

def okx_taker():
    j = get(f"{OKX}/rubik/stat/taker-volume-contract?instId={INST}&period=15m&unit=0&limit=100", "OKX: taker flow 15m (εφεδρική)")
    rows = (j or {}).get("data") or []
    return {int(r[0]): float(r[2]) - float(r[1]) for r in rows}   # ts → buy − sell (BTC)

def okx_oi():
    j = get(f"{OKX}/rubik/stat/contracts/open-interest-history?instId={INST}&period=5m&limit=100", "OKX: open interest 5m")
    return [(int(r[0]), float(r[2])) for r in reversed((j or {}).get("data") or [])]

def okx_liqs(minutes=15):
    ct = 0.01
    j = get(f"{OKX}/public/instruments?instType=SWAP&instId={INST}", "OKX: μέγεθος συμβολαίου")
    try: ct = float(j["data"][0]["ctVal"])
    except Exception: pass
    cut = dt.datetime.now(dt.timezone.utc).timestamp() * 1000 - minutes * 60000
    L = S = 0.0; after = ""; done = False
    for _ in range(10):
        j = get(f"{OKX}/public/liquidation-orders?instType=SWAP&uly=BTC-USDT&state=filled&limit=100" + (f"&after={after}" if after else ""),
                "OKX: ρευστοποιήσεις")
        if j is None: return None
        det = ((j.get("data") or [{}])[0]).get("details") or []
        if not det: done = True; break
        for x in det:
            if float(x["ts"]) < cut: done = True; break
            usd = float(x["sz"]) * ct * float(x["bkPx"])
            if x.get("posSide") == "long": L += usd
            elif x.get("posSide") == "short": S += usd
        if done: break
        after = det[-1]["ts"]
    return (L, S) if done else None

def book():
    j = get(f"{CB}/products/BTC-USD/book?level=2", "Coinbase: βιβλίο εντολών spot", timeout=30)
    if j and j.get("bids") and j.get("asks"):
        return [(float(p), float(s)) for p, s, *_ in j["bids"]], [(float(p), float(s)) for p, s, *_ in j["asks"]], "Coinbase spot"
    j = get(f"{OKX}/market/books-full?instId={INST}&sz=5000", "OKX: βιβλίο εντολών (εφεδρική)")
    try:
        d = j["data"][0]
        return [(float(p), float(s) * 0.01) for p, s, *_ in d["bids"]], [(float(p), float(s) * 0.01) for p, s, *_ in d["asks"]], "OKX perp"
    except Exception:
        return None, None, None

def deribit_options():
    j = get(f"{DER}/get_book_summary_by_currency?currency=BTC&kind=option", "Deribit: options", timeout=30)
    return (j or {}).get("result") or []

# ── Options: Black-76 gamma, zero gamma, walls, max pain ──────────────────────

def parse_options(raw, now):
    rows = []
    for o in raw:
        m = re.match(r"BTC-(\d{1,2})([A-Z]{3})(\d{2})-(\d+)-([CP])$", o.get("instrument_name", ""))
        if not m: continue
        d, mon, y, k, t = m.groups()
        try: exp = dt.datetime(2000 + int(y), MON[mon], int(d), 8, tzinfo=dt.timezone.utc)
        except Exception: continue
        T = (exp - now).total_seconds() / (365 * 86400)
        oi = float(o.get("open_interest") or 0); iv = float(o.get("mark_iv") or 0) / 100; F = float(o.get("underlying_price") or 0)
        if T <= 0.5 / 365 or oi <= 0 or iv <= 0 or F <= 0: continue
        rows.append({"K": float(k), "cp": t, "T": T, "iv": iv, "F": F, "oi": oi, "exp": exp})
    return rows

def gamma(F, K, T, iv):
    d1 = (math.log(F / K) + 0.5 * iv * iv * T) / (iv * math.sqrt(T))
    return math.exp(-0.5 * d1 * d1) / math.sqrt(2 * math.pi) / (F * iv * math.sqrt(T))

def gex_at(rows, scale=1.0):
    """$ ανά 1% κίνησης. Υπόθεση (ίδια με τη Basis): dealers long calls (+), short puts (−). Μοντέλο, όχι μέτρηση."""
    tot = 0.0
    for r in rows:
        F = r["F"] * scale
        g = gamma(F, r["K"], r["T"], r["iv"]) * r["oi"] * F * F * 0.01
        tot += g if r["cp"] == "C" else -g
    return tot

def options_levels(rows, spot):
    if not rows: return None
    short = [r["F"] for r in rows if r["T"] < 60 / 365]
    F0 = statistics.median(short) if short else rows[0]["F"]
    grid = [0.70 + i * 0.0025 for i in range(241)]
    vals = [gex_at(rows, s) for s in grid]
    zg, best = None, 9e9
    for (s0, v0), (s1, v1) in zip(zip(grid, vals), zip(grid[1:], vals[1:])):
        if (v0 < 0) != (v1 < 0):
            s = s0 + (s1 - s0) * (-v0) / (v1 - v0)
            if abs(s - 1) < best: best, zg = abs(s - 1), s * F0
    near = [r for r in rows if r["T"] <= 35 / 365]
    by_k = {}
    for r in near:
        g = gamma(r["F"], r["K"], r["T"], r["iv"]) * r["oi"] * r["F"] * r["F"] * 0.01
        c, p = by_k.get(r["K"], (0.0, 0.0))
        by_k[r["K"]] = (c + g, p) if r["cp"] == "C" else (c, p + g)
    above = [(k, v[0]) for k, v in by_k.items() if k > spot]
    below = [(k, v[1]) for k, v in by_k.items() if k < spot]
    tot = {}
    for r in near:
        if r["T"] >= 1 / 365: tot[r["exp"]] = tot.get(r["exp"], 0) + r["oi"]
    mp = None
    if tot:
        big = max(tot, key=tot.get); ex = [r for r in near if r["exp"] == big]
        pain = lambda k: sum(r["oi"] * (max(0, k - r["K"]) if r["cp"] == "C" else max(0, r["K"] - k)) for r in ex)
        mp = {"strike": min(sorted({r["K"] for r in ex}), key=pain), "expiry": big.strftime("%d/%m")}
    return {"net": gex_at(rows), "zg": zg,
            "call_wall": max(above, key=lambda x: x[1])[0] if above else None,
            "put_wall": max(below, key=lambda x: x[1])[0] if below else None, "maxpain": mp}

# ── Ρευστότητα βιβλίου ────────────────────────────────────────────────────────

def walls(bids, asks, price):
    """Κάδοι 0,1% μέσα σε ±2%. Τοίχος = κάδος ≥ wall_btc και ≥ wall_mult × διάμεσο κάδο."""
    step = round(price * 0.001, -1) or 10
    lo, hi = price * 0.98, price * 1.02
    bins, wpx = {}, {}
    for side, lst in (("bid", bids), ("ask", asks)):
        for p, s in lst:
            if lo <= p <= hi:
                k = (side, round(p / step) * step); bins[k] = bins.get(k, 0.0) + s; wpx[k] = wpx.get(k, 0.0) + p * s
    if not bins: return []
    med = statistics.median(bins.values())
    out = [{"side": k[0], "px": k[1], "at": round(wpx[k] / v), "btc": v} for k, v in bins.items()
           if v >= P["wall_btc"] and v >= P["wall_mult"] * med]
    return sorted(out, key=lambda w: -w["btc"])

# ── Στοιχεία ανά οικογένεια. Κάθε στοιχείο: (οικογένεια, κλειδί, κατεύθυνση up|down|None, κείμενο) ──

def zscore(x, hist):
    if len(hist) < 20: return 0.0
    sd = statistics.pstdev(hist)
    return (x - statistics.fmean(hist)) / sd if sd > 0 else 0.0

def orderflow_evidence(closed, delta, src):
    """Από το τελευταίο κλειστό κερί 15m: absorption (delta χωρίς πρόοδο στο άκρο), επιθετική ροή, απόκλιση CVD."""
    ev = []
    bars = [dict(b, d=delta[b["t"]]) for b in closed if b["t"] in delta]
    if len(bars) < 21 or bars[-1]["t"] != closed[-1]["t"]: return ev
    last, prev = bars[-1], bars[-97:-1]
    z = zscore(last["d"], [b["d"] for b in prev])
    rng = last["h"] - last["l"]; med = statistics.median(b["h"] - b["l"] for b in prev)
    hi8 = max(b["h"] for b in closed[-33:-1]); lo8 = min(b["l"] for b in closed[-33:-1])
    mv = last["c"] - last["o"]
    if abs(z) >= P["z_absorb"] and rng <= med:
        if last["d"] > 0 and last["h"] >= hi8 * 0.999:
            ev.append(("flow", "absorb:up", "down", f"Order flow ({src}): επιθετικές αγορές {n(last['d'],1)} BTC ({n(z,1)}σ) χωρίς πρόοδο στο υψηλό 8 ωρών — υποψήφιο absorption από πωλητές."))
        elif last["d"] < 0 and last["l"] <= lo8 * 1.001:
            ev.append(("flow", "absorb:down", "up", f"Order flow ({src}): επιθετικές πωλήσεις {n(-last['d'],1)} BTC ({n(z,1)}σ) χωρίς πτώση στο χαμηλό 8 ωρών — υποψήφιο absorption από αγοραστές."))
    if abs(z) >= P["z_aggr"] and abs(mv) >= 1.5 * med and (mv > 0) == (last["d"] > 0):
        d = "up" if mv > 0 else "down"
        ev.append(("flow", "aggr:" + d, d, f"Order flow ({src}): επιθετικοί {'αγοραστές' if d=='up' else 'πωλητές'} {n(abs(last['d']),1)} BTC ({n(z,1)}σ) που μετακίνησαν την τιμή {n(mv)} μονάδες."))
    win = bars[-96:]
    if len(win) >= 60:
        cvd, s = [], 0.0
        for b in win: s += b["d"]; cvd.append(s)
        if win[-1]["h"] > max(b["h"] for b in win[:-1]) and cvd[-1] < max(cvd[:-1]):
            ev.append(("flow", "div:up", "down", f"Order flow ({src}): νέο υψηλό {len(win)//4} ωρών χωρίς νέο υψηλό CVD — η άνοδος δεν τροφοδοτείται από νέους αγοραστές."))
        if win[-1]["l"] < min(b["l"] for b in win[:-1]) and cvd[-1] > min(cvd[:-1]):
            ev.append(("flow", "div:down", "up", f"Order flow ({src}): νέο χαμηλό {len(win)//4} ωρών χωρίς νέο χαμηλό CVD — η πτώση δεν τροφοδοτείται από νέους πωλητές."))
    return ev

def footprint_evidence(of, bar, closed):
    """Από το footprint της Basis για το τελευταίο κλειστό κερί: absorption και stacked imbalance."""
    ev = []
    if not of or not bar: return ev
    sig = of.get("signals") or []
    ask = sum(1 for s in sig if s.get("kind") == "absorption" and s.get("side") == "sell")   # επιθετικές αγορές χωρίς πρόοδο
    bid = sum(1 for s in sig if s.get("kind") == "absorption" and s.get("side") == "buy")
    hi8 = max(b["h"] for b in closed[-33:-1]); lo8 = min(b["l"] for b in closed[-33:-1])
    cov = "" if of.get("coverageComplete", True) else f" (κάλυψη {of.get('coveredMinutes')}′)"
    if ask >= P["fp_absorb_min"] and ask >= 2 * bid and bar["h"] >= hi8 * 0.999:
        ev.append(("flow", "fp_absorb:up", "down", f"Footprint (Binance): {ask} σήματα ask absorption στο υψηλό 8 ωρών{cov}."))
    if bid >= P["fp_absorb_min"] and bid >= 2 * ask and bar["l"] <= lo8 * 1.001:
        ev.append(("flow", "fp_absorb:down", "up", f"Footprint (Binance): {bid} σήματα bid absorption στο χαμηλό 8 ωρών{cov}."))
    rows = sorted(of.get("footprint") or [], key=lambda r: r.get("price", 0))
    for side, d in (("buy", "up"), ("sell", "down")):
        run = best = 0; at = None
        for r in rows:
            b, s = float(r.get("buy") or 0), float(r.get("sell") or 0)
            ok = (b >= 3 * s and b > 0) if side == "buy" else (s >= 3 * b and s > 0)
            run = run + 1 if ok else 0
            if run > best: best, at = run, r.get("price")
        if best >= P["fp_stack_rows"]:
            ev.append(("flow", f"stack:{side}", d, f"Footprint (Binance): stacked {'αγοραστές' if side=='buy' else 'πωλητές'} σε {best} γειτονικά επίπεδα γύρω στο {n(at)}{cov}."))
    return ev

def leverage_evidence(oi, closed, liq):
    ev = []
    if len(oi) >= 13 and len(closed) >= 5:
        ch = pct(oi[-1][1], oi[-13][1]); pm = pct(closed[-1]["c"], closed[-5]["c"])
        if abs(ch) >= P["oi_1h_ev"]:
            ev.append(("lev", "oi:" + ("up" if ch > 0 else "down"), None,
                       f"Μόχλευση (OKX): open interest {sgn(ch)}% σε μία ώρα με τιμή {sgn(pm)}% — " +
                       ("νέες θέσεις ανοίγουν." if ch > 0 else "θέσεις κλείνουν.")))
    if liq:
        L, S = liq
        if L >= P["liq_ev_usd"]: ev.append(("lev", "liq:long", "down", f"Μόχλευση (OKX): ρευστοποιήσεις long ${n(L/1e6,1)} εκ. σε 15′ — αναγκαστικές πωλήσεις."))
        if S >= P["liq_ev_usd"]: ev.append(("lev", "liq:short", "up", f"Μόχλευση (OKX): ρευστοποιήσεις short ${n(S/1e6,1)} εκ. σε 15′ — αναγκαστικές αγορές."))
    return ev

def book_evidence(W, prev, price, hi, lo, src):
    ev = []; cur = {f"{w['side']}:{w['px']}": w for w in W}
    for key, pw in (prev or {}).items():
        if pw.get("seen", 0) < P["wall_seen"] or abs(pct(price, pw["at"])) > P["wall_near_pct"]: continue
        now_w = cur.get(key)
        if now_w and now_w["btc"] >= 0.3 * pw["btc"]: continue
        ask = pw["side"] == "ask"
        crossed = (ask and hi >= pw["at"]) or (not ask and lo <= pw["at"])
        d = "up" if ask else "down"
        what = "καταναλώθηκε" if crossed else "αποσύρθηκε"
        ev.append(("book", f"wall:{key}", d, f"Ρευστότητα ({src}): {what} τοίχος {'πωλητών' if ask else 'αγοραστών'} {n(pw['btc'])} BTC στο {n(pw['at'])}."))
    return ev

# ── Επίπεδα από τη δομή ───────────────────────────────────────────────────────

def levels(O, closed, prev_walls):
    L = []
    if O:
        if O.get("call_wall"): L.append(("cw", O["call_wall"], "call wall (options, Deribit)"))
        if O.get("put_wall"): L.append(("pw", O["put_wall"], "put wall (options, Deribit)"))
        if O.get("zg"): L.append(("zg", O["zg"], "zero gamma (options, Deribit)"))
    if len(closed) >= 97:
        win = closed[-97:-1]
        L.append(("h24", max(b["h"] for b in win), "υψηλό 24 ωρών (OKX perp)"))
        L.append(("l24", min(b["l"] for b in win), "χαμηλό 24 ωρών (OKX perp)"))
    for key, w in (prev_walls or {}).items():
        if w.get("seen", 0) >= P["wall_seen"]:
            L.append(("bw:" + key, w["at"], f"τοίχος {'πωλητών' if w['side']=='ask' else 'αγοραστών'} {n(w['btc'])} BTC (βιβλίο)"))
    return L

def at_level(lv, price, closed):
    near = []
    recent = closed[-3:] if closed else []
    for key, px, lab in lv:
        crossed = any(b["l"] <= px <= b["h"] for b in recent)
        if abs(pct(price, px)) <= P["near_level_pct"] or crossed: near.append((key, px, lab))
    return sorted(near, key=lambda x: abs(price - x[1]))

def confluence(ev):
    """Οικογένειες με στοιχείο και η κατεύθυνσή τους. Ισχυρή αν ≥2 οικογένειες, ≥1 κατευθυντική, καμία αντίθετη."""
    fam = {}
    for e in ev:
        fam.setdefault(e["fam"], []).append(e)
    dirs = {}
    for f, items in fam.items():
        ds = {i["dir"] for i in items if i["dir"]}
        dirs[f] = ds.pop() if len(ds) == 1 else ("mixed" if ds else None)
    if len(fam) < 2 or "mixed" in dirs.values(): return None, fam
    dset = {d for d in dirs.values() if d}
    if len(dset) != 1: return None, fam
    return dset.pop(), fam

# ── Μηνύματα ──────────────────────────────────────────────────────────────────

def tg_ready():
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN", "").strip() and os.environ.get("TELEGRAM_CHAT_ID", "").strip())

def tg_send(text, label):
    tok = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(); cid = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    try:
        r = requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                          json={"chat_id": cid, "text": text[:4000], "disable_web_page_preview": True}, timeout=20)
        if r.status_code == 200: log(label); return True
        log(f"{label} — HTTP {r.status_code}", False); return False
    except Exception as e:
        log(f"{label} — {type(e).__name__}", False); return False   # ποτέ το κείμενο του σφάλματος: περιέχει το κλειδί

def regime():
    try:
        with open(ALERT, encoding="utf-8") as f: a = json.load(f)
        return f"{a.get('cell','')} · {a.get('stance','')}".strip(" ·")
    except Exception:
        return None

def context(price, O, W, wsrc, deriv):
    p = [f"Τιμή {n(price)} (OKX perp)"]
    r = regime()
    if r: p.append(f"Πίνακας BTC: {r}")
    if O:
        b = []
        if O.get("zg"): b.append(f"zero gamma {n(O['zg'])} ({'πάνω' if price > O['zg'] else 'κάτω'} του)")
        if O.get("call_wall"): b.append(f"call wall {n(O['call_wall'])}")
        if O.get("put_wall"): b.append(f"put wall {n(O['put_wall'])}")
        if O.get("maxpain"): b.append(f"max pain {n(O['maxpain']['strike'])} ({O['maxpain']['expiry']})")
        if b: p.append("Options (Deribit): " + " · ".join(b))
    if W:
        bw = next((w for w in W if w["side"] == "bid"), None); aw = next((w for w in W if w["side"] == "ask"), None)
        b = ([f"αγοραστές {n(bw['at'])} ({n(bw['btc'])} BTC)"] if bw else []) + ([f"πωλητές {n(aw['at'])} ({n(aw['btc'])} BTC)"] if aw else [])
        if b: p.append(f"Τοίχοι ({wsrc}): " + " · ".join(b))
    if deriv:
        try:
            f = deriv["funding"]["annualisedPercent"]; tt = deriv.get("topTraderRatio")
            p.append(f"Binance (Basis): funding {sgn(f)}% ετήσια" + (f" · top traders {n(tt,2)} long/short" if tt else ""))
        except Exception:
            pass
    return p

FOOT = ["", "Αριθμός και κατεύθυνση, όχι είσοδος.", PAGE_URL]

# ── Κύριο ─────────────────────────────────────────────────────────────────────

def load_state():
    try:
        with open(STATE, encoding="utf-8") as f: return json.load(f)
    except Exception:
        return {}

def main():
    now = dt.datetime.now(dt.timezone.utc); ts = now.timestamp()
    st = load_state(); first = not st
    for k, v in (("cool", {}), ("ev", []), ("dhist", {}), ("walls", {}), ("fails", 0)): st.setdefault(k, v)

    closed, live = okx_candles()
    price = (live or (closed[-1] if closed else {})).get("c")
    last_t = closed[-1]["t"] if closed else None
    new_bar = last_t is not None and st.get("bar_t") != last_t

    B = Basis(); bz = basis_read(B, last_t if new_bar else None) if B.start() else {}
    for t, v in (bz.get("delta") or {}).items(): st["dhist"][str(t)] = v
    st["dhist"] = dict(sorted(st["dhist"].items())[-120:])
    oi = okx_oi(); liq = okx_liqs()
    bids, asks, wsrc = book()
    O = None
    try:
        rows = parse_options(deribit_options(), now)
        if rows and price: O = options_levels(rows, price)
    except Exception as e:
        log(f"options: υπολογισμός — {type(e).__name__} {str(e)[:60]}", False)

    if not price:
        st["fails"] += 1
        if tg_ready() and ts - st["cool"].get("fail", 0) >= COOL["fail"] * 60 and st["fails"] >= 6:
            tg_send(f"Φρουρός BTC — {lt(now)}\nΗ πηγή τιμής δεν απαντά για 30′. Η επιτήρηση είναι σε παύση.", "telegram: βλάβη")
            st["cool"]["fail"] = ts
        save(st); print("\n".join(LOG)); return
    st["fails"] = 0

    # Στοιχεία
    new_ev = []
    if new_bar:
        dB = {int(t): v for t, v in st["dhist"].items()}
        if sum(1 for b in closed[-21:] if b["t"] in dB) >= 21:
            new_ev += orderflow_evidence(closed, dB, "Binance μέσω Basis")
        else:
            new_ev += orderflow_evidence(closed, okx_taker(), "OKX")
        new_ev += footprint_evidence(bz.get("of"), closed[-1], closed)
        st["bar_t"] = last_t
    new_ev += leverage_evidence(oi, closed, liq)
    W = walls(bids, asks, price) if bids and asks else []
    hi = live["h"] if live else price; lo = live["l"] if live else price
    if W:
        new_ev += book_evidence(W, st["walls"], price, hi, lo, wsrc)
        st["walls"] = {f"{w['side']}:{w['px']}": dict(w, seen=st["walls"].get(f"{w['side']}:{w['px']}", {}).get("seen", 0) + 1) for w in W[:40]}
    keys = {e["key"] for e in st["ev"]}
    for fam, key, d, txt in new_ev:
        if key not in keys: st["ev"].append({"fam": fam, "key": key, "dir": d, "txt": txt, "ts": ts})
    st["ev"] = [e for e in st["ev"] if ts - e["ts"] <= P["window_min"] * 60]

    msgs = []
    # 1. Ισχυρή ένδειξη: επίπεδο + συρροή
    near = at_level(levels(O, closed, st["walls"]), price, closed)
    d, fam = confluence(st["ev"])
    if near and d:
        key, px, lab = near[0]
        ck = f"strong:{key}:{round(px)}:{d}"
        if ts - st["cool"].get(ck, 0) >= COOL["strong"] * 60:
            inv = (f"15m κλείσιμο πάνω από {n(px)} με θετικό delta" if d == "down" else f"15m κλείσιμο κάτω από {n(px)} με αρνητικό delta")
            body = [f"ΙΣΧΥΡΗ ΕΝΔΕΙΞΗ — {lt(now)}", f"Επίπεδο: {lab} {n(px)}",
                    f"Κατεύθυνση που δείχνουν τα στοιχεία: {'ΚΑΘΟΔΙΚΗ' if d=='down' else 'ΑΝΟΔΙΚΗ'} ({len(fam)} ανεξάρτητες οικογένειες)"]
            body += ["• " + e["txt"] for f in ("flow", "lev", "book") for e in fam.get(f, [])]
            body += [f"Ακυρώνεται με: {inv}."]
            msgs.append(body); st["cool"][ck] = ts
    # 2. Ακραία γεγονότα
    ext = []
    if liq:
        for side, v in (("long", liq[0]), ("short", liq[1])):
            if v >= P["liq_extreme_usd"] and ts - st["cool"].get("liq:" + side, 0) >= COOL["liq"] * 60:
                ext.append(f"Κασκάδα ρευστοποιήσεων {side}: ${n(v/1e6,1)} εκ. σε 15′ (OKX) — αναγκαστικές {'πωλήσεις' if side=='long' else 'αγορές'}.")
                st["cool"]["liq:" + side] = ts
    if O and O.get("zg"):
        zg = O["zg"]; h = P["zg_hyst_pct"] / 100
        side = "above" if price > zg * (1 + h) else "below" if price < zg * (1 - h) else st.get("zg_side")
        if st.get("zg_side") and side and side != st["zg_side"] and ts - st["cool"].get("zg", 0) >= COOL["zg"] * 60:
            ext.append(f"Η τιμή πέρασε {'κάτω' if side=='below' else 'πάνω'} από το zero gamma ({n(zg)}). " +
                       ("Το hedging των dealers τώρα ενισχύει τις κινήσεις: μεγαλύτερο εύρος, πιο βίαια σπασίματα."
                        if side == "below" else "Το hedging των dealers τώρα φρενάρει τις κινήσεις: η αγορά τείνει σε εύρος."))
            st["cool"]["zg"] = ts
        st["zg_side"] = side
    if len(closed) >= 20:
        m1 = pct(price, closed[-4]["o"])
        tr = [max(b["h"] - b["l"], abs(b["h"] - a["c"]), abs(b["l"] - a["c"])) for a, b in zip(closed, closed[1:])]
        atr = statistics.fmean(tr[-14:]); rng = closed[-1]["h"] - closed[-1]["l"]
        if ts - st["cool"].get("move", 0) >= COOL["move"] * 60:
            if abs(m1) >= P["move_1h_pct"]:
                ext.append(f"Απότομη κίνηση: {sgn(m1)}% σε μία ώρα."); st["cool"]["move"] = ts
            elif new_bar and atr and rng >= P["range_atr"] * atr:
                ext.append(f"Κερί 15m με εύρος {n(rng)} μονάδες, {n(rng/atr,1)}× το κανονικό (ATR14)."); st["cool"]["move"] = ts
    if ext: msgs.append([f"ΑΚΡΑΙΟ ΓΕΓΟΝΟΣ — {lt(now)}"] + ["• " + e for e in ext])
    st["cool"] = {k: v for k, v in st["cool"].items() if ts - v < 86400}

    ctx = context(price, O, W, wsrc, bz.get("deriv"))
    if tg_ready():
        if first:
            ok = [l[2:].split(" —")[0] for l in LOG if l.startswith("✓")]
            bad = [l[2:] for l in LOG if l.startswith("✗")]
            tg_send("\n".join([f"Φρουρός BTC — ενεργός ({lt(now)})",
                               "Ελέγχω κάθε 5 λεπτά. Γράφω μόνο σε ΙΣΧΥΡΗ ΕΝΔΕΙΞΗ (επίπεδο + 2 ανεξάρτητες οικογένειες στοιχείων) ή σε ΑΚΡΑΙΟ ΓΕΓΟΝΟΣ.", ""]
                              + ctx + ["", f"Πηγές που απάντησαν: {len(set(ok))}" + (f" · δεν απάντησαν: {'; '.join(bad)[:500]}" if bad else " · όλες")] + FOOT),
                    "telegram: έναρξη")
        for m in msgs:
            tg_send("\n".join(m + [""] + ctx + FOOT), "telegram: " + m[0].split(" —")[0].lower())
    st["last"] = {"t": now.isoformat(timespec="seconds"), "price": price, "options": O and {k: O[k] for k in ("net", "zg", "call_wall", "put_wall", "maxpain")},
                  "evidence": [e["txt"] for e in st["ev"]], "sent": [m[0] for m in msgs]}
    save(st)
    print(f"τιμή {price} | στοιχεία 30′: {len(st['ev'])} | μηνύματα: {[m[0] for m in msgs] or '—'}")
    if O: print(f"options: net GEX {O['net']/1e6:.1f}M · zero gamma {O['zg'] and round(O['zg'])} · call wall {O['call_wall']} · put wall {O['put_wall']} · max pain {O['maxpain']}")
    print("\n".join(ctx)); print("\n".join(LOG))

def save(st):
    with open(STATE, "w", encoding="utf-8") as f: json.dump(st, f, ensure_ascii=False)

if __name__ == "__main__":
    main()
