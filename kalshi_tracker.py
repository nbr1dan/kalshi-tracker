#!/usr/bin/env python3
"""
Kalshi Top-Trader Tracker  (Still Systems)

Runs on GitHub Actions every ~5 minutes:
  1. Once a day, pulls Kalshi's public social leaderboards, computes each
     trader's win% from their closed positions, and keeps the top 5
     (minimum 50 resolved positions).
  2. Every run, checks those 5 traders' public trades and sends a Discord
     webhook alert for each new one.
  3. Writes a phone-friendly dashboard to docs/index.html (GitHub Pages).

Modes (env MODE):  run (default) | rerank (force re-rank now) | probe
  probe = dumps raw Kalshi responses into probe/ and sends a Discord test
          message, so endpoint/field changes can be diagnosed from a phone.

NOTE: Kalshi's social endpoints are NOT an official, documented API. They are
what kalshi.com itself loads. Endpoint paths below are configurable and the
parser is deliberately tolerant of field-name changes.
"""
import datetime as dt
import html
import json
import os
import time
from pathlib import Path

import requests

# ----------------------------- config ---------------------------------------
BASE = os.getenv("KALSHI_SOCIAL_BASE", "https://api.elections.kalshi.com/v1/social").rstrip("/")
WEBHOOK = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
MODE = (os.getenv("MODE") or "run").strip().lower()
PROBE_NICK = (os.getenv("PROBE_NICKNAME") or "").strip()

TOP_N = int(os.getenv("TOP_N", "5"))
MIN_RESOLVED = int(os.getenv("MIN_RESOLVED", "50"))
POOL_SIZE = int(os.getenv("POOL_SIZE", "100"))         # leaderboard names pulled per board
MAX_SCORED = int(os.getenv("MAX_SCORED", "40"))         # visible traders scored per re-rank
RERANK_HOURS = float(os.getenv("RERANK_HOURS", "24"))
MAX_TRADE_AGE_MIN = int(os.getenv("MAX_TRADE_AGE_MIN", "180"))  # never alert on stale trades
REQUEST_DELAY = float(os.getenv("REQUEST_DELAY", "1.2"))  # be polite; Kalshi returns 429 fast

# Many top traders lock trades behind Kalshi's "Inner Circle", so we search wide
# (profit / volume / markets-traded boards, several time periods) for public ones.
LEADERBOARD_QUERIES = [
    {"metric_name": m, "time_period": t}
    for t in ("all_time", "monthly", "weekly")
    for m in ("projected_pnl", "num_markets_traded", "volume")
]
MAX_VISIBILITY_CHECKS = int(os.getenv("MAX_VISIBILITY_CHECKS", "300"))
# Kalshi's official public market API: used to look up how markets settled.
MARKETS_BASE = "https://api.elections.kalshi.com/trade-api/v2"
TRADE_HISTORY_PAGES = int(os.getenv("TRADE_HISTORY_PAGES", "10"))   # 100 trades/page
HIDDEN_STATES = {"hidden", "private", "restricted"}
TRADES_PATH = "trades"
PROFILE_URL = "https://kalshi.com/profile/{}"

STATE_FILE = Path("state.json")
DOCS_DIR = Path("docs")
PROBE_DIR = Path("probe")

# ----------------------------- helpers --------------------------------------
S = requests.Session()
S.headers.update({
    "User-Agent": "Mozilla/5.0 (compatible; kalshi-tracker/1.0)",
    "Accept": "application/json",
})
LOG = []


def log(msg):
    line = f"[{now().strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    LOG.append(line)


def now():
    return dt.datetime.now(dt.timezone.utc)


def get_raw(path, params=None, tries=4):
    """Return (status_code, json_or_text). status 0 = network failure."""
    url = path if path.startswith("http") else f"{BASE}/{path}"
    status, body = 0, None
    for attempt in range(tries):
        try:
            r = S.get(url, params=params, timeout=20)
        except requests.RequestException as e:
            log(f"network error {path}: {e}")
            time.sleep(2 * (attempt + 1))
            continue
        status = r.status_code
        if status == 429:
            wait = r.headers.get("Retry-After")
            wait = float(wait) if wait and wait.replace(".", "").isdigit() else 5 * (attempt + 1)
            log(f"429 rate-limited on {path}, waiting {wait:.0f}s")
            time.sleep(min(wait, 60))
            continue
        try:
            body = r.json()
        except ValueError:
            body = r.text[:500]
        break
    time.sleep(REQUEST_DELAY)
    return status, body


