#!/usr/bin/env python3
"""
Simmer Sports Arbitrage Bot v2.0 — Request Optimizado
Compara odds de casas de apuestas (via The Odds API) con Polymarket
y entra cuando hay edge > 8% a favor de Polymarket.

OPTIMIZACIÓN DE REQUESTS:
- Odds se cachean 30 minutos (las odds no cambian en segundos)
- Polymarket se cachea 5 minutos
- ~240 requests/día reales (5 deportes × cada 30min × 24h)
- Plan gratuito (500/mes) dura ~2 días
- Plan Basic ($29/mes, 10k req) dura ~41 días ✅

Deportes: NBA, NHL, MLS, Champions League, MMA/UFC
Estrategia: O/U y resultados directos (SIN spreads)

Usage:
    python sports_trader.py              # Paper mode
    python sports_trader.py --live       # Trades reales
    python sports_trader.py --loop       # Loop continuo
    python sports_trader.py --quota      # Ver requests restantes

Requiere:
    SIMMER_API_KEY    → simmer.markets/dashboard
    ODDS_API_KEY      → the-odds-api.com
"""

import os, sys, json, time, argparse, threading, random
from datetime import datetime, timezone, timedelta
from urllib.request import urlopen, Request
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from http.server import HTTPServer, BaseHTTPRequestHandler

sys.stdout.reconfigure(line_buffering=True)

# =============================================================================
# CONFIG
# =============================================================================

SIMMER_API_KEY   = os.getenv("SIMMER_API_KEY", "")
ODDS_API_KEY     = os.getenv("ODDS_API_KEY", "c2262242e1326d83c6d8b33d1fca2e62")
DAILY_BUDGET     = float(os.getenv("SPORTS_DAILY_BUDGET", "50.0"))
MAX_PER_TRADE    = float(os.getenv("SPORTS_MAX_PER_TRADE", "15.0"))
MIN_EDGE         = float(os.getenv("SPORTS_MIN_EDGE", "0.08"))
LOOP_SEC         = int(os.getenv("SPORTS_LOOP_SEC", "300"))     # 5 min default
ODDS_CACHE_SEC   = int(os.getenv("SPORTS_ODDS_CACHE", "1800"))  # 30 min caché odds
POLY_CACHE_SEC   = int(os.getenv("SPORTS_POLY_CACHE", "300"))   # 5 min caché polymarket
HEALTHCHECK_PORT = int(os.getenv("HEALTHCHECK_PORT", "8080"))
PAPER_JOURNAL    = "sports_journal.json"

SPORTS_CONFIG = {
    "basketball_nba":          {"name": "NBA",      "markets": ["totals"],        "enabled": True, "max_per_trade": 15.0, "priority": 1},
    "icehockey_nhl":           {"name": "NHL",      "markets": ["totals","h2h"],  "enabled": True, "max_per_trade": 12.0, "priority": 2},
    "mma_mixed_martial_arts":  {"name": "MMA/UFC",  "markets": ["h2h"],           "enabled": True, "max_per_trade": 8.0,  "priority": 3},
    "soccer_uefa_champs_league":{"name":"Champions","markets": ["h2h"],           "enabled": True, "max_per_trade": 10.0, "priority": 4},
    "soccer_usa_mls":          {"name": "MLS",      "markets": ["h2h"],           "enabled": True, "max_per_trade": 10.0, "priority": 5},
}

REFERENCE_BOOKS = ["pinnacle", "draftkings", "fanduel", "betmgm"]
SIMMER_BASE = "https://api.simmer.markets"
GAMMA_BASE  = "https://gamma-api.polymarket.com"
ODDS_BASE   = "https://api.the-odds-api.com/v4"

# =============================================================================
# CACHÉ INTELIGENTE
# =============================================================================

class SmartCache:
    def __init__(self):
        self._data = {}
        self.hits = 0
        self.misses = 0

    def get(self, key):
        if key in self._data:
            value, expires_at = self._data[key]
            if datetime.now(timezone.utc) < expires_at:
                self.hits += 1
                return value
            del self._data[key]
        self.misses += 1
        return None

    def set(self, key, value, ttl):
        self._data[key] = (value, datetime.now(timezone.utc) + timedelta(seconds=ttl))

    def hit_rate(self):
        total = self.hits + self.misses
        return round(self.hits / total * 100, 1) if total else 0

    def clear_expired(self):
        now = datetime.now(timezone.utc)
        for k in [k for k,(_, e) in self._data.items() if now >= e]:
            del self._data[k]

