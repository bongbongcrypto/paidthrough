"""PaidThrough refund keeper.

    python keeper/paidthrough_keeper.py scan  [--network testnet]
    python keeper/paidthrough_keeper.py due
    python keeper/paidthrough_keeper.py bill 7
    python keeper/paidthrough_keeper.py run              # dry-run (default): simulate only, no key
    python keeper/paidthrough_keeper.py run --send       # sign and send refund(id) for due bills
    python keeper/paidthrough_keeper.py run --send --notify

The only transaction this program can build is `refund(billId)` to the configured PaidThrough
contract with value 0. `refund` pays the bill's original payer; the keeper never receives or moves
money anywhere else. It spends only its own gas.

Global options: --network mainnet|testnet, --contract 0x.., --from-block N, --rpc URL, --state-dir DIR,
--no-cache. Deployment defaults come from keeper/deployments.json:
    {"mainnet": {"address": "0x..", "fromBlock": 123}, "testnet": null}
Key for --send: file at $PAIDTHROUGH_KEEPER_ENV (default ~/.paidthrough-keeper.env), line KEEPER_PRIVATE_KEY=0x...

Exit codes: 0 ok (skips included), 1 error or unknown state, 2 usage/config refused.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import abi  # noqa: E402
import notify as notifier  # noqa: E402
import state as st  # noqa: E402
from fmt import GWEI, fee_usdc, gwei, short, usdc6, utc  # noqa: E402
from rpc import DEFAULT_CHUNK, Rpc, RpcError, RpcUnavailable, is_range_error  # noqa: E402

NETWORKS = {
    "mainnet": {"rpc": "https://rpc.mainnet.arc.io", "chainId": 5042},
    "testnet": {"rpc": "https://rpc.testnet.arc.network", "chainId": 5042002},
}
ZERO = "0x0000000000000000000000000000000000000000"
MIN_FEE_FLOOR = 20 * GWEI          # Arc base fee floor; underpriced tx are dropped silently
GAS_MULT_NUM, GAS_MULT_DEN = 12, 10  # gas limit = ceil(estimate * 1.2)
RESCAN_OVERLAP = 100               # blocks re-read on every incremental scan
INFLIGHT_DROP_BLOCKS = 1200        # ~10 min at 0.5 s blocks: unmined and unknown to the node -> presumed dropped
MAX_MISSING_GETBILL = 5000         # new ids per run absent from events, filled by direct getBill reads
SKIP_RENOTIFY_S = 24 * 3600
ERROR_RENOTIFY_S = 6 * 3600
DEPLOYMENTS = HERE / "deployments.json"


class ConfigError(Exception):
    """Refused before touching the network (exit 2)."""


class NoDeployment(Exception):
    """Nothing configured for this network (exit 0)."""


# ---------------------------------------------------------------- config
def load_deployment(network: str, contract: str = None, from_block: int = None, path: Path = DEPLOYMENTS):
    entry = None
    if path.exists():
        try:
            entry = json.loads(path.read_text(encoding="utf-8")).get(network)
        except (ValueError, AttributeError) as e:
            raise ConfigError("%s is not valid JSON: %s" % (path.name, e)) from None
    addr = contract
    block = from_block
    if isinstance(entry, str):
        addr = addr or entry
    elif isinstance(entry, dict):
        addr = addr or entry.get("address")
        if block is None:
            for k in ("fromBlock", "deployBlock", "block"):
                if entry.get(k) is not None:
                    block = int(entry[k])
                    break
    if not addr:
        raise NoDeployment("no deployment configured for %s (keeper/deployments.json has %s; "
                           "pass --contract 0x.. --from-block N to override)" % (network, json.dumps(entry)))
    try:
        addr = abi.checksum(addr)
    except (abi.DecodeError, ValueError):
        raise ConfigError("contract address is not a 20-byte hex address: %r" % addr) from None
    if block is None:
        raise ConfigError("no from-block for %s: add \"fromBlock\" (the deploy block) to deployments.json or "
                          "pass --from-block N (scanning from block 0 would take thousands of getLogs calls)" % network)
    if block < 0:
        raise ConfigError("--from-block must be >= 0")
    return addr, int(block)


def key_path() -> Path:
    return Path(os.environ.get("PAIDTHROUGH_KEEPER_ENV") or (Path.home() / ".paidthrough-keeper.env")).expanduser()


class KeeperKey:
    """Holds the signing account. Its repr/str never contain the key."""

    def __init__(self, account):
        self._acct = account
        self.address = account.address

    def __repr__(self):
        return "<KeeperKey %s>" % self.address

    __str__ = __repr__

    def sign(self, tx: dict) -> str:
        signed = self._acct.sign_transaction(tx)
        raw = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction")
        return "0x" + bytes(raw).hex()


_KEY_RE = re.compile(r"^(0x)?[0-9a-fA-F]{64}$")


def load_key(path: Path) -> KeeperKey:
    """Read KEEPER_PRIVATE_KEY from the file. Errors never quote the file's contents."""
    from eth_account import Account

    if not path.is_file():
        raise ConfigError("--send refused: key file not found at %s (set PAIDTHROUGH_KEEPER_ENV)" % path)
    if os.name == "posix":
        mode = path.stat().st_mode & 0o077
        if mode:
            print("warning: %s is readable by group/others (chmod 600 it)" % path, file=sys.stderr)
    value = None
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s.startswith("KEEPER_PRIVATE_KEY") and "=" in s:
            value = s.split("=", 1)[1].strip().strip('"').strip("'")
    if not value:
        raise ConfigError("--send refused: no KEEPER_PRIVATE_KEY line in %s" % path)
    if not _KEY_RE.match(value):
        raise ConfigError("--send refused: KEEPER_PRIVATE_KEY in %s is not 32 bytes of hex" % path)
    try:
        acct = Account.from_key(value if value.startswith("0x") else "0x" + value)
    except Exception:  # noqa: BLE001 - never echo the exception (it may carry the key)
        raise ConfigError("--send refused: KEEPER_PRIVATE_KEY in %s is not a valid key" % path) from None
    finally:
        value = None
    return KeeperKey(acct)