def get(path, params=None):
    status, body = get_raw(path, params)
    if status != 200 or not isinstance(body, (dict, list)):
        if status not in (0, 429):
            log(f"HTTP {status} {path} {params or ''}")
        return None
    return body


def find_list(obj, key):
    """Find the first list of dicts (anywhere in obj) whose items contain `key`."""
    if isinstance(obj, list):
        if obj and isinstance(obj[0], dict) and any(key in d for d in obj[:5] if isinstance(d, dict)):
            return obj
        for v in obj:
            if isinstance(v, (dict, list)):
                hit = find_list(v, key)
                if hit is not None:
                    return hit
    elif isinstance(obj, dict):
        for v in obj.values():
            hit = find_list(v, key)
            if hit is not None:
                return hit
    return None


def paged(path, params, item_key, max_pages=10):
    """Follow cursor pagination. Returns list, or None if the first call failed."""
    items, cursor = [], None
    for page in range(max_pages):
        p = dict(params)
        if cursor:
            p["cursor"] = cursor
        data = get(path, p)
        if data is None:
            return items if page else None
        batch = find_list(data, item_key) or []
        items.extend(batch)
        cursor = data.get("cursor") or data.get("next_cursor") if isinstance(data, dict) else None
        if not cursor or not batch:
            break
    return items


def num(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def parse_time(s):
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except ValueError:
            log("state.json unreadable, starting fresh")
    return {"tracked": [], "seen": {}, "alerts": [], "last_rerank": None,
            "positions_endpoint": None, "status": "", "ranking_mode": ""}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=1, sort_keys=True))


# ----------------------------- ranking --------------------------------------
def leaderboard_pool():
    pool, order = {}, 0
    for q in LEADERBOARD_QUERIES:
        data = get("leaderboard", {**q, "limit": POOL_SIZE})
        rows = find_list(data, "nickname") or [] if data is not None else []
        for r in rows:
            nick = (r.get("nickname") or "").strip()
            if not nick or r.get("is_anonymous"):
                continue
            if nick not in pool:
                pool[nick] = {"nickname": nick, "lb_rank": r.get("rank"),
                              "lb_metric": q["metric_name"], "order": order}
                order += 1
    log(f"leaderboard pool: {len(pool)} public traders")
    return list(pool.values())


def yes_price_c(t):
    """Trade price in cents of YES (Kalshi trade prints are YES-priced)."""
    if t.get("price_dollars") not in (None, ""):
        return num(t["price_dollars"]) * 100
    p = num(t.get("price"))
    return p * 100 if 0 < p <= 1 else p


def contracts(t):
    return num(t.get("count_fp") or t.get("count"))


def is_maker(t, nick):
    return ((t.get("taker_nickname") or "").lower() != nick.lower()
            and (t.get("maker_nickname") or "").lower() == nick.lower())


def yes_dir(t, nick):
    """+1 if the trade added YES exposure for this trader, -1 if it reduced it."""
    side = (t.get("taker_side") or "").lower()
    action = (t.get("taker_action") or "buy").lower()
    d = 1 if (side == "yes") == (action == "buy") else -1
    return -d if is_maker(t, nick) else d


def market_results(tickers, state):
    """ticker -> 'yes'/'no' for settled markets (cached in state)."""
    cache = state.setdefault("market_results", {})
    need = [t for t in tickers if t not in cache]
    for i in range(0, len(need), 40):
        chunk = need[i:i + 40]
        data = get(f"{MARKETS_BASE}/markets", {"tickers": ",".join(chunk), "limit": 100})
        for m in (find_list(data, "ticker") or []) if data else []:
            res = str(m.get("result") or "").lower()
            if res in ("yes", "no"):
                cache[m["ticker"]] = res
    if len(cache) > 8000:                       # keep state.json small
        state["market_results"] = cache = dict(list(cache.items())[-6000:])
    return cache


