"""Payout commands for bounty ledgers."""

import hashlib
import os
import re
import shlex
import sys

import orjson

ROOM = re.compile(r"[A-Za-z0-9_-]{43}")
BECH32 = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
BECH32_GEN = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
BASE58_VERSIONS = {0x00, 0x05, 0x6F, 0xC4}  # mainnet and testnet P2PKH / P2SH
# Each bounty room is pinned to one payout network. Add new rooms explicitly.
ROOM_NETWORKS = {"L58ME6ufiP49v97UIOBIpvWKEgj4912JmECPuDzlvCI": "main"}


def room_network(room):
    """Payout network configured for ROOM."""
    if room not in ROOM_NETWORKS:
        sys.exit(f"no payout network configured for room {room}")
    return ROOM_NETWORKS[room]


def bech32_polymod(values):
    """BIP173 checksum accumulator over 5-bit VALUES."""
    chk = 1
    for value in values:
        top = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ value
        for i, gen in enumerate(BECH32_GEN):
            chk ^= gen if (top >> i) & 1 else 0
    return chk


def valid_bech32(address):
    """Whether ADDRESS is a checksum-correct bech32 (v0) or bech32m (v1+) address."""
    valid = len(address) <= 90 and (
        address.lower() == address or address.upper() == address
    )
    if valid:
        address = address.lower()
        hrp, sep, data = address.rpartition("1")
        valid = bool(sep and hrp in ("bc", "tb", "bcrt") and len(data) >= 7)
    if valid:
        values = [BECH32.index(c) for c in data if c in BECH32]
        valid = len(values) == len(data)
    if valid:
        hrp_values = [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]
        check = bech32_polymod(hrp_values + values)
        version, program = values[0], values[1:-6]
        valid = version <= 16 and check == (1 if version == 0 else 0x2BC830A3)
    if valid:
        acc = bits = 0
        out = []
        for value in program:
            acc = (acc << 5) | value
            bits += 5
            while bits >= 8:
                bits -= 8
                out.append((acc >> bits) & 0xFF)
            acc &= (1 << bits) - 1
        valid = bits < 5 and acc == 0
        valid = valid and (
            len(out) in (20, 32) if version == 0 else 2 <= len(out) <= 40
        )
    return valid


