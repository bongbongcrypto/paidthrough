"""Tests for scripts/test_bills.py against a fake Arc chain (no network).

The fake decodes each signed transaction like a node, runs a small model of PaidThrough + Arc USDC (native balance
18 decimals, ERC-20 view 6 decimals, the duplicate 18-decimal Transfer log from 0xff..fe that real Arc emits),
verifies EIP-3009 signatures with its OWN authNonce, keeps per-block history for eth_getBalance / eth_call at a
block tag, and can lag, hide receipts or drop transactions on purpose. Keys come from Account.create() and live
only in a temp directory.
"""
from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "keeper"))

from eth_abi import decode, encode  # noqa: E402
from eth_account import Account  # noqa: E402
from eth_account.messages import _hash_eip191_message, encode_typed_data  # noqa: E402
from eth_account.typed_transactions import TypedTransaction  # noqa: E402
from eth_keys import keys as eth_keys  # noqa: E402
from hexbytes import HexBytes  # noqa: E402

import abi as kabi  # noqa: E402
import test_bills as tb  # noqa: E402
from rpc import Rpc, RpcError  # noqa: E402

GWEI = 10**9
PT, _ = tb.deployment()
USDC = tb.USDC
NATIVE_LOG = "0xfffffffffffffffffffffffffffffffffffffffe"
U = tb.WEI_PER_UNIT


def S(sig):
    return tb.fn(sig)[:10]


SEL = {
    "issue": S("issue(uint96,uint64,uint32,address,bytes32)"),
    "pay": S("pay(uint256)"),
    "pwa": S("payWithAuthorization(uint256,address,uint256,uint256,uint8,bytes32,bytes32)"),
    "claim": S("claim(uint256)"),
    "decline": S("decline(uint256)"),
    "refund": S("refund(uint256)"),
    "getBill": S("getBill(uint256)"),
    "authNonce": S("authNonce(uint256)"),
    "billCount": S("billCount()"),
    "approve": S("approve(address,uint256)"),
    "allowance": S("allowance(address,address)"),
    "balanceOf": S("balanceOf(address)"),
    "isBlacklisted": S("isBlacklisted(address)"),
    "DOMAIN_SEPARATOR": S("DOMAIN_SEPARATOR()"),
}
GAS = {SEL[k]: tb.GAS_HINT[k] for k in ("issue", "pay", "pwa", "claim", "decline", "approve")}
GAS[SEL["refund"]] = 66_463


class Revert(Exception):
    pass


def t32(addr):
    return "0x" + "0" * 24 + addr.lower()[2:]


def hx(b):
    return "0x" + bytes(b).hex()