def score_trader(nick, state):
    """Win% = share of settled markets (in recent public trades) the trader profited on."""
    trades = paged(TRADES_PATH, {"nickname": nick, "limit": 100}, "trade_id", TRADE_HISTORY_PAGES)
    if not trades:
        return None
    by_mkt = {}
    for t in trades:
        if t.get("ticker"):
            by_mkt.setdefault(t["ticker"], []).append(t)
    if len(trades) >= TRADE_HISTORY_PAGES * 100:
        # History was cut off: the oldest market may be missing earlier fills.
        oldest = min(trades, key=lambda t: t.get("create_date") or "")
        by_mkt.pop(oldest.get("ticker"), None)
    results = market_results(list(by_mkt), state)
    wins = losses = 0
    pnl = 0.0
    for tk, rows in by_mkt.items():
        res = results.get(tk)
        if res not in ("yes", "no"):
            continue                            # still open
        settle = 100 if res == "yes" else 0
        p = sum(yes_dir(t, nick) * contracts(t) * (settle - yes_price_c(t)) for t in rows) / 100
        pnl += p
        if p > 0.005:
            wins += 1
        elif p < -0.005:
            losses += 1
    resolved = wins + losses
    return {"wins": wins, "losses": losses, "resolved": resolved,
            "win_pct": round(100 * wins / resolved, 1) if resolved else 0.0,
            "sample_pnl": round(pnl, 2), "markets_seen": len(by_mkt), "trades_seen": len(trades)}


def rerank(state):
    pool = leaderboard_pool()
    if not pool:
        state["status"] = "Could not read Kalshi leaderboard (run probe mode)."
        return False

    # Only traders whose trades are public can be alerted on.
    visible, hidden = [], 0
    for c in pool[:MAX_VISIBILITY_CHECKS]:
        trades = recent_trades(c["nickname"])
        if trades is None:
            hidden += 1
            continue
        visible.append(c)
        if len(visible) >= MAX_SCORED:
            break
    log(f"{len(visible)} traders with public trades, {hidden} hidden/unreachable")
    if not visible:
        state["status"] = "Every leaderboard trader checked has hidden trades."
        return False

    scored = []
    for c in visible:
        stats = score_trader(c["nickname"], state)
        if stats is None:
            continue
        c.update(stats)
        scored.append(c)
        log(f"  {c['nickname']}: {c['win_pct']}% over {c['resolved']} settled markets")

    eligible = [c for c in scored if c["resolved"] >= MIN_RESOLVED]
    eligible.sort(key=lambda c: (c["win_pct"], c["resolved"]), reverse=True)
    others = sorted((c for c in visible if c not in eligible),
                    key=lambda c: ((c.get("resolved") or 0) >= 10, c.get("win_pct") or 0, -c["order"]),
                    reverse=True)
    top = (eligible + others)[:TOP_N]
    short = len(top) - min(len(eligible), len(top))
    state["ranking_mode"] = f"Win% on settled markets (min {MIN_RESOLVED})"
    if short:
        state["ranking_mode"] += f" · {short} pick(s) have fewer settled markets"
    if len(visible) < TOP_N:
        state["ranking_mode"] += f" · only {len(visible)} traders with public trades found"
    log(state["ranking_mode"])

    state["tracked"] = [{k: c.get(k) for k in
                         ("nickname", "win_pct", "wins", "losses", "resolved", "sample_pnl",
                          "lb_rank", "lb_metric")}
                        for c in top]
    state["last_rerank"] = now().isoformat(timespec="minutes")
    state["status"] = ""
    log("tracking: " + ", ".join(t["nickname"] for t in state["tracked"]))
    return True


def rerank_due(state):
    if (MODE == "rerank" or state.get("needs_rerank")
            or not state.get("tracked") or not state.get("last_rerank")):
        return True
    last = parse_time(state["last_rerank"])
    return last is None or now() - last > dt.timedelta(hours=RERANK_HOURS)


# ----------------------------- trades & alerts ------------------------------
def recent_trades(nick):
    """List of the trader's public trades, or None if hidden/unreachable."""
    data = get(TRADES_PATH, {"nickname": nick, "limit": 50})
    if data is None:
        return None
    if isinstance(data, dict) and str(data.get("visibility_state", "")).lower() in HIDDEN_STATES:
        return None
    rows = find_list(data, "trade_id") or []
    lo = nick.lower()
    out = []
    for t in rows:
        names = {(t.get("maker_nickname") or "").lower(), (t.get("taker_nickname") or "").lower()}
        if names == {""} or lo in names:
            out.append(t)
    return out


