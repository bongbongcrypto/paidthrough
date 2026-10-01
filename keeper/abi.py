"""PaidThrough ABI pieces for the keeper, derived from the exact signatures in SPEC.md.

Selectors and topics are computed with keccak at import time from the canonical
signatures below (never hard-coded hex), so a typo in a signature shows up as a
test failure against the fixed values in tests/test_abi.py.

Units: `amount` is USDC base units (6 decimals). Native gas values are wei (18 decimals).
"""
from __future__ import annotations

from dataclasses import dataclass

from eth_abi import decode as abi_decode
from eth_abi import encode as abi_encode


def keccak(data: bytes) -> bytes:
    try:
        from Crypto.Hash import keccak as _k

        h = _k.new(digest_bits=256)
        h.update(data)
        return h.digest()
    except ImportError:  # pragma: no cover - eth_hash fallback
        from eth_hash.auto import keccak as _k2

        return _k2(data)


def selector(signature: str) -> bytes:
    return keccak(signature.encode())[:4]


def topic(signature: str) -> str:
    return "0x" + keccak(signature.encode()).hex()


# --- Status enum (SPEC.md order; Refunded=4 comes before Declined=5) ---
STATUS_NAMES = ("None", "Open", "Paid", "Claimed", "Refunded", "Declined", "Cancelled")
NONE, OPEN, PAID, CLAIMED, REFUNDED, DECLINED, CANCELLED = range(7)

# --- Functions the keeper calls ---
SIG_REFUND = "refund(uint256)"
SIG_GET_BILL = "getBill(uint256)"
SIG_BILL_COUNT = "billCount()"

SEL_REFUND = selector(SIG_REFUND)
SEL_GET_BILL = selector(SIG_GET_BILL)
SEL_BILL_COUNT = selector(SIG_BILL_COUNT)

# Bill struct: all fields static, so the return value is 9 inline words (no offset).
BILL_TUPLE = "(address,uint96,address,uint64,uint32,address,uint64,uint8,bytes32)"

# --- Events (canonical signatures; indexed-ness does not change the topic) ---
EV_ISSUED = "BillIssued(uint256,address,address,uint96,uint64,uint32,bytes32)"
EV_CANCELLED = "BillCancelled(uint256)"
EV_PAID = "BillPaid(uint256,address,uint64)"
EV_CLAIMED = "BillClaimed(uint256,address,uint96)"
EV_DECLINED = "BillDeclined(uint256,address,uint96)"
EV_REFUNDED = "BillRefunded(uint256,address,uint96,address)"

# name -> (topic0, indexed types after billId, data types, field names for indexed, field names for data)
EVENTS = {
    "Issued": (topic(EV_ISSUED), ["address", "address"], ["uint96", "uint64", "uint32", "bytes32"],
               ["payee", "allowedPayer"], ["amount", "payBy", "claimWindow", "ref"]),
    "Cancelled": (topic(EV_CANCELLED), [], [], [], []),
    "Paid": (topic(EV_PAID), ["address"], ["uint64"], ["payer"], ["claimBy"]),
    "Claimed": (topic(EV_CLAIMED), ["address"], ["uint96"], ["payee"], ["amount"]),
    "Declined": (topic(EV_DECLINED), ["address"], ["uint96"], ["payer"], ["amount"]),
    "Refunded": (topic(EV_REFUNDED), ["address"], ["uint96", "address"], ["payer"], ["amount", "caller"]),
}
TOPIC_TO_EVENT = {v[0]: k for k, v in EVENTS.items()}
ALL_TOPICS = [v[0] for v in EVENTS.values()]

# Known revert shapes. SPEC.md names the custom errors but not their argument lists; we map the
# zero-argument form. A contract that adds arguments gets a different selector and is shown raw.
ERROR_NAMES = ("NotPayee", "WrongStatus", "PayWindowClosed", "ClaimWindowClosed", "ClaimWindowOpen",
               "NotAllowedPayer", "BadAmount", "BadPayBy", "BadClaimWindow", "TransferFailed",
               "BalanceMismatch")
ERROR_SELECTORS = {selector(n + "()").hex(): n for n in ERROR_NAMES}
SEL_ERROR_STRING = selector("Error(string)").hex()   # 08c379a0
SEL_PANIC = selector("Panic(uint256)").hex()          # 4e487b71


class DecodeError(ValueError):
    pass


def _hex_to_bytes(h: str) -> bytes:
    if not isinstance(h, str) or not h.startswith("0x"):
        raise DecodeError("expected 0x-hex, got %r" % (h,))
    try:
        return bytes.fromhex(h[2:])
    except ValueError as e:
        raise DecodeError("bad hex: %s" % e) from e


def _checksum(addr: str) -> str:
    a = addr.lower().replace("0x", "")
    h = keccak(a.encode()).hex()
    return "0x" + "".join(c.upper() if c.isalpha() and int(h[i], 16) >= 8 else c for i, c in enumerate(a))