# ---------------------------------------------------------------- state file (incremental scan + in-flight txs)
def state_dir(arg: str = None) -> Path:
    return Path(arg or os.environ.get("PAIDTHROUGH_KEEPER_STATE") or (Path.home() / ".paidthrough-keeper")).expanduser()


def state_file(d: Path, network: str, contract: str, from_block: int) -> Path:
    return d / ("%s-%s-%d.json" % (network, contract.lower(), from_block))


STATE_VERSION = 2   # v2: logs no longer include BillCancelled; adds seeds / terminal / lost / degraded


def load_state(path: Path, chain_id: int, contract: str, from_block: int) -> dict:
    blank = {"version": STATE_VERSION, "chainId": chain_id, "contract": contract, "fromBlock": from_block,
             "scannedTo": None, "logs": [], "inflight": {}, "notified": {}, "seeds": {}, "terminal": {},
             "lost": [], "degraded": False}
    if not path or not path.exists():
        return blank
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        print("warning: state file %s unreadable; rebuilding from events" % path, file=sys.stderr)
        return blank
    if (data.get("chainId") != chain_id or str(data.get("contract", "")).lower() != contract.lower()
            or data.get("fromBlock") != from_block):
        print("warning: state file %s is for another deployment; rebuilding" % path, file=sys.stderr)
        return blank
    if data.get("version") != STATE_VERSION:
        # older layout: re-read the logs, but never forget a sent-but-unconfirmed refund
        blank["inflight"] = data.get("inflight") or {}
        blank["notified"] = data.get("notified") or {}
        return blank
    for k, v in blank.items():
        data.setdefault(k, v)
    return data


def save_state(path: Path, data: dict) -> None:
    if not path:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------- chain reads
class Ctx:
    def __init__(self, rpc: Rpc, network: str, contract: str, from_block: int, chain_id: int,
                 out=None, err=None):
        self.rpc = rpc
        self.network = network
        self.contract = contract
        self.from_block = from_block
        self.chain_id = chain_id
        self.out = out or sys.stdout
        self.err = err or sys.stderr
        self.head = None          # block number of the snapshot
        self.head_ts = None       # its timestamp
        self.base_fee = None
        self.warnings = []

    def p(self, *a):
        print(*a, file=self.out)

    def warn(self, msg):
        self.warnings.append(msg)
        print("warning: " + msg, file=self.err)


def connect(ctx: Ctx) -> None:
    got = ctx.rpc.chain_id()
    if got != ctx.chain_id:
        raise ConfigError("RPC chain id %d does not match %s (%d); refusing" % (got, ctx.network, ctx.chain_id))
    code = ctx.rpc.call("eth_getCode", [ctx.contract, "latest"])
    if not isinstance(code, str):
        raise RpcUnavailable("eth_getCode returned %r" % (code,))
    if code in ("0x", "0x0", ""):
        raise ConfigError("no contract code at %s on %s" % (ctx.contract, ctx.network))
    b = ctx.rpc.latest_block()
    ctx.head = int(b["number"], 16)
    ctx.head_ts = int(b["timestamp"], 16)
    ctx.base_fee = int(b["baseFeePerGas"], 16) if b.get("baseFeePerGas") else None
    if ctx.head < ctx.from_block:
        raise ConfigError("from-block %d is above the chain head %d" % (ctx.from_block, ctx.head))


_RAW_FIELDS = ("address", "topics", "data", "blockNumber", "logIndex", "transactionHash")


