"""Real mainnet evidence with two TEST wallets: a bill paid and collected, a bill declined (money back), and a
bill left uncollected so the server keeper refunds it. Dry run by default: nothing is signed or sent.

Owner steps (Windows PowerShell 5.1, from the paidthrough folder):

    python scripts/test_bills.py fund                  # dry run: balances, gas, what A would send
    python scripts/test_bills.py fund --send           # type SEND: A sends 1.10 USDC to payer, 0.05 USDC to biller
    python scripts/test_bills.py bills                 # dry run: checks + the full plan
    python scripts/test_bills.py bills --send          # type SEND: runs bills A -> C -> B
    python scripts/test_bills.py bills --resume --send # after a stop: continues from the first unfinished step
    python scripts/test_bills.py bills --resume --send --accept-step A.pay
                                                       # only if a step stopped on a native balance-delta mismatch
                                                       # (e.g. someone sent dust in that block): type ACCEPT A.pay
    python scripts/test_bills.py status                # read-only: the three bills now (and bill B's refund tx)

Money and units
  * Native USDC (gas, tx value, eth_getBalance) has 18 decimals; the ERC-20 view at 0x3600...0000 has 6 decimals of
    the same money (1 base unit = 1e12 wei). Bills are 500000 base units = 0.50 USDC each. Funding is native:
    payer 1.1e18 wei (1.10 USDC), biller 5e16 wei (0.05 USDC). Printed costs round UP (never understated).
  * Order A -> C -> B so the payer never holds more than 1.0 USDC of bills at once (A and C are both paid before
    C's decline returns 0.50; then B is paid). Payer needs 1.0 USDC + gas, about 1.02 USDC; 1.10 leaves margin.
  * fund refuses if deployer A (also the server refund keeper, same key) would keep less than 0.50 USDC.
  * Fees: maxFeePerGas = max(2 x baseFee, 20 gwei, baseFee + priority) (Arc drops tx under its 20 gwei floor
    silently); priority from eth_maxPriorityFeePerGas; gas limit = ceil(estimate x 1.2). Pending nonce, re-read
    right before every send.

Evidence checked after every confirmed step (the run stops at the first mismatch)
  * Bill ids come from the BillIssued log in the issue receipt (emitted by the PaidThrough address), never
    predicted. getBill is read at the receipt's block and must show the expected status and fields.
  * The USDC Transfer logs in the receipt (address 0x3600...0000, 6 decimals) must be exactly the expected ones.
  * Payer and biller native balances at block-1 vs block must move by exactly
    (token flow x 1e12) - (gasUsed x effectiveGasPrice if that wallet sent the tx), to the wei.

Crash safety
  * The run record (status/test-bills-2026-10-03.json + .md, addresses / bill ids / ref texts / salts / tx hashes,
    never keys) is written before the first transaction (salts), before every broadcast (hash + nonce) and after
    every confirmed step.
  * --resume re-derives each step from the chain: an in-flight hash is looked up (receipt, then nonce); issue
    looks for a BillIssued log with our salted ref first; pay re-reads the bill status (only Open is paid);
    approve re-reads the allowance; claim/decline re-read the status. A dropped tx (not pending, nonce still
    free) is re-sent with that same nonce, so the original and the re-send can never both land. A tx whose nonce
    another tx used and that no block holds after 3 more blocks is dead and the step is sent fresh.
  * Nonces: nothing is signed while the sender has a tx in the pool (pending != latest; the deployer key is also
    the server keeper's) or while the node reports a nonce below one the record already saw confirmed.
  * fund --send first resolves every funding tx the record holds as 'sent': landed -> adopted, never sent again;
    no receipt and its nonce still free -> stop (it can still land); nonce used by another tx -> dead after 3 blocks.

Keys: read only by the loader below from files outside the repo ($PAIDTHROUGH_DEPLOYER_ENV,
$PAIDTHROUGH_TEST_BILLER_ENV, $PAIDTHROUGH_TEST_PAYER_ENV; defaults ~/.paidthrough-deployer.env,
~/.paidthrough-test-biller.env, ~/.paidthrough-test-payer.env). Never printed or written. The dry run uses a key
file only to derive its address (no signing) and falls back to the known address when the file is absent.

Exit codes: 0 ok / ready, 1 stopped mid-run (see the resume line), 2 refused before sending, 3 dry run not ready.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import sys
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "keeper"))

from eth_abi import decode, encode  # noqa: E402
from eth_account import Account  # noqa: E402

import abi as kabi  # noqa: E402  (keeper/abi.py: getBill / events / revert decoding, from SPEC.md signatures)
import deploy_mainnet as dm  # noqa: E402  (fee floor, gas limit, unit formatting, receipt wait, checksum)
from rpc import Rpc, RpcError, RpcUnavailable  # noqa: E402  (keeper/rpc.py: retries, 429 backoff, User-Agent)

# ---------------------------------------------------------------------------------------------- constants

CHAIN_ID = 5042
RPC_URL = "https://rpc.mainnet.arc.io"
EXPLORER = "https://explorer.arc.io"
PAGES = "https://bongbongcrypto.github.io/paidthrough/"
USDC = dm.USDC
DEPLOYMENTS = ROOT / "keeper" / "deployments.json"
DEFAULT_RECORD = ROOT / "status" / "test-bills-2026-10-03.json"

USDC_DOMAIN_SEPARATOR = "0x940506929bba468048a19b567f4f0d534714bc06604b5c3017e5d16785ccdf84"  # SPEC.md, mainnet
RECEIVE_TYPE = ("ReceiveWithAuthorization(address from,address to,uint256 value,uint256 validAfter,"
                "uint256 validBefore,bytes32 nonce)")
RECEIVE_TYPEHASH = "0xd099cc98ef71107a616c4f0f941f04c322d8e254fe26b3c6668db87aae413de8"  # SPEC.md
DOMAIN_TYPE = "EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

WEI_PER_UNIT = 10**12                 # 1 USDC base unit (6 dec) = 1e12 wei (18 dec)
AMOUNT = 500_000                      # 0.50 USDC per bill, 6-decimal base units
FUND_PAYER = 11 * 10**17              # 1.10 USDC native (18 decimals)
FUND_BILLER = 5 * 10**16              # 0.05 USDC native
A_RESERVE = 5 * 10**17                # deployer/keeper A keeps >= 0.50 USDC for refund gas
PAY_WINDOW = 3 * 86400                # payBy = block time + 3 days
VALID_FOR = 3600                      # EIP-3009 validBefore = min(payBy, block time + 1 hour)
KST = timezone(timedelta(hours=9))

# Addresses the owner recorded for these roles. A key file that derives something else is refused for --send.
KNOWN = {
    "deployer": "0x708B05A24D300c83f3D365DAD79a4c0b52D9038D",
    "biller": "0x7421346433d41fb6fA52317a8f0EEb5275bc2051",
    "payer": "0x6fAc56a942714aEa400f521D4c870dFB12D9d83b",
}
ROLES = {  # role -> (env var with the key file path, default file name in ~, variable inside the file)
    "deployer": ("PAIDTHROUGH_DEPLOYER_ENV", ".paidthrough-deployer.env", "DEPLOYER_PRIVATE_KEY"),
    "biller": ("PAIDTHROUGH_TEST_BILLER_ENV", ".paidthrough-test-biller.env", "BILLER_PRIVATE_KEY"),
    "payer": ("PAIDTHROUGH_TEST_PAYER_ENV", ".paidthrough-test-payer.env", "PAYER_PRIVATE_KEY"),
}

# The three bills, in run order. claimWindow in seconds (B uses the 1-hour minimum so the keeper refunds it soon).
BILLS = {
    "A": {"label": "collected", "text": "Test bill A (mainnet check)", "claimWindow": 7 * 86400},
    "C": {"label": "declined", "text": "Test bill C (mainnet check)", "claimWindow": 7 * 86400},
    "B": {"label": "refunded by the keeper", "text": "Test bill B (mainnet check)", "claimWindow": 3600},
}
# (step name, bill, kind, who sends). Order A -> C -> B is load-bearing (payer peak 1.0 USDC).
STEPS = [
    ("A.issue", "A", "issue", "biller"),
    ("A.pay", "A", "pwa", "payer"),
    ("A.claim", "A", "claim", "biller"),
    ("C.issue", "C", "issue", "biller"),
    ("C.approve", "C", "approve", "payer"),
    ("C.pay", "C", "pay", "payer"),
    ("C.decline", "C", "decline", "biller"),
    ("B.issue", "B", "issue", "biller"),
    ("B.pay", "B", "pwa", "payer"),
]
# Gas from the real-node rehearsal (status/rehearsal-2026-10-02.md), used only for the dry-run budget.
GAS_HINT = {"issue": 139_189, "pwa": 111_862, "approve": 56_253, "pay": 74_601, "claim": 59_722, "decline": 66_384}
STATUS_AFTER = {"issue": kabi.OPEN, "approve": kabi.OPEN, "pwa": kabi.PAID, "pay": kabi.PAID,
                "claim": kabi.CLAIMED, "decline": kabi.DECLINED}
EVENT_OF = {"pwa": "Paid", "pay": "Paid", "claim": "Claimed", "decline": "Declined"}

_KEY_RE = re.compile(r"^(0x)?[0-9a-fA-F]{64}$")


class Refused(Exception):
    """Refused before anything was sent (exit 2)."""


class Stop(Exception):
    """A run stopped part-way: unknown state, mismatch or failed tx (exit 1). The record says where."""


# ---------------------------------------------------------------------------------------------- small helpers


def kec(b: bytes) -> bytes:
    return dm.kec(b)


def fn(sig: str, types=(), args=()) -> str:
    return "0x" + (kec(sig.encode())[:4] + (encode(list(types), list(args)) if types else b"")).hex()


def usdc18(wei: int) -> str:
    return dm.usdc18(wei)


def usdc6(units: int) -> str:
    return dm.usdc6(units)


def same(a: str, b: str) -> bool:
    return isinstance(a, str) and isinstance(b, str) and a.lower() == b.lower()


def when(ts: int) -> str:
    t = int(ts)
    return "%d = %s UTC = %s KST" % (t, datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                                     datetime.fromtimestamp(t, KST).strftime("%Y-%m-%d %H:%M:%S"))


def fingerprint(salt_hex: str, text: str) -> str:
    """SPEC.md: sha256(utf8("paidthrough:v1:" + saltHex + ":" + text)), saltHex = 32 lowercase hex chars."""
    if not re.fullmatch(r"[0-9a-f]{32}", salt_hex):
        raise ValueError("salt must be 32 lowercase hex chars")
    return "0x" + hashlib.sha256(("paidthrough:v1:" + salt_hex + ":" + text).encode("utf-8")).hexdigest()


def family_link(bill_id: int, text: str, salt: str) -> str:
    return "%s#/bill/%d?%s" % (PAGES, int(bill_id), urllib.parse.urlencode({"r": text, "s": salt}))


def tx_link(h: str) -> str:
    return "%s/tx/%s" % (EXPLORER, h)


def domain_separator(chain_id: int = CHAIN_ID, verifying: str = USDC) -> bytes:
    return kec(encode(["bytes32", "bytes32", "bytes32", "uint256", "address"],
                      [kec(DOMAIN_TYPE.encode()), kec(b"USDC"), kec(b"2"), chain_id, verifying]))


def auth_digest(payer: str, to: str, value: int, valid_after: int, valid_before: int, nonce: bytes,
                ds: bytes) -> bytes:
    """EIP-712 digest of USDC ReceiveWithAuthorization (EIP-3009)."""
    struct = kec(encode(["bytes32", "address", "address", "uint256", "uint256", "uint256", "bytes32"],
                        [kec(RECEIVE_TYPE.encode()), payer, to, value, valid_after, valid_before, nonce]))
    return kec(b"\x19\x01" + ds + struct)


def revert_reason(e: RpcError) -> str:
    data = e.data if isinstance(e.data, str) else (e.data.get("data") if isinstance(e.data, dict) else None)
    label = kabi.decode_revert(data) if data else ""
    return ("%s (%s)" % (label, e.message)) if label and label != "reverted (no reason)" else e.message


def deployment() -> tuple[str, int]:
    d = json.loads(DEPLOYMENTS.read_text(encoding="utf-8")).get("mainnet")
    if not d or not d.get("address"):
        raise Refused("keeper/deployments.json has no mainnet address")
    return dm.checksum(d["address"]), int(d["fromBlock"])


# ---------------------------------------------------------------------------------------------- keys


class Signer:
    """Holds one signing account. repr/str never contain the key."""

    def __init__(self, role: str, account):
        self.role = role
        self._acct = account
        self.address = dm.checksum(account.address)

    def __repr__(self):
        return "<Signer %s %s>" % (self.role, self.address)

    __str__ = __repr__

    def sign_tx(self, tx: dict) -> str:
        signed = self._acct.sign_transaction(tx)
        raw = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction")
        return "0x" + bytes(raw).hex()

    def sign_digest(self, digest: bytes):
        sig = self._acct.unsafe_sign_hash(digest)
        return int(sig.v), int(sig.r).to_bytes(32, "big"), int(sig.s).to_bytes(32, "big")


def key_file(role: str) -> Path:
    env, name, _ = ROLES[role]
    return Path(os.environ.get(env) or (Path.home() / name)).expanduser()


def load_key(role: str, path: Path | None = None, repo_root: Path = ROOT) -> Signer:
    """Read <ROLE>_PRIVATE_KEY from the role's key file. Messages never quote the file's contents."""
    path = Path(path or key_file(role))
    var = ROLES[role][2]
    try:
        resolved = path.resolve()
        if resolved == repo_root.resolve() or repo_root.resolve() in resolved.parents:
            raise Refused("%s key file must live outside the repo (%s)" % (role, path))
    except OSError:
        pass
    if not path.is_file():
        raise Refused("%s key file not found at %s (set %s)" % (role, path, ROLES[role][0]))
    value = None
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s.startswith("export "):
            s = s[7:].strip()
        if "=" in s and s.split("=", 1)[0].strip() == var:
            value = s.split("=", 1)[1].strip().strip('"').strip("'")
    if not value:
        raise Refused("no %s line in %s" % (var, path))
    if not _KEY_RE.match(value):
        value = None
        raise Refused("%s in %s is not 32 bytes of hex" % (var, path))
    try:
        acct = Account.from_key(value if value.startswith("0x") else "0x" + value)
    except Exception:  # noqa: BLE001 - never echo the exception (it may carry the key)
        raise Refused("%s in %s is not a valid key" % (var, path)) from None
    finally:
        value = None
    return Signer(role, acct)


def dry_address(role: str, p) -> tuple[str, bool]:
    """Dry run: the role's address derived from its key file when present (no signing), else the known one.
    Returns (address, usable_for_send)."""
    path = key_file(role)
    if not path.is_file():
        p("  %-8s %s (known address; no key file at %s)" % (role, KNOWN[role], path))
        return KNOWN[role], False
    try:
        addr = load_key(role, path).address
    except Refused as e:
        p("  %-8s %s (known address; key file unusable: %s)" % (role, KNOWN[role], e))
        return KNOWN[role], False
    if not same(addr, KNOWN[role]):
        p("  %-8s %s (from %s) DIFFERS from the recorded %s" % (role, addr, path, KNOWN[role]))
        return addr, False
    p("  %-8s %s (derived from %s, matches the recorded address)" % (role, addr, path))
    return addr, True


def send_signer(role: str) -> Signer:
    s = load_key(role)
    if not same(s.address, KNOWN[role]):
        raise Refused("%s key file derives %s, not the recorded %s" % (role, s.address, KNOWN[role]))
    return s


# ---------------------------------------------------------------------------------------------- record


def load_record(path: Path):
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def save_record(path: Path, rec: dict) -> None:
    rec["updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(rec, indent=2) + "\n", encoding="utf-8", newline="\n")
    os.replace(tmp, path)
    md = path.with_suffix(".md")
    tmp = md.with_suffix(".md.tmp")
    tmp.write_text(record_markdown(rec), encoding="utf-8", newline="\n")
    os.replace(tmp, md)


def record_markdown(rec: dict) -> str:
    a = rec.get("addresses", {})
    out = ["## PaidThrough mainnet test bills", "",
           "Contract `%s` on Arc mainnet (chain %d). Biller `%s`, payer `%s`, funder `%s`. Updated %s." % (
               rec.get("contract"), CHAIN_ID, a.get("biller"), a.get("payer"), a.get("deployer"),
               rec.get("updated")), ""]
    fund = (rec.get("fund") or {}).get("txs") or []
    if fund:
        out += ["| Funding | State | Value USDC | Tx | Block | Gas | Cost USDC |", "|---|---|---|---|---|---|---|"]
        for t in fund:
            out.append("| %s | %s | %s | [%s](%s) | %s | %s | %s |" % (
                t.get("role"), t.get("state", ""), usdc18(t.get("valueWei", 0)), t["hash"][:12], tx_link(t["hash"]),
                t.get("block", ""), t.get("gasUsed", ""), t.get("costUSDC", "")))
        out.append("")
    bills = rec.get("bills") or {}
    if bills:
        out += ["| Bill | Purpose | Id | Amount | Claim window | Family link |", "|---|---|---|---|---|---|"]
        for k in ("A", "C", "B"):
            b = bills.get(k)
            if not b:
                continue
            link = family_link(b["billId"], b["text"], b["salt"]) if b.get("billId") else ""
            out.append("| %s | %s | %s | %s USDC | %ss | %s |" % (k, b["label"], b.get("billId") or "-", usdc6(AMOUNT),
                                                                b["claimWindow"], link and "[open](%s)" % link))
        out.append("")
        out += ["| Step | State | Tx | Block | Gas used | Cost USDC |", "|---|---|---|---|---|---|"]
        for name, *_ in STEPS:
            s = (rec.get("steps") or {}).get(name) or {}
            t = s.get("tx") or {}
            out.append("| %s | %s | %s | %s | %s | %s |" % (
                name, s.get("state", "todo"), ("[%s](%s)" % (t["hash"][:12], tx_link(t["hash"]))) if t.get("hash")
                else (s.get("note") or ""), t.get("block", ""), t.get("gasUsed", ""), t.get("costUSDC", "")))
        for name, *_ in STEPS:
            if ((rec.get("steps") or {}).get(name) or {}).get("accepted"):
                out += ["", "%s: %s" % (name, rec["steps"][name]["accepted"])]
        if bills.get("B", {}).get("claimBy"):
            out += ["", "Bill B claimBy %s; the server keeper (every 5 min) refunds it after that." % when(
                bills["B"]["claimBy"])]
        ref = (rec.get("refund") or {})
        if ref.get("hash"):
            out += ["", "Bill B refund: [%s](%s) in block %s by %s." % (ref["hash"][:12], tx_link(ref["hash"]),
                                                                         ref.get("block"), ref.get("caller"))]
    if rec.get("stoppedAt"):
        out += ["", "Stopped at %s: %s" % (rec["stoppedAt"].get("step"), rec["stoppedAt"].get("reason"))]
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------------------------- chain reads


class Chain:
    def __init__(self, rpc: Rpc, pt: str):
        self.rpc = rpc
        self.pt = pt

    def chain_id(self) -> int:
        return int(self.rpc.call("eth_chainId", []), 16)

    def balance(self, addr: str, tag="latest") -> int:
        return int(self.rpc.call("eth_getBalance", [addr, tag]), 16)

    def code(self, addr: str) -> bool:
        return dm.has_code(self.rpc, addr)

    def nonce(self, addr: str, tag: str) -> int:
        return int(self.rpc.call("eth_getTransactionCount", [addr, tag]), 16)

    def fees(self):
        head = self.rpc.call("eth_getBlockByNumber", ["latest", False])
        if not head or "number" not in head or "timestamp" not in head:
            raise RpcUnavailable("eth_getBlockByNumber(latest) returned no block")
        base = int(head.get("baseFeePerGas") or "0x0", 16)
        prio = int(self.rpc.call("eth_maxPriorityFeePerGas", []), 16)
        max_fee, prio = dm.fee_params(base, prio)
        return int(head["number"], 16), int(head["timestamp"], 16), base, max_fee, prio

    def call(self, to: str, data: str, tag="latest", frm: str | None = None) -> str:
        tx = {"to": to, "data": data}
        if frm:
            tx["from"] = frm
        return self.rpc.call("eth_call", [tx, tag])

    def bill(self, bill_id: int, tag="latest") -> kabi.Bill:
        return kabi.decode_bill(bill_id, self.call(self.pt, kabi.encode_get_bill(bill_id), tag))

    def auth_nonce(self, bill_id: int) -> bytes:
        raw = bytes.fromhex(self.call(self.pt, fn("authNonce(uint256)", ["uint256"], [bill_id]))[2:])
        if len(raw) != 32:
            raise RpcUnavailable("authNonce(%d) returned %d bytes" % (bill_id, len(raw)))
        return raw

    def bill_count(self) -> int:
        return kabi.decode_uint(self.call(self.pt, kabi.encode_bill_count()))

    def allowance(self, owner: str, spender: str, tag="latest") -> int:
        return kabi.decode_uint(self.call(USDC, fn("allowance(address,address)", ["address", "address"],
                                                   [owner, spender]), tag))

    def blacklisted(self, addr: str) -> bool:
        return bool(kabi.decode_uint(self.call(USDC, fn("isBlacklisted(address)", ["address"], [addr]))))

    def usdc_domain(self) -> str:
        return self.call(USDC, fn("DOMAIN_SEPARATOR()")).lower()

    def receipt(self, h: str):
        return self.rpc.call("eth_getTransactionReceipt", [h])

    def block_time(self, number: int) -> int:
        b = self.rpc.call("eth_getBlockByNumber", [hex(number), False])
        if not b or "timestamp" not in b:
            raise RpcUnavailable("block %d not available" % number)
        return int(b["timestamp"], 16)

    def logs(self, topics, from_block: int) -> list:
        head = int(self.rpc.call("eth_blockNumber", []), 16)
        return self.rpc.get_logs_chunked(self.pt, topics, from_block, head)


def id_topic(bill_id: int) -> str:
    return "0x" + int(bill_id).to_bytes(32, "big").hex()


def addr_topic(addr: str) -> str:
    return "0x" + "0" * 24 + addr.lower()[2:]


def usdc_transfers(receipt: dict) -> list[tuple[str, str, int]]:
    out = []
    for lg in receipt.get("logs") or []:
        t = lg.get("topics") or []
        if same(lg.get("address"), USDC) and t and t[0].lower() == TRANSFER_TOPIC and len(t) == 3:
            out.append((dm.checksum("0x" + t[1][-40:]), dm.checksum("0x" + t[2][-40:]), int(lg.get("data"), 16)))
    return out


def tx_info(receipt: dict, h: str, nonce, **extra) -> dict:
    gu = int(receipt.get("gasUsed", "0x0"), 16)
    egp = int(receipt.get("effectiveGasPrice", "0x0"), 16)
    d = {"hash": h, "nonce": nonce, "from": dm.checksum(receipt["from"]) if receipt.get("from") else None,
         "block": int(receipt["blockNumber"], 16), "gasUsed": gu, "effectiveGasPrice": egp, "costWei": gu * egp,
         "costUSDC": usdc18(gu * egp), "explorer": tx_link(h)}
    d.update(extra)
    return d


def check_shape(tx: dict, to: str, value: int) -> None:
    ok = (tx.get("type") == 2 and tx.get("chainId") == CHAIN_ID and same(tx.get("to"), to)
          and tx.get("value") == value and tx.get("maxFeePerGas", 0) >= dm.MIN_FEE_FLOOR
          and 0 <= tx.get("maxPriorityFeePerGas", -1) <= tx["maxFeePerGas"] and tx.get("gas", 0) > 0)
    if not ok:
        raise AssertionError("tx shape check failed")


# ---------------------------------------------------------------------------------------------- sending


RERUN_FUND = "python scripts/test_bills.py fund --send"            # what the owner reruns after a Stop
RERUN_BILLS = "python scripts/test_bills.py bills --resume --send"
NONCE_TRIES = 5          # nonce reads before giving up (2 s apart): a keeper tx in flight mines in seconds
DEAD_BLOCKS = 3          # blocks to wait before calling a tx whose nonce was used by another tx dead ...
DEAD_SECONDS = 10.0      # ... and at least this long (Arc blocks are ~0.5 s; RPC replicas may index late)


class Sender:
    def __init__(self, chain: Chain, p, sleep, timeout: float, rerun: str = RERUN_BILLS):
        self.chain = chain
        self.p = p
        self.sleep = sleep
        self.timeout = timeout
        self.rerun = rerun

    def next_nonce(self, frm: str, label: str, floor: int = 0, want: int | None = None) -> int:
        """The nonce to sign with. Stops (nothing recorded, nothing sent) when the sender still has a tx in the
        pool (pending != latest, as deploy_mainnet.py refuses), when the node reports a nonce below one the record
        already saw confirmed (a lagging node), or when `want` (the nonce of a dropped tx this one supersedes) is
        not the next nonce."""
        c = self.chain
        why = ""
        for i in range(NONCE_TRIES):
            if i:
                self.sleep(2.0)
            latest, pending = c.nonce(frm, "latest"), c.nonce(frm, "pending")
            if pending != latest:
                why = "%s has %d pending transaction(s) (latest nonce %d, pending %d)" % (
                    frm, pending - latest, latest, pending)
            elif pending < floor:
                why = "the node reports nonce %d for %s but the record already holds a confirmed tx at nonce %d " \
                      "(node behind)" % (pending, frm, floor - 1)
            elif want is not None and pending != want:
                why = "the dropped tx at nonce %d can only be superseded at that nonce, but the next nonce is %d" % (
                    want, pending)
            else:
                return pending
        raise Stop("%s: %s; nothing sent (wait a minute, then rerun: %s)" % (label, why, self.rerun))

    def send(self, signer: Signer, to: str, data: str, value: int, label: str, on_sent, extra_need: int = 0,
             floor: int = 0, want_nonce: int | None = None):
        """Simulate, price, sign, record (via on_sent) BEFORE broadcasting, broadcast, wait for the receipt.
        Returns (hash, nonce, receipt). Raises Stop on anything unknown."""
        c, rpc, frm = self.chain, self.chain.rpc, signer.address
        call = {"from": frm, "to": to, "data": data, "value": hex(value)}
        try:
            rpc.call("eth_call", [call, "latest"])
        except RpcError as e:
            raise Stop("%s: simulation reverted: %s; nothing sent" % (label, revert_reason(e))) from None
        try:
            est = int(rpc.call("eth_estimateGas", [call]), 16)
        except RpcError as e:
            raise Stop("%s: eth_estimateGas failed: %s; nothing sent" % (label, revert_reason(e))) from None
        gas = dm.gas_limit(est)
        _, _, base, max_fee, prio = c.fees()
        if c.code(frm):
            raise Stop("%s: %s has code (EIP-7702 delegation?); nothing sent" % (label, frm))
        bal = c.balance(frm)
        need = value + extra_need + gas * max_fee
        if bal < need:
            raise Stop("%s: %s holds %s USDC, needs %s USDC (value/bill + gas limit x maxFee); nothing sent"
                       % (label, frm, usdc18(bal), usdc18(need)))
        nonce = self.next_nonce(frm, label, floor, want_nonce)
        tx = {"type": 2, "chainId": CHAIN_ID, "nonce": nonce, "to": to, "value": value, "data": data, "gas": gas,
              "maxFeePerGas": max_fee, "maxPriorityFeePerGas": prio, "accessList": []}
        check_shape(tx, to, value)
        raw = signer.sign_tx(tx)
        h = "0x" + kec(bytes.fromhex(raw[2:])).hex()
        on_sent(h, nonce)  # the record holds hash + nonce before the network sees the tx
        try:
            got = rpc.call("eth_sendRawTransaction", [raw])
        except RpcError as e:
            if not re.search(r"already known", e.message or "", re.I):
                raise Stop("%s: broadcast rejected (%s); recorded hash %s nonce %d; %s re-checks it first"
                           % (label, e.message, h, nonce, self.rerun)) from None
            got = h
        except RpcUnavailable as e:
            raise Stop("%s: broadcast state unknown (%s); recorded hash %s nonce %d; %s re-checks it first"
                       % (label, e, h, nonce, self.rerun)) from None
        if got and not same(got, h):
            raise Stop("%s: node returned hash %s, expected %s" % (label, got, h))
        self.p("  sent       %s  %s  (gas limit %d, maxFee %s, nonce %d)" % (label, tx_link(h), gas,
                                                                              dm.gwei(max_fee), nonce))
        receipt = self.wait(h, label)
        return h, nonce, receipt

    def wait(self, h: str, label: str):
        r = dm._wait_receipt(self.chain.rpc, h, self.timeout, self.sleep)
        if r is None:
            raise Stop("%s: no receipt for %s after %.0f s (state unknown); %s re-checks it first"
                       % (label, h, self.timeout, self.rerun))
        return r

    def wait_blocks(self, n: int, label: str) -> None:
        """Wait until the head is n blocks past where it is now and at least DEAD_SECONDS (capped by the receipt
        timeout) have passed; Stop when the chain does not advance within the receipt timeout."""
        rpc = self.chain.rpc
        start = int(rpc.call("eth_blockNumber", []), 16)
        min_wait = min(DEAD_SECONDS, self.timeout)
        waited = 0.0
        while True:
            try:
                if int(rpc.call("eth_blockNumber", []), 16) >= start + n and waited >= min_wait:
                    return
            except RpcUnavailable:
                pass
            if waited >= self.timeout:
                raise Stop("%s: the chain did not advance %d blocks in %.0f s (state unknown); %s re-checks it"
                           % (label, n, self.timeout, self.rerun))
            self.sleep(1.0)
            waited += 1.0


def tx_block(chain: Chain, h: str):
    """eth_getTransactionByHash: ('none', None) unknown to the node, ('pool', tx) known but not in a block,
    ('mined', tx) in a block."""
    t = chain.rpc.call("eth_getTransactionByHash", [h])
    if not t:
        return "none", None
    return ("mined" if t.get("blockNumber") else "pool"), t


def nonce_floor(entries, addr: str) -> int:
    """1 + the highest nonce among recorded txs from addr that have a block (a receipt was seen); 0 if none."""
    used = [int(t["nonce"]) for t in entries if t and t.get("block") is not None and t.get("nonce") is not None
            and same(t.get("from"), addr)]
    return max(used) + 1 if used else 0


def need_status_1(receipt: dict, h: str, label: str) -> None:
    if int(receipt.get("status", "0x0"), 16) != 1:
        raise Stop("%s: transaction failed on chain (status 0, gas was paid): %s" % (label, tx_link(h)))


# ---------------------------------------------------------------------------------------------- fund


def cmd_fund(args, chain: Chain, p, confirm, sender_factory, record_path: Path) -> int:
    if args.send:
        dep = send_signer("deployer")
        payer, biller = send_signer("payer").address, send_signer("biller").address
        a = dep.address
        p("wallets      deployer %s, payer %s, biller %s (from their key files)" % (a, payer, biller))
    else:
        p("wallets")
        a, _ = dry_address("deployer", p)
        payer, _ = dry_address("payer", p)
        biller, _ = dry_address("biller", p)
    number, ts, base, max_fee, prio = chain.fees()
    p("block        %d (%s), base fee %s, priority %s, maxFee %s" % (number, when(ts), dm.gwei(base),
                                                                    dm.gwei(prio), dm.gwei(max_fee)))
    targets = (("payer", payer, FUND_PAYER), ("biller", biller, FUND_BILLER))
    rec = load_record(record_path)
    if rec and rec.get("addresses") and not (same(rec["addresses"].get("payer"), payer)
                                             and same(rec["addresses"].get("biller"), biller)):
        raise Refused("the record %s belongs to other wallets" % record_path)
    unresolved = [t for t in ((rec or {}).get("fund") or {}).get("txs") or [] if t.get("state") == "sent"]
    landed = {}
    if unresolved and args.send:
        # an earlier run stopped after signing: resolve those txs first (reads + record writes only, nothing signed)
        snd = sender_factory(RERUN_FUND)
        landed = {role: settle_fund(chain, snd, rec, record_path, role, addr, target, a, p)
                  for role, addr, target in targets}
    for t in unresolved if not args.send else []:
        p("record       funding tx to %s %s (nonce %s) is still unresolved (receipt: %s); fund --send checks it "
          "first and sends nothing new while it can still land" % (
              t.get("role"), t.get("hash"), t.get("nonce"), "yes" if chain.receipt(t["hash"]) else "none yet"))
    a_bal = chain.balance(a)
    p("deployer A   %s holds %s USDC (also the refund keeper; must keep >= %s USDC)" % (
        a, usdc18(a_bal), usdc18(A_RESERVE)))
    plan = []
    for role, addr, target in targets:
        bal = chain.balance(addr)
        if landed.get(role):
            p("%-12s %s holds %s USDC; funded by the earlier recorded tx: not funded again" % (role, addr,
                                                                                            usdc18(bal)))
            continue
        if bal >= target:
            p("%-12s %s holds %s USDC >= target %s USDC: not funded again" % (role, addr, usdc18(bal),
                                                                             usdc18(target)))
            continue
        try:
            est = int(chain.rpc.call("eth_estimateGas", [{"from": a, "to": addr, "value": hex(target)}]), 16)
        except RpcError as e:
            raise Refused("eth_estimateGas for funding %s failed: %s" % (role, revert_reason(e))) from None
        gas = dm.gas_limit(est)
        plan.append((role, addr, target, est, gas))
        p("%-12s %s holds %s USDC -> send %s USDC (%d wei); gas estimate %d, limit %d, cost ~%s, at most %s USDC"
          % (role, addr, usdc18(bal), usdc18(target), target, est, gas, usdc18(est * (base + prio)),
             usdc18(gas * max_fee)))
    if not plan:
        p("nothing to fund: both wallets already hold their targets")
        return 0
    total = sum(t for _, _, t, _, _ in plan)
    worst_gas = sum(g * max_fee for *_, g in plan)
    left = a_bal - total - worst_gas
    p("totals       value %s USDC + gas at most %s USDC; A keeps at least %s USDC (needs >= %s)" % (
        usdc18(total), usdc18(worst_gas), usdc18(max(left, 0)) if left >= 0 else "-" + usdc18(-left),
        usdc18(A_RESERVE)))
    if left < A_RESERVE:
        raise Refused("A would keep less than %s USDC for keeper gas; nothing sent" % usdc18(A_RESERVE))
    if not args.send:
        p("DRY RUN      nothing signed, nothing sent (add --send to fund; you will type SEND)")
        return 0
    answer = confirm("Type SEND to sign and send %d funding transaction(s) from %s on Arc mainnet: " % (len(plan), a))
    if answer.strip() != "SEND":
        raise Refused("not confirmed; nothing sent")
    rec = rec or new_record(chain.pt, a, payer, biller)
    rec.setdefault("fund", {"txs": []})
    snd = sender_factory(RERUN_FUND)
    for role, addr, target, _, gas in plan:
        if chain.balance(addr) >= target:
            p("%-12s already holds its target now; skipped" % role)
            continue
        entry = {"role": role, "to": addr, "valueWei": target, "from": a, "maxCostWei": gas * max_fee}

        def on_sent(h, nonce, entry=entry):
            entry.update({"hash": h, "nonce": nonce, "state": "sent"})
            if entry not in rec["fund"]["txs"]:
                rec["fund"]["txs"].append(entry)
            save_record(record_path, rec)

        a_now = chain.balance(a)
        in_flight = sum(int(t.get("valueWei", 0)) + int(t.get("maxCostWei", 0))
                        for t in rec["fund"]["txs"] if t.get("state") == "sent")  # 0 after settle_fund; kept as a guard
        if a_now - in_flight - target - gas * max_fee < A_RESERVE:
            raise Stop("fund %s: A holds %s USDC now (%s USDC still in flight); sending would leave it under %s USDC; "
                       "nothing sent" % (role, usdc18(a_now), usdc18(in_flight), usdc18(A_RESERVE)))
        h, nonce, receipt = snd.send(dep, addr, "0x", target, "fund " + role, on_sent,
                                     floor=nonce_floor(rec["fund"]["txs"], a))
        fund_landed(chain, rec, record_path, entry, receipt, h, nonce, role, addr, target, p)
    p("balances     A %s, payer %s, biller %s USDC" % (usdc18(chain.balance(a)), usdc18(chain.balance(payer)),
                                                      usdc18(chain.balance(biller))))
    p("record       %s" % record_path)
    return 0


def fund_landed(chain: Chain, rec, path, entry, receipt, h, nonce, role, addr, target, p) -> None:
    """A funding receipt: record it, require status 1 and an exact recipient balance delta in its block."""
    ok = int(receipt.get("status", "0x0"), 16) == 1
    entry.update(tx_info(receipt, h, nonce, role=role, to=addr, valueWei=target,
                         state="confirmed" if ok else "reverted"))
    save_record(path, rec)
    need_status_1(receipt, h, "fund " + role)
    blk = int(receipt["blockNumber"], 16)
    delta = chain.balance(addr, hex(blk)) - chain.balance(addr, hex(blk - 1))
    if delta != target:
        raise Stop("fund %s: %s balance moved by %d wei in block %d, expected exactly %d (the tx itself is "
                   "confirmed and recorded; a rerun will not send it again)" % (role, addr, delta, blk, target))
    p("  confirmed  block %d, gas used %d, cost %s USDC; %s received exactly %s USDC" % (
        blk, entry["gasUsed"], entry["costUSDC"], role, usdc18(target)))


def settle_fund(chain: Chain, snd: Sender, rec, path, role, addr, target, a, p) -> bool:
    """Resolve every recorded funding tx for `role` still in state 'sent' (an earlier run stopped after signing).
    True when one of them landed (the role is funded and is never sent to again). Stops, nothing sent, while one
    can still land. Only a tx whose nonce another tx has used, and that no block holds after DEAD_BLOCKS more
    blocks, is marked dead (it can never land), so a fresh one may be sent."""
    label = "fund " + role
    landed = False
    for e in [t for t in rec["fund"]["txs"] if t.get("role") == role and t.get("state") == "sent"]:
        h, n, frm = e["hash"], int(e["nonce"]), e.get("from") or a
        r = chain.receipt(h)
        if not r:
            latest = chain.nonce(frm, "latest")
            if latest <= n:
                raise Stop("%s: the earlier funding tx %s (nonce %d) has no receipt yet and can still land; nothing "
                           "sent (wait a minute, then rerun: %s)" % (label, h, n, RERUN_FUND))
            where, _ = tx_block(chain, h)
            if where != "mined":
                p("  checking   %s: nonce %d was used by another tx; waiting %d blocks / %.0f s before calling it "
                  "dead" % (h, n, DEAD_BLOCKS, DEAD_SECONDS))
                snd.wait_blocks(DEAD_BLOCKS, label)
                r = chain.receipt(h)
                if not r and tx_block(chain, h)[0] != "mined":
                    e.update({"state": "dead", "note": "nonce %d used by another tx; this tx can never land" % n})
                    save_record(path, rec)
                    p("  dead       %s never landed (nonce %d used by another tx)" % (h, n))
                    continue
            if not r:
                r = snd.wait(h, label)   # in a block per the node but no receipt yet
        p("  found      earlier funding tx %s landed" % h)
        fund_landed(chain, rec, path, e, r, h, n, role, addr, target, p)
        landed = True
    return landed


# ---------------------------------------------------------------------------------------------- bills


def new_record(pt: str, deployer: str, payer: str, biller: str) -> dict:
    return {"version": 1, "network": "mainnet", "chainId": CHAIN_ID, "contract": pt, "usdc": USDC,
            "addresses": {"deployer": deployer, "biller": biller, "payer": payer}}


def add_bills(rec: dict, start_block: int) -> None:
    rec["startBlock"] = start_block
    rec["bills"] = {}
    for k, b in BILLS.items():
        salt = secrets.token_bytes(16).hex()
        rec["bills"][k] = {"label": b["label"], "text": b["text"], "salt": salt, "ref": fingerprint(salt, b["text"]),
                           "amount": AMOUNT, "claimWindow": b["claimWindow"], "billId": None}
    rec["steps"] = {name: {"state": "todo"} for name, *_ in STEPS}
    rec["stoppedAt"] = None


def budget(kinds, max_fee: int) -> int:
    return sum(dm.gas_limit(GAS_HINT[k]) * max_fee for k in kinds)


PAYER_KINDS = [k for _, _, k, who in STEPS if who == "payer"]
BILLER_KINDS = [k for _, _, k, who in STEPS if who == "biller"]
PAYER_PEAK = 2 * AMOUNT * WEI_PER_UNIT  # A and C paid before C's decline returns 0.50


def print_plan(p, payer: str, biller: str, pt: str) -> None:
    p("plan (each bill %s USDC = %d base units; allowedPayer = payer %s; biller %s)" % (
        usdc6(AMOUNT), AMOUNT, payer, biller))
    p("  1 A.issue    biller issue(500000, payBy = now + 3 days, claimWindow = 7 days, payer, ref A)")
    p("  2 A.pay      payer signs ReceiveWithAuthorization(nonce = authNonce(A), validAfter 0, validBefore = "
      "min(payBy, now + 1 h)) and sends payWithAuthorization itself      payer -0.50")
    p("  3 A.claim    biller claim(A)                                     biller +0.50")
    p("  4 C.issue    biller issue(500000, 3 days, 7 days, payer, ref C)")
    p("  5 C.approve  payer USDC.approve(%s, 500000)" % pt)
    p("  6 C.pay      payer pay(C)  (two-transaction fallback)            payer -0.50 (peak: 1.00 paid out)")
    p("  7 C.decline  biller decline(C)                                   payer +0.50 back")
    p("  8 B.issue    biller issue(500000, 3 days, claimWindow = 3600 s, payer, ref B)")
    p("  9 B.pay      payer payWithAuthorization(B)                       payer -0.50")
    p("  then         nothing: after claimBy (paid + 1 h) the server keeper refunds B   payer +0.50 back")
    p("  end state    payer gets back C and B, so it ends near 1.10 - 0.50 (bill A) - gas; biller ends near "
      "0.05 + 0.50 - gas")


def cmd_bills(args, chain: Chain, p, confirm, sender_factory, record_path: Path) -> int:
    rec = load_record(record_path)
    has_bills = bool(rec and rec.get("bills"))
    if args.send and has_bills and not args.resume:
        raise Refused("%s already holds a bills run; use --resume --send to continue it" % record_path)
    if args.resume and not has_bills:
        raise Refused("--resume: no bills run in %s" % record_path)
    if args.accept_step and not args.resume:
        raise Refused("--accept-step works only with --resume")

    domain_local = "0x" + domain_separator().hex()
    domain_chain = chain.usdc_domain()
    if not (domain_local == USDC_DOMAIN_SEPARATOR == domain_chain):
        raise Refused("USDC DOMAIN_SEPARATOR mismatch: local %s, SPEC %s, chain %s" % (
            domain_local, USDC_DOMAIN_SEPARATOR, domain_chain))
    if "0x" + kec(RECEIVE_TYPE.encode()).hex() != RECEIVE_TYPEHASH:
        raise Refused("ReceiveWithAuthorization typehash differs from SPEC.md")
    p("usdc         %s, DOMAIN_SEPARATOR %s (local = SPEC = chain)" % (USDC, domain_chain))

    if args.send:
        biller_s, payer_s = send_signer("biller"), send_signer("payer")
        biller, payer = biller_s.address, payer_s.address
        p("wallets      biller %s, payer %s (from their key files)" % (biller, payer))
        files_ok = True
    else:
        p("wallets")
        biller, ok_b = dry_address("biller", p)
        payer, ok_p = dry_address("payer", p)
        files_ok = ok_b and ok_p
        biller_s = payer_s = None
    if same(biller, payer):
        raise Refused("biller and payer are the same address")
    if rec and rec.get("addresses") and not (same(rec["addresses"].get("payer"), payer)
                                             and same(rec["addresses"].get("biller"), biller)):
        raise Refused("the record %s belongs to other wallets" % record_path)

    number, ts, base, max_fee, prio = chain.fees()
    p("block        %d (%s), base fee %s, priority %s, maxFee %s" % (number, when(ts), dm.gwei(base),
                                                                    dm.gwei(prio), dm.gwei(max_fee)))
    problems = [] if files_ok else ["biller/payer key files are missing, unusable or not the recorded wallets"]
    for role, addr in (("biller", biller), ("payer", payer)):
        if chain.code(addr):
            problems.append("%s %s has code (EIP-7702 delegation would break the signature path)" % (role, addr))
        if chain.blacklisted(addr):
            problems.append("%s %s is on the USDC blocklist" % (role, addr))
    if chain.blacklisted(KNOWN["deployer"]):
        problems.append("deployer/keeper %s is on the USDC blocklist" % KNOWN["deployer"])
    p("checks       payer and biller have no code (eth_getCode 0x): %s; none of A/payer/biller blocklisted: %s" % (
        "no" if any("has code" in x for x in problems) else "yes",
        "no" if any("blocklist" in x for x in problems) else "yes"))
    pay_bal, bill_bal = chain.balance(payer), chain.balance(biller)
    pay_need = PAYER_PEAK + budget(PAYER_KINDS, max_fee)
    bill_need = budget(BILLER_KINDS, max_fee)
    p("payer        holds %s USDC; needs %s USDC = 1.00 peak (bills A and C paid before C's decline returns) + "
      "gas budget %s (4 tx at limit x maxFee)" % (usdc18(pay_bal), usdc18(pay_need), usdc18(pay_need - PAYER_PEAK)))
    p("biller       holds %s USDC; needs %s USDC gas budget (5 tx at limit x maxFee)" % (
        usdc18(bill_bal), usdc18(bill_need)))
    if not has_bills or not args.resume:
        if pay_bal < pay_need:
            problems.append("payer holds %s USDC < %s (run: python scripts/test_bills.py fund --send)" % (
                usdc18(pay_bal), usdc18(pay_need)))
        if bill_bal < bill_need:
            problems.append("biller holds %s USDC < %s (run: python scripts/test_bills.py fund --send)" % (
                usdc18(bill_bal), usdc18(bill_need)))

    # simulate the first issue (eth_call from the biller; estimateGas with a 1-USDC balance override)
    count = chain.bill_count()
    salt = secrets.token_bytes(16).hex()
    ref = fingerprint(salt, BILLS["A"]["text"])
    issue_data = fn("issue(uint96,uint64,uint32,address,bytes32)",
                    ["uint96", "uint64", "uint32", "address", "bytes32"],
                    [AMOUNT, ts + PAY_WINDOW, BILLS["A"]["claimWindow"], payer, bytes.fromhex(ref[2:])])
    try:
        ret = chain.call(chain.pt, issue_data, "latest", frm=biller)
        sim_id = kabi.decode_uint(ret)
        est = int(chain.rpc.call("eth_estimateGas", [{"from": biller, "to": chain.pt, "data": issue_data},
                                                     "latest", {biller: {"balance": hex(10**18)}}]), 16)
        p("simulate     issue(500000, payBy %d, 7 days, payer, ref) from biller: ok, would return id %d "
          "(billCount now %d; the real id is read from the BillIssued log); gas %d, limit %d, ~%s USDC" % (
              ts + PAY_WINDOW, sim_id, count, est, dm.gas_limit(est), usdc18(est * (base + prio))))
    except RpcError as e:
        problems.append("simulated issue reverted: %s" % revert_reason(e))
        p("simulate     issue: REVERTED %s" % revert_reason(e))

    dummy = count + 1
    nonce = chain.auth_nonce(dummy)
    local_nonce = kec(encode(["uint256", "address", "uint256"], [CHAIN_ID, chain.pt, dummy]))
    vb = min(ts + PAY_WINDOW, ts + VALID_FOR)
    digest = auth_digest(payer, chain.pt, AMOUNT, 0, vb, nonce, bytes.fromhex(domain_chain[2:]))
    p("eip-3009     dummy bill %d: authNonce (contract) %s %s local; ReceiveWithAuthorization(from %s, to %s, "
      "value 500000, validAfter 0, validBefore %d) digest %s (not signed)" % (
          dummy, "0x" + nonce.hex(), "=" if nonce == local_nonce else "!=", payer, chain.pt, vb, "0x" + digest.hex()))
    print_plan(p, payer, biller, chain.pt)

    if has_bills:
        p("record       %s (bills run started at block %s)" % (record_path, rec.get("startBlock")))
        for name, *_ in STEPS:
            s = rec["steps"].get(name, {})
            t = s.get("tx") or {}
            p("  %-10s %-12s %s" % (name, s.get("state", "todo"), t.get("explorer") or s.get("note") or ""))
        nxt = next((n for n, *_ in STEPS if rec["steps"].get(n, {}).get("state") != "done"), None)
        p("next         %s" % (nxt or "nothing: all steps done (use: status)"))
    if args.accept_step:
        show_accept(chain, rec, args.accept_step, p)
    if problems:
        p("NOT READY")
        for x in problems:
            p("  - %s" % x)
    if not args.send:
        p("DRY RUN      nothing signed, nothing sent (%s)" % (
            "--resume --send continues" if has_bills else "add --send to run; you will type SEND once"))
        return 3 if problems else 0
    if problems:
        raise Refused("not ready; nothing sent")
    answer = confirm("Type SEND to sign and send the test-bill transactions on Arc mainnet: ")
    if answer.strip() != "SEND":
        raise Refused("not confirmed; nothing sent")
    if args.accept_step:
        word = "ACCEPT " + args.accept_step
        if confirm("Type %s to accept the balance-delta mismatch shown above and continue: " % word).strip() != word:
            raise Refused("override not confirmed; nothing sent, nothing accepted")
    if not has_bills:
        rec = rec or new_record(chain.pt, KNOWN["deployer"], payer, biller)
        add_bills(rec, number)
        save_record(record_path, rec)  # salts on disk before the first issue
        p("record       %s (salts written before the first transaction)" % record_path)
    run = Run(chain, rec, record_path, p, sender_factory(), biller_s, payer_s, accept=args.accept_step)
    return run.go()


def show_accept(chain: Chain, rec: dict, name: str, p) -> None:
    """--accept-step: the step must have stopped in check_failed with a successful receipt. Prints what would be
    accepted; refuses anything else."""
    st = rec["steps"].get(name) or {}
    h = (st.get("tx") or {}).get("hash")
    if st.get("state") != "check_failed" or not h:
        raise Refused("--accept-step %s: that step is %s, not check_failed with a recorded tx; nothing to accept"
                      % (name, st.get("state", "todo")))
    r = chain.receipt(h)
    if not r or int(r.get("status", "0x0"), 16) != 1:
        raise Refused("--accept-step %s: receipt for %s is %s; only a successful tx can be accepted" % (
            name, h, "missing" if not r else "status 0"))
    reason = st.get("checkError") or ((rec.get("stoppedAt") or {}).get("reason") if (
        rec.get("stoppedAt") or {}).get("step") == name else "") or "(not recorded)"
    p("accept       %s: tx %s in block %d, status 1, gas used %d" % (name, h, int(r["blockNumber"], 16),
                                                                   int(r["gasUsed"], 16)))
    p("             %s" % tx_link(h))
    p("             stopped because: %s" % reason)
    p("             with --send you type ACCEPT %s; the bill status, event and USDC Transfer checks are re-run "
      "strictly, only the native balance deltas of this one step are accepted and written into the record" % name)


class Run:
    def __init__(self, chain: Chain, rec: dict, path: Path, p, sender: Sender, biller: Signer, payer: Signer,
                 accept: str | None = None):
        self.c, self.rec, self.path, self.p, self.snd = chain, rec, path, p, sender
        self.biller, self.payer = biller, payer
        self.who = {"biller": biller, "payer": payer}
        self.accept = accept          # the one step whose native balance deltas the owner accepted (--accept-step)
        self.accepted = []            # balance-delta mismatches accepted for that step

    def save(self):
        save_record(self.path, self.rec)

    def go(self) -> int:
        for name, key, kind, who in STEPS:
            st = self.rec["steps"].setdefault(name, {"state": "todo"})
            if st.get("state") == "done":
                continue
            try:
                self.step(name, key, kind, who, st)
            except Stop as e:
                self.rec["stoppedAt"] = {"step": name, "reason": str(e)}
                self.save()
                self.p("STOPPED      at %s: %s" % (name, e))
                self.p("record       %s" % self.path)
                self.p("resume       python scripts/test_bills.py bills --resume            (dry run: shows the "
                       "next step)")
                self.p("             python scripts/test_bills.py bills --resume --send     (continues; nothing "
                       "already paid is paid again)")
                if st.get("state") == "check_failed" and "native balance moved" in str(e):
                    self.p("             if the same mismatch repeats (other traffic in that block), look at the tx, "
                           "then: python scripts/test_bills.py bills --resume --send --accept-step %s" % name)
                return 1
            except (RpcError, RpcUnavailable, kabi.DecodeError) as e:
                self.rec["stoppedAt"] = {"step": name, "reason": "RPC: %s" % e}
                self.save()
                self.p("STOPPED      at %s: RPC error, state unknown (%s)" % (name, e))
                self.p("resume       python scripts/test_bills.py bills --resume --send")
                return 1
        self.rec["stoppedAt"] = None
        self.save()
        self.summary()
        return 0

    # ---- one step

    def step(self, name, key, kind, who, st):
        bill = self.rec["bills"][key]
        self.p("step         %s (%s, bill %s %s)" % (name, kind, key, bill.get("billId") or "new"))
        if st.get("state") in ("confirmed", "check_failed") and (st.get("tx") or {}).get("hash"):
            r = self.c.receipt(st["tx"]["hash"])
            if not r:
                raise Stop("%s: receipt for %s not visible now (state unknown)" % (name, st["tx"]["hash"]))
            return self.finish(name, key, kind, st, st["tx"]["hash"], st["tx"].get("nonce"), r)
        if st.get("state") == "sent" and (st.get("tx") or {}).get("hash"):
            if self.inflight(name, key, kind, who, st):
                return
        if st.get("resendNonce") is not None and self.resend_settled(name, key, kind, who, st):
            return
        if self.adopt(name, key, kind, st):
            return
        signer = self.who[who]
        to, data, extra = self.build(name, key, kind, bill)

        def on_sent(h, nonce):
            st.pop("resendNonce", None)
            st.update({"state": "sent", "tx": {"hash": h, "nonce": nonce, "from": signer.address,
                                               "explorer": tx_link(h)}})
            self.save()

        floor = nonce_floor([s.get("tx") for s in self.rec["steps"].values()], signer.address)
        h, nonce, receipt = self.snd.send(signer, to, data, 0, name, on_sent, extra_need=extra, floor=floor,
                                          want_nonce=st.get("resendNonce"))
        self.finish(name, key, kind, st, h, nonce, receipt)

    def inflight(self, name, key, kind, who, st) -> bool:
        """A recorded broadcast without a confirmed receipt. True when it was resolved (step finished)."""
        h, nonce = st["tx"]["hash"], int(st["tx"]["nonce"])
        r = self.c.receipt(h)
        if r:
            self.finish(name, key, kind, st, h, nonce, r)
            return True
        addr = self.who[who].address
        latest, pending = self.c.nonce(addr, "latest"), self.c.nonce(addr, "pending")
        if latest <= nonce < pending:
            self.p("  waiting    %s is still pending" % h)
            r = self.snd.wait(h, name)
            self.finish(name, key, kind, st, h, nonce, r)
            return True
        if latest == nonce:
            # Not mined, not pending, its nonce still free. The re-send uses this same nonce, so the original and
            # the re-send can never both land.
            self.p("  dropped    %s never landed (nonce %d unused); the step is re-sent with that nonce" % (h, nonce))
            st.setdefault("dropped", []).append(st["tx"])
            st.update({"state": "todo", "tx": None, "resendNonce": nonce})
            self.save()
            return False
        if latest < nonce:
            raise Stop("%s: %s (nonce %d) is not pending but nonce %d below it is still free, so it could land "
                       "later (state unknown); wait, then rerun: %s" % (name, h, nonce, latest, RERUN_BILLS))
        # latest > nonce: some tx used this nonce
        if self.adopt(name, key, kind, st):
            return True
        r = self.landed_or_dead(h, name)
        if r:
            self.finish(name, key, kind, st, h, nonce, r)
            return True
        if self.adopt(name, key, kind, st):   # effects re-checked after the wait
            return True
        self.p("  dead       %s never landed (nonce %d was used by another tx); the step is re-sent" % (h, nonce))
        st.setdefault("dropped", []).append(dict(st["tx"], consumed=True))
        st.update({"state": "todo", "tx": None})
        st.pop("resendNonce", None)
        self.save()
        return False

    def landed_or_dead(self, h: str, name: str):
        """For a tx whose nonce is already used: its receipt if it landed, None if it never can (no block holds
        it after DEAD_BLOCKS more blocks). Stops when a block holds it but no receipt shows up."""
        if tx_block(self.c, h)[0] == "mined":
            return self.snd.wait(h, name)
        self.p("  checking   %s: its nonce is used and it has no receipt; waiting %d blocks / %.0f s" % (
            h, DEAD_BLOCKS, DEAD_SECONDS))
        self.snd.wait_blocks(DEAD_BLOCKS, name)
        r = self.c.receipt(h)
        if r:
            return r
        if tx_block(self.c, h)[0] == "mined":
            return self.snd.wait(h, name)
        return None

    def resend_settled(self, name, key, kind, who, st) -> bool:
        """A step waiting to re-send at the nonce of its dropped tx. If that nonce has been used since, find out by
        what: the dropped tx itself (finish with it) or something else (the dropped tx is dead; send fresh)."""
        want = int(st["resendNonce"])
        if self.c.nonce(self.who[who].address, "latest") <= want:
            return False
        for d in reversed(st.get("dropped") or []):
            if d.get("nonce") is not None and int(d["nonce"]) == want and not d.get("consumed"):
                r = self.c.receipt(d["hash"]) or self.landed_or_dead(d["hash"], name)
                if r:
                    self.p("  found      the dropped tx %s landed after all" % d["hash"])
                    self.finish(name, key, kind, st, d["hash"], want, r)
                    return True
                d["consumed"] = True
                self.p("  dead       %s never landed (nonce %d was used by another tx); the step is sent fresh" % (
                    d["hash"], want))
        st.pop("resendNonce", None)
        self.save()
        return False

    def adopt(self, name, key, kind, st) -> bool:
        """State-derived check before sending: True when the chain already shows this step done."""
        bill = self.rec["bills"][key]
        payer = self.payer.address
        if kind == "issue":
            topics = [kabi.EVENTS["Issued"][0], None, addr_topic(self.biller.address)]
            for lg in self.c.logs(topics, int(self.rec["startBlock"])):
                d = kabi.decode_log(lg)
                if same(d.get("ref"), bill["ref"]):
                    self.p("  found      BillIssued for ref %s already on chain (bill %d)" % (bill["ref"], d["billId"]))
                    return self.adopt_tx(name, key, kind, st, d["txHash"])
            return False
        b = self.c.bill(int(bill["billId"]))
        if kind == "approve":
            if b.status != kabi.OPEN:
                st.update({"state": "done", "note": "bill already %s" % b.status_name})
                self.save()
                return True
            if self.c.allowance(payer, self.c.pt) >= AMOUNT:
                st.update({"state": "done", "note": "allowance already >= 500000"})
                self.save()
                self.p("  done       allowance already set; no approve sent")
                return True
            return False
        if kind in ("pwa", "pay"):
            if b.status == kabi.OPEN:
                return False
            if b.status in (kabi.PAID, kabi.CLAIMED, kabi.DECLINED, kabi.REFUNDED) and same(b.payer, payer):
                return self.adopt_event(name, key, kind, st, "Paid", int(bill["billId"]))
            raise Stop("%s: bill %s is %s, expected Open before paying" % (name, bill["billId"], b.status_name))
        target = STATUS_AFTER[kind]
        if b.status == kabi.PAID:
            return False
        if b.status == target:
            return self.adopt_event(name, key, kind, st, EVENT_OF[kind], int(bill["billId"]))
        raise Stop("%s: bill %s is %s, expected Paid" % (name, bill["billId"], b.status_name))

    def adopt_event(self, name, key, kind, st, event, bill_id) -> bool:
        logs = self.c.logs([kabi.EVENTS[event][0], id_topic(bill_id)], int(self.rec["startBlock"]))
        if len(logs) != 1:
            raise Stop("%s: bill %d shows the step done but %d Bill%s logs were found" % (name, bill_id, len(logs),
                                                                                           event))
        self.p("  found      Bill%s for bill %d already on chain: %s" % (event, bill_id, logs[0]["transactionHash"]))
        return self.adopt_tx(name, key, kind, st, logs[0]["transactionHash"])

    def adopt_tx(self, name, key, kind, st, h) -> bool:
        r = self.c.receipt(h)
        if not r:
            raise Stop("%s: receipt for %s not visible (state unknown)" % (name, h))
        known = (st.get("tx") or {})
        self.finish(name, key, kind, st, h, known.get("nonce") if same(known.get("hash"), h) else None, r)
        return True

    def build(self, name, key, kind, bill):
        pt, payer = self.c.pt, self.payer.address
        if kind == "issue":
            _, ts, *_ = self.c.fees()
            return pt, fn("issue(uint96,uint64,uint32,address,bytes32)",
                          ["uint96", "uint64", "uint32", "address", "bytes32"],
                          [AMOUNT, ts + PAY_WINDOW, bill["claimWindow"], payer, bytes.fromhex(bill["ref"][2:])]), 0
        bid = int(bill["billId"])
        if kind == "approve":
            return USDC, fn("approve(address,uint256)", ["address", "uint256"], [pt, AMOUNT]), 0
        if kind == "pay":
            return pt, fn("pay(uint256)", ["uint256"], [bid]), AMOUNT * WEI_PER_UNIT
        if kind in ("claim", "decline"):
            return pt, fn(kind + "(uint256)", ["uint256"], [bid]), 0
        if kind == "pwa":
            b = self.c.bill(bid)
            if b.status != kabi.OPEN:
                raise Stop("%s: bill %d is %s right before signing, expected Open" % (name, bid, b.status_name))
            if self.c.code(payer):
                raise Stop("%s: payer %s has code; the signature path is unsafe, nothing signed" % (name, payer))
            _, ts, *_ = self.c.fees()
            vb = min(int(b.payBy), ts + VALID_FOR)
            nonce = self.c.auth_nonce(bid)
            local = kec(encode(["uint256", "address", "uint256"], [CHAIN_ID, pt, bid]))
            if nonce != local:
                self.p("  note       authNonce(%d) from the contract differs from the local formula; using the "
                       "contract's" % bid)
            ds = self.c.usdc_domain()
            if ds != USDC_DOMAIN_SEPARATOR:
                raise Stop("%s: USDC DOMAIN_SEPARATOR changed to %s; nothing signed" % (name, ds))
            digest = auth_digest(payer, pt, AMOUNT, 0, vb, nonce, bytes.fromhex(ds[2:]))
            v, r, s = self.payer.sign_digest(digest)
            self.p("  signed     ReceiveWithAuthorization for bill %d: nonce 0x%s, validBefore %s" % (
                bid, nonce.hex(), when(vb)))
            return pt, fn("payWithAuthorization(uint256,address,uint256,uint256,uint8,bytes32,bytes32)",
                          ["uint256", "address", "uint256", "uint256", "uint8", "bytes32", "bytes32"],
                          [bid, payer, 0, vb, v, r, s]), AMOUNT * WEI_PER_UNIT
        raise AssertionError(kind)

    # ---- evidence after a receipt

    def finish(self, name, key, kind, st, h, nonce, receipt):
        bill = self.rec["bills"][key]
        st["tx"] = tx_info(receipt, h, nonce)
        if int(receipt.get("status", "0x0"), 16) != 1:
            st["state"] = "reverted"
            self.save()
            raise Stop("%s: transaction failed on chain (status 0, gas was paid): %s" % (name, tx_link(h)))
        st["state"] = "confirmed"
        self.save()
        self.accepted = []
        try:
            self.verify(name, key, kind, bill, receipt)
        except Stop as e:
            st["state"] = "check_failed"
            st["checkError"] = str(e)
            self.save()
            raise
        if self.accepted:
            st["accepted"] = "owner accepted (--accept-step): " + "; ".join(self.accepted)
            self.p("  ACCEPTED   %s" % st["accepted"])
        st.pop("checkError", None)
        st["state"] = "done"
        st.pop("note", None)
        self.rec["stoppedAt"] = None
        self.save()
        t = st["tx"]
        self.p("  done       block %d, gas used %d, cost %s USDC  %s" % (t["block"], t["gasUsed"], t["costUSDC"],
                                                                      t["explorer"]))

    def verify(self, name, key, kind, bill, receipt):
        c, pt = self.c, self.c.pt
        payer, biller = self.payer.address, self.biller.address
        blk = int(receipt["blockNumber"], 16)
        tag = hex(blk)
        if kind == "issue":
            issued = [kabi.decode_log(lg) for lg in receipt.get("logs") or []
                      if same(lg.get("address"), pt) and (lg.get("topics") or [""])[0].lower() == kabi.EVENTS[
                          "Issued"][0]]
            if len(issued) != 1:
                raise Stop("%s: %d BillIssued logs from %s in the receipt, expected 1" % (name, len(issued), pt))
            d = issued[0]
            want = {"payee": biller, "allowedPayer": payer, "amount": AMOUNT, "claimWindow": bill["claimWindow"],
                    "ref": bill["ref"]}
            for k, v in want.items():
                if not (same(d[k], v) if isinstance(v, str) else d[k] == v):
                    raise Stop("%s: BillIssued %s is %r, expected %r" % (name, k, d[k], v))
            bill["billId"] = int(d["billId"])
            bill["payBy"] = int(d["payBy"])
            bill["link"] = family_link(bill["billId"], bill["text"], bill["salt"])
            self.save()
        bid = int(bill["billId"])
        b = c.bill(bid, tag)
        want_status = STATUS_AFTER[kind]
        if b.status != want_status:
            raise Stop("%s: getBill(%d) at block %d is %s, expected %s" % (name, bid, blk, b.status_name,
                                                                          kabi.STATUS_NAMES[want_status]))
        if not (same(b.payee, biller) and b.amount == AMOUNT and same(b.allowedPayer, payer)
                and b.ref.lower() == bill["ref"].lower() and b.claimWindow == bill["claimWindow"]):
            raise Stop("%s: getBill(%d) fields differ from the issued bill" % (name, bid))
        if kind in ("pwa", "pay"):
            paid = [kabi.decode_log(lg) for lg in receipt.get("logs") or []
                    if same(lg.get("address"), pt) and (lg.get("topics") or [""])[0].lower() == kabi.EVENTS[
                        "Paid"][0]]
            if len(paid) != 1 or paid[0]["billId"] != bid or not same(paid[0]["payer"], payer):
                raise Stop("%s: expected one BillPaid(%d, payer) log" % (name, bid))
            ts = c.block_time(blk)
            if not (same(b.payer, payer) and b.claimBy == paid[0]["claimBy"] == ts + bill["claimWindow"]):
                raise Stop("%s: payer/claimBy differ (claimBy %d, log %d, block time %d + %d)" % (
                    name, b.claimBy, paid[0]["claimBy"], ts, bill["claimWindow"]))
            bill["claimBy"] = int(b.claimBy)
            bill["paidBlock"] = blk
        if kind == "approve":
            al = c.allowance(payer, pt, tag)
            if al != AMOUNT:
                raise Stop("%s: allowance at block %d is %d, expected %d" % (name, blk, al, AMOUNT))
        flows = {"issue": [], "approve": [], "pwa": [(payer, pt)], "pay": [(payer, pt)], "claim": [(pt, biller)],
                 "decline": [(pt, payer)]}[kind]
        expected = [(dm.checksum(a), dm.checksum(b2), AMOUNT) for a, b2 in flows]
        got = usdc_transfers(receipt)
        if sorted(got) != sorted(expected):
            raise Stop("%s: USDC Transfer logs %s, expected %s" % (name, got, expected))
        frm = dm.checksum(receipt["from"])
        cost = int(receipt["gasUsed"], 16) * int(receipt["effectiveGasPrice"], 16)
        for who in (payer, biller):
            flow = sum(AMOUNT for a, b2 in flows if same(b2, who)) - sum(AMOUNT for a, b2 in flows if same(a, who))
            want = flow * WEI_PER_UNIT - (cost if same(frm, who) else 0)
            delta = c.balance(who, tag) - c.balance(who, hex(blk - 1))
            if delta != want:
                msg = "%s native balance moved by %d wei in block %d, expected exactly %d" % (who, delta, blk, want)
                if self.accept != name:
                    raise Stop("%s: %s" % (name, msg))
                self.accepted.append(msg)
        self.p("  verified   bill %d %s at block %d; USDC transfers %s; payer/biller balance deltas %s" % (
            bid, kabi.STATUS_NAMES[want_status], blk, ", ".join("%s->%s %d" % (a[:8], b2[:8], v)
                                                                for a, b2, v in got) or "none",
            "ACCEPTED BY THE OWNER (not exact)" if self.accepted else "exact"))
        if kind == "issue":
            self.p("  bill %s     id %d  family link %s" % (key, bid, bill["link"]))
        if key == "B" and kind == "pwa":
            self.p("  bill B     claimBy %s; the server keeper refunds it after that" % when(bill["claimBy"]))

    def summary(self):
        self.p("ALL STEPS DONE")
        for k in ("A", "C", "B"):
            b = self.rec["bills"][k]
            self.p("bill %s (%s) id %d  %s" % (k, b["label"], b["billId"], b["link"]))
            for name, key, *_ in STEPS:
                if key == k and (self.rec["steps"][name].get("tx") or {}).get("hash"):
                    self.p("  %-10s %s" % (name, self.rec["steps"][name]["tx"]["explorer"]))
        self.p("bill B claimBy %s; check later with: python scripts/test_bills.py status" % when(
            self.rec["bills"]["B"]["claimBy"]))
        self.p("record       %s (+ .md)" % self.path)


# ---------------------------------------------------------------------------------------------- status


def cmd_status(args, chain: Chain, p, record_path: Path) -> int:
    rec = load_record(record_path)
    if not rec or not rec.get("bills"):
        raise Refused("no bills run in %s" % record_path)
    _, ts, *_ = chain.fees()
    p("now          %s" % when(ts))
    found = None
    for k in ("A", "C", "B"):
        b = rec["bills"][k]
        if not b.get("billId"):
            p("bill %s        not issued yet" % k)
            continue
        bill = chain.bill(int(b["billId"]))
        extra = ""
        if bill.claimBy:
            extra = "; claimBy %s" % when(bill.claimBy)
            if bill.status == kabi.PAID and ts >= bill.claimBy:
                extra += " (refund due; the keeper runs every 5 min)"
        p("bill %s        id %d  %-9s (%s)%s" % (k, b["billId"], bill.status_name, b["label"], extra))
        p("             %s" % family_link(b["billId"], b["text"], b["salt"]))
        if k == "B" and bill.status == kabi.REFUNDED:
            logs = chain.logs([kabi.EVENTS["Refunded"][0], id_topic(b["billId"])],
                              int(b.get("paidBlock") or rec.get("startBlock") or 0))
            if len(logs) != 1:
                p("             refund log not found uniquely (%d logs); state unknown" % len(logs))
            else:
                d = kabi.decode_log(logs[0])
                found = {"hash": d["txHash"], "block": d["blockNumber"], "caller": d["caller"],
                         "amount": d["amount"]}
                p("             refunded %s USDC to %s by %s in block %d: %s" % (
                    usdc6(d["amount"]), d["payer"], d["caller"], d["blockNumber"], tx_link(d["txHash"])))
    if found and args.save:
        rec["refund"] = found
        save_record(record_path, rec)
        p("record       %s updated with the refund tx" % record_path)
    return 0


# ---------------------------------------------------------------------------------------------- main


def parse(argv):
    ap = argparse.ArgumentParser(description="PaidThrough mainnet test bills with two test wallets (dry run by "
                                             "default).")
    ap.add_argument("command", choices=["fund", "bills", "status"])
    ap.add_argument("--send", action="store_true", help="sign and send (you type SEND)")
    ap.add_argument("--resume", action="store_true", help="bills: continue the run in the record")
    ap.add_argument("--save", action="store_true", help="status: write bill B's refund tx into the record")
    ap.add_argument("--accept-step", choices=[n for n, *_ in STEPS], default=None,
                    help="bills --resume: accept a native balance-delta mismatch at this check_failed step (you type "
                         "ACCEPT <step>); bill status, events and USDC transfers stay strict")
    ap.add_argument("--rpc", default=RPC_URL)
    ap.add_argument("--record", default=str(DEFAULT_RECORD))
    ap.add_argument("--receipt-timeout", type=float, default=120.0)
    return ap.parse_args(argv)


def main(argv=None, rpc=None, out=None, confirm=input, sleep=time.sleep) -> int:
    out = out or sys.stdout

    def p(*a):
        print(*a, file=out, flush=True)

    try:
        args = parse(argv)
        if args.command == "status" and args.send:
            raise Refused("status is read-only")
        if args.accept_step and args.command != "bills":
            raise Refused("--accept-step belongs to: bills --resume")
        pt, _ = deployment()
        rpc = rpc or Rpc(args.rpc)
        chain = Chain(rpc, pt)
        cid = chain.chain_id()
        if cid != CHAIN_ID:
            raise Refused("chain id mismatch: RPC says %d, Arc mainnet is %d" % (cid, CHAIN_ID))
        if not chain.code(pt):
            raise Refused("no contract code at %s" % pt)
        p("network      Arc mainnet (chain id %d) via %s; PaidThrough %s" % (cid, args.rpc, pt))
        record_path = Path(args.record)

        def sender_factory(rerun=RERUN_BILLS):
            return Sender(chain, p, sleep, args.receipt_timeout, rerun)

        if args.command == "fund":
            return cmd_fund(args, chain, p, confirm, sender_factory, record_path)
        if args.command == "bills":
            return cmd_bills(args, chain, p, confirm, sender_factory, record_path)
        return cmd_status(args, chain, p, record_path)
    except Refused as e:
        p("REFUSED: %s" % e)
        return 2
    except Stop as e:
        p("STOPPED: %s" % e)
        p("record       %s" % args.record)
        p("rerun        %s   (recorded txs are checked first; nothing that can still land is sent again)" % (
            RERUN_FUND if args.command == "fund" else RERUN_BILLS))
        return 1
    except (RpcError, RpcUnavailable) as e:
        p("RPC ERROR (state unknown, nothing assumed): %s" % e)
        return 2


if __name__ == "__main__":
    sys.exit(main())