def trader_side(t, nick):
    """(SIDE, ACTION) from this trader's point of view."""
    side = (t.get("taker_side") or "").lower()
    action = (t.get("taker_action") or "buy").lower()
    if is_maker(t, nick):          # maker took the opposite side (selling YES == buying NO)
        side = {"yes": "no", "no": "yes"}.get(side, side)
    return side.upper() or "?", action.upper()


def market_title(ticker, cache={}):
    if ticker not in cache:
        data = get(f"{MARKETS_BASE}/markets/{ticker}") or {}
        m = data.get("market", {}) if isinstance(data, dict) else {}
        title = m.get("title") or ""
        sub = m.get("yes_sub_title") or m.get("subtitle") or ""
        cache[ticker] = (f"{title} — {sub}" if sub and sub not in title else title) or ticker
    return cache[ticker]


def send_discord(payload):
    if not WEBHOOK:
        log("DISCORD_WEBHOOK_URL not set, skipping alert")
        return False
    for _ in range(3):
        try:
            r = requests.post(WEBHOOK, json=payload, timeout=15)
        except requests.RequestException as e:
            log(f"discord error: {e}")
            return False
        if r.status_code == 429:
            time.sleep(num(r.json().get("retry_after"), 2) + 0.5)
            continue
        if r.status_code >= 300:
            log(f"discord HTTP {r.status_code}: {r.text[:200]}")
            return False
        time.sleep(1)
        return True
    return False


def alert(trader, group):
    """One alert for a burst of fills on the same market/side/action."""
    nick = trader["nickname"]
    t0 = group[0]
    side, action = trader_side(t0, nick)
    qty = sum(contracts(t) for t in group)
    side_px = [(yes_price_c(t) if side == "YES" else 100 - yes_price_c(t)) for t in group]
    avg = sum(p * contracts(t) for p, t in zip(side_px, group)) / qty if qty else 0
    cost = avg * qty / 100
    market = market_title(t0.get("ticker") or "?")
    wr = (f"{trader['win_pct']}% ({trader['wins']}W-{trader['losses']}L)"
          if trader.get("resolved") else "n/a")
    fills = f" · {len(group)} fills" if len(group) > 1 else ""
    embed = {
        "title": f"{nick}: {action} {side} @ {avg:.1f}¢",
        "description": market[:250],
        "url": PROFILE_URL.format(nick),
        "color": 0x2ECC71 if action == "BUY" else 0xE74C3C,
        "fields": [
            {"name": "Contracts", "value": f"{qty:,.0f}{fills}", "inline": True},
            {"name": "≈ Cost", "value": f"${cost:,.2f}", "inline": True},
            {"name": "Trader win%", "value": wr, "inline": True},
            {"name": "Ticker", "value": f"`{t0.get('ticker') or '?'}`", "inline": False},
        ],
        "footer": {"text": "Kalshi tracker · alert only, not advice"},
        "timestamp": group[-1].get("create_date") or now().isoformat(),
    }
    ok = send_discord({"username": "Kalshi Tracker", "embeds": [embed]})
    return ok, {"time": group[-1].get("create_date") or now().isoformat(timespec="minutes"),
                "nickname": nick, "action": action, "side": side, "price": round(avg, 1),
                "count": round(qty), "market": market[:120], "ticker": t0.get("ticker")}