def scan_logs(ctx: Ctx, cache: dict) -> list:
    """Decoded logs from from_block to ctx.head. With a cache, only re-reads from the last scanned
    block minus RESCAN_OVERLAP; cached logs at or above the rescan point are replaced by fresh ones.
    The cache holds the RAW logs (chain truth) and they are decoded again on every run, so a decoder
    fix can never be masked by stale cached decodes. The cache is updated only after the whole range
    was read and decoded.

    Only abi.KEEPER_TOPICS are read (no BillCancelled). If one block holds more of our logs than the RPC
    returns for a single-block query, that block is re-read one event type at a time; a type that still
    overflows is recorded in cache["lost"] and the scan continues (build_bills then fills the gap with
    getBill). A spam block can therefore never stop the keeper or freeze its cache."""
    start = ctx.from_block
    kept = []
    if cache.get("scannedTo") is not None:
        start = max(ctx.from_block, int(cache["scannedTo"]) - RESCAN_OVERLAP + 1)
        kept = [lg for lg in cache["logs"] if int(lg["blockNumber"], 16) < start]
    lost = []

    def overflow(block, err):
        part_logs = []
        for name in abi.KEEPER_EVENTS:
            try:
                part = ctx.rpc.call("eth_getLogs", [{"address": ctx.contract, "topics": [abi.EVENTS[name][0]],
                                                     "fromBlock": hex(block), "toBlock": hex(block)}])
            except RpcError as e:
                if not is_range_error(e):
                    raise
                lost.append([name, block])
                continue
            if not isinstance(part, list):
                raise RpcUnavailable("eth_getLogs block %d returned %r" % (block, type(part).__name__))
            part_logs.extend(part)
        here = [n for n, b in lost if b == block]
        ctx.warn("block %d holds more logs than the RPC returns in one query (%s); %s" % (
            block, err.message[:80],
            ("lost its %s logs, filling from getBill" % ",".join(here)) if here else "read it per event type"))
        return part_logs

    got = ctx.rpc.get_logs_chunked(ctx.contract, [abi.KEEPER_TOPICS], start, ctx.head, chunk=DEFAULT_CHUNK,
                                   on_overflow=overflow)
    fresh = []
    for lg in got:
        if lg.get("removed"):
            continue
        if str(lg.get("address", "")).lower() != ctx.contract.lower():
            raise abi.DecodeError("log from %s in a query for %s" % (lg.get("address"), ctx.contract))
        fresh.append({k: lg.get(k) for k in _RAW_FIELDS})
    raw = kept + fresh
    decoded = st.dedupe([abi.decode_log(lg) for lg in raw])
    by_key = {}
    for lg in raw:
        by_key[(int(lg["blockNumber"], 16), int(lg["logIndex"], 16))] = lg
    cache["logs"] = [by_key[k] for k in sorted(by_key)]
    cache["scannedTo"] = ctx.head
    known = cache.setdefault("lost", [])
    for item in lost:
        if item not in known:
            known.append(item)
        if item[0] in abi.LIFECYCLE_EVENTS:
            cache["degraded"] = True   # sticky: from now on getBill decides every Open/Paid bill
    return decoded


def get_bill(ctx: Ctx, bill_id: int, block="latest") -> abi.Bill:
    return abi.decode_bill(bill_id, ctx.rpc.eth_call(ctx.contract, abi.encode_get_bill(bill_id), block=block))


def bill_count(ctx: Ctx) -> int:
    return abi.decode_uint(ctx.rpc.eth_call(ctx.contract, abi.encode_bill_count(), block=hex(ctx.head)))


TERMINAL = (abi.CLAIMED, abi.REFUNDED, abi.DECLINED, abi.CANCELLED)


def _adopt(ctx: Ctx, bills: dict, cache: dict, bid: int, b: abi.Bill, why: str) -> None:
    """Make getBill the record for `bid` when it disagrees with the events. Open -> Cancelled is expected
    (BillCancelled is not scanned) and silent; anything else is a warning."""
    rec = bills.get(bid)
    if b.status in TERMINAL:
        cache.setdefault("terminal", {})[str(bid)] = b.status   # terminal states never change
    if rec is not None and not rec.broken and not st.matches_chain(rec, b):
        return
    if rec is not None and not rec.broken and rec.status == abi.OPEN and b.status == abi.CANCELLED:
        rec.status = abi.CANCELLED
        return
    if rec is not None and rec.source != "getBill":
        ctx.warn("bill %d: %s; events say %s, getBill says %s - using getBill" % (
            bid, why, rec.status_name, b.status_name))
    new = st.BillRecord.from_bill(b)
    if rec is not None:
        new.history = rec.history
    bills[bid] = new


