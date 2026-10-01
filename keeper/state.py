"""Pure state machine: fold decoded PaidThrough events into bill records.

No I/O. Input = decoded logs (abi.decode_log output) in any order, possibly with duplicates.
Output = {billId: BillRecord}. A transition the contract cannot produce (Paid after Claimed,
an event for a bill that was never issued, a Claimed amount that differs from the issued amount)
raises ImpossibleTransition: it means our decoding or our log scan is wrong, and the keeper must
stop rather than act on a wrong picture of who is owed money.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from abi import CANCELLED, CLAIMED, DECLINED, NONE, OPEN, PAID, REFUNDED, STATUS_NAMES, Bill

ZERO = "0x0000000000000000000000000000000000000000"


class ImpossibleTransition(RuntimeError):
    pass


@dataclass
class BillRecord:
    id: int
    payee: str = ZERO
    amount: int = 0              # USDC base units (6 decimals)
    payer: str = ZERO
    payBy: int = 0
    claimWindow: int = 0
    allowedPayer: str = ZERO
    claimBy: int = 0
    status: int = NONE
    ref: str = "0x" + "00" * 32
    history: list = field(default_factory=list)   # decoded logs, in chain order
    source: str = "events"                         # "events" or "getBill" (filled from a direct read)

    @property
    def status_name(self) -> str:
        return STATUS_NAMES[self.status]

    def as_bill(self) -> Bill:
        return Bill(self.id, self.payee, self.amount, self.payer, self.payBy, self.claimWindow,
                    self.allowedPayer, self.claimBy, self.status, self.ref)

    @classmethod
    def from_bill(cls, b: Bill, source: str = "getBill") -> "BillRecord":
        return cls(b.id, b.payee, b.amount, b.payer, b.payBy, b.claimWindow, b.allowedPayer,
                   b.claimBy, b.status, b.ref, [], source)


def log_key(ev: dict):
    return (ev["blockNumber"], ev["logIndex"])


def dedupe(events) -> list:
    """Drop exact duplicates (same block, log index and tx) and sort in chain order.
    Two different logs claiming the same (block, logIndex) is a data error."""
    seen = {}
    for ev in events:
        k = log_key(ev)
        if k in seen:
            if seen[k].get("txHash") != ev.get("txHash") or seen[k]["event"] != ev["event"] \
                    or seen[k]["billId"] != ev["billId"]:
                raise ImpossibleTransition("two different logs at block %d index %d" % k)
            continue
        seen[k] = ev
    return [seen[k] for k in sorted(seen)]


def _need(rec: BillRecord, ev: dict, allowed_from: int):
    if rec.status != allowed_from:
        raise ImpossibleTransition(
            "bill %d: %s while %s (block %d, log %d)" % (
                rec.id, ev["event"], rec.status_name, ev["blockNumber"], ev["logIndex"]))


def _same(rec: BillRecord, ev: dict, what: str, have, got):
    if (have.lower() if isinstance(have, str) else have) != (got.lower() if isinstance(got, str) else got):
        raise ImpossibleTransition("bill %d: %s %s mismatch (record %s, event %s)" % (
            rec.id, ev["event"], what, have, got))


def apply(bills: dict, ev: dict) -> None:
    bid = ev["billId"]
    name = ev["event"]
    if name == "Issued":
        if bid in bills:
            raise ImpossibleTransition("bill %d issued twice (block %d)" % (bid, ev["blockNumber"]))
        if bid < 1:
            raise ImpossibleTransition("bill id %d < 1" % bid)
        rec = BillRecord(bid, payee=ev["payee"], amount=ev["amount"], payBy=ev["payBy"],
                         claimWindow=ev["claimWindow"], allowedPayer=ev["allowedPayer"], status=OPEN,
                         ref=ev["ref"])
        rec.history.append(ev)
        bills[bid] = rec
        return
    rec = bills.get(bid)
    if rec is None:
        raise ImpossibleTransition(
            "bill %d: %s before BillIssued (block %d) - from-block later than the bill's issue?" % (
                bid, name, ev["blockNumber"]))
    if name == "Cancelled":
        _need(rec, ev, OPEN)
        rec.status = CANCELLED
    elif name == "Paid":
        _need(rec, ev, OPEN)
        if rec.allowedPayer.lower() != ZERO and rec.allowedPayer.lower() != ev["payer"].lower():
            raise ImpossibleTransition("bill %d paid by %s, allowed payer is %s" % (
                bid, ev["payer"], rec.allowedPayer))
        rec.payer = ev["payer"]
        rec.claimBy = ev["claimBy"]
        rec.status = PAID
    elif name == "Claimed":
        _need(rec, ev, PAID)
        _same(rec, ev, "payee", rec.payee, ev["payee"])
        _same(rec, ev, "amount", rec.amount, ev["amount"])
        rec.status = CLAIMED
    elif name == "Declined":
        _need(rec, ev, PAID)
        _same(rec, ev, "payer", rec.payer, ev["payer"])
        _same(rec, ev, "amount", rec.amount, ev["amount"])
        rec.status = DECLINED
    elif name == "Refunded":
        _need(rec, ev, PAID)
        _same(rec, ev, "payer", rec.payer, ev["payer"])
        _same(rec, ev, "amount", rec.amount, ev["amount"])
        rec.status = REFUNDED
    else:
        raise ImpossibleTransition("unknown event %r" % name)
    rec.history.append(ev)


def fold(events) -> dict:
    bills: dict = {}
    for ev in dedupe(events):
        apply(bills, ev)
    return bills


def is_due(rec, block_timestamp: int) -> bool:
    """Refundable now: status Paid and claimBy <= block timestamp (contract: refund needs now >= claimBy)."""
    return rec.status == PAID and rec.claimBy > 0 and rec.claimBy <= block_timestamp


def due(bills: dict, block_timestamp: int) -> list:
    return [bills[k] for k in sorted(bills) if is_due(bills[k], block_timestamp)]


def matches_chain(rec: BillRecord, b: Bill) -> list:
    """Field names where the event-built record differs from getBill (empty = consistent)."""
    diffs = []
    for f in ("payee", "amount", "payer", "payBy", "claimWindow", "allowedPayer", "claimBy", "status", "ref"):
        a, c = getattr(rec, f), getattr(b, f)
        if isinstance(a, str):
            a, c = a.lower(), c.lower()
        if a != c:
            diffs.append(f)
    return diffs
