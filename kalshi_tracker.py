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

LEADERBOARD_QUERIES = [
    {"metric_name": "projected_pnl", "time_period": "all_time"},
    {"metric_name": "projected_pnl", "time_period": "monthly"},
    {"metric_name": "num_markets_traded", "time_period": "all_time"},
]
# Tried in order until one returns positions with P&L; the winner is remembered.
# "{nick}" / "{sid}" are filled with the trader's nickname / social_id.
POSITION_ENDPOINTS = [
    ["profile/positions", {"nickname": "{nick}"}],
    ["profile/positions", {"nickname": "{nick}", "status": "closed"}],
    ["profile/positions", {"social_id": "{sid}"}],
    ["profile/closed_positions", {"social_id": "{sid}"}],
    ["profile/positions/closed", {"nickname": "{nick}"}],
    ["profile/settled_positions", {"nickname": "{nick}"}],
    ["profile/market_positions", {"nickname": "{nick}"}],
    ["profile/holdings", {"nickname": "{nick}"}],
    ["positions", {"social_id": "{sid}"}],
    ["positions", {"nickname": "{nick}", "status": "closed"}],
    ["profile/{nick}/positions", {}],
    ["users/{sid}/positions", {}],
]
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
    url = f"{BASE}/{path}"
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
    return list(pool.values())[:POOL_SIZE]


def fill(template, nick, sid):
    return template.replace("{nick}", nick).replace("{sid}", sid or "")


def social_id(nick, cache={}):
    if nick not in cache:
        data = get("profile", {"nickname": nick}) or {}
        prof = data.get("social_profile", data) if isinstance(data, dict) else {}
        cache[nick] = prof.get("social_id", "") if isinstance(prof, dict) else ""
    return cache[nick]


def closed_positions(nick, state):
    eps = POSITION_ENDPOINTS
    known = state.get("positions_endpoint")
    if known:
        eps = [known]          # once found, don't waste calls on dead guesses
    else:
        missing = parse_time(state.get("positions_missing_since"))
        if missing and now() - missing < dt.timedelta(days=7):
            return None        # all guesses 404'd recently; probe mode re-tests them
    any_response = False
    for path, extra in eps:
        needs_sid = "{sid}" in path or any("{sid}" in str(v) for v in extra.values())
        sid = social_id(nick) if needs_sid else ""
        if needs_sid and not sid:
            continue
        params = {k: fill(str(v), nick, sid) for k, v in extra.items()}
        rows = paged(fill(path, nick, sid), {**params, "limit": 200}, "pnl")
        if rows is None:
            continue
        any_response = True
        rows = [r for r in rows if not r.get("open_position", False)]
        if rows:
            state["positions_endpoint"] = [path, extra]
            return rows
    return [] if any_response else None


def win_stats(rows):
    wins = sum(1 for r in rows if num(r.get("pnl")) > 0)
    losses = sum(1 for r in rows if num(r.get("pnl")) < 0)
    resolved = wins + losses
    return {"wins": wins, "losses": losses, "resolved": resolved,
            "win_pct": round(100 * wins / resolved, 1) if resolved else 0.0}


def rerank(state):
    pool = leaderboard_pool()
    if not pool:
        state["status"] = "Could not read Kalshi leaderboard (run probe mode)."
        return False

    # Only traders whose trades are public can be alerted on.
    visible, hidden = [], 0
    for c in pool:
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

    scored, positions_worked = [], False
    for c in visible:
        rows = closed_positions(c["nickname"], state)
        if rows is None:
            if not state.get("positions_endpoint"):
                state["positions_missing_since"] = now().isoformat(timespec="minutes")
                break      # no working endpoint: stop hammering Kalshi with 404s
            continue
        positions_worked = positions_worked or bool(rows)
        c.update(win_stats(rows))
        scored.append(c)
        log(f"  {c['nickname']}: {c['win_pct']}% over {c['resolved']}")

    eligible = [c for c in scored if c["resolved"] >= MIN_RESOLVED]
    if eligible:
        eligible.sort(key=lambda c: (c["win_pct"], c["resolved"]), reverse=True)
        top = eligible[:TOP_N]
        state["ranking_mode"] = f"Win% (min {MIN_RESOLVED} resolved positions)"
    else:
        # Fallback keeps alerts working while win% data is unavailable.
        top = sorted(visible, key=lambda c: c["order"])[:TOP_N]
        state["ranking_mode"] = ("FALLBACK: top leaderboard profit among traders with public "
                                 "trades (win% data not available yet)")
        log(state["ranking_mode"])

    state["tracked"] = [{k: c.get(k) for k in
                         ("nickname", "win_pct", "wins", "losses", "resolved", "lb_rank", "lb_metric")}
                        for c in top]
    state["last_rerank"] = now().isoformat(timespec="minutes")
    state["status"] = "" if positions_worked else state["ranking_mode"]
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
    """Work out what *this trader* did. Kalshi trade prints are YES-priced."""
    taker_side = (t.get("taker_side") or "").lower()
    if (t.get("taker_nickname") or "").lower() == nick.lower():
        side, action = taker_side, (t.get("taker_action") or "buy")
    elif (t.get("maker_nickname") or "").lower() == nick.lower():
        side = {"yes": "no", "no": "yes"}.get(taker_side, taker_side)  # maker holds the other side
        action = t.get("maker_action") or "buy"
    else:
        side, action = taker_side, (t.get("taker_action") or "?")
    yes_c = num(t.get("price"))
    if 0 < yes_c <= 1:            # dollars, not cents
        yes_c *= 100
    if not yes_c and t.get("price_dollars"):
        yes_c = num(t["price_dollars"]) * 100
    price_c = yes_c if side == "yes" else (100 - yes_c if yes_c else 0)
    count = num(t.get("count") or t.get("count_fp"))
    return side.upper() or "?", action.upper(), round(price_c), count


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