def build_bills(ctx: Ctx, cache: dict, refresh_open: bool = False) -> dict:
    """Fold events, then cross-check against billCount() at the same block. An empty or short getLogs
    answer is 'unknown', not 'no bills':
      * ids 1..billCount() with no BillIssued log are read with getBill once; their immutable fields are
        kept in cache["seeds"] so later events for them still fold;
      * after a lost lifecycle log (cache["degraded"]), every Open/Paid bill is re-read with getBill;
      * refresh_open (scan, bill): Open-looking bills are re-read, since BillCancelled is not scanned.
    Bills getBill has shown to be in a terminal state are remembered in cache["terminal"]."""
    events = scan_logs(ctx, cache)
    count = bill_count(ctx)
    top = max((ev["billId"] for ev in events), default=0)
    if top > count:
        raise st.ImpossibleTransition("events show bill %d but billCount() is %d at block %d" % (top, count, ctx.head))
    issued = {ev["billId"] for ev in events if ev["event"] == "Issued"}
    seeds = cache.setdefault("seeds", {})
    missing = [i for i in range(1, count + 1) if i not in issued and str(i) not in seeds]
    reads = {}
    if missing:
        ctx.warn("%d bill(s) have no BillIssued log in blocks %d..%d (ids %s%s); reading them with getBill" % (
            len(missing), ctx.from_block, ctx.head, missing[:10], "..." if len(missing) > 10 else ""))
        if len(missing) > MAX_MISSING_GETBILL:
            raise RpcUnavailable("%d bills missing from events; from-block is probably wrong" % len(missing))
        for i in missing:
            b = get_bill(ctx, i, block=hex(ctx.head))
            if b.status == abi.NONE:
                raise st.ImpossibleTransition("getBill(%d) is empty but billCount() is %d" % (i, count))
            seeds[str(i)] = {k: getattr(b, k) for k in st.IMMUTABLE}
            reads[i] = b
    degraded = bool(cache.get("degraded"))
    bills = st.fold(events, seeds={int(k): v for k, v in seeds.items()}, strict=not degraded)
    for k, status in cache.get("terminal", {}).items():
        rec = bills.get(int(k))
        if rec is not None and rec.status not in TERMINAL:
            rec.status = status
    for i, b in reads.items():
        _adopt(ctx, bills, cache, i, b, "BillIssued log missing")
    recheck = set()
    if degraded:
        recheck |= {i for i, r in bills.items() if r.broken or r.status in (abi.OPEN, abi.PAID)}
        ctx.warn("degraded: lifecycle logs were lost at blocks %s; reading %d Open/Paid bills with getBill" % (
            [b for n, b in cache.get("lost", []) if n in abi.LIFECYCLE_EVENTS][:5], len(recheck)))
    if refresh_open:
        recheck |= {i for i, r in bills.items() if r.status == abi.OPEN}
    for i in sorted(recheck - set(reads)):
        _adopt(ctx, bills, cache, i, get_bill(ctx, i, block=hex(ctx.head)), "log missing")
    return bills


# ---------------------------------------------------------------- printing
def print_table(ctx: Ctx, bills) -> None:
    hdr = "%5s  %-9s  %18s  %-12s  %-12s  %-20s  %-20s" % (
        "id", "status", "amount USDC", "payee", "payer", "payBy (UTC)", "claimBy (UTC)")
    ctx.p(hdr)
    ctx.p("-" * len(hdr))
    for b in bills:
        flag = " *getBill" if getattr(b, "source", "events") != "events" else ""
        ctx.p("%5d  %-9s  %18s  %-12s  %-12s  %-20s  %-20s%s" % (
            b.id, b.status_name, usdc6(b.amount), short(b.payee), short(b.payer), utc(b.payBy), utc(b.claimBy), flag))


def head_line(ctx: Ctx) -> str:
    return "%s  contract %s  block %d  time %s" % (ctx.network, ctx.contract, ctx.head, utc(ctx.head_ts))


def cmd_scan(ctx: Ctx, cache: dict) -> int:
    bills = build_bills(ctx, cache, refresh_open=True)   # true status of Open-looking (maybe cancelled) bills
    ctx.p(head_line(ctx))
    print_table(ctx, [bills[k] for k in sorted(bills)])
    counts = {}
    for b in bills.values():
        counts[b.status_name] = counts.get(b.status_name, 0) + 1
    due = st.due(bills, ctx.head_ts)
    ctx.p("%d bills  %s  due now: %d" % (len(bills), "  ".join("%s=%d" % kv for kv in sorted(counts.items())), len(due)))
    return 0


def cmd_due(ctx: Ctx, cache: dict) -> int:
    bills = build_bills(ctx, cache)
    due = st.due(bills, ctx.head_ts)
    ctx.p(head_line(ctx))
    if not due:
        ctx.p("no bill is due for refund (status Paid and claimBy <= %s)" % utc(ctx.head_ts))
        return 0
    print_table(ctx, due)
    ctx.p("%d due, %s USDC owed back to payers" % (len(due), usdc6(sum(b.amount for b in due))))
    return 0


