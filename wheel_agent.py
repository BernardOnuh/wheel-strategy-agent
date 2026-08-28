#!/usr/bin/env python3
"""
Wheel Strategy Agent — Alpaca AI Trading Agents Hackathon

Autonomous options-selling agent that:
  1. Sells cash-secured puts on a watchlist of liquid names
  2. If assigned, switches to selling covered calls on the resulting shares
  3. Enforces explicit risk gates before every trade
  4. Logs every decision (including skipped trades) to trade_log.csv for the write-up

Drives ALL Alpaca interaction through the `alpaca` CLI (subprocess calls),
satisfying the hackathon's "must use Alpaca's MCP server or CLI" requirement.

BEFORE RUNNING:
  - Run `alpaca profile login` (or set ALPACA_API_KEY / ALPACA_SECRET_KEY env vars)
  - Confirm you're authenticated against your NEW, dedicated hackathon paper account
  - Run `alpaca --help-all` once and diff against the command names below —
    this CLI is in alpha preview and flag names can change between releases.
    Anywhere you see a comment "VERIFY:", check that flag before trusting it.
"""

import json
import subprocess
import csv
import os
import sys
from datetime import datetime, timedelta, timezone

# ─────────────────────────── CONFIG ───────────────────────────

WATCHLIST = ["AAPL", "AMZN", "MSFT"]  # SPY/QQQ need >65% collateral each on a $100k account

# Strike selection
TARGET_DELTA_MIN = 0.20
TARGET_DELTA_MAX = 0.30
DTE_MIN = 30
DTE_MAX = 45

# Risk gates — tuned for ~$100k paper account wheeling 1–2 names at a time
MAX_COLLATERAL_PCT_PER_POSITION = 0.50   # max 50% of account per CSP ($50k → $500 max strike)
MAX_TOTAL_DEPLOYED_PCT = 0.65            # never deploy more than 65% of account at once
DAILY_LOSS_HALT_PCT = 0.03               # halt new entries if down >3% today
MIN_OPEN_INTEREST = 100                  # skip illiquid contracts
EARNINGS_BLACKOUT_DAYS = 3               # skip if earnings within N days (manual list below)

# Manage-and-exit rules
PROFIT_TARGET_PCT = 0.60                 # close at 60% of max profit captured
ROLL_DTE_THRESHOLD = 21                  # roll short strike if tested and DTE < this

LOG_FILE = "trade_log.csv"

# Optional manual earnings blackout list: {"AAPL": "2026-09-10", ...}
# Fill this in by hand before running — Alpaca's API does not provide earnings dates.
EARNINGS_DATES = {}

# ─────────────────────────── CLI WRAPPER ───────────────────────────