def alert(trader, t):
    nick = trader["nickname"]
    side, action, price_c, count = trader_side(t, nick)
    cost = price_c * count / 100
    market = t.get("name") or t.get("ticker") or "Unknown market"
    color = 0x2ECC71 if action == "BUY" else 0xE74C3C
    wr = (f"{trader['win_pct']}% ({trader['wins']}W-{trader['losses']}L)"
          if trader.get("resolved") else "n/a")
    embed = {
        "title": f"{nick}: {action} {side}",
        "description": market[:250],
        "url": PROFILE_URL.format(nick),
        "color": color,
        "fields": [
            {"name": "Ticker", "value": t.get("ticker") or "?", "inline": True},
            {"name": "Price", "value": f"{price_c}¢", "inline": True},
            {"name": "Contracts", "value": f"{count:g}", "inline": True},
            {"name": "≈ Cost", "value": f"${cost:,.2f}", "inline": True},
            {"name": "Trader win%", "value": wr, "inline": True},
        ],
        "footer": {"text": "Kalshi tracker · alert only, not advice"},
        "timestamp": t.get("create_date") or now().isoformat(),
    }
    ok = send_discord({"username": "Kalshi Tracker", "embeds": [embed]})
    return ok, {"time": t.get("create_date") or now().isoformat(timespec="minutes"),
                "nickname": nick, "action": action, "side": side, "price": price_c,
                "count": count, "market": market[:120], "ticker": t.get("ticker")}


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
        for t in new:
            ts = parse_time(t.get("create_date"))
            if ts and now() - ts > dt.timedelta(minutes=MAX_TRADE_AGE_MIN):
                continue
            ok, rec = alert(trader, t)
            if ok:
                state["alerts"].insert(0, rec)
                log(f"ALERT {nick} {rec['action']} {rec['side']} {rec['ticker']}")
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
    nick, checked, hidden = PROBE_NICK, 0, 0
    st, body = call("leaderboard", "leaderboard", {**LEADERBOARD_QUERIES[0], "limit": 30})
    names = [r["nickname"] for r in (find_list(body, "nickname") or [])
             if r.get("nickname") and not r.get("is_anonymous")]
    if not nick:
        for n in names[:30]:
            checked += 1
            s, b = get_raw(TRADES_PATH, {"nickname": n, "limit": 5})
            vis = str(b.get("visibility_state", "")).lower() if isinstance(b, dict) else ""
            if s == 200 and vis not in HIDDEN_STATES and find_list(b, "trade_id"):
                nick = n
                break
            hidden += 1
    report["visibility_scan"] = {"checked": checked, "hidden_or_empty": hidden, "picked": nick}

    # 2) Test every closed-positions guess on that trader.
    found = None
    if nick:
        _, prof = call("profile", "profile", {"nickname": nick})
        p = prof.get("social_profile", {}) if isinstance(prof, dict) else {}
        sid = p.get("social_id", "") if isinstance(p, dict) else ""
        call("metrics", "profile/metrics", {"nickname": nick})
        call("trades", TRADES_PATH, {"nickname": nick, "limit": 5})
        for path, extra in POSITION_ENDPOINTS:
            params = {k: fill(str(v), nick, sid) for k, v in extra.items()}
            s, b = call("positions", fill(path, nick, sid), {**params, "limit": 5})
            if s == 200 and found is None and find_list(b, "pnl"):
                found = [path, extra]
    if found:
        state["positions_endpoint"] = found
        state.pop("positions_missing_since", None)
        state["needs_rerank"] = True
    else:
        state.pop("positions_missing_since", None)   # let the next re-rank try again
    save_state(state)
    report["nickname_tested"] = nick
    report["positions_endpoint_found"] = found
    (PROBE_DIR / "probe.json").write_text(json.dumps(report, indent=1))
    vs = report["visibility_scan"]
    summary = (f"public-trades scan: checked {vs['checked']}, hidden/empty {vs['hidden_or_empty']}, "
               f"picked {vs['picked'] or 'NONE'}\npositions endpoint: {found or 'not found'}\n")
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