class FakeArc(Rpc):
    def __init__(self, balances=None, base_fee=20 * GWEI, prio=1, first_id=0, nonce_twist=False):
        super().__init__("http://fake.invalid")
        self.st = {"bal": {k.lower(): v for k, v in (balances or {}).items()}, "allow": {}, "bills": [],
                   "auth": set()}
        for _ in range(first_id):  # bills other people issued before us
            self.st["bills"].append(self._bill("0x" + "11" * 20, 1, 0, 3600, "0x" + "00" * 20, b"\0" * 32))
        self.base_fee, self.prio = base_fee, prio
        self.codes = {PT.lower(): b"\x60\x80"}
        self.nonces = {}
        self.head, self.ts = 5_000, 1_790_000_000
        self.hist = {self.head: (copy.deepcopy(self.st), self.ts)}
        self.logs, self.receipts, self.sent, self.calls = [], {}, [], []
        self.nonce_twist = nonce_twist           # contract authNonce differs from the local formula
        self.foreign_issue = False               # someone else issues a bill in the same block before ours
        self.stale_after = None                  # selector: after it is mined, the next tagged getBill lags
        self._stale_armed = False
        self.hide_after = None                   # selector: receipt hidden (mined) until release()
        self.hidden = {}
        self.drop_next = None                    # selector: accepted but never mined, once
        self.payer_low = None
        self.hold = None                         # predicate(sent entry): the tx waits in the pool until flush()
        self.pool = []                           # (decoded tx, sent entry), pending, counted by 'pending' nonce
        self.dust_after = None                   # selector: an outside 1-wei transfer to its sender, same block

    # ---- model helpers
    @staticmethod
    def _bill(payee, amount, pay_by, cw, allowed, ref):
        return {"payee": payee, "amount": amount, "payer": "0x" + "00" * 20, "payBy": pay_by, "claimWindow": cw,
                "allowed": allowed, "claimBy": 0, "status": 1, "ref": ref}

    def auth_nonce(self, bid):
        extra = [b"twist"] if self.nonce_twist else []
        return tb.kec(encode(["uint256", "address", "uint256"] + (["bytes"] if extra else []),
                             [tb.CHAIN_ID, PT, bid] + extra))

    def move(self, st, frm, to, units, logs):
        frm, to = frm.lower(), to.lower()
        if st["bal"].get(frm, 0) < units * U:
            raise Revert("ERC20: transfer amount exceeds balance")
        st["bal"][frm] = st["bal"].get(frm, 0) - units * U
        st["bal"][to] = st["bal"].get(to, 0) + units * U
        logs.append((NATIVE_LOG, [tb.TRANSFER_TOPIC, t32(frm), t32(to)], encode(["uint256"], [units * U])))
        logs.append((USDC, [tb.TRANSFER_TOPIC, t32(frm), t32(to)], encode(["uint256"], [units])))

    def execute(self, st, sender, to, data, value, ts, logs):
        sender = sender.lower()
        if value:
            if st["bal"].get(sender, 0) < value:
                raise Revert("insufficient value")
            st["bal"][sender] -= value
            st["bal"][to.lower()] = st["bal"].get(to.lower(), 0) + value
        if not data or data == b"":
            return b""
        sel, args = hx(data[:4]), bytes(data[4:])
        if to.lower() == USDC.lower():
            if sel == SEL["approve"]:
                spender, amt = decode(["address", "uint256"], args)
                st["allow"][(sender, spender.lower())] = amt
                return encode(["bool"], [True])
            raise Revert("usdc: unknown")
        if to.lower() != PT.lower():
            raise Revert("no code")
        bills = st["bills"]

        def get(bid):
            if not 1 <= bid <= len(bills):
                raise Revert("WrongStatus")
            return bills[bid - 1]

        if sel == SEL["issue"]:
            amount, pay_by, cw, allowed, ref = decode(["uint96", "uint64", "uint32", "address", "bytes32"], args)
            if not 0 < amount <= 10_000 * 10**6 or not ts < pay_by <= ts + 365 * 86400 or not 3600 <= cw:
                raise Revert("Bad*")
            if self.foreign_issue:
                bills.append(self._bill("0x" + "22" * 20, 7, pay_by, cw, "0x" + "00" * 20, b"\1" * 32))
            bills.append(self._bill(sender, amount, pay_by, cw, allowed.lower(), ref))
            bid = len(bills)
            logs.append((PT, [kabi.EVENTS["Issued"][0], "0x%064x" % bid, t32(sender), t32(allowed)],
                         encode(["uint96", "uint64", "uint32", "bytes32"], [amount, pay_by, cw, ref])))
            return encode(["uint256"], [bid])
        if sel in (SEL["pay"], SEL["pwa"]):
            if sel == SEL["pay"]:
                (bid,) = decode(["uint256"], args)
                payer = sender
            else:
                bid, payer, va, vb, v, r, s = decode(["uint256", "address", "uint256", "uint256", "uint8", "bytes32",
                                                      "bytes32"], args)
                payer = payer.lower()
            b = get(bid)
            if b["status"] != 1:
                raise Revert("WrongStatus")
            if ts >= b["payBy"]:
                raise Revert("PayWindowClosed")
            if int(b["allowed"], 16) and b["allowed"] != payer:
                raise Revert("NotAllowedPayer")
            if sel == SEL["pay"]:
                if st["allow"].get((payer, PT.lower()), 0) < b["amount"]:
                    raise Revert("ERC20: transfer amount exceeds allowance")
                st["allow"][(payer, PT.lower())] -= b["amount"]
            else:
                nonce = self.auth_nonce(bid)
                digest = tb.auth_digest(payer, PT, b["amount"], va, vb, nonce, bytes.fromhex(
                    tb.USDC_DOMAIN_SEPARATOR[2:]))
                try:
                    who = eth_keys.Signature(vrs=(v - 27, int.from_bytes(r, "big"), int.from_bytes(s, "big"))) \
                        .recover_public_key_from_msg_hash(digest).to_checksum_address()
                except Exception:  # noqa: BLE001
                    raise Revert("FiatTokenV2: invalid signature") from None
                if who.lower() != payer or nonce in st["auth"]:
                    raise Revert("FiatTokenV2: invalid signature")
                if not (ts > va and ts < vb):
                    raise Revert("FiatTokenV2: authorization is not yet valid / expired")
                st["auth"].add(nonce)
            b.update({"payer": payer, "claimBy": ts + b["claimWindow"], "status": 2})
            self.move(st, payer, PT, b["amount"], logs)
            logs.append((PT, [kabi.EVENTS["Paid"][0], "0x%064x" % bid, t32(payer)], encode(["uint64"], [b["claimBy"]])))
            return b""
        if sel in (SEL["claim"], SEL["decline"]):
            (bid,) = decode(["uint256"], args)
            b = get(bid)
            if b["payee"] != sender:
                raise Revert("NotPayee")
            if b["status"] != 2:
                raise Revert("WrongStatus")
            if ts >= b["claimBy"]:
                raise Revert("ClaimWindowClosed")
            if sel == SEL["claim"]:
                b["status"] = 3
                self.move(st, PT, b["payee"], b["amount"], logs)
                logs.append((PT, [kabi.EVENTS["Claimed"][0], "0x%064x" % bid, t32(b["payee"])],
                             encode(["uint96"], [b["amount"]])))
            else:
                b["status"] = 5
                self.move(st, PT, b["payer"], b["amount"], logs)
                logs.append((PT, [kabi.EVENTS["Declined"][0], "0x%064x" % bid, t32(b["payer"])],
                             encode(["uint96"], [b["amount"]])))
            return b""
        if sel == SEL["refund"]:
            (bid,) = decode(["uint256"], args)
            b = get(bid)
            if b["status"] != 2 or ts < b["claimBy"]:
                raise Revert("ClaimWindowOpen")
            b["status"] = 4
            self.move(st, PT, b["payer"], b["amount"], logs)
            logs.append((PT, [kabi.EVENTS["Refunded"][0], "0x%064x" % bid, t32(b["payer"])],
                         encode(["uint96", "address"], [b["amount"], sender])))
            return b""
        raise Revert("unknown selector")

    def view(self, st, to, data, tag):
        sel, args = data[:10], bytes.fromhex(data[10:])
        if to.lower() == PT.lower():
            if sel == SEL["getBill"]:
                (bid,) = decode(["uint256"], args)
                b = st["bills"][bid - 1] if 1 <= bid <= len(st["bills"]) else self._bill(
                    "0x" + "00" * 20, 0, 0, 0, "0x" + "00" * 20, b"\0" * 32) | {"status": 0}
                status = b["status"]
                if self._stale_armed and tag not in ("latest", "pending"):
                    self._stale_armed = False
                    status = 1
                return hx(encode([kabi.BILL_TUPLE], [(b["payee"], b["amount"], b["payer"], b["payBy"],
                                                      b["claimWindow"], b["allowed"], b["claimBy"], status,
                                                      b["ref"])]))
            if sel == SEL["authNonce"]:
                return hx(self.auth_nonce(decode(["uint256"], args)[0]))
            if sel == SEL["billCount"]:
                return hx(encode(["uint256"], [len(st["bills"])]))
        if to.lower() == USDC.lower():
            if sel == SEL["allowance"]:
                o, s = decode(["address", "address"], args)
                return hx(encode(["uint256"], [st["allow"].get((o.lower(), s.lower()), 0)]))
            if sel == SEL["balanceOf"]:
                return hx(encode(["uint256"], [st["bal"].get(decode(["address"], args)[0].lower(), 0) // U]))
            if sel == SEL["isBlacklisted"]:
                return hx(encode(["bool"], [False]))
            if sel == SEL["DOMAIN_SEPARATOR"]:
                return tb.USDC_DOMAIN_SEPARATOR
        return None

    def state_at(self, tag):
        if tag in ("latest", "pending"):
            return self.st, self.ts
        return self.hist[int(tag, 16)]

    # ---- mining
    def mine(self, raw):
        tx = TypedTransaction.from_bytes(HexBytes(raw)).as_dict()
        sender = Account.recover_transaction(raw).lower()
        to, data, value = hx(tx["to"]), bytes(tx["data"]), int(tx["value"])
        if tx["chainId"] != tb.CHAIN_ID:
            raise RpcError("eth_sendRawTransaction", {"code": -32000, "message": "invalid chain id"})
        n = self.nonces.get(sender, 0)
        queued = sum(1 for _, e in self.pool if e["sender"] == sender)
        if tx["nonce"] < n:
            raise RpcError("eth_sendRawTransaction", {"code": -32000, "message": "nonce too low"})
        if tx["nonce"] < n + queued:
            raise RpcError("eth_sendRawTransaction", {"code": -32000, "message": "replacement transaction underpriced"})
        if tx["nonce"] > n + queued:
            raise RpcError("eth_sendRawTransaction", {"code": -32000, "message": "nonce too high"})
        if tx["maxFeePerGas"] < max(20 * GWEI, self.base_fee):
            raise RpcError("eth_sendRawTransaction", {"code": -32000, "message": "underpriced (dropped)"})
        if self.st["bal"].get(sender, 0) < tx["gas"] * tx["maxFeePerGas"] + value:
            raise RpcError("eth_sendRawTransaction", {"code": -32000, "message": "insufficient funds"})
        h = hx(tb.kec(bytes(HexBytes(raw))))
        sel = hx(data[:4]) if data else "0x"
        self.sent.append({"hash": h, "sender": sender, "to": to.lower(), "sel": sel, "data": data, "value": value,
                          "nonce": tx["nonce"], "maxFee": tx["maxFeePerGas"]})
        if self.drop_next and sel == self.drop_next:
            self.drop_next = None
            self.sent[-1]["dropped"] = True
            return h
        if (self.hold and self.hold(self.sent[-1])) or queued:
            self.sent[-1]["pending"] = True
            self.pool.append((tx, self.sent[-1]))
            return h
        return self._include(tx, self.sent[-1])

    def _include(self, tx, entry):
        """Mine one tx in its own block."""
        sender, to, data, value, h, sel = (entry["sender"], entry["to"], entry["data"], entry["value"], entry["hash"],
                                           entry["sel"])
        n = self.nonces.get(sender, 0)
        assert tx["nonce"] == n
        gas_used = GAS.get(sel, 21_000)
        egp = self.base_fee + min(tx["maxPriorityFeePerGas"], tx["maxFeePerGas"] - self.base_fee)
        self.head += 1
        self.ts += 2
        before = copy.deepcopy(self.st)
        logs = []
        status = 1
        try:
            self.execute(self.st, sender, to, data, value, self.ts, logs)
        except Revert:
            self.st = before
            logs = []
            status = 0
        self.st["bal"][sender] -= gas_used * egp
        self.nonces[sender] = n + 1
        if self.dust_after and sel == self.dust_after:
            self.dust_after = None
            self.st["bal"][sender] += 1          # someone else's 1-wei transfer lands in the same block
        self.hist[self.head] = (copy.deepcopy(self.st), self.ts)
        rlogs = []
        for i, (addr, topics, d) in enumerate(logs):
            lg = {"address": addr, "topics": topics, "data": hx(d), "blockNumber": hex(self.head),
                  "logIndex": hex(i), "transactionHash": h}
            rlogs.append(lg)
            self.logs.append(lg)
        rc = {"transactionHash": h, "status": hex(status), "blockNumber": hex(self.head), "from": sender, "to": to,
              "gasUsed": hex(gas_used), "effectiveGasPrice": hex(egp), "logs": rlogs}
        if self.hide_after and sel == self.hide_after:
            self.hide_after = None
            self.hidden[h] = rc
        else:
            self.receipts[h] = rc
        if self.stale_after and sel == self.stale_after:
            self.stale_after = None
            self._stale_armed = True
        return h

    def flush(self):
        """The pool drains: held txs are mined in nonce order."""
        self.hold = None
        pool, self.pool = sorted(self.pool, key=lambda x: x[0]["nonce"]), []
        for tx, e in pool:
            e.pop("pending", None)
            self._include(tx, e)

    def forget_pool(self):
        """The node drops every pooled tx (they are never mined)."""
        for _, e in self.pool:
            e.pop("pending", None)
            e["dropped"] = True
        self.pool = []

    def tick(self, *_):
        """An empty block."""
        self.head += 1
        self.ts += 2
        self.hist[self.head] = (copy.deepcopy(self.st), self.ts)

    def outside_tx(self, acct, nonce=None):
        """A tx the script did not send, from a key it shares (the keeper on the deployer key): a 0-value self-send."""
        n = self.nonces.get(acct.address.lower(), 0) if nonce is None else nonce
        tx = {"type": 2, "chainId": tb.CHAIN_ID, "nonce": n, "to": acct.address, "value": 0, "data": b"",
              "gas": 21_000, "maxFeePerGas": 40 * GWEI, "maxPriorityFeePerGas": 1, "accessList": []}
        return self.mine("0x" + bytes(acct.sign_transaction(tx).raw_transaction).hex())

    def release(self):
        self.receipts.update(self.hidden)
        self.hidden = {}

    def keeper_refund(self, bid):
        """Advance time past claimBy and let a third party (the keeper) refund."""
        self.ts = self.st["bills"][bid - 1]["claimBy"] + 10
        keeper = Account.create()
        self.st["bal"][keeper.address.lower()] = 10**18
        tx = {"type": 2, "chainId": tb.CHAIN_ID, "nonce": 0, "to": PT, "value": 0,
              "data": tb.fn("refund(uint256)", ["uint256"], [bid]), "gas": 100_000, "maxFeePerGas": 40 * GWEI,
              "maxPriorityFeePerGas": 1, "accessList": []}
        return self.mine("0x" + bytes(keeper.sign_transaction(tx).raw_transaction).hex())

    # ---- JSON-RPC
    def call(self, method, params):
        self.calls.append((method, params))
        if method == "eth_chainId":
            return hex(tb.CHAIN_ID)
        if method == "eth_blockNumber":
            return hex(self.head)
        if method == "eth_getBlockByNumber":
            tag = params[0]
            n = self.head if tag == "latest" else int(tag, 16)
            ts = self.ts if tag == "latest" else self.hist[n][1]
            return {"number": hex(n), "timestamp": hex(ts), "baseFeePerGas": hex(self.base_fee)}
        if method == "eth_maxPriorityFeePerGas":
            return hex(self.prio)
        if method == "eth_getCode":
            return hx(self.codes.get(params[0].lower(), b""))
        if method == "eth_getTransactionCount":
            a = params[0].lower()
            n = self.nonces.get(a, 0)
            if params[1] == "pending":
                n += sum(1 for s in self.sent if s["sender"] == a and s.get("pending"))
            return hex(n)
        if method == "eth_getBalance":
            st, _ = self.state_at(params[1])
            return hex(st["bal"].get(params[0].lower(), 0))
        if method in ("eth_call", "eth_estimateGas"):
            call = params[0]
            tag = params[1] if len(params) > 1 else "latest"
            st, ts = self.state_at(tag)
            data = call.get("data") or "0x"
            if method == "eth_call" and "from" not in call:
                out = self.view(st, call["to"], data, tag)
                if out is None:
                    raise RpcError(method, {"code": 3, "message": "execution reverted"})
                return out
            st = copy.deepcopy(st)
            if len(params) > 2:
                for a, o in params[2].items():
                    st["bal"][a.lower()] = int(o["balance"], 16)
            try:
                ret = self.execute(st, call["from"], call["to"], bytes.fromhex(data[2:]),
                                   int(call.get("value", "0x0"), 16), ts + 1, [])
            except Revert as e:
                raise RpcError(method, {"code": 3, "message": "execution reverted: %s" % e}) from None
            if method == "eth_estimateGas":
                return hex(GAS.get(data[:10], 21_000))
            return hx(ret)
        if method == "eth_sendRawTransaction":
            return self.mine(params[0])
        if method == "eth_getTransactionReceipt":
            return self.receipts.get(params[0])
        if method == "eth_getTransactionByHash":
            for s in self.sent:
                if s["hash"] == params[0] and not s.get("dropped"):
                    rc = self.receipts.get(s["hash"]) or self.hidden.get(s["hash"])
                    return {"hash": s["hash"], "nonce": hex(s["nonce"]),
                            "blockNumber": rc["blockNumber"] if rc and not s.get("pending") else None}
            return None
        if method == "eth_getLogs":
            f = params[0]
            lo, hi = int(f["fromBlock"], 16), int(f["toBlock"], 16)
            out = []
            for lg in self.logs:
                if not tb.same(lg["address"], f["address"]) or not lo <= int(lg["blockNumber"], 16) <= hi:
                    continue
                if all(t is None or (i < len(lg["topics"]) and lg["topics"][i].lower() == t.lower())
                       for i, t in enumerate(f.get("topics") or [])):
                    out.append(lg)
            return out
        raise AssertionError("unexpected RPC method " + method)

    # ---- inspection
    def mined(self, sel=None):
        return [s for s in self.sent if not s.get("dropped") and not s.get("pending")
                and (sel is None or s["sel"] == sel)]

    def bal(self, a):
        return self.st["bal"].get(a.lower(), 0)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.record = self.dir / "status" / "test-bills.json"
        self.accts = {r: Account.create() for r in ("deployer", "biller", "payer")}
        self.keyhex = {r: a.key.hex().removeprefix("0x") for r, a in self.accts.items()}
        self.files = {}
        self._env = {}
        for role, (env, _, var) in tb.ROLES.items():
            f = self.dir / ("%s.env" % role)
            f.write_text("%s=0x%s\n" % (var, self.keyhex[role]), encoding="utf-8")
            self.files[role] = f
            self._env[env] = os.environ.get(env)
            os.environ[env] = str(f)
        self._known = dict(tb.KNOWN)
        for r, a in self.accts.items():
            tb.KNOWN[r] = a.address
        self.A, self.B, self.P = (self.accts[r].address for r in ("deployer", "biller", "payer"))

    def tearDown(self):
        tb.KNOWN.clear()
        tb.KNOWN.update(self._known)
        for env, v in self._env.items():
            if v is None:
                os.environ.pop(env, None)
            else:
                os.environ[env] = v
        self.tmp.cleanup()

    def run_main(self, args, rpc, confirm=lambda _: "SEND", sleep=lambda s: None):
        out, err = io.StringIO(), io.StringIO()
        prompts = []

        def conf(q):
            prompts.append(q)
            return confirm(q)

        with contextlib.redirect_stderr(err):
            code = tb.main(args + ["--record", str(self.record), "--receipt-timeout", "3"], rpc=rpc, out=out,
                           confirm=conf, sleep=sleep)
        self.prompts = prompts
        text = out.getvalue() + err.getvalue()
        self.assert_no_keys(text)
        return code, text

    def assert_no_keys(self, text):
        for k in self.keyhex.values():
            self.assertNotIn(k.lower(), text.lower())
            self.assertNotIn(k[:24].lower(), text.lower())
        for p in (self.record, self.record.with_suffix(".md")):
            if p.exists():
                body = p.read_text(encoding="utf-8").lower()
                for k in self.keyhex.values():
                    self.assertNotIn(k.lower(), body)

    def funded_chain(self, extra=0, **kw):
        max_fee = max(2 * 20 * GWEI, 20 * GWEI)
        payer = tb.PAYER_PEAK + tb.budget(tb.PAYER_KINDS, max_fee)  # exactly 1.0 USDC + gas budget, nothing more
        biller = tb.budget(tb.BILLER_KINDS, max_fee)
        return FakeArc({self.A: 2_077_256_479_435_960_704, self.P: payer + extra, self.B: biller + extra}, **kw)


# ---------------------------------------------------------------------------------------------- fund


class FundTests(Base):
    def test_dry_run_without_key_files_uses_known_addresses_and_never_signs(self):
        for f in self.files.values():
            f.unlink()
        rpc = FakeArc({self.A: 2 * 10**18})
        with mock.patch.object(tb.Account, "from_key", side_effect=AssertionError("no key read")), \
                mock.patch.object(tb.Signer, "sign_tx", side_effect=AssertionError("no signing")):
            code, text = self.run_main(["fund"], rpc)
        self.assertEqual(code, 0, text)
        self.assertIn("DRY RUN", text)
        self.assertIn("no key file", text)
        self.assertEqual(rpc.sent, [])
        self.assertEqual(self.prompts, [])
        est = [p[0] for m, p in rpc.calls if m == "eth_estimateGas"]
        self.assertEqual([(e["to"], int(e["value"], 16)) for e in est],
                         [(self.P, 1_100_000_000_000_000_000), (self.B, 50_000_000_000_000_000)])
        self.assertFalse(self.record.exists())

    def test_dry_run_with_key_files_derives_addresses_only(self):
        rpc = FakeArc({self.A: 2 * 10**18})
        with mock.patch.object(tb.Signer, "sign_tx", side_effect=AssertionError("no signing")), \
                mock.patch.object(tb.Signer, "sign_digest", side_effect=AssertionError("no signing")):
            code, text = self.run_main(["fund"], rpc)
        self.assertEqual(code, 0, text)
        self.assertIn("matches the recorded address", text)
        self.assertNotIn("eth_sendRawTransaction", [m for m, _ in rpc.calls])

    def test_send_exact_amounts_then_idempotent(self):
        rpc = FakeArc({self.A: 2_077_256_479_435_960_704})
        code, text = self.run_main(["fund", "--send"], rpc)
        self.assertEqual(code, 0, text)
        self.assertEqual(len(self.prompts), 1)
        sent = rpc.mined()
        self.assertEqual([(s["sender"], s["to"], s["value"], s["data"]) for s in sent],
                         [(self.A.lower(), self.P.lower(), 11 * 10**17, b""),
                          (self.A.lower(), self.B.lower(), 5 * 10**16, b"")])
        self.assertEqual(rpc.bal(self.P), 11 * 10**17)
        self.assertEqual(rpc.bal(self.B), 5 * 10**16)
        self.assertTrue(all(s["maxFee"] >= 40 * GWEI for s in sent))
        rec = json.loads(self.record.read_text(encoding="utf-8"))
        self.assertEqual([t["valueWei"] for t in rec["fund"]["txs"]], [11 * 10**17, 5 * 10**16])
        self.assertEqual([t["state"] for t in rec["fund"]["txs"]], ["confirmed", "confirmed"])
        code, text = self.run_main(["fund", "--send"], rpc)
        self.assertEqual(code, 0, text)
        self.assertIn("nothing to fund", text)
        self.assertEqual(len(rpc.mined()), 2)

    def test_refuses_to_leave_a_under_half_usdc(self):
        rpc = FakeArc({self.A: 1_640_000_000_000_000_000})  # 1.64 - 1.15 - gas < 0.50
        for args in (["fund"], ["fund", "--send"]):
            code, text = self.run_main(args, rpc)
            self.assertEqual(code, 2, text)
            self.assertIn("less than 0.50000000 USDC", text)
        self.assertEqual(rpc.sent, [])
        self.assertEqual(self.prompts, [])

    def test_only_unfunded_recipient_is_funded(self):
        rpc = FakeArc({self.A: 2 * 10**18, self.P: 11 * 10**17})
        code, text = self.run_main(["fund", "--send"], rpc)
        self.assertEqual(code, 0, text)
        self.assertEqual([(s["to"], s["value"]) for s in rpc.mined()], [(self.B.lower(), 5 * 10**16)])

    def test_confirmation_required(self):
        rpc = FakeArc({self.A: 2 * 10**18})
        code, text = self.run_main(["fund", "--send"], rpc, confirm=lambda _: "send")
        self.assertEqual(code, 2)
        self.assertIn("not confirmed", text)
        self.assertEqual(rpc.sent, [])

    def test_send_refuses_key_that_is_not_the_recorded_wallet(self):
        tb.KNOWN["payer"] = Account.create().address
        rpc = FakeArc({self.A: 2 * 10**18})
        code, text = self.run_main(["fund", "--send"], rpc)
        self.assertEqual(code, 2)
        self.assertIn("not the recorded", text)
        self.assertEqual(rpc.sent, [])


# ---------------------------------------------------------------------------------------------- keys


class KeyTests(Base):
    def test_key_file_inside_repo_refused(self):
        with self.assertRaises(tb.Refused) as cm:
            tb.load_key("payer", ROOT / "scripts" / "tests" / "no-such-key.env")
        self.assertIn("outside the repo", str(cm.exception))

    def test_malformed_key_lines_never_echoed(self):
        k = self.keyhex["payer"]
        for body in ("PAYER_PRIVATE_KEY=0x%s\n" % k[:-1],            # 63 hex chars
                     "PAYER_PRIVATE_KEY=0x%sZ\n" % k[:-1],           # non-hex
                     "PAYER_PRIVATE_KEY=0x%s\n" % ("0" * 64),       # out of curve range
                     "PAYER_PRIVATE_KEY = '%s' extra\n" % k):        # trailing junk
            self.files["payer"].write_text(body, encoding="utf-8")
            with self.assertRaises(tb.Refused) as cm:
                tb.load_key("payer")
            self.assertNotIn(k[:20], str(cm.exception))
            self.assertNotIn(k[-20:], str(cm.exception))
            rpc = self_rpc = FakeArc({self.A: 2 * 10**18})
            code, text = self.run_main(["bills"], rpc)  # dry run reports the file as unusable
            self.assertEqual(code, 3)
            self.assertIn("key file unusable", text)
            self.assertEqual(self_rpc.sent, [])

    def test_signer_repr_has_no_key(self):
        s = tb.load_key("payer")
        self.assertNotIn(self.keyhex["payer"], repr(s) + str(s))


# ---------------------------------------------------------------------------------------------- digest


class DigestTests(unittest.TestCase):
    def test_eip3009_digest_matches_eth_account_typed_data(self):
        payer = Account.create()
        nonce = bytes.fromhex("120d1c916ccf32ebe62fd8bc82e63cf4bce63ae27ed2f5bb2df419dc956b224a")
        msg = {
            "types": {
                "EIP712Domain": [{"name": "name", "type": "string"}, {"name": "version", "type": "string"},
                                 {"name": "chainId", "type": "uint256"},
                                 {"name": "verifyingContract", "type": "address"}],
                "ReceiveWithAuthorization": [{"name": "from", "type": "address"}, {"name": "to", "type": "address"},
                                             {"name": "value", "type": "uint256"},
                                             {"name": "validAfter", "type": "uint256"},
                                             {"name": "validBefore", "type": "uint256"},
                                             {"name": "nonce", "type": "bytes32"}],
            },
            "primaryType": "ReceiveWithAuthorization",
            "domain": {"name": "USDC", "version": "2", "chainId": 5042,
                       "verifyingContract": "0x3600000000000000000000000000000000000000"},
            "message": {"from": payer.address, "to": PT, "value": 500000, "validAfter": 0,
                        "validBefore": 1_790_974_697, "nonce": nonce},
        }
        signable = encode_typed_data(full_message=msg)
        self.assertEqual("0x" + bytes(signable.header).hex(),
                         "0x940506929bba468048a19b567f4f0d534714bc06604b5c3017e5d16785ccdf84")
        self.assertEqual(tb.domain_separator().hex(), bytes(signable.header).hex())
        mine = tb.auth_digest(payer.address, PT, 500000, 0, 1_790_974_697, nonce, tb.domain_separator())
        self.assertEqual(mine, bytes(_hash_eip191_message(signable)))
        self.assertEqual("0x" + tb.kec(tb.RECEIVE_TYPE.encode()).hex(), tb.RECEIVE_TYPEHASH)
        v, r, s = tb.Signer("payer", payer).sign_digest(mine)
        self.assertEqual(Account.recover_message(signable, vrs=(v, r, s)), payer.address)

    def test_fingerprint_and_link(self):
        salt = "00112233445566778899aabbccddeeff"
        import hashlib
        want = "0x" + hashlib.sha256(("paidthrough:v1:" + salt + ":Test bill A (mainnet check)").encode()).hexdigest()
        self.assertEqual(tb.fingerprint(salt, "Test bill A (mainnet check)"), want)
        self.assertEqual(tb.family_link(7, "Test bill A (mainnet check)", salt),
                         "https://bongbongcrypto.github.io/paidthrough/#/bill/7?r=Test+bill+A+%28mainnet+check%29"
                         "&s=00112233445566778899aabbccddeeff")
        with self.assertRaises(ValueError):
            tb.fingerprint("0x" + salt[2:], "x")

    def test_amount_constants(self):
        self.assertEqual(tb.AMOUNT, 500_000)
        self.assertEqual(tb.FUND_PAYER, 1_100_000_000_000_000_000)
        self.assertEqual(tb.FUND_BILLER, 50_000_000_000_000_000)
        self.assertEqual(tb.A_RESERVE, 500_000_000_000_000_000)
        self.assertEqual(tb.PAYER_PEAK, 10**18)
        self.assertEqual([n for n, *_ in tb.STEPS], ["A.issue", "A.pay", "A.claim", "C.issue", "C.approve",
                                                     "C.pay", "C.decline", "B.issue", "B.pay"])
        self.assertEqual(tb.BILLS["B"]["claimWindow"], 3600)


# ---------------------------------------------------------------------------------------------- bills


class BillsTests(Base):
    def sequence(self, rpc):
        names = {v: k for k, v in SEL.items()}
        return [(names.get(s["sel"], s["sel"]), "biller" if s["sender"] == self.B.lower() else "payer")
                for s in rpc.mined()]

    def test_dry_run_reports_not_ready_and_never_signs(self):
        rpc = FakeArc({self.A: 2 * 10**18})
        with mock.patch.object(tb.Signer, "sign_tx", side_effect=AssertionError("no signing")), \
                mock.patch.object(tb.Signer, "sign_digest", side_effect=AssertionError("no signing")):
            code, text = self.run_main(["bills"], rpc)
            self.assertEqual(code, 3, text)
            self.assertIn("NOT READY", text)
            self.assertIn("plan", text)
            rpc2 = self.funded_chain()
            code, text = self.run_main(["bills"], rpc2)
        self.assertEqual(code, 0, text)
        self.assertNotIn("NOT READY", text)
        self.assertIn("simulate     issue(500000", text)
        self.assertIn("digest 0x", text)
        self.assertEqual(rpc.sent + rpc2.sent, [])
        self.assertFalse(self.record.exists())

    def test_payer_with_code_is_not_ready(self):
        rpc = self.funded_chain()
        rpc.codes[self.P.lower()] = bytes.fromhex("ef0100") + bytes(20)
        code, text = self.run_main(["bills", "--send"], rpc)
        self.assertEqual(code, 2, text)
        self.assertIn("has code", text)
        self.assertEqual(rpc.sent, [])

    def test_full_run(self):
        rpc = self.funded_chain(first_id=40, nonce_twist=True)
        rpc.foreign_issue = True   # ids cannot be predicted from billCount
        code, text = self.run_main(["bills", "--send"], rpc)
        self.assertEqual(code, 0, text)
        self.assertEqual(len(self.prompts), 1)
        self.assertIn("ALL STEPS DONE", text)
        self.assertEqual(self.sequence(rpc), [
            ("issue", "biller"), ("pwa", "payer"), ("claim", "biller"),
            ("issue", "biller"), ("approve", "payer"), ("pay", "payer"), ("decline", "biller"),
            ("issue", "biller"), ("pwa", "payer")])
        rec = json.loads(self.record.read_text(encoding="utf-8"))
        ours = {i + 1: b for i, b in enumerate(rpc.st["bills"]) if b["payee"] == self.B.lower()}
        self.assertEqual(sorted(ours), sorted(rec["bills"][k]["billId"] for k in "ACB"))
        self.assertEqual(sorted(ours), [42, 44, 46])  # foreign bills 41, 43, 45 interleaved
        status = {k: ours[rec["bills"][k]["billId"]]["status"] for k in "ACB"}
        self.assertEqual(status, {"A": kabi.CLAIMED, "C": kabi.DECLINED, "B": kabi.PAID})
        for k in "ACB":
            b = rec["bills"][k]
            bill = ours[b["billId"]]
            self.assertEqual(bill["amount"], 500_000)
            self.assertEqual(bill["allowed"], self.P.lower())
            self.assertEqual("0x" + bill["ref"].hex(), tb.fingerprint(b["salt"], b["text"]))
            self.assertIn("#/bill/%d?r=Test+bill+%s+%%28mainnet+check%%29&s=%s" % (b["billId"], k, b["salt"]),
                          text)
        self.assertEqual(ours[rec["bills"]["B"]["billId"]]["claimWindow"], 3600)
        self.assertEqual(rec["bills"]["B"]["claimBy"], ours[rec["bills"]["B"]["billId"]]["claimBy"])
        self.assertIn("KST", text)
        # payWithAuthorization verified against the fake contract's OWN (twisted) authNonce
        self.assertIn("differs from the local formula", text)
        for s in rpc.mined(SEL["pwa"]):
            bid = decode(["uint256"], s["data"][4:36])[0]
            self.assertIn(rpc.auth_nonce(bid), rpc.st["auth"])
        # payer: exactly 1.0 USDC + gas budget was enough; bills A and B paid, C returned
        self.assertTrue(all(st["state"] == "done" for st in rec["steps"].values()))
        self.assertTrue(all(st["tx"]["costWei"] == st["tx"]["gasUsed"] * st["tx"]["effectiveGasPrice"]
                            for st in rec["steps"].values()))
        self.assertIsNone(rec["stoppedAt"])
        self.assertIn("| A.claim | done |", self.record.with_suffix(".md").read_text(encoding="utf-8"))

    def test_payer_peak_is_one_usdc(self):
        rpc = self.funded_chain()
        start = rpc.bal(self.P)
        low = start
        orig = rpc.mine

        def watch(raw):
            nonlocal low
            h = orig(raw)
            low = min(low, rpc.bal(self.P))
            return h

        rpc.mine = watch
        code, text = self.run_main(["bills", "--send"], rpc)
        self.assertEqual(code, 0, text)
        gas_paid = sum(int(rpc.receipts[s["hash"]]["gasUsed"], 16) * int(rpc.receipts[s["hash"]]["effectiveGasPrice"],
                                                                         16)
                       for s in rpc.mined() if s["sender"] == self.P.lower())
        self.assertLessEqual(start - low, 10**18 + gas_paid)            # never more than 1.0 USDC + gas out at once
        self.assertEqual(start - rpc.bal(self.P), 10**18 + gas_paid)     # A and B paid, C came back

    def test_status_mismatch_stops_and_resume_does_not_repay(self):
        rpc = self.funded_chain()
        rpc.stale_after = SEL["pwa"]   # the first read after paying bill A lags (shows Open)
        code, text = self.run_main(["bills", "--send"], rpc)
        self.assertEqual(code, 1, text)
        self.assertIn("STOPPED      at A.pay", text)
        self.assertIn("--resume --send", text)
        rec = json.loads(self.record.read_text(encoding="utf-8"))
        self.assertEqual(rec["steps"]["A.pay"]["state"], "check_failed")
        self.assertEqual(rec["stoppedAt"]["step"], "A.pay")
        self.assertEqual(len(rpc.mined(SEL["pwa"])), 1)
        code, text = self.run_main(["bills", "--send"], rpc)   # a fresh run is refused
        self.assertEqual(code, 2)
        self.assertIn("--resume", text)
        code, text = self.run_main(["bills", "--resume"], rpc)  # dry run shows where it stands
        self.assertEqual(code, 0, text)
        self.assertIn("next         A.pay", text)
        self.assertEqual(len(rpc.mined()), 2)
        code, text = self.run_main(["bills", "--resume", "--send"], rpc)
        self.assertEqual(code, 0, text)
        self.assertEqual(len(rpc.mined(SEL["pwa"])), 2)   # A once, B once
        a_id = json.loads(self.record.read_text(encoding="utf-8"))["bills"]["A"]["billId"]
        pays_a = [s for s in rpc.mined(SEL["pwa"]) if decode(["uint256"], s["data"][4:36])[0] == a_id]
        self.assertEqual(len(pays_a), 1)

    def test_crash_after_broadcast_resume_does_not_double_pay(self):
        rpc = self.funded_chain()
        rpc.hide_after = SEL["pay"]    # C.pay mined but the receipt is not visible to this run
        code, text = self.run_main(["bills", "--send"], rpc)
        self.assertEqual(code, 1, text)
        self.assertIn("no receipt", text)
        rec = json.loads(self.record.read_text(encoding="utf-8"))
        self.assertEqual(rec["steps"]["C.pay"]["state"], "sent")
        self.assertTrue(rec["steps"]["C.pay"]["tx"]["hash"])
        rpc.release()
        code, text = self.run_main(["bills", "--resume", "--send"], rpc)
        self.assertEqual(code, 0, text)
        self.assertEqual(len(rpc.mined(SEL["pay"])), 1)
        self.assertEqual(len(rpc.mined(SEL["issue"])), 3)

    def test_receipt_lost_but_chain_shows_paid_adopts_without_repay(self):
        rpc = self.funded_chain()
        rpc.hide_after = SEL["pwa"]
        code, _ = self.run_main(["bills", "--send"], rpc)
        self.assertEqual(code, 1)
        # the record lost the in-flight hash (e.g. crash before the write finished)
        rec = json.loads(self.record.read_text(encoding="utf-8"))
        rec["steps"]["A.pay"] = {"state": "todo"}
        self.record.write_text(json.dumps(rec), encoding="utf-8")
        rpc.release()
        code, text = self.run_main(["bills", "--resume", "--send"], rpc)
        self.assertEqual(code, 0, text)
        self.assertIn("found      BillPaid", text)
        self.assertEqual(len(rpc.mined(SEL["pwa"])), 2)

    def test_dropped_issue_is_resent_with_same_nonce(self):
        rpc = self.funded_chain()
        rpc.drop_next = SEL["issue"]
        code, text = self.run_main(["bills", "--send"], rpc)
        self.assertEqual(code, 1, text)
        dropped = [s for s in rpc.sent if s.get("dropped")]
        self.assertEqual(len(dropped), 1)
        code, text = self.run_main(["bills", "--resume", "--send"], rpc)
        self.assertEqual(code, 0, text)
        self.assertIn("dropped", text)
        issues = rpc.mined(SEL["issue"])
        self.assertEqual(len(issues), 3)
        self.assertEqual(issues[0]["nonce"], dropped[0]["nonce"])
        self.assertEqual(len([b for b in rpc.st["bills"] if b["payee"] == self.B.lower()]), 3)

    def test_issue_already_on_chain_is_adopted_not_reissued(self):
        rpc = self.funded_chain()
        rpc.hide_after = SEL["issue"]
        code, _ = self.run_main(["bills", "--send"], rpc)
        self.assertEqual(code, 1)
        rec = json.loads(self.record.read_text(encoding="utf-8"))
        rec["steps"]["A.issue"] = {"state": "todo"}
        self.record.write_text(json.dumps(rec), encoding="utf-8")
        rpc.release()
        code, text = self.run_main(["bills", "--resume", "--send"], rpc)
        self.assertEqual(code, 0, text)
        self.assertIn("found      BillIssued", text)
        self.assertEqual(len(rpc.mined(SEL["issue"])), 3)

    def test_reverted_tx_stops(self):
        rpc = self.funded_chain()
        orig = rpc.execute

        def broken(st, sender, to, data, value, ts, logs):
            if data and "0x" + bytes(data[:4]).hex() == SEL["claim"] and logs is not None and st is rpc.st:
                raise Revert("boom")
            return orig(st, sender, to, data, value, ts, logs)

        rpc.execute = broken
        code, text = self.run_main(["bills", "--send"], rpc)
        self.assertEqual(code, 1, text)
        self.assertIn("status 0", text)
        rec = json.loads(self.record.read_text(encoding="utf-8"))
        self.assertEqual(rec["steps"]["A.claim"]["state"], "reverted")

    def test_resume_without_record_refused_and_confirmation_required(self):
        rpc = self.funded_chain()
        code, text = self.run_main(["bills", "--resume", "--send"], rpc)
        self.assertEqual(code, 2)
        self.assertIn("no bills run", text)
        code, text = self.run_main(["bills", "--send"], rpc, confirm=lambda _: "yes")
        self.assertEqual(code, 2)
        self.assertIn("not confirmed", text)
        self.assertEqual(rpc.sent, [])
        self.assertFalse(self.record.exists())

    def test_fee_floor_and_shape(self):
        rpc = self.funded_chain(extra=10**17, base_fee=25 * GWEI, prio=3)
        code, text = self.run_main(["bills", "--send"], rpc)
        self.assertEqual(code, 0, text)
        self.assertTrue(all(s["maxFee"] == 50 * GWEI for s in rpc.mined()))
        self.assertTrue(all(s["value"] == 0 for s in rpc.mined()))

    def test_status_finds_keeper_refund(self):
        rpc = self.funded_chain()
        code, _ = self.run_main(["bills", "--send"], rpc)
        self.assertEqual(code, 0)
        b_id = json.loads(self.record.read_text(encoding="utf-8"))["bills"]["B"]["billId"]
        code, text = self.run_main(["status"], rpc)
        self.assertEqual(code, 0, text)
        self.assertRegex(text, r"bill B\s+id %d\s+Paid" % b_id)
        n = len(rpc.sent)
        h = rpc.keeper_refund(b_id)
        code, text = self.run_main(["status", "--save"], rpc)
        self.assertEqual(code, 0, text)
        self.assertRegex(text, r"bill B\s+id %d\s+Refunded" % b_id)
        self.assertIn(h, text)
        self.assertEqual(len(rpc.sent), n + 1)  # status itself sent nothing
        self.assertEqual(json.loads(self.record.read_text(encoding="utf-8"))["refund"]["hash"], h)


# ---------------------------------------------------------------------------------------------- in-flight txs


class FundInflightTests(Base):
    A_BAL = 2_077_256_479_435_960_704

    def rec(self):
        return json.loads(self.record.read_text(encoding="utf-8"))

    def test_rerun_while_recorded_funding_tx_unmined_sends_nothing_new(self):
        rpc = FakeArc({self.A: self.A_BAL})
        rpc.hold = lambda e: e["to"] == self.B.lower()        # the biller transfer sits in the pool
        code, text = self.run_main(["fund", "--send"], rpc)
        self.assertEqual(code, 1, text)
        self.assertIn("no receipt", text)
        self.assertIn("rerun        python scripts/test_bills.py fund --send", text)
        self.assertNotIn("--resume", text)
        n = len(rpc.sent)
        for _ in range(2):                                    # the owner reruns, as the STOPPED text says
            code, text = self.run_main(["fund", "--send"], rpc)
            self.assertEqual(code, 1, text)
            self.assertIn("can still land", text)
            self.assertEqual(len(rpc.sent), n)                # nothing new signed or broadcast
        code, text = self.run_main(["fund"], rpc)             # the dry run names the unresolved tx
        self.assertEqual(code, 0, text)
        self.assertIn("still unresolved", text)
        rpc.flush()
        self.assertEqual(rpc.bal(self.B), tb.FUND_BILLER)     # exactly once
        code, text = self.run_main(["fund", "--send"], rpc)
        self.assertEqual(code, 0, text)
        self.assertIn("funded by the earlier recorded tx", text)
        self.assertEqual(len(rpc.sent), n)
        self.assertEqual([t["state"] for t in self.rec()["fund"]["txs"]], ["confirmed", "confirmed"])

    def test_nonce_used_by_keeper_marks_recorded_tx_dead_and_sends_one_fresh(self):
        rpc = FakeArc({self.A: self.A_BAL})
        rpc.hold = lambda e: e["to"] == self.B.lower()
        code, text = self.run_main(["fund", "--send"], rpc)
        self.assertEqual(code, 1, text)
        rpc.forget_pool()                                     # the node drops the biller transfer...
        rpc.hold = None
        rpc.outside_tx(self.accts["deployer"])                # ...and the keeper (same key) uses nonce 1
        code, text = self.run_main(["fund", "--send"], rpc, sleep=rpc.tick)
        self.assertEqual(code, 0, text)
        self.assertIn("dead", text)
        to_biller = [s for s in rpc.mined() if s["to"] == self.B.lower()]
        self.assertEqual(len(to_biller), 1)
        self.assertEqual(to_biller[0]["nonce"], 2)
        self.assertEqual(rpc.bal(self.B), tb.FUND_BILLER)
        self.assertEqual([t["state"] for t in self.rec()["fund"]["txs"]], ["confirmed", "dead", "confirmed"])
        self.assertIn("| biller | dead |", self.record.with_suffix(".md").read_text(encoding="utf-8"))

    def test_refuses_while_deployer_has_a_pending_tx(self):
        rpc = FakeArc({self.A: self.A_BAL})
        rpc.hold = lambda e: e["sender"] == self.A.lower()
        rpc.outside_tx(self.accts["deployer"])                # the keeper's tx is in the pool
        code, text = self.run_main(["fund", "--send"], rpc)
        self.assertEqual(code, 1, text)
        self.assertIn("1 pending transaction", text)
        self.assertIn("nothing sent", text)
        self.assertEqual(len(rpc.sent), 1)                    # only the keeper's
        self.assertFalse(self.record.exists())                # nothing recorded, so a rerun needs no cleanup
        code, text = self.run_main(["fund", "--send"], rpc, sleep=lambda s: rpc.flush())  # it mines in seconds
        self.assertEqual(code, 0, text)
        self.assertEqual(rpc.bal(self.P), tb.FUND_PAYER)
        self.assertEqual(rpc.bal(self.B), tb.FUND_BILLER)

    def test_accept_step_belongs_to_bills(self):
        rpc = FakeArc({self.A: self.A_BAL})
        code, text = self.run_main(["fund", "--send", "--accept-step", "A.pay"], rpc)
        self.assertEqual(code, 2, text)
        self.assertEqual(rpc.sent, [])


class BillsInflightTests(Base):
    def rec(self):
        return json.loads(self.record.read_text(encoding="utf-8"))

    def write(self, rec):
        self.record.write_text(json.dumps(rec), encoding="utf-8")

    def pays_for(self, rpc, key):
        bid = self.rec()["bills"][key]["billId"]
        return [s for s in rpc.mined(SEL["pwa"]) + rpc.mined(SEL["pay"])
                if decode(["uint256"], s["data"][4:36])[0] == bid]

    def test_resume_waits_for_a_pending_tx(self):
        rpc = self.funded_chain()
        rpc.hold = lambda e: e["sel"] == SEL["pwa"]
        code, text = self.run_main(["bills", "--send"], rpc)
        self.assertEqual(code, 1, text)
        self.assertEqual(self.rec()["steps"]["A.pay"]["state"], "sent")
        code, text = self.run_main(["bills", "--resume", "--send"], rpc, sleep=lambda s: rpc.flush())
        self.assertEqual(code, 0, text)
        self.assertIn("still pending", text)
        self.assertEqual(len(self.pays_for(rpc, "A")), 1)
        self.assertEqual(len(rpc.mined(SEL["pwa"])), 2)

    def _arm_stale(self, rpc, tags=("pending",), accepted=False):
        """The payer's next nonce read(s) after its first tx answer one too low (a node a block behind), once.
        accepted=True: that node also takes a too-low-nonce tx into its pool and never mines it."""
        orig_call, orig_mine = rpc.call, rpc.mine
        left = set(tags)
        payer = self.P.lower()

        def call(method, params):
            if (method == "eth_getTransactionCount" and params[1] in left and params[0].lower() == payer
                    and rpc.nonces.get(payer, 0) == 1):
                left.discard(params[1])
                rpc.calls.append((method, params))
                return hex(0)
            return orig_call(method, params)

        def mine(raw):
            tx = TypedTransaction.from_bytes(HexBytes(raw)).as_dict()
            sender = Account.recover_transaction(raw).lower()
            if accepted and sender == payer and tx["nonce"] < rpc.nonces.get(sender, 0):
                h = hx(tb.kec(bytes(HexBytes(raw))))
                rpc.sent.append({"hash": h, "sender": sender, "to": "", "sel": "stale", "data": b"", "value": 0,
                                 "nonce": tx["nonce"], "maxFee": 0, "dropped": True})
                return h
            return orig_mine(raw)

        rpc.call, rpc.mine = call, mine

    def test_stale_pending_nonce_is_never_signed(self):
        for tags in (("pending",), ("latest", "pending")):
            for accepted in (False, True):
                with self.subTest(tags=tags, accepted=accepted):
                    if self.record.exists():
                        self.record.unlink()
                    rpc = self.funded_chain()
                    self._arm_stale(rpc, tags, accepted)
                    code, text = self.run_main(["bills", "--send"], rpc)
                    self.assertEqual(code, 0, text)
                    payer_nonces = [s["nonce"] for s in rpc.sent if s["sender"] == self.P.lower()]
                    self.assertEqual(payer_nonces, [0, 1, 2, 3])   # no tx signed with a used nonce
                    self.assertEqual(self.rec()["steps"]["C.approve"]["tx"]["nonce"], 1)

    def _stuck_record(self, accepted):
        """The dead end the old code reached: C.approve recorded as 'sent' with a nonce A.pay already used."""
        rpc = self.funded_chain()
        self._arm_stale(rpc, ("latest", "pending"), accepted)
        with mock.patch.object(tb, "nonce_floor", return_value=0):
            code, text = self.run_main(["bills", "--send"], rpc)
        self.assertEqual(code, 1, text)
        st = self.rec()["steps"]["C.approve"]
        self.assertEqual((st["state"], st["tx"]["nonce"]), ("sent", 0))
        return rpc

    def test_stuck_record_with_used_nonce_recovers(self):
        for accepted in (False, True):
            with self.subTest(accepted=accepted):
                if self.record.exists():
                    self.record.unlink()
                rpc = self._stuck_record(accepted)
                code, text = self.run_main(["bills", "--resume", "--send"], rpc, sleep=rpc.tick)
                self.assertEqual(code, 0, text)
                self.assertIn("dead", text)
                st = self.rec()["steps"]["C.approve"]
                self.assertEqual(st["state"], "done")
                self.assertTrue(st["dropped"][0]["consumed"])
                self.assertEqual(len(rpc.mined(SEL["approve"])), 1)
                self.assertEqual(len(self.pays_for(rpc, "C")), 1)

    def test_used_nonce_without_blocks_advancing_stops_unknown(self):
        rpc = self._stuck_record(False)
        n = len(rpc.sent)
        code, text = self.run_main(["bills", "--resume", "--send"], rpc)   # the chain never advances
        self.assertEqual(code, 1, text)
        self.assertIn("did not advance", text)
        self.assertEqual(len(rpc.sent), n)

    def test_dropped_tx_above_a_free_nonce_is_not_resent(self):
        rpc = self.funded_chain()
        rpc.drop_next = SEL["issue"]
        code, text = self.run_main(["bills", "--send"], rpc)
        self.assertEqual(code, 1, text)
        rec = self.rec()
        rec["steps"]["A.issue"]["tx"]["nonce"] = 2      # a tx at nonce 2 while 0 and 1 are free: it could land later
        self.write(rec)
        n = len(rpc.sent)
        code, text = self.run_main(["bills", "--resume", "--send"], rpc)
        self.assertEqual(code, 1, text)
        self.assertIn("could land later", text)
        self.assertEqual(len(rpc.sent), n)

    def test_resend_nonce_used_since_the_drop(self):
        rpc = self.funded_chain()
        rpc.drop_next = SEL["issue"]
        code, text = self.run_main(["bills", "--send"], rpc)
        self.assertEqual(code, 1, text)
        rec = self.rec()
        st = rec["steps"]["A.issue"]
        rec["steps"]["A.issue"] = {"state": "todo", "tx": None, "dropped": [st["tx"]], "resendNonce": 0}
        self.write(rec)
        rpc.outside_tx(self.accts["biller"])            # nonce 0 is used by something else meanwhile
        code, text = self.run_main(["bills", "--resume", "--send"], rpc, sleep=rpc.tick)
        self.assertEqual(code, 0, text)
        self.assertIn("dead", text)
        self.assertEqual(len([b for b in rpc.st["bills"] if b["payee"] == self.B.lower()]), 3)
        self.assertEqual(rpc.mined(SEL["issue"])[0]["nonce"], 1)

    def test_resend_after_drop_must_reuse_the_nonce(self):
        rpc = self.funded_chain()
        sender = tb.Sender(tb.Chain(rpc, PT), lambda *a: None, lambda s: None, 3)
        with self.assertRaises(tb.Stop) as cm:
            sender.next_nonce(self.B, "A.issue", want=1)
        self.assertIn("only be superseded at that nonce", str(cm.exception))
        self.assertEqual(sender.next_nonce(self.B, "A.issue", want=0), 0)

    def test_outside_dust_in_a_step_block_needs_owner_accept(self):
        rpc = self.funded_chain()
        rpc.dust_after = SEL["pwa"]                     # someone sends the payer 1 wei in the A.pay block
        for args in (["bills", "--send"], ["bills", "--resume", "--send"]):
            code, text = self.run_main(args, rpc)
            self.assertEqual(code, 1, text)
            self.assertIn("native balance moved", text)
            self.assertIn("--accept-step A.pay", text)
        n = len(rpc.sent)
        code, text = self.run_main(["bills", "--resume", "--send", "--accept-step", "A.claim"], rpc)
        self.assertEqual(code, 2, text)
        self.assertIn("not check_failed", text)
        code, text = self.run_main(["bills", "--send", "--accept-step", "A.pay"], rpc)
        self.assertEqual(code, 2, text)
        code, text = self.run_main(["bills", "--resume", "--accept-step", "A.pay"], rpc)   # dry run shows it
        self.assertEqual(code, 0, text)
        self.assertIn("stopped because", text)
        self.assertIn("expected exactly", text)
        code, text = self.run_main(["bills", "--resume", "--send", "--accept-step", "A.pay"], rpc)  # types SEND only
        self.assertEqual(code, 2, text)
        self.assertIn("override not confirmed", text)
        self.assertEqual(len(rpc.sent), n)
        accept = lambda q: "ACCEPT A.pay" if "ACCEPT" in q else "SEND"   # noqa: E731
        rec = self.rec()
        rec["bills"]["A"]["claimWindow"] = 3600          # bill-field checks stay strict under --accept-step
        self.write(rec)
        code, text = self.run_main(["bills", "--resume", "--send", "--accept-step", "A.pay"], rpc, confirm=accept)
        self.assertEqual(code, 1, text)
        self.assertIn("fields differ", text)
        self.assertEqual(len(rpc.sent), n)
        rec = self.rec()
        rec["bills"]["A"]["claimWindow"] = tb.BILLS["A"]["claimWindow"]
        self.write(rec)
        code, text = self.run_main(["bills", "--resume", "--send", "--accept-step", "A.pay"], rpc, confirm=accept)
        self.assertEqual(code, 0, text)
        self.assertIn("ALL STEPS DONE", text)
        self.assertIn("ACCEPTED", text)
        st = self.rec()["steps"]["A.pay"]
        self.assertEqual(st["state"], "done")
        self.assertIn("moved by", st["accepted"])
        self.assertIn("owner accepted", self.record.with_suffix(".md").read_text(encoding="utf-8"))
        self.assertEqual(len(self.pays_for(rpc, "A")), 1)
        self.assertTrue(all(self.rec()["steps"][s]["state"] == "done" for s in self.rec()["steps"]))


if __name__ == "__main__":
    unittest.main()