def cmd_bill(ctx: Ctx, cache: dict, bill_id: int) -> int:
    b = get_bill(ctx, bill_id, block=hex(ctx.head))
    ctx.p(head_line(ctx))
    if b.status == abi.NONE:
        ctx.p("bill %d does not exist (getBill status None)" % bill_id)
        return 0
    ctx.p("bill %d (getBill at block %d)" % (bill_id, ctx.head))
    for f, v in (("status", b.status_name), ("amount", "%s USDC (%d units)" % (usdc6(b.amount), b.amount)),
                 ("payee", b.payee), ("payer", b.payer), ("allowedPayer", b.allowedPayer),
                 ("payBy", "%s (%d)" % (utc(b.payBy), b.payBy)), ("claimWindow", "%d s" % b.claimWindow),
                 ("claimBy", "%s (%d)" % (utc(b.claimBy), b.claimBy)), ("ref", b.ref)):
        ctx.p("  %-12s %s" % (f, v))
    ctx.p("  %-12s %s" % ("due now", "yes" if st.is_due(b, ctx.head_ts) else "no"))
    bills = build_bills(ctx, cache)
    rec = bills.get(bill_id)
    if rec is None:
        ctx.warn("no events found for bill %d from block %d" % (bill_id, ctx.from_block))
        return 1
    if b.status == abi.CANCELLED and not any(ev["event"] == "Cancelled" for ev in rec.history):
        rec.status = abi.CANCELLED   # BillCancelled is not scanned by the keeper; getBill is the truth
        ctx.p("  (cancelled: status from getBill; the keeper does not scan BillCancelled logs)")
    if rec.source != "events":
        ctx.p("  (BillIssued log for this bill could not be read; issue fields came from getBill)")
    ctx.p("events:")
    for ev in rec.history:
        extra = {k: v for k, v in ev.items() if k not in ("event", "billId", "blockNumber", "logIndex", "txHash")}
        if "amount" in extra:
            extra["amount"] = usdc6(extra["amount"])
        for k in ("payBy", "claimBy"):
            if k in extra:
                extra[k] = utc(extra[k])
        ctx.p("  block %-10d %-9s %s  tx %s" % (ev["blockNumber"], ev["event"],
                                                 " ".join("%s=%s" % kv for kv in extra.items()), ev["txHash"]))
    diffs = st.matches_chain(rec, b)
    if diffs:
        ctx.warn("event-built record differs from getBill in: %s" % ", ".join(diffs))
        return 1
    return 0


# ---------------------------------------------------------------- refund planning and sending
def fee_params(base_fee: int, priority: int) -> tuple:
    """(maxFeePerGas, maxPriorityFeePerGas) in wei. maxFee = max(2*base, 20 gwei, base + priority)."""
    if base_fee is None or priority is None:
        raise RpcUnavailable("base fee or priority fee unknown")
    priority = max(0, int(priority))
    max_fee = max(2 * int(base_fee), MIN_FEE_FLOOR, int(base_fee) + priority)
    return max_fee, priority