def checksum(addr: str) -> str:
    if not isinstance(addr, str) or len(addr) != 42 or not addr.startswith("0x"):
        raise DecodeError("not an address: %r" % (addr,))
    int(addr[2:], 16)
    return _checksum(addr)


# --- Calldata ---
def encode_refund(bill_id: int) -> str:
    return "0x" + (SEL_REFUND + abi_encode(["uint256"], [int(bill_id)])).hex()


def encode_get_bill(bill_id: int) -> str:
    return "0x" + (SEL_GET_BILL + abi_encode(["uint256"], [int(bill_id)])).hex()


def encode_bill_count() -> str:
    return "0x" + SEL_BILL_COUNT.hex()


def is_refund_calldata(data: str, bill_id: int) -> bool:
    return isinstance(data, str) and data.lower() == encode_refund(bill_id).lower()


# --- Return values ---
@dataclass
class Bill:
    id: int
    payee: str
    amount: int          # USDC base units, 6 decimals
    payer: str
    payBy: int           # unix seconds
    claimWindow: int     # seconds
    allowedPayer: str
    claimBy: int         # unix seconds, 0 until paid
    status: int
    ref: str             # 0x + 64 hex

    @property
    def status_name(self) -> str:
        return STATUS_NAMES[self.status] if 0 <= self.status < len(STATUS_NAMES) else "?%d" % self.status


def decode_bill(bill_id: int, data: str) -> Bill:
    raw = _hex_to_bytes(data)
    if len(raw) != 9 * 32:
        raise DecodeError("getBill(%d) returned %d bytes, expected 288" % (bill_id, len(raw)))
    (payee, amount, payer, pay_by, claim_window, allowed, claim_by, status, ref), = abi_decode([BILL_TUPLE], raw)
    if status >= len(STATUS_NAMES):
        raise DecodeError("getBill(%d): unknown status %d" % (bill_id, status))
    return Bill(bill_id, checksum(payee), amount, checksum(payer), pay_by, claim_window,
                checksum(allowed), claim_by, status, "0x" + ref.hex())


def decode_uint(data: str) -> int:
    raw = _hex_to_bytes(data)
    if len(raw) != 32:
        raise DecodeError("expected one uint256 word, got %d bytes" % len(raw))
    return int.from_bytes(raw, "big")


# --- Events ---
def decode_log(log: dict) -> dict:
    """One raw eth_getLogs entry -> {'event', 'billId', fields..., 'blockNumber', 'logIndex', 'txHash'}.
    Raises DecodeError for a log that carries one of our topics but the wrong shape."""
    topics = log.get("topics") or []
    if not topics:
        raise DecodeError("log without topics")
    name = TOPIC_TO_EVENT.get(topics[0].lower())
    if name is None:
        raise DecodeError("unknown topic0 %s" % topics[0])
    _, itypes, dtypes, inames, dnames = EVENTS[name]
    if len(topics) != 2 + len(itypes):
        raise DecodeError("%s: expected %d topics, got %d" % (name, 2 + len(itypes), len(topics)))
    out = {
        "event": name,
        "billId": int(topics[1], 16),
        "blockNumber": int(log["blockNumber"], 16),
        "logIndex": int(log["logIndex"], 16),
        "txHash": log.get("transactionHash"),
    }
    for t, n, tp in zip(itypes, inames, topics[2:]):
        out[n] = abi_decode([t], _hex_to_bytes(tp))[0]
    data = _hex_to_bytes(log.get("data") or "0x")
    if len(data) != 32 * len(dtypes):
        raise DecodeError("%s: data is %d bytes, expected %d" % (name, len(data), 32 * len(dtypes)))
    if dtypes:
        for n, v in zip(dnames, abi_decode(dtypes, data)):
            out[n] = v
    for k, v in list(out.items()):
        if isinstance(v, bytes):
            out[k] = "0x" + v.hex()
        elif isinstance(v, str) and k not in ("event", "txHash") and v.startswith("0x") and len(v) == 42:
            out[k] = checksum(v)
    return out


def decode_revert(data) -> str:
    """Human label for revert data from eth_call / eth_estimateGas (hex string or None)."""
    if not data or not isinstance(data, str) or not data.startswith("0x") or len(data) < 10:
        return "reverted (no reason)"
    sel = data[2:10].lower()
    if sel in ERROR_SELECTORS:
        return ERROR_SELECTORS[sel]
    if sel == SEL_ERROR_STRING:
        try:
            return "Error(%r)" % abi_decode(["string"], bytes.fromhex(data[10:]))[0]
        except Exception:  # noqa: BLE001 - malformed reason string
            return "Error(<undecodable>)"
    if sel == SEL_PANIC:
        try:
            return "Panic(0x%x)" % abi_decode(["uint256"], bytes.fromhex(data[10:]))[0]
        except Exception:  # noqa: BLE001
            return "Panic(<undecodable>)"
    return "custom error 0x%s" % sel