cache = SmartCache()
odds_quota = {"remaining": None, "used": None}

# =============================================================================
# HTTP
# =============================================================================

def _http(url, headers=None, retries=3):
    hdrs = {"Content-Type": "application/json", "User-Agent": "SportsBot/2.0"}
    if headers:
        hdrs.update(headers)
    for attempt in range(retries):
        try:
            req = Request(url, headers=hdrs)
            with urlopen(req, timeout=12) as r:
                rh = dict(r.headers)
                rem = rh.get("x-requests-remaining") or rh.get("X-Requests-Remaining")
                used = rh.get("x-requests-used") or rh.get("X-Requests-Used")
                if rem:  odds_quota["remaining"] = int(rem)
                if used: odds_quota["used"] = int(used)
                return json.loads(r.read())
        except Exception as e:
            if attempt == retries - 1: raise
            time.sleep(2 ** attempt)

def simmer_post(path, body):
    import urllib.request
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        f"{SIMMER_BASE}{path}", data=data,
        headers={"Authorization": f"Bearer {SIMMER_API_KEY}", "Content-Type": "application/json"},
        method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())

def gamma_get(path): return _http(f"{GAMMA_BASE}{path}")
def odds_get(path):  return _http(f"{ODDS_BASE}{path}")

# =============================================================================
# HEALTHCHECK
# =============================================================================

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/healthz":
            self.send_response(200); self.end_headers(); self.wfile.write(b"OK")
    def log_message(self, *a): pass

