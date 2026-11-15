#!/usr/bin/env python3
"""Build the public XPR to Ethereum withdrawals page as static files.

The browser version (utils/xpr_eth_withdrawals.html) makes every visitor page through Hyperion and
Blockscout themselves (about 30 s, and 429s once a few people open it). This does that work once, on
a schedule, and writes:

  <out>/index.html   page with the snapshot embedded (first paint needs no second request)
  <out>/data.json    the same snapshot, polled by the page every 60 s

Run it from cron every few minutes; serve <out> from any static host. A failed run exits non-zero and
leaves the previous files untouched, so the page keeps showing the last good snapshot and flags it as
stale once it is older than 15 minutes.

  python3 build_snapshot.py --out /var/www/xpr-withdrawals
"""
import argparse
import json
import os
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

BRIDGE = "0x8329D783F96e48d50f0f1f5a3c7F83b73A5c0218"
# Tried in order; a host whose history index is down (eosusa 2026-10-30: nodeos healthy, every
# get_actions a 504 after 5 s) is skipped after two tries and moved to the back.
HYPERIONS = ["https://proton.eosusa.io", "https://proton.protonuk.io", "https://proton.eu.eosamsterdam.net"]
BLOCKSCOUT = "https://eth.blockscout.com/api/v2"
WRAP = "proton.wrap"
SHOW_DAYS = 30
MATCH_BUFFER_DAYS = 3

# memo coin -> real payout token on Ethereum. Matching is by contract address, never by symbol:
# the bridge wallet receives look-alike scam tokens named "USDC", "ETH" and so on.
TOKENS = {
    "USDC":  {"addr": "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48", "tol": 1e-6},
    "USDT":  {"addr": "0xdac17f958d2ee523a2206206994597c13d831ec7", "tol": 1e-6},
    "PYUSD": {"addr": "0x6c3ea9036406852006290770bedfcaba0e23a0e8", "tol": 1e-6},
    "PAXG":  {"addr": "0x45804880de22913dafe09f4980848ece6ecbaf78", "tol": 1e-6},
    "PAX":   {"addr": "0x8e870d67f660d95d5be530380d0ec0bd388289e1", "tol": 1e-6},
    "METAL": {"addr": "0x294559fa758c88d639fd085751e463fee7806eab", "tol": 1e-6},
    "ETH":   {"addr": "eth", "tol": 2e-8},
}
ADDR_TO_COIN = {t["addr"]: c for c, t in TOKENS.items()}

# Requests that are not owed, by XPR transaction id -> the label the page shows. They are listed as
# excluded, never as waiting: they count toward no waiting total, oldest-waiting age or wallet shortfall,
# and they take no payout in matching (anything the bridge sent such an address shows as unmatched instead).
EXCLUDED = {
    # 1,143,288.20 XUSDC from rohtie1 to 0x8fc774...06716f on 2026-10-17: a hacker's withdrawal
    "66a6b47658faf90e8380014dc1629a233c11ba1107ace717a82172706cfdadb3": "hacker",
}


def get_json(url, tries=6):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "xpr-withdrawals-snapshot/1"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(i + 1)


def hyperion_json(path):
    """GET path from the first Hyperion host that answers; a host that fails moves to the back."""
    errors = []
    for host in list(HYPERIONS):
        try:
            return get_json(host + path, tries=2)
        except Exception as e:
            errors.append(f"{host}: {e!r}")
            HYPERIONS.remove(host)
            HYPERIONS.append(host)
    raise RuntimeError("every Hyperion host failed: " + "; ".join(errors))


def iso_no_z(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def is_addr(s):
    return len(s) == 42 and s.startswith("0x") and all(c in "0123456789abcdefABCDEF" for c in s[2:])


def fetch_requests(since):
    """Every xtokens transfer to proton.wrap in the window, newest first, paged by `before`."""
    seen, before = {}, None
    while True:
        u = (f"/v2/history/get_actions?account={WRAP}&filter=xtokens:transfer&transfer.to={WRAP}"
             f"&limit=1000&sort=desc&after={since}")
        if before:
            u += f"&before={before}"
        d = hyperion_json(u)
        for a in d["actions"]:
            seen[a["global_sequence"]] = a
        if len(d["actions"]) < 1000:
            break
        last = d["actions"][-1]["timestamp"]
        if last == before:
            raise RuntimeError("more than 1000 XPR transfers share one timestamp, cannot page")
        before = last
    out = []
    for a in seen.values():
        x = a["act"]["data"]
        m = str(x.get("memo") or "").split(":")
        # COIN:erc20:0xRecipient:fee, or ETH:mainnet:0xRecipient:fee for native ETH
        # Ethereum only: any ERC-20 memo, and "mainnet" only for native ETH (other coins' mainnet is their own chain).
        if len(m) < 3 or not (m[1] == "erc20" or (m[0] == "ETH" and m[1] == "mainnet")) or not is_addr(m[2]):
            continue
        out.append({
            "ts": a["timestamp"][:19], "trx": a["trx_id"], "from": x["from"], "coin": m[0],
            "to": m[2].lower(), "qty": float(str(x["quantity"]).split()[0]),
            "fee": float(m[3]) if len(m) > 3 and m[3] else 0.0,
        })
    out.sort(key=lambda r: r["ts"])
    return out


def fetch_payouts(since):
    """All outgoing transfers of the bridge wallet back to `since`."""
    pay = []

    def walk(path, on_item):
        params = ""
        while True:
            d = get_json(f"{BLOCKSCOUT}/addresses/{BRIDGE}{path}{params}")
            for it in d["items"]:
                on_item(it)
            last = d["items"][-1] if d["items"] else None
            if not d.get("next_page_params") or not last or last["timestamp"][:19] < since:
                break
            params = "&" + urllib.parse.urlencode(d["next_page_params"])
            time.sleep(0.35)

    def erc20(t):
        pay.append({
            "ts": t["timestamp"][:19], "hash": t["transaction_hash"], "tok": t["token"]["address_hash"].lower(),
            "to": t["to"]["hash"].lower(), "amt": int(t["total"]["value"]) / 10 ** int(t["total"]["decimals"]),
            "name": t["to"].get("name") or "",  # Blockscout's contract name, e.g. "CircleDeposit1"
        })

    def native(t):
        if t["status"] == "ok" and int(t["value"]) > 0:
            pay.append({
                "ts": t["timestamp"][:19], "hash": t["hash"], "tok": "eth",
                "to": t["to"]["hash"].lower(), "amt": int(t["value"]) / 1e18,
            })

    walk("/token-transfers?type=ERC-20&filter=from", erc20)
    walk("/transactions?filter=from", native)
    return pay


def fetch_wallet():
    info = get_json(f"{BLOCKSCOUT}/addresses/{BRIDGE}")
    bal = get_json(f"{BLOCKSCOUT}/addresses/{BRIDGE}/token-balances")
    w = {"ETH": int(info.get("coin_balance") or "0") / 1e18}
    for b in bal:
        coin = ADDR_TO_COIN.get(b["token"]["address_hash"].lower())
        if coin:
            w[coin] = int(b["value"]) / 10 ** int(b["token"]["decimals"])
    return w


def match(reqs, pay):
    """Requests and payouts are both walked oldest first. Repeat withdrawals of the same amount to one
    address are interchangeable, so each request takes the EARLIEST unused payout after it."""
    reqs.sort(key=lambda r: r["ts"])
    pay.sort(key=lambda p: p["ts"])
    # 30 days is ~3k requests x ~5k payouts, so look payouts up by (token, recipient), oldest first.
    by_key = {}
    for p in pay:
        p["used"] = False
        by_key.setdefault((p["tok"], p["to"]), []).append(p)
    for r in reqs:
        r["net"] = r["qty"] - r["fee"]
        if r["trx"] in EXCLUDED:
            r["state"] = "excluded"
            continue
        t = TOKENS.get(r["coin"])
        if not t:
            r["state"] = "unmapped"
            continue
        hit = next((p for p in by_key.get((t["addr"], r["to"]), ())
                    if not p["used"] and p["ts"] >= r["ts"] and abs(p["amt"] - r["net"]) <= t["tol"]), None)
        if hit:
            hit["used"] = True
            r["state"], r["payout"] = "paid", hit
        else:
            r["state"] = "waiting"
    for r in reqs:
        if r["state"] == "waiting":
            t = TOKENS[r["coin"]]
            r["others"] = [p for p in by_key.get((t["addr"], r["to"]), ()) if not p["used"] and p["ts"] >= r["ts"]]


def snapshot():
    now = datetime.now(timezone.utc)
    since = iso_no_z(now - timedelta(days=SHOW_DAYS))
    read_since = iso_no_z(now - timedelta(days=SHOW_DAYS + MATCH_BUFFER_DAYS))
    # Hyperion and Blockscout are independent hosts, so read them side by side.
    with ThreadPoolExecutor(3) as ex:
        f_req, f_pay, f_wal = ex.submit(fetch_requests, read_since), ex.submit(fetch_payouts, read_since), ex.submit(fetch_wallet)
        allr, pay, wallet = f_req.result(), f_pay.result(), f_wal.result()
    match(allr, pay)
    reqs = [r for r in allr if r["ts"] >= since]

    waiting, unmapped, paid, excluded = [], [], [], []
    for r in reqs:
        base = [r["ts"], r["from"], r["coin"], r["qty"], r["fee"], r["to"], r["trx"]]
        if r["state"] == "waiting":
            waiting.append(base + [[[p["hash"], p["ts"], p["amt"]] for p in r["others"]]])
        elif r["state"] == "excluded":
            excluded.append(base + [EXCLUDED[r["trx"]]])
        elif r["state"] == "paid":
            paid.append(base + [r["payout"]["ts"], r["payout"]["amt"], r["payout"]["hash"]])
        else:
            unmapped.append(base)
    paid.sort(key=lambda x: x[0], reverse=True)
    # USDC the bridge wallet sent that paid no request: treasury moves, payouts of requests older than the
    # read window, or partial payments. Listed so readers can see where funds went while requests wait.
    usdc = TOKENS["USDC"]["addr"]
    unmatched = sorted(([p["ts"], p["to"], p["amt"], p["hash"], p["name"]] for p in pay
                        if p["tok"] == usdc and not p["used"] and p["ts"] >= since), reverse=True)
    return {
        "generated": iso_no_z(now),
        "bridge": BRIDGE,
        "total": len(reqs),
        "payouts_read": len(pay),
        "wallet": wallet,
        # row layouts, consumed by template.html:
        # waiting: [ts, from, coin, qty, fee, to, xpr_trx, [[eth_hash, ts, amt], ...]]
        # paid:    [ts, from, coin, qty, fee, to, xpr_trx, paid_ts, paid_amt, eth_hash], every one, newest first
        # unmapped: [ts, from, coin, qty, fee, to, xpr_trx] (a coin with no Ethereum contract in TOKENS)
        "waiting": waiting,
        "paid": paid,
        "unmapped": unmapped,
        # excluded: [ts, from, coin, qty, fee, to, xpr_trx, label] (EXCLUDED: not owed, e.g. a hacker's request)
        "excluded": excluded,
        "usdc_unmatched": unmatched,  # [[ts, to, amt, eth_hash, blockscout_name], ...], newest first
    }


def write_atomic(path, text):
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="directory served by the web host")
    args = ap.parse_args()
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "template.html"), encoding="utf-8") as f:
        template = f.read()
    if "/*SNAPSHOT*/null" not in template:
        raise RuntimeError("template.html has no /*SNAPSHOT*/null placeholder")
    t0 = time.time()
    snap = snapshot()
    data = json.dumps(snap, separators=(",", ":"))
    os.makedirs(args.out, exist_ok=True)
    # data.json first: a page that loads between the two writes then sees the newer snapshot.
    write_atomic(os.path.join(args.out, "data.json"), data)
    write_atomic(os.path.join(args.out, "index.html"), template.replace("/*SNAPSHOT*/null", data.replace("</", "<\\/")))
    print(f"{snap['generated']}Z waiting={len(snap['waiting'])} paid={len(snap['paid'])} excluded={len(snap['excluded'])} usdc_unmatched={len(snap['usdc_unmatched'])} "
          f"payouts={snap['payouts_read']} took={time.time() - t0:.1f}s -> {args.out}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("snapshot failed:", repr(e), file=sys.stderr)
        sys.exit(1)