def check_trades(state):
    seen = state.setdefault("seen", {})
    reached = 0
    for trader in state.get("tracked", []):
        nick = trader["nickname"]
        trades = recent_trades(nick)
        if trades is None:
            continue
        reached += 1
        ids = [str(t.get("trade_id")) for t in trades if t.get("trade_id")]
        if nick not in seen:                       # first sighting: seed, don't spam
            seen[nick] = ids[:300]
            log(f"seeded {nick} with {len(ids)} existing trades")
            continue
        known = set(seen[nick])
        new = [t for t in trades if str(t.get("trade_id")) not in known]
        new.sort(key=lambda t: t.get("create_date") or "")
        new = [t for t in new if not (parse_time(t.get("create_date")) and
               now() - parse_time(t.get("create_date")) > dt.timedelta(minutes=MAX_TRADE_AGE_MIN))]
        groups = {}
        for t in new:
            key = (t.get("ticker"),) + trader_side(t, nick)
            groups.setdefault(key, []).append(t)
        for group in groups.values():
            ok, rec = alert(trader, group)
            if ok:
                state["alerts"].insert(0, rec)
                log(f"ALERT {nick} {rec['action']} {rec['side']} {rec['ticker']} x{rec['count']}")
        seen[nick] = (ids + seen[nick])[:300] if new else seen[nick]
    state["alerts"] = state.get("alerts", [])[:40]
    tracked_names = {t["nickname"] for t in state.get("tracked", [])}
    state["seen"] = {k: v for k, v in seen.items() if k in tracked_names}
    # If no tracked trader's trades are readable (all went hidden), re-rank next run.
    state["needs_rerank"] = bool(state.get("tracked")) and reached == 0
    if state["needs_rerank"]:
        state["status"] = "Tracked traders' trades are hidden or unreachable; re-ranking next run."