def start_healthcheck():
    try:
        srv = HTTPServer(("0.0.0.0", HEALTHCHECK_PORT), HealthHandler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        print(f"🏥 Healthcheck en http://0.0.0.0:{HEALTHCHECK_PORT}/healthz")
    except Exception as e:
        print(f"⚠️  Healthcheck: {e}")

# =============================================================================
# THE ODDS API — con caché 30 min
# =============================================================================

def get_odds_cached(sport_key, markets):
    key = f"odds:{sport_key}"
    cached = cache.get(key)
    if cached is not None:
        return cached, True
    try:
        params = urlencode({
            "apiKey": ODDS_API_KEY, "regions": "us,eu",
            "markets": ",".join(markets), "oddsFormat": "decimal",
            "bookmakers": ",".join(REFERENCE_BOOKS),
        })
        data = odds_get(f"/sports/{sport_key}/odds?{params}")
        # Fix: filtrar elementos que no sean dicts (a veces la API devuelve strings)
        if isinstance(data, list):
            data = [g for g in data if isinstance(g, dict)]
        cache.set(key, data, ODDS_CACHE_SEC)
        return data, False
    except Exception as e:
        print(f"  ⚠️  Odds API ({sport_key}): {e}")
        return [], False

def decimal_to_prob(d):
    return 1/d if d > 1 else 0

def get_best_prob(game, outcome_name, market_key):
    best_prob = best_book = None
    for bm in game.get("bookmakers", []):
        bk = bm.get("key","")
        for mkt in bm.get("markets", []):
            if mkt.get("key") != market_key: continue
            for oc in mkt.get("outcomes", []):
                if oc.get("name","").lower() == outcome_name.lower():
                    p = decimal_to_prob(oc.get("price", 1))
                    if best_prob is None or bk == "pinnacle":
                        best_prob, best_book = p, bk
    return best_prob, best_book

def get_quota():
    try:
        params = urlencode({"apiKey": ODDS_API_KEY})
        odds_get(f"/sports?{params}")
        return odds_quota.get("remaining")
    except:
        return None

# =============================================================================
# POLYMARKET — con caché 5 min
# =============================================================================

def search_poly_cached():
    key = "poly:markets"
    cached = cache.get(key)
    if cached is not None:
        return cached, True
    markets = []
    seen = set()
    for term in ["NBA NHL UFC", "win over under"]:
        try:
            params = urlencode({"search": term, "active": "true", "closed": "false", "limit": 100})
            data = gamma_get(f"/markets?{params}")
            for m in (data if isinstance(data, list) else data.get("markets", [])):
                mid = m.get("id") or m.get("conditionId")
                if mid and mid not in seen:
                    seen.add(mid)
                    markets.append(m)
        except:
            pass
        time.sleep(0.2)
    cache.set(key, markets, POLY_CACHE_SEC)
    return markets, False

def parse_market(m):
    if not isinstance(m, dict): return None  # Fix: ignorar no-dicts
    question = m.get("question","")
    end_date = m.get("endDate") or m.get("end_date_iso")
    yes_price = no_price = None
    for t in m.get("tokens", []):
        name = (t.get("outcome") or "").upper()
        price = float(t.get("price") or 0)
        if name == "YES": yes_price = price
        elif name == "NO": no_price = price
    if yes_price is None:
        for o in m.get("outcomes", []):
            name = (o.get("value") or o.get("label") or "").upper()
            price = float(o.get("price") or 0)
            if name == "YES": yes_price = price
            elif name == "NO": no_price = price
    seconds_left = None
    if end_date:
        try:
            end_dt = datetime.fromisoformat(end_date.replace("Z","+00:00"))
            if end_dt.tzinfo is None: end_dt = end_dt.replace(tzinfo=timezone.utc)
            seconds_left = (end_dt - datetime.now(timezone.utc)).total_seconds()
        except: pass
    return {"id": m.get("id") or m.get("conditionId"), "question": question,
            "yes_price": yes_price, "no_price": no_price,
            "seconds_left": seconds_left, "volume": float(m.get("volume") or 0)}

# =============================================================================
# MATCHING & EDGE
# =============================================================================

_ALIASES = {
    "golden state warriors":"warriors","los angeles lakers":"lakers",
    "los angeles clippers":"clippers","oklahoma city thunder":"thunder",
    "new york knicks":"knicks","boston celtics":"celtics","miami heat":"heat",
    "chicago bulls":"bulls","philadelphia 76ers":"76ers","milwaukee bucks":"bucks",
    "toronto raptors":"raptors","denver nuggets":"nuggets","phoenix suns":"suns",
    "sacramento kings":"kings","memphis grizzlies":"grizzlies",
    "minnesota timberwolves":"timberwolves","cleveland cavaliers":"cavaliers",
    "indiana pacers":"pacers","orlando magic":"magic","atlanta hawks":"hawks",
    "washington wizards":"wizards","detroit pistons":"pistons",
    "san antonio spurs":"spurs","portland trail blazers":"trail blazers",
    "utah jazz":"jazz","houston rockets":"rockets","dallas mavericks":"mavericks",
    "brooklyn nets":"nets","toronto maple leafs":"maple leafs",
    "minnesota wild":"wild","st. louis blues":"blues","new jersey devils":"devils",
}

def norm(name):
    n = name.lower().strip()
    return _ALIASES.get(n, n)

def match_game(question, games):
    q = question.lower()
    for g in games:
        if not isinstance(g, dict): continue  # Fix: ignorar elementos que no sean dicts
        h, a = norm(g.get("home_team","")), norm(g.get("away_team",""))
        if not h or not a: continue
        if (h in q or any(w in q for w in h.split() if len(w)>4)) and \
           (a in q or any(w in q for w in a.split() if len(w)>4)):
            return g
    return None

def market_type(q):
    ql = q.lower()
    if "o/u" in ql or "over" in ql or "under" in ql: return "totals"
    if "spread" in ql: return "spreads"
    return "h2h"

# =============================================================================
# PAPER JOURNAL
# =============================================================================

def load_journal():
    if os.path.exists(PAPER_JOURNAL):
        with open(PAPER_JOURNAL) as f: return json.load(f)
    return {"capital":100.0,"start_capital":100.0,"trades":[],"daily":{},"today":""}

def save_journal(j):
    with open(PAPER_JOURNAL,"w") as f: json.dump(j, f, indent=2)

def print_summary(j):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    d = j.get("daily",{}).get(today, {"pnl":0,"wins":0,"losses":0})
    trades = j.get("trades",[])
    wins = sum(1 for t in trades if t.get("result")=="win")
    losses = sum(1 for t in trades if t.get("result")=="loss")
    wr = wins/len(trades)*100 if trades else 0
    pnl = j["capital"] - j["start_capital"]
    print(f"\n📒 SPORTS JOURNAL — {today}")
    print(f"   Capital: ${j['capital']:.2f}  |  P&L total: ${pnl:+.2f}")
    print(f"   Hoy: ${d['pnl']:+.2f}  |  {d.get('wins',0)}W/{d.get('losses',0)}L")
    print(f"   Win rate: {wr:.0f}%  ({wins}W/{losses}L total)")
    print(f"   🔋 Caché hit rate: {cache.hit_rate()}%  |  Requests reales: {cache.misses}")
    if odds_quota.get("remaining") is not None:
        print(f"   📡 Odds API: {odds_quota['remaining']} requests restantes este mes")
    print()

def paper_trade(j, info, side, price, amount, book_prob):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if today not in j["daily"]:
        j["daily"][today] = {"pnl":0,"wins":0,"losses":0}
    won = random.random() < min(book_prob * 1.02, 0.95)
    fee = amount * 0.02
    profit = (amount - fee) / price - amount if won else -amount
    result = "win" if won else "loss"
    emoji = "✅" if won else "❌"
    j["capital"] += profit
    j["daily"][today]["pnl"] += profit
    j["daily"][today]["wins" if won else "losses"] += 1
    j["trades"].append({"ts": datetime.now(timezone.utc).isoformat(), "market": info["question"][:60],
        "side": side, "price": price, "amount": amount, "book_prob": book_prob,
        "edge": book_prob - price, "profit": profit, "result": result})
    save_journal(j)
    print(f"  {emoji} PAPER: {side} @{price:.3f} | ${amount:.2f} → ${profit:+.2f} | Capital: ${j['capital']:.2f}")
    return profit

def real_trade(market_id, side, price, amount):
    try:
        shares = round(amount / price, 1)
        if shares < 5:
            print(f"  ⚠️  Shares {shares} < 5 mínimo"); return False
        r = simmer_post("/v1/trade", {"marketId": market_id, "outcome": side,
                                       "amount": amount, "source": "sports-arb-v2"})
        print(f"  🎯 TRADE: {side} ${amount:.2f} | tx: {r.get('txHash','N/A')[:16]}...")
        return True
    except Exception as e:
        print(f"  ❌ Error: {e}"); return False

# =============================================================================
# CYCLE
# =============================================================================

def run_cycle(live_mode, journal, daily_spent):
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    if daily_spent["date"] != today:
        daily_spent = {"date": today, "amount": 0.0}

    budget = DAILY_BUDGET - daily_spent["amount"]
    mode = "LIVE" if live_mode else "PAPER"

    print(f"\n{'='*60}")
    print(f"⚽ {now.strftime('%H:%M:%S')} UTC | {mode} | Budget: ${budget:.2f}  |  Caché: {cache.hit_rate()}%")
    if odds_quota.get("remaining") is not None:
        print(f"   📡 Odds restantes: {odds_quota['remaining']}")
    print(f"{'='*60}")

    if budget <= 0:
        print("⛔ Budget agotado."); return daily_spent

    cache.clear_expired()

    # Polymarket (cacheado 5min)
    print(f"\n🔍 Polymarket...")
    poly_markets, pc = search_poly_cached()
    print(f"   {len(poly_markets)} mercados ({'📦 caché' if pc else '🌐 fresh'})")

    opportunities = []
    for sport_key, cfg in sorted(SPORTS_CONFIG.items(), key=lambda x: x[1]["priority"]):
        if not cfg["enabled"]: continue
        games, fc = get_odds_cached(sport_key, cfg["markets"])
        lbl = "📦" if fc else "🌐"
        if not games:
            print(f"   {lbl} {cfg['name']}: sin datos"); continue
        print(f"   {lbl} {cfg['name']}: {len(games)} partidos")

        for pm in poly_markets:
            info = parse_market(pm)
            if not info: continue  # Fix: parse_market puede devolver None
            if not info["yes_price"] or not info["no_price"]: continue
            sl = info["seconds_left"]
            if sl and (sl < 300 or sl > 86400 * 5): continue
            mt = market_type(info["question"])
            if mt == "spreads": continue

            g = match_game(info["question"], games)
            if not g: continue

            for side in ["YES", "NO"]:
                price = info["yes_price"] if side == "YES" else info["no_price"]
                if not price or price < 0.1 or price > 0.9: continue

                if mt == "totals":
                    outcome = "Over" if side == "YES" else "Under"
                    bp, bn = get_best_prob(g, outcome, "totals")
                else:
                    team = g.get("home_team","") if side == "YES" else g.get("away_team","")
                    bp, bn = get_best_prob(g, team, "h2h")

                if not bp: continue
                edge = bp - price
                if edge >= MIN_EDGE:
                    opportunities.append({"market": info, "side": side, "poly_price": price,
                        "book_prob": bp, "book_name": bn or "?", "edge": edge,
                        "sport": cfg["name"], "mtype": mt,
                        "max_bet": min(cfg["max_per_trade"], MAX_PER_TRADE)})

    opportunities.sort(key=lambda x: x["edge"], reverse=True)

    if not opportunities:
        print(f"\n⏸️  Sin oportunidades (edge >{MIN_EDGE*100:.0f}%) en este ciclo")
        return daily_spent

    print(f"\n🎯 {len(opportunities)} oportunidades!")
    done = 0
    for opp in opportunities[:3]:
        if daily_spent["amount"] >= DAILY_BUDGET: break
        m, side, price, bp, edge = opp["market"], opp["side"], opp["poly_price"], opp["book_prob"], opp["edge"]
        kelly = max(0.01, min((bp - price) / (1 - price) * 0.25, 0.12))
        bet = round(max(1.0, min(budget * kelly, opp["max_bet"], budget)), 2)
        hrs = (m["seconds_left"] or 0) / 3600
        tstr = f"{hrs:.1f}h" if hrs >= 1 else f"{(m['seconds_left'] or 0)/60:.0f}min"

        print(f"\n  🏆 [{opp['sport']}] {m['question'][:52]}")
        print(f"     {side} @{price:.3f} poly  |  book: {bp:.3f} ({opp['book_name']})")
        print(f"     Edge: {edge*100:.1f}%  |  Apuesta: ${bet:.2f}  |  Cierra: {tstr}")

        if live_mode:
            if real_trade(m["id"], side, price, bet):
                daily_spent["amount"] += bet; done += 1
        else:
            paper_trade(journal, m, side, price, bet, bp)
            daily_spent["amount"] += bet; done += 1

    print(f"\n📊 {done} trades | Gastado: ${daily_spent['amount']:.2f} | Requests ahorrados: {cache.hits}")
    return daily_spent

# =============================================================================
# MAIN
# =============================================================================

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live",  action="store_true")
    parser.add_argument("--loop",  action="store_true")
    parser.add_argument("--scan",  action="store_true")
    parser.add_argument("--quota", action="store_true")
    args = parser.parse_args()

    live_mode = args.live and not args.scan

    if args.quota:
        print("📡 Consultando Odds API quota...")
        r = get_quota()
        print(f"   Restantes: {r}" if r else "   No disponible")
        return

    if not SIMMER_API_KEY and live_mode:
        print("❌ SIMMER_API_KEY no configurada."); sys.exit(1)

    enabled = [c["name"] for c in SPORTS_CONFIG.values() if c["enabled"]]
    n_sports = len(enabled)
    req_per_day = n_sports * (86400 / ODDS_CACHE_SEC)   # 1 req/deporte cada 30min

    print(f"\n{'='*60}")
    print(f"⚽ SPORTS ARBITRAGE BOT v2.0")
    print(f"   Modo:     {'🔴 LIVE' if live_mode else '📝 PAPER'}")
    print(f"   Deportes: {', '.join(enabled)}")
    print(f"   Edge mín: {MIN_EDGE*100:.0f}%  |  Max/trade: ${MAX_PER_TRADE:.0f}")
    print(f"   Loop:     cada {LOOP_SEC}s  |  Caché odds: {ODDS_CACHE_SEC//60}min")
    print(f"")
    print(f"   📡 REQUESTS PROYECTADOS:")
    print(f"      Sin caché:  ~{n_sports * 86400/LOOP_SEC:.0f}/día  ❌")
    print(f"      Con caché:  ~{req_per_day:.0f}/día  ✅")
    print(f"      Free (500/mes):   ~{500/req_per_day:.1f} días")
    print(f"      Basic ($29/mes):  ~{10000/req_per_day:.0f} días ✅")
    print(f"{'='*60}")

    start_healthcheck()
    journal = load_journal()
    journal["today"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    print_summary(journal)

    daily_spent = {"date": journal["today"], "amount": 0.0}

    if args.loop:
        print(f"🔄 Loop cada {LOOP_SEC}s. Ctrl+C para parar.\n")
        cycle = 0
        while True:
            try:
                daily_spent = run_cycle(live_mode, journal, daily_spent)
                cycle += 1
                if cycle % 12 == 0: print_summary(journal)
                time.sleep(LOOP_SEC)
            except KeyboardInterrupt:
                print("\n👋 Detenido"); print_summary(journal); break
            except Exception as e:
                print(f"⚠️  Error: {e}"); time.sleep(30)
    else:
        run_cycle(live_mode, journal, daily_spent)
        print_summary(journal)

if __name__ == "__main__":
    main()