def gas_limit(estimate: int) -> int:
    return -(-int(estimate) * GAS_MULT_NUM // GAS_MULT_DEN)   # ceil(estimate * 1.2)


def build_tx(contract: str, bill_id: int, chain_id: int, nonce: int, gas: int, max_fee: int, priority: int) -> dict:
    tx = {"type": 2, "chainId": int(chain_id), "nonce": int(nonce), "to": abi.checksum(contract), "value": 0,
          "data": abi.encode_refund(bill_id), "gas": int(gas), "maxFeePerGas": int(max_fee),
          "maxPriorityFeePerGas": int(priority), "accessList": []}
    check_tx(tx, contract, bill_id, chain_id)
    return tx


def check_tx(tx: dict, contract: str, bill_id: int, chain_id: int) -> None:
    """The keeper's spending invariant: only refund(billId) to the configured contract, value 0, right chain."""
    if str(tx.get("to", "")).lower() != contract.lower():
        raise AssertionError("tx 'to' is not the configured contract")
    if not abi.is_refund_calldata(tx.get("data"), bill_id):
        raise AssertionError("tx data is not refund(%d)" % bill_id)
    if tx.get("value", 0) != 0:
        raise AssertionError("tx value must be 0")
    if tx.get("chainId") != chain_id or tx.get("type") != 2:
        raise AssertionError("tx chainId/type mismatch")
    if tx["maxFeePerGas"] < MIN_FEE_FLOOR or tx["maxFeePerGas"] < tx["maxPriorityFeePerGas"]:
        raise AssertionError("maxFeePerGas below the floor or below the priority fee")


def revert_reason(err: RpcError) -> str:
    data = err.data
    if isinstance(data, dict):
        data = data.get("data")
    if isinstance(data, str) and data.startswith("0x") and len(data) >= 10:
        return abi.decode_revert(data)
    return err.message or "reverted"


def plan_refund(ctx: Ctx, bill_id: int, sender: str, priority: int, fee_cap: int) -> dict:
    """Re-check on chain and simulate. Returns {'id', 'ok': bool, 'reason', 'detail', ...}."""
    b = get_bill(ctx, bill_id)
    if b.status != abi.PAID or not (0 < b.claimBy <= ctx.head_ts):
        return {"id": bill_id, "ok": False, "reason": "state_changed",
                "detail": "getBill says %s, claimBy %s" % (b.status_name, utc(b.claimBy)), "amount": b.amount}
    data = abi.encode_refund(bill_id)
    try:
        ctx.rpc.eth_call(ctx.contract, data, frm=sender)
        est = int(ctx.rpc.call("eth_estimateGas", [{"from": sender, "to": ctx.contract, "data": data}]), 16)
    except RpcError as e:
        return {"id": bill_id, "ok": False, "reason": "revert", "detail": revert_reason(e), "amount": b.amount,
                "payer": b.payer}
    max_fee, prio = fee_params(ctx.base_fee, priority)
    gas = gas_limit(est)
    plan = {"id": bill_id, "ok": True, "amount": b.amount, "payer": b.payer, "estimate": est, "gas": gas,
            "maxFee": max_fee, "priority": prio,
            "likely_fee_wei": est * (ctx.base_fee + prio), "max_fee_wei": gas * max_fee}
    if max_fee > fee_cap:
        plan.update(ok=False, reason="fee_cap", detail="maxFee %s gwei > cap %s gwei" % (gwei(max_fee), gwei(fee_cap)))
    return plan


def print_plan(ctx: Ctx, plan: dict, sender: str) -> None:
    if not plan["ok"]:
        ctx.p("  bill %d  SKIP %s: %s" % (plan["id"], plan["reason"], plan.get("detail", "")))
        return
    ctx.p("  bill %d  refund %s USDC -> payer %s" % (plan["id"], usdc6(plan["amount"]), plan["payer"]))
    ctx.p("          tx: from %s to %s data %s value 0 chainId %d type 2" % (
        sender, ctx.contract, abi.encode_refund(plan["id"]), ctx.chain_id))
    ctx.p("          gas estimate %d, limit %d; maxFee %s gwei, priority %s gwei" % (
        plan["estimate"], plan["gas"], gwei(plan["maxFee"]), gwei(plan["priority"])))
    ctx.p("          fee ~%s USDC (at most %s USDC)" % (fee_usdc(plan["likely_fee_wei"]), fee_usdc(plan["max_fee_wei"])))


def wait_receipt(ctx: Ctx, tx_hash: str, timeout: float, poll: float, sleep=time.sleep, clock=time.monotonic):
    deadline = clock() + timeout
    while True:
        try:
            r = ctx.rpc.call("eth_getTransactionReceipt", [tx_hash])
        except RpcUnavailable:
            r = None
        if r:
            return r
        if clock() >= deadline:
            return None
        sleep(poll)


def resolve_inflight(ctx: Ctx, cache: dict) -> set:
    """Check refunds sent by earlier runs. Returns bill ids that must not be sent again this run."""
    blocked = set()
    for bid, rec in list(cache.get("inflight", {}).items()):
        r = ctx.rpc.call("eth_getTransactionReceipt", [rec["tx"]])
        if r:
            ok = int(r.get("status", "0x0"), 16) == 1
            ctx.p("  earlier tx for bill %s %s in block %d" % (bid, "succeeded" if ok else "FAILED", int(r["blockNumber"], 16)))
            del cache["inflight"][bid]
            continue
        known = ctx.rpc.call("eth_getTransactionByHash", [rec["tx"]])
        if known is None and ctx.head - int(rec["block"]) > INFLIGHT_DROP_BLOCKS:
            ctx.warn("earlier tx %s for bill %s never mined and the node no longer knows it; treating as dropped"
                     % (rec["tx"], bid))
            del cache["inflight"][bid]
            continue
        blocked.add(int(bid))
    return blocked


def cmd_run(ctx: Ctx, cache: dict, args, key: KeeperKey = None, sleep=time.sleep, clock=time.monotonic) -> tuple:
    """Returns (exit code, summary dict for the notifier)."""
    summary = {"sent": [], "skipped": [], "failed": [], "below_min": 0}
    sender = key.address if key else (args.from_address or ZERO)
    blocked = resolve_inflight(ctx, cache) if args.send else set()
    bills = build_bills(ctx, cache)
    due = st.due(bills, ctx.head_ts)
    ctx.p(head_line(ctx))
    small = sum(1 for r in due if r.amount < args.min_amount)
    ctx.p("mode: %s  sender %s  due bills: %d (%d below the %s USDC keeper minimum)" % (
        "SEND" if args.send else "dry-run", sender, len(due), small, usdc6(args.min_amount)))
    if not due:
        return 0, summary
    priority = int(ctx.rpc.call("eth_maxPriorityFeePerGas", []), 16)
    fee_cap = int(args.max_fee_gwei * GWEI)
    exit_code = 0
    sends = 0
    stop_reason = None
    for rec in due:   # largest amount first (st.due_order), so dust can never delay a real refund
        if rec.amount < args.min_amount:
            summary["below_min"] += 1
            ctx.p("  bill %d  SKIP below keeper minimum (%s < %s USDC) - payer can refund it themselves" % (
                rec.id, usdc6(rec.amount), usdc6(args.min_amount)))
            continue
        if stop_reason:
            summary["skipped"].append({"id": rec.id, "reason": stop_reason})
            ctx.p("  bill %d  SKIP %s" % (rec.id, stop_reason))
            continue
        if rec.id in blocked:
            summary["skipped"].append({"id": rec.id, "reason": "inflight"})
            ctx.p("  bill %d  SKIP inflight: earlier refund tx %s not mined yet" % (rec.id, cache["inflight"][str(rec.id)]["tx"]))
            continue
        plan = plan_refund(ctx, rec.id, sender, priority, fee_cap)
        print_plan(ctx, plan, sender)
        if not plan["ok"]:
            summary["skipped"].append({"id": rec.id, "reason": plan["reason"], "detail": plan.get("detail")})
            continue
        if not args.send:
            continue
        if sends >= args.max_sends:
            stop_reason = "max_sends"
            summary["skipped"].append({"id": rec.id, "reason": stop_reason})
            continue
        balance = int(ctx.rpc.call("eth_getBalance", [key.address, "latest"]), 16)
        if balance < plan["max_fee_wei"]:
            ctx.p("  keeper balance %s USDC < worst-case fee %s USDC; stopping sends" % (
                fee_usdc(balance), fee_usdc(plan["max_fee_wei"])))
            stop_reason = "no_gas_money"
            summary["skipped"].append({"id": rec.id, "reason": stop_reason})
            continue
        nonce = int(ctx.rpc.call("eth_getTransactionCount", [key.address, "pending"]), 16)
        tx = build_tx(ctx.contract, rec.id, ctx.chain_id, nonce, plan["gas"], plan["maxFee"], plan["priority"])
        check_tx(tx, ctx.contract, rec.id, ctx.chain_id)   # again, right before signing
        raw = key.sign(tx)
        try:
            tx_hash = ctx.rpc.call("eth_sendRawTransaction", [raw])
        except RpcError as e:
            ctx.p("  bill %d  send rejected by node: %s" % (rec.id, e.message))
            summary["failed"].append({"id": rec.id, "why": "rejected: " + e.message})
            exit_code = 1
            continue
        finally:
            raw = None
        sends += 1
        cache.setdefault("inflight", {})[str(rec.id)] = {"tx": tx_hash, "block": ctx.head, "nonce": nonce}
        ctx.p("  bill %d  sent %s (nonce %d)" % (rec.id, tx_hash, nonce))
        receipt = wait_receipt(ctx, tx_hash, args.receipt_timeout, args.poll, sleep=sleep, clock=clock)
        if receipt is None:
            ctx.p("  bill %d  no receipt after %ss; status unknown, stopping sends this run" % (rec.id, args.receipt_timeout))
            summary["failed"].append({"id": rec.id, "why": "no receipt"})
            stop_reason = "inflight"
            exit_code = 1
            continue
        del cache["inflight"][str(rec.id)]
        used = int(receipt["gasUsed"], 16)
        price = int(receipt.get("effectiveGasPrice") or hex(plan["maxFee"]), 16)
        fee_wei = used * price
        ok = int(receipt.get("status", "0x0"), 16) == 1
        ctx.p("  bill %d  %s in block %d, gas %d, fee %s USDC (%d wei)" % (
            rec.id, "REFUNDED" if ok else "FAILED (reverted)", int(receipt["blockNumber"], 16), used,
            fee_usdc(fee_wei), fee_wei))
        if ok:
            summary["sent"].append({"id": rec.id, "amount": plan["amount"], "fee_wei": fee_wei, "tx": tx_hash})
        else:
            summary["failed"].append({"id": rec.id, "why": "reverted on chain", "fee_wei": fee_wei})
            exit_code = 1
    return exit_code, summary


def maybe_notify(network: str, summary: dict, cache: dict, error: str = "", sender=None, now=None,
                 out=sys.stdout) -> None:
    """One card per run, only when something happened. Repeat skips (e.g. a blocklisted payer) and repeat
    read errors are de-duplicated through the state file so the operator is not messaged every 5 minutes."""
    now = now if now is not None else time.time()
    sender = sender or notifier.send_card
    seen = cache.setdefault("notified", {})
    if error:
        if now - seen.get("error", 0) < ERROR_RENOTIFY_S:
            return
        seen["error"] = now
        text = notifier.summary_card(network, [], [], [], error=error)
    else:
        seen.pop("error", None)
        fresh = []
        for s in summary["skipped"]:
            k = "skip:%s:%s" % (s["id"], s["reason"])
            if now - seen.get(k, 0) >= SKIP_RENOTIFY_S:
                seen[k] = now
                fresh.append(s)
        text = notifier.summary_card(network, summary["sent"], fresh, summary["failed"],
                                     below_min=summary.get("below_min", 0))
    if not text:
        return
    ok, why = sender(text)
    print("telegram: %s" % ("sent" if ok else "not sent (%s)" % why), file=out)


# ---------------------------------------------------------------- CLI
DEFAULT_MIN_AMOUNT = 50_000   # 0.05 USDC in 6-decimal units, ~25x the expected refund gas (~0.002 USDC)


def usdc_units(text: str) -> int:
    """'0.05' -> 50000 (6-decimal units). Exact: more than 6 decimals or a negative value is refused."""
    from decimal import Decimal, InvalidOperation
    try:
        d = Decimal(text)
    except InvalidOperation:
        raise argparse.ArgumentTypeError("not a number: %r" % text) from None
    units = d * 10 ** 6
    if not d.is_finite() or d < 0 or units != units.to_integral_value():
        raise argparse.ArgumentTypeError("USDC amount must be >= 0 with at most 6 decimals: %r" % text)
    return int(units)


def _global_options(p, defaults: bool):
    d = (lambda v: v) if defaults else (lambda v: argparse.SUPPRESS)
    p.add_argument("--network", choices=sorted(NETWORKS), default=d("mainnet"))
    p.add_argument("--contract", default=d(None), help="PaidThrough address (default: deployments.json)")
    p.add_argument("--from-block", type=int, default=d(None),
                   help="first block to scan (default: deployments.json)")
    p.add_argument("--rpc", default=d(None), help="RPC URL (default: the public Arc RPC for the network)")
    p.add_argument("--state-dir", default=d(None), help="incremental scan cache (default ~/.paidthrough-keeper)")
    p.add_argument("--no-cache", action="store_true", default=d(False),
                   help="re-read all logs from from-block (in-flight tx records are kept)")


def parse(argv):
    """Global options work before or after the command (`scan --network testnet` == `--network testnet scan`)."""
    ap = argparse.ArgumentParser(prog="paidthrough_keeper", description="PaidThrough refund keeper")
    _global_options(ap, True)
    common = argparse.ArgumentParser(add_help=False)
    _global_options(common, False)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("scan", parents=[common], help="rebuild every bill from events")
    sub.add_parser("due", parents=[common], help="bills that can be refunded now")
    b = sub.add_parser("bill", parents=[common], help="one bill: getBill + event history")
    b.add_argument("id", type=int)
    r = sub.add_parser("run", parents=[common], help="refund due bills (dry-run unless --send)")
    r.add_argument("--send", action="store_true", help="sign and send (needs the key file)")
    r.add_argument("--notify", action="store_true", help="send one Telegram summary")
    r.add_argument("--from-address", help="dry-run simulation sender (default zero address)")
    r.add_argument("--max-sends", type=int, default=20)
    r.add_argument("--min-amount", type=usdc_units, default=DEFAULT_MIN_AMOUNT, metavar="USDC",
                   help="skip bills below this amount (default 0.05): the payer can call refund themselves")
    r.add_argument("--max-fee-gwei", type=float, default=500.0, help="skip if maxFeePerGas would exceed this")
    r.add_argument("--receipt-timeout", type=float, default=90.0)
    r.add_argument("--poll", type=float, default=1.0)
    return ap.parse_args(argv)


def main(argv=None, rpc_factory=None, out=None, err=None, key_loader=None, notify_sender=None,
         sleep=time.sleep, clock=time.monotonic, deployments: Path = DEPLOYMENTS) -> int:
    out = out or sys.stdout
    err = err or sys.stderr
    args = parse(argv if argv is not None else sys.argv[1:])
    net = NETWORKS[args.network]
    try:
        contract, from_block = load_deployment(args.network, args.contract, args.from_block, deployments)
    except NoDeployment as e:
        print(str(e), file=out)
        return 0
    except ConfigError as e:
        print("error: " + str(e), file=err)
        return 2
    if args.cmd == "run" and args.from_address:
        try:
            args.from_address = abi.checksum(args.from_address)
        except (abi.DecodeError, ValueError):
            print("error: --from-address is not a 20-byte hex address", file=err)
            return 2
    key = None
    if args.cmd == "run" and args.send:
        try:
            key = (key_loader or load_key)(key_path())
        except ConfigError as e:
            print("error: " + str(e), file=err)
            return 2
    rpc = (rpc_factory or (lambda url: Rpc(url)))(args.rpc or net["rpc"])
    ctx = Ctx(rpc, args.network, contract, from_block, net["chainId"], out=out, err=err)
    spath = state_file(state_dir(args.state_dir), args.network, contract, from_block)
    cache = load_state(spath, net["chainId"], contract, from_block)
    if args.no_cache:   # forget cached logs only; in-flight txs and alert de-dup are kept (double-send guard)
        cache["logs"], cache["scannedTo"] = [], None
    notify = args.cmd == "run" and args.notify
    try:
        connect(ctx)
        if args.cmd == "scan":
            code = cmd_scan(ctx, cache)
        elif args.cmd == "due":
            code = cmd_due(ctx, cache)
        elif args.cmd == "bill":
            code = cmd_bill(ctx, cache, args.id)
        else:
            try:
                code, summary = cmd_run(ctx, cache, args, key=key, sleep=sleep, clock=clock)
            finally:
                save_state(spath, cache)   # in-flight txs must survive any later failure
            if notify:
                maybe_notify(args.network, summary, cache, sender=notify_sender, out=out)
        save_state(spath, cache)
        return code
    except ConfigError as e:
        print("error: " + str(e), file=err)
        return 2
    except (RpcUnavailable, RpcError, abi.DecodeError, st.ImpossibleTransition) as e:
        print("error (state unknown, nothing sent after this point): %s: %s" % (type(e).__name__, e), file=err)
        if notify:
            maybe_notify(args.network, {"sent": [], "skipped": [], "failed": []}, cache,
                         error=type(e).__name__, sender=notify_sender, out=out)
        try:
            save_state(spath, cache)   # raw logs are only committed after a full read; keeps in-flight + notified
        except OSError:
            pass
        return 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass
    sys.exit(main())