# ----------------------------- dashboard ------------------------------------
def write_dashboard(state):
    DOCS_DIR.mkdir(exist_ok=True)
    e = html.escape
    cards = []
    for i, t in enumerate(state.get("tracked", []), 1):
        wr = f"{t['win_pct']}%" if t.get("resolved") else "—"
        rec = (f"{t.get('wins') or 0}W · {t.get('losses') or 0}L · {t.get('resolved') or 0} resolved"
               if t.get("resolved") else f"Leaderboard #{t.get('lb_rank') or '?'} (profit)")
        cards.append(
            f'<a class="card" href="{e(PROFILE_URL.format(t["nickname"]))}">'
            f'<div class="rank">#{i}</div><div class="who"><b>{e(t["nickname"])}</b>'
            f'<span>{e(rec)}</span></div><div class="wr">{e(wr)}</div></a>')
    alerts = []
    for a in state.get("alerts", [])[:25]:
        cls = "buy" if a.get("action") == "BUY" else "sell"
        alerts.append(
            f'<li><div><b>{e(a["nickname"])}</b> <span class="{cls}">{e(a["action"])} {e(a["side"])}</span>'
            f' {e(str(a["price"]))}¢ × {e(format(num(a.get("count")), "g"))}</div>'
            f'<div class="mkt">{e(a["market"])}</div><div class="ts">{e(str(a["time"])[:16].replace("T", " "))} UTC</div></li>')
    status = state.get("status") or ""
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="300"><title>Kalshi Tracker</title>
<style>
:root{{--bg:#0e1116;--card:#171b22;--line:#262c36;--fg:#e8ecf1;--mut:#8a94a3;--acc:#3ddc97;--red:#ff6b6b}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.4 -apple-system,system-ui,Segoe UI,Roboto,sans-serif;padding:16px;max-width:560px;margin:auto}}
h1{{font-size:20px;margin:4px 0 2px}}h2{{font-size:13px;letter-spacing:.08em;text-transform:uppercase;color:var(--mut);margin:22px 0 8px}}
.sub{{color:var(--mut);font-size:12px}}.warn{{background:#3a2a12;color:#ffd28a;padding:10px;border-radius:10px;font-size:13px;margin-top:10px}}
.card{{display:flex;align-items:center;gap:12px;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px;margin-bottom:8px;color:inherit;text-decoration:none}}
.rank{{color:var(--mut);width:26px}}.who{{flex:1;display:flex;flex-direction:column}}.who span{{color:var(--mut);font-size:12px}}
.wr{{font-size:20px;font-weight:700;color:var(--acc)}}ul{{list-style:none;padding:0;margin:0}}
li{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:10px 12px;margin-bottom:8px}}
.buy{{color:var(--acc);font-weight:600}}.sell{{color:var(--red);font-weight:600}}.mkt{{font-size:13px;margin-top:2px}}.ts{{color:var(--mut);font-size:11px;margin-top:2px}}
</style></head><body>
<h1>Kalshi Tracker</h1>
<div class="sub">{e(state.get("ranking_mode") or "Not ranked yet")}<br>
Re-ranked: {e(str(state.get("last_rerank") or "—").replace("T", " "))} UTC · checked ~every 5 min · last heartbeat {e(str(state.get("heartbeat") or "—"))}</div>
{f'<div class="warn">{e(status)}</div>' if status else ''}
<h2>Top {TOP_N} by win%</h2>{''.join(cards) or '<div class="sub">No traders yet. The first run fills this in.</div>'}
<h2>Recent alerts</h2><ul>{''.join(alerts) or '<li class="sub">No trades yet.</li>'}</ul>
<p class="sub">Alerts only. Not financial advice.</p>
</body></html>"""
    (DOCS_DIR / "index.html").write_text(page)


# ----------------------------- probe ----------------------------------------
def shrink(obj, n=3):
    if isinstance(obj, list):
        return [shrink(x, n) for x in obj[:n]] + ([f"... {len(obj) - n} more"] if len(obj) > n else [])
    if isinstance(obj, dict):
        return {k: shrink(v, n) for k, v in obj.items()}
    return obj


def probe(state):
    PROBE_DIR.mkdir(exist_ok=True)
    report = {"base": BASE, "time": now().isoformat(timespec="seconds"), "calls": []}

    def call(label, path, params):
        status, body = get_raw(path, params)
        report["calls"].append({"label": label, "path": path, "params": params,
                                "status": status, "sample": shrink(body)})
        return status, body

    # 1) Find a trader whose trades are public (most top traders hide them).
    nick, checked, hidden, states, visible_empty = PROBE_NICK, 0, 0, {}, ""
    call("leaderboard", "leaderboard", {**LEADERBOARD_QUERIES[0], "limit": POOL_SIZE})
    if not nick:
        names = [c["nickname"] for c in leaderboard_pool()]
        report["pool_size"] = len(names)
        for n in names[:150]:
            checked += 1
            s, b = get_raw(TRADES_PATH, {"nickname": n, "limit": 5})
            vis = str(b.get("visibility_state", "?")).lower() if isinstance(b, dict) else f"http{s}"
            states[vis] = states.get(vis, 0) + 1
            if s == 200 and vis not in HIDDEN_STATES:
                if find_list(b, "trade_id"):
                    nick = n
                    break
                visible_empty = visible_empty or n
            hidden += 1
        nick = nick or visible_empty
    report["visibility_scan"] = {"checked": checked, "hidden_or_empty": hidden,
                                 "picked": nick, "states": states}

    # 2) Score the picked trader from trade history + official market results.
    stats = None
    if nick:
        call("profile", "profile", {"nickname": nick})
        call("metrics", "profile/metrics", {"nickname": nick})
        _, tb = call("trades", TRADES_PATH, {"nickname": nick, "limit": 5})
        first = (find_list(tb, "trade_id") or [{}])[0].get("ticker")
        if first:
            call("market", f"{MARKETS_BASE}/markets/{first}", {})
        stats = score_trader(nick, state)
        save_state(state)
    report["win_stats"] = stats
    report["nickname_tested"] = nick
    (PROBE_DIR / "probe.json").write_text(json.dumps(report, indent=1))
    vs = report["visibility_scan"]
    summary = (f"public-trades scan: checked {vs['checked']}, hidden/empty {vs['hidden_or_empty']}, "
               f"picked {vs['picked'] or 'NONE'}\nstates: {vs['states']} "
               f"(pool {report.get('pool_size', '?')} traders)\n"
               f"win stats: {report['win_stats']}\n")
    summary += "\n".join(f"{c['status']}  {c['label']:<11} {c['path']} {c['params']}" for c in report["calls"])
    (PROBE_DIR / "summary.txt").write_text(summary + "\n")
    log("probe summary:\n" + summary)
    sent = send_discord({"username": "Kalshi Tracker",
                         "content": f"✅ Kalshi tracker connected. Probe tested `{nick or 'no nickname found'}`.\n```{summary[:1800]}```"})
    log("discord test " + ("sent" if sent else "FAILED"))


# ----------------------------- main -----------------------------------------
def main():
    state = load_state()
    if MODE == "probe":
        probe(state)
        return
    if rerank_due(state):
        log("re-ranking traders")
        rerank(state)
    check_trades(state)
    state["heartbeat"] = now().strftime("%Y-%m-%d %H:00 UTC")  # changes hourly → hourly commit
    save_state(state)
    write_dashboard(state)


if __name__ == "__main__":
    main()