def valid_base58(address):
    """Whether ADDRESS is a checksum-correct base58check P2PKH or P2SH address."""
    pad = len(address) - len(address.lstrip("1"))
    number = 0
    for char in address[pad:]:
        if char not in BASE58:
            return False
        number = number * 58 + BASE58.index(char)
    raw = b"\x00" * pad + number.to_bytes((number.bit_length() + 7) // 8, "big")
    if len(raw) != 25:
        return False
    digest = hashlib.sha256(hashlib.sha256(raw[:21]).digest()).digest()
    return raw[21:] == digest[:4] and raw[0] in BASE58_VERSIONS


def valid_address(address):
    """Whether ADDRESS looks like, and checksums as, a Bitcoin address."""
    if not isinstance(address, str):
        return False
    if is_bech32(address):
        return valid_bech32(address)
    return 26 <= len(address) <= 35 and valid_base58(address)


def is_bech32(address):
    """Whether ADDRESS has a bech32 human-readable prefix."""
    return address.lower().startswith(("bc1", "tb1", "bcrt1"))


def normalize_address(address):
    """Lowercase bech32 addresses (case-insensitive); leave base58 unchanged."""
    return address.lower() if is_bech32(address) else address


def network_of(address):
    """Network a valid ADDRESS pays on: main, test or regtest."""
    if is_bech32(address):
        hrp = address.lower().rpartition("1")[0]
        return {"bc": "main", "tb": "test"}.get(hrp, "regtest")
    return "main" if valid_base58(address) and address[0] in "13" else "test"


def valid_payout_address(address, network="main"):
    """Validate an address for this room's configured payout network."""
    return valid_address(address) and network_of(address) == network


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
            if address is None or not valid_payout_address(address, room_network(room)):
                noaddr.append((row, row["prize_sats"]))
                continue
            address = normalize_address(address)
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


def print_payout(pay, args, pending_path, included):
    """Print the Bitcoin CLI PSBT command and save its pending batch."""
    outputs = (
        "[" + ",".join(f'{{"{a}":{btc(v)}}}' for a, v in sorted(pay.items())) + "]"
    )
    options = {"fee_rate": args.fee_rate}
    if args.change:
        options["changeAddress"] = args.change
    cmd = ["bitcoin-cli", "-named", "walletcreatefundedpsbt", "inputs=[]"]
    cmd += [f"outputs={outputs}", f"options={orjson.dumps(options).decode()}"]
    print(f"# {len(pay)} outputs, {sum(pay.values())} sats (+ network fee)")
    print(shlex.join(cmd))
    write_json(pending_path, {"outputs": pay, "rows": included})


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
    networks = {network_of(address) for address in pay}
    if len(networks) > 1:
        sys.exit(
            f"batch mixes payout networks ({', '.join(sorted(networks))}); "
            "one wallet cannot pay them. Fix the addresses with "
            "'address' or --address"
        )
    print_payout(pay, args, pending_path, included)


def rows_to_confirm(pending_rows, ledger, txid):
    """Validate pending snapshots, allowing an already-applied matching txid."""
    by_target = {}
    for row in ledger:
        target = row.get("target")
        if target in by_target:
            sys.exit(f"duplicate ledger row for target {target!r}")
        by_target[target] = row

    selected = []
    total_sats = 0
    for item in pending_rows:
        snapshot = item.get("row") if isinstance(item, dict) else None
        if not isinstance(snapshot, dict) or snapshot.get("status") != "unpaid":
            sys.exit("pending payout contains an invalid ledger snapshot")
        target = snapshot.get("target")
        row = by_target.get(target)
        if row == snapshot:
            selected.append(row)
        else:
            expected = {**snapshot, "status": "paid", "txid": txid}
            if row != expected:
                sys.exit(
                    f"pending payout row for target {target!r} no longer matches ledger"
                )
        total_sats += snapshot["prize_sats"]
    return selected, total_sats


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
    txid = args.txid.lower()
    selected, total_sats = rows_to_confirm(pending_rows, ledger, txid)

    for row in selected:
        row["status"] = "paid"
        row["txid"] = txid
    if selected:
        temp_path = f"{ledger_path}.tmp"
        with open(temp_path, "wb") as f:
            for row in ledger:
                f.write(orjson.dumps(row) + b"\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, ledger_path)
    os.unlink(pending_path)
    print(
        f"confirmed {len(pending_rows)} ledger row(s), "
        f"{total_sats} sats, paid in {txid}"
    )


def cmd_address(args):
    """Record a payout address on a claimant's unpaid, address-less ledger rows."""
    if not ROOM.fullmatch(args.room):
        sys.exit(f"invalid room ID {args.room!r}")
    if not valid_payout_address(args.payout, room_network(args.room)):
        sys.exit(f"{args.payout!r} is not a Bitcoin address")
    if os.path.exists(f"{args.room}/payout-pending.json"):
        sys.exit("a payout batch is pending; record it as paid or replace it first")
    ledger_path = f"{args.room}/ledger.jsonl"
    if not os.path.exists(ledger_path):
        sys.exit(f"no ledger for room {args.room}")
    with open(ledger_path, "rb") as f:
        ledger = [orjson.loads(line) for line in f if line.strip()]
    rows = [r for r in ledger if r["claimant"] == args.claimant]
    if not rows:
        sys.exit(f"no ledger rows for claimant {args.claimant!r}")
    changed = [r for r in rows if r["status"] == "unpaid" and r.get("address") is None]
    if not changed:
        sys.exit(f"{args.claimant}: no unpaid rows without an address")
    for row in changed:
        row["address"] = normalize_address(args.payout)
    temp_path = f"{ledger_path}.tmp"
    with open(temp_path, "wb") as f:
        for row in ledger:
            f.write(orjson.dumps(row) + b"\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp_path, ledger_path)
    sats = sum(r["prize_sats"] for r in changed)
    print(f"set the address on {len(changed)} row(s), {sats} sats, for {args.claimant}")