def run_cli(args, input_data=None):
    """Run an alpaca CLI command and return parsed JSON. Raises on error."""
    cmd = ["alpaca"] + args
    try:
        result = subprocess.run(
            cmd,
            input=input_data,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except FileNotFoundError:
        sys.exit("ERROR: `alpaca` CLI not found on PATH. Did you `brew install alpacahq/tap/cli`?")
    except subprocess.TimeoutExpired:
        log_event("ERROR", "cli_timeout", " ".join(cmd))
        return None

    if result.returncode != 0:
        # Errors come back as JSON on stderr per the CLI's documented behavior
        try:
            err = json.loads(result.stderr)
        except json.JSONDecodeError:
            err = {"error": result.stderr.strip()}
        log_event("ERROR", "cli_error", f"{' '.join(cmd)} -> {err}")
        return None

    if not result.stdout.strip():
        return {}

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        log_event("ERROR", "bad_json", f"{' '.join(cmd)} -> {result.stdout[:200]}")
        return None


# ─────────────────────────── LOGGING ───────────────────────────

def log_event(level, action, detail):
    """Append every decision (trade, skip, error) to a CSV for the write-up / judging."""
    new_file = not os.path.exists(LOG_FILE)
    with open(LOG_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        if new_file:
            writer.writerow(["timestamp", "level", "action", "detail"])
        writer.writerow([datetime.now(timezone.utc).isoformat(), level, action, detail])
    print(f"[{level}] {action}: {detail}")


# ─────────────────────────── ACCOUNT / RISK STATE ───────────────────────────

def get_account():
    return run_cli(["account", "get"])


def get_positions():
    return run_cli(["position", "list"]) or []


def get_open_orders():
    return run_cli(["order", "list", "--status", "open"]) or []


def pending_csp_collateral(open_orders):
    """Collateral reserved by unfilled short put orders."""
    reserved = 0.0
    for order in open_orders:
        if order.get("asset_class") != "us_option" or order.get("side") != "sell":
            continue
        unfilled = float(order.get("qty", 0)) - float(order.get("filled_qty", 0))
        if unfilled <= 0:
            continue
        parsed = parse_occ_symbol(order["symbol"])
        if parsed["type"] == "put":
            reserved += parsed["strike_price"] * 100 * unfilled
    return reserved


def underlyings_with_open_short_options(open_orders):
    roots = set()
    for order in open_orders:
        if order.get("asset_class") == "us_option" and order.get("side") == "sell":
            roots.add(parse_occ_symbol(order["symbol"])["root"])
    return roots


def daily_loss_breached(account):
    """Halt condition: today's equity drop exceeds DAILY_LOSS_HALT_PCT."""
    equity = float(account.get("equity", 0))
    last_equity = float(account.get("last_equity", equity))
    if last_equity == 0:
        return False
    drawdown = (last_equity - equity) / last_equity
    return drawdown > DAILY_LOSS_HALT_PCT


def total_collateral_deployed(positions, account):
    equity = float(account.get("equity", 1))
    deployed = sum(abs(float(p.get("market_value", 0))) for p in positions)
    return deployed / equity


# ─────────────────────────── MARKET DATA ───────────────────────────

def get_spot_price(symbol):
    """Latest bar close for the underlying, used to target near-the-money strikes."""
    # `data bars --limit 1` without --start returns {"bars": null}; use latest-bar instead.
    response = run_cli(["data", "latest-bar", "--symbol", symbol])
    if not response:
        return None
    bar = response.get("bar")
    if not bar:
        return None
    return float(bar.get("c", 0)) or None


def max_affordable_strike(account):
    """Highest put strike we can cash-secure given the per-position collateral cap."""
    equity = float(account.get("equity", 0))
    return equity * MAX_COLLATERAL_PCT_PER_POSITION / 100


def get_option_chain(symbol, option_type, spot_price, otm_band=0.15, max_strike=None):
    """
    Pull option chain snapshots within a strike band near the money, and within
    our DTE window — filtering server-side keeps the payload small and avoids
    pulling hundreds of deep-OTM strikes with near-zero delta and no liquidity.
    `otm_band` = how far below/above spot to search (0.15 = within 15%).
    """
    today = datetime.now(timezone.utc)
    exp_gte = (today + timedelta(days=DTE_MIN)).strftime("%Y-%m-%d")
    exp_lte = (today + timedelta(days=DTE_MAX)).strftime("%Y-%m-%d")

    args = [
        "data", "option", "chain",
        "--underlying-symbol", symbol,
        "--type", option_type,
        "--expiration-date-gte", exp_gte,
        "--expiration-date-lte", exp_lte,
    ]
    if spot_price:
        if option_type == "put":
            upper = round(spot_price * 0.99, 2)
            lower = round(spot_price * (1 - otm_band), 2)
            if max_strike is not None:
                upper = min(upper, max_strike)
            if lower > upper:
                lower = round(upper * 0.90, 2)  # collateral cap binds — search deeper OTM
            if lower > upper:
                return {}
            args += ["--strike-price-gte", str(lower), "--strike-price-lte", str(upper)]
        else:
            args += ["--strike-price-gte", str(round(spot_price * 1.01, 2)),
                      "--strike-price-lte", str(round(spot_price * (1 + otm_band), 2))]

    response = run_cli(args)
    if not response:
        return []
    return response.get("snapshots", {})


def parse_occ_symbol(occ_symbol):
    """
    Decode an OCC option symbol, e.g. 'AAPL260904P00110000':
      root (variable length) + YYMMDD (6) + C/P (1) + strike*1000 (8, zero-padded)
    """
    date_and_strike = occ_symbol[-15:]
    root = occ_symbol[:-15]
    date_str = date_and_strike[0:6]
    option_type = "call" if date_and_strike[6] == "C" else "put"
    strike = int(date_and_strike[7:15]) / 1000.0
    expiration_date = datetime.strptime(date_str, "%y%m%d").strftime("%Y-%m-%d")
    return {
        "root": root,
        "expiration_date": expiration_date,
        "type": option_type,
        "strike_price": strike,
    }


def days_to_expiry(expiry_str):
    exp = datetime.strptime(expiry_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return (exp - datetime.now(timezone.utc)).days


def is_earnings_blackout(symbol):
    date_str = EARNINGS_DATES.get(symbol)
    if not date_str:
        return False
    earnings_date = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return 0 <= (earnings_date - datetime.now(timezone.utc)).days <= EARNINGS_BLACKOUT_DAYS


# ─────────────────────────── SIGNAL: PICK A PUT TO SELL ───────────────────────────

def select_put_candidate(symbol, chain, option_type="put", spot_price=None, max_strike=None):
    """
    Filter the chain snapshots down to one short-option candidate matching our rules.
    `chain` is the {occ_symbol: snapshot} dict returned by get_option_chain.
    Returns a normalized dict (not the raw snapshot) or None if nothing qualifies.

    Paper/OPRA data here is thin: many contracts show delta=0 and bid=0 even
    when a real quote exists on the ask side. So delta is used as a SOFT
    preference (closest to target) rather than a hard filter, and strike
    distance from spot is the primary way we pick "how far OTM" — this is
    the standard fallback when greeks are unreliable on a sparse feed.
    Liquidity gate: require a non-zero ask (some market exists) rather than
    requiring a non-zero bid, since bids are frequently empty in paper data.
    """
    candidates = []

    for occ_symbol, snap in chain.items():
        parsed = parse_occ_symbol(occ_symbol)
        if parsed["type"] != option_type:
            continue

        dte = days_to_expiry(parsed["expiration_date"])
        if not (DTE_MIN <= dte <= DTE_MAX):
            continue

        quote = snap.get("latestQuote", {})
        bid = float(quote.get("bp", 0) or 0)
        ask = float(quote.get("ap", 0) or 0)
        if ask <= 0:
            continue  # no market at all on this contract, skip regardless of anything else

        delta = abs(float(snap.get("greeks", {}).get("delta", 0) or 0))

        strike = parsed["strike_price"]
        if max_strike is not None and strike > max_strike:
            continue  # would exceed per-position collateral cap

        pct_from_spot = abs(strike - spot_price) / spot_price if spot_price else None

        candidates.append({
            "symbol": occ_symbol,
            "delta": delta,
            "strike_price": strike,
            "expiration_date": parsed["expiration_date"],
            "bid_price": bid,
            "ask_price": ask,
            "pct_from_spot": pct_from_spot,
        })

    if not candidates:
        return None

    # Prefer real, in-range delta if any contract has one; otherwise fall back
    # to strike-distance-from-spot as the OTM proxy.
    in_range_delta = [c for c in candidates if TARGET_DELTA_MIN <= c["delta"] <= TARGET_DELTA_MAX]
    if in_range_delta:
        midpoint = (TARGET_DELTA_MIN + TARGET_DELTA_MAX) / 2
        in_range_delta.sort(key=lambda c: abs(c["delta"] - midpoint))
        return in_range_delta[0]

    if spot_price:
        candidates = [c for c in candidates if c["pct_from_spot"] is not None]
        candidates.sort(key=lambda c: abs(c["pct_from_spot"] - 0.10))  # target ~10% OTM
        return candidates[0] if candidates else None

    return None


# ─────────────────────────── RISK GATES ───────────────────────────

def passes_risk_gates(symbol, contract, account, positions):
    """Every reason a trade gets vetoed lives here — this is what goes in the write-up."""
    equity = float(account.get("equity", 0))

    if daily_loss_breached(account):
        log_event("SKIP", symbol, "daily loss halt triggered")
        return False

    if total_collateral_deployed(positions, account) >= MAX_TOTAL_DEPLOYED_PCT:
        log_event("SKIP", symbol, "max total collateral deployed reached")
        return False

    strike = float(contract["strike_price"])
    collateral_required = strike * 100  # one cash-secured put = 100 shares of collateral
    if collateral_required / equity > MAX_COLLATERAL_PCT_PER_POSITION:
        log_event("SKIP", symbol, f"position collateral {collateral_required} exceeds per-position cap")
        return False

    if any(p["symbol"] == symbol for p in positions):
        log_event("SKIP", symbol, "already holding a position in this name")
        return False

    if is_earnings_blackout(symbol):
        log_event("SKIP", symbol, "inside earnings blackout window")
        return False

    return True


# ─────────────────────────── ORDER EXECUTION ───────────────────────────

def sell_put(symbol, contract):
    """
    Sell to open one cash-secured put.
    VERIFY: option order flags with `alpaca order submit --help` — options orders
    typically need the OCC option symbol, side, qty, order type, and time-in-force.
    """
    option_symbol = contract["symbol"]  # OCC-format contract symbol from the chain
    client_order_id = f"wheel-{symbol}-{int(datetime.now(timezone.utc).timestamp())}"

    args = [
        "order", "submit",
        "--symbol", option_symbol,
        "--side", "sell",
        "--qty", "1",
        "--type", "limit",
        "--limit-price", str(short_option_limit_price(contract)),
        "--time-in-force", "day",
        "--client-order-id", client_order_id,
    ]
    result = run_cli(args)
    if result:
        log_event("TRADE", symbol, f"sold put {option_symbol} strike={contract['strike_price']} delta={contract['delta']:.2f}")
    return result


def short_option_limit_price(contract):
    """
    Pick a realistic limit price for a sell-to-open order. Prefer the real bid;
    if the book has no bid (common in this paper/OPRA feed), price a bit below
    the ask instead of blindly using 0, which would either reject or fill terribly.
    """
    if contract["bid_price"] > 0:
        return round(contract["bid_price"], 2)
    return round(contract["ask_price"] * 0.85, 2)


def sell_covered_call(symbol, shares_qty, chain, spot_price=None):
    """Called when we're holding shares from an assignment — mirror logic, calls instead of puts."""
    candidate = select_put_candidate(symbol, chain, option_type="call", spot_price=spot_price)
    if not candidate:
        log_event("SKIP", symbol, "no qualifying covered call strike found")
        return None

    option_symbol = candidate["symbol"]
    client_order_id = f"cc-{symbol}-{int(datetime.now(timezone.utc).timestamp())}"
    args = [
        "order", "submit",
        "--symbol", option_symbol,
        "--side", "sell",
        "--qty", str(shares_qty // 100),
        "--type", "limit",
        "--limit-price", str(short_option_limit_price(candidate)),
        "--time-in-force", "day",
        "--client-order-id", client_order_id,
    ]
    result = run_cli(args)
    if result:
        log_event("TRADE", symbol, f"sold covered call {option_symbol} strike={candidate['strike_price']}")
    return result


# ─────────────────────────── MANAGE OPEN POSITIONS ───────────────────────────

def manage_open_positions(positions):
    """Close-early-at-profit-target and roll-if-tested logic for existing short options."""
    for p in positions:
        if p.get("asset_class") != "us_option":
            continue

        symbol = p["symbol"]
        unrealized_plpc = float(p.get("unrealized_plpc", 0))

        # Short options: a positive unrealized_plpc here means the premium has decayed in our favor
        if unrealized_plpc >= PROFIT_TARGET_PCT:
            log_event("MANAGE", symbol, f"closing at {unrealized_plpc:.0%} of max profit")
            run_cli(["order", "submit", "--symbol", symbol, "--side", "buy",
                      "--qty", p["qty"], "--type", "market", "--time-in-force", "day"])
            continue

        # VERIFY: contract metadata field name for expiration on a position — check `alpaca position list --schema`
        expiry = p.get("expiration_date")
        if expiry and days_to_expiry(expiry) < ROLL_DTE_THRESHOLD:
            log_event("MANAGE", symbol, f"under {ROLL_DTE_THRESHOLD} DTE — flagged for manual/roll review")


# ─────────────────────────── MAIN LOOP ───────────────────────────

def run_cycle():
    log_event("INFO", "cycle_start", datetime.now(timezone.utc).isoformat())

    account = get_account()
    if not account:
        log_event("ERROR", "cycle_abort", "could not fetch account")
        return

    positions = get_positions()
    open_orders = get_open_orders()

    if daily_loss_breached(account):
        log_event("HALT", "risk_gate", "daily loss limit breached — no new entries this cycle")
        manage_open_positions(positions)
        return

    manage_open_positions(positions)

    held_symbols = {p["symbol"] for p in positions if p.get("asset_class") == "us_equity"}
    max_strike = max_affordable_strike(account)
    equity = float(account.get("equity", 0))
    deployed_collateral = sum(abs(float(p.get("market_value", 0))) for p in positions)
    deployed_collateral += pending_csp_collateral(open_orders)
    pending_underlyings = underlyings_with_open_short_options(open_orders)

    for symbol in WATCHLIST:
        if symbol in pending_underlyings:
            log_event("SKIP", symbol, "already has an open short-option order")
            continue
        spot_price = get_spot_price(symbol)
        if not spot_price:
            log_event("SKIP", symbol, "could not fetch spot price")
            continue

        option_type = "call" if symbol in held_symbols else "put"
        chain = get_option_chain(symbol, option_type, spot_price, max_strike=max_strike)
        if not chain:
            log_event("SKIP", symbol, f"no option chain within collateral cap (max strike ${max_strike:.0f})")
            continue

        if symbol in held_symbols:
            shares_position = next((p for p in positions if p["symbol"] == symbol), None)
            if shares_position:
                sell_covered_call(symbol, int(float(shares_position["qty"])), chain, spot_price)
            continue

        candidate = select_put_candidate(
            symbol, chain, option_type="put", spot_price=spot_price, max_strike=max_strike,
        )
        if not candidate:
            log_event("SKIP", symbol, "no qualifying put strike found")
            continue

        needed = float(candidate["strike_price"]) * 100
        if equity and (deployed_collateral + needed) / equity > MAX_TOTAL_DEPLOYED_PCT:
            log_event(
                "SKIP", symbol,
                f"would exceed total deployed cap "
                f"({deployed_collateral + needed:.0f} / {equity:.0f})",
            )
            continue

        if not passes_risk_gates(symbol, candidate, account, positions):
            continue

        result = sell_put(symbol, candidate)
        if result:
            deployed_collateral += needed

    log_event("INFO", "cycle_end", datetime.now(timezone.utc).isoformat())


if __name__ == "__main__":
    run_cycle()