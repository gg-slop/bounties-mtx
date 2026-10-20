"""Payout commands for bounty ledgers."""

import os
import re
import subprocess
import sys

import orjson

ROOM = re.compile(r"[A-Za-z0-9_-]{43}")
ADDR = re.compile(r"(?:bc1|tb1|bcrt1)[a-z0-9]{8,87}|[13mn2][a-km-zA-HJ-NP-Z1-9]{25,39}")


def read_json(path):
    """Load one JSON document."""
    with open(path, "rb") as f:
        return orjson.loads(f.read())


def write_json(path, obj):
    """Write one JSON document, indented."""
    with open(path, "wb") as f:
        f.write(orjson.dumps(obj, option=orjson.OPT_INDENT_2))


def btc(sats):
    """Whole satoshis as a BTC amount with 8 decimals (integer math, no floats)."""
    return f"{sats // 10**8}.{sats % 10**8:08d}"


def unpaid_rows(room, overrides):
    """Return unpaid ledger rows grouped by their effective payout address."""
    by_address = {}
    noaddr = []
    path = f"{room}/ledger.jsonl"
    if not os.path.exists(path):
        sys.exit(f"no ledger for room {room}")
    with open(path, "rb") as f:
        for line in f:
            row = orjson.loads(line)
            if row["status"] != "unpaid":
                continue
            address = overrides.get(row["claimant"], row.get("address"))
            if address is None or not ADDR.fullmatch(str(address)):
                noaddr.append((row, row["prize_sats"]))
                continue
            by_address.setdefault(address, []).append(row)
    return by_address, noaddr


def select_payout_rows(by_address, minimum):
    """Split eligible address totals from carried rows; return exact batch rows."""
    pay, carry, included = {}, [], []
    for address, rows in sorted(by_address.items()):
        sats = sum(row["prize_sats"] for row in rows)
        if sats < minimum:
            carry.append((address, sats))
        else:
            pay[address] = sats
            included.extend({"row": row, "address": address} for row in rows)
    return pay, carry, included


def run_payout(pay, args, pending_path, included):
    """Print and optionally run the Bitcoin CLI PSBT command; save its batch."""
    outputs = (
        "[" + ",".join(f'{{"{a}":{btc(v)}}}' for a, v in sorted(pay.items())) + "]"
    )
    options = {"fee_rate": args.fee_rate}
    if args.change:
        options["changeAddress"] = args.change
    cmd = ["bitcoin-cli", "-named", "walletcreatefundedpsbt", "inputs=[]"]
    cmd += [f"outputs={outputs}", f"options={orjson.dumps(options).decode()}"]
    print(f"# {len(pay)} outputs, {sum(pay.values())} sats (+ network fee)")
    print(" ".join(f"'{c}'" if " " in c or "[" in c or "{" in c else c for c in cmd))
    write_json(pending_path, {"outputs": pay, "rows": included})
    if args.run:
        res = orjson.loads(subprocess.run(cmd, check=True, capture_output=True).stdout)
        print(f"# unsigned PSBT, fee {res['fee']} BTC")
        print(res["psbt"])


def cmd_payout(args):
    """Print the unsigned payout transaction for a room's unpaid prizes."""
    if not ROOM.fullmatch(args.room):
        sys.exit(f"invalid room ID {args.room!r}")
    pending_path = f"{args.room}/payout-pending.json"
    if os.path.exists(pending_path) and not args.replace:
        sys.exit(
            f"pending payout already exists at {pending_path}; "
            "use --replace only if the earlier batch was not broadcast"
        )
    overrides = dict(x.split("=", 1) for x in args.address)
    by_address, noaddr = unpaid_rows(args.room, overrides)
    pay, carry, included = select_payout_rows(by_address, args.min)
    for row, sats in noaddr:
        print(
            f"no valid address, skipped: {row['claimant']} {sats} sats",
            file=sys.stderr,
        )
    for address, sats in carry:
        print(
            f"below {args.min} sats, carried over: {address} {sats} sats",
            file=sys.stderr,
        )
    if not pay:
        sys.exit("nothing to pay")
    run_payout(pay, args, pending_path, included)


def cmd_paid(args):
    """Mark the exact rows from the latest payout batch as paid."""
    if not ROOM.fullmatch(args.room):
        sys.exit(f"invalid room ID {args.room!r}")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", args.txid):
        sys.exit("transaction ID must be 64 hexadecimal characters")
    pending_path = f"{args.room}/payout-pending.json"
    if not os.path.exists(pending_path):
        sys.exit(f"no pending payout for room {args.room}")
    pending = read_json(pending_path)
    ledger_path = f"{args.room}/ledger.jsonl"
    if not os.path.exists(ledger_path):
        sys.exit(f"no ledger for room {args.room}")

    with open(ledger_path, "rb") as f:
        ledger = [orjson.loads(line) for line in f if line.strip()]
    pending_rows = pending.get("rows")
    if not isinstance(pending_rows, list) or not pending_rows:
        sys.exit("pending payout has no rows")
    by_target = {}
    for row in ledger:
        target = row.get("target")
        if target in by_target:
            sys.exit(f"duplicate ledger row for target {target!r}")
        by_target[target] = row

    selected = []
    for item in pending_rows:
        snapshot = item.get("row") if isinstance(item, dict) else None
        target = snapshot.get("target") if isinstance(snapshot, dict) else None
        row = by_target.get(target)
        if row != snapshot or row.get("status") != "unpaid":
            sys.exit(
                f"pending payout row for target {target!r} no longer matches ledger"
            )
        selected.append(row)

    for row in selected:
        row["status"] = "paid"
        row["txid"] = args.txid.lower()
    temp_path = f"{ledger_path}.tmp"
    with open(temp_path, "wb") as f:
        for row in ledger:
            f.write(orjson.dumps(row) + b"\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp_path, ledger_path)
    os.unlink(pending_path)
    print(
        f"marked {len(selected)} ledger row(s), "
        f"{sum(r['prize_sats'] for r in selected)} sats, "
        f"paid in {args.txid.lower()}"
    )
