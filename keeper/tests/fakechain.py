"""A fake Arc node for keeper tests.

FakeChain keeps PaidThrough bills and emits logs exactly as SPEC.md's events would appear on chain
(topics + ABI-encoded data via eth_abi). FakeRpc subclasses the real Rpc and only replaces the transport
(`call`), so chunked getLogs, retries and error handling under test are the production code.
Arc-specific error shapes are copied from read-only probes of the real RPCs on 2026-10-01.
"""
from __future__ import annotations

import kpath  # noqa: F401

from eth_abi import encode
from eth_account import Account
from eth_account.typed_transactions import TypedTransaction
from hexbytes import HexBytes

import abi
from rpc import Rpc, RpcError

CONTRACT = "0x00000000000000000000000000000000000Bea11"
ZERO = "0x0000000000000000000000000000000000000000"
GWEI = 10 ** 9


def addr(n: int) -> str:
    return abi.checksum("0x" + ("%040x" % n))


def _t_uint(v: int) -> str:
    return "0x" + encode(["uint256"], [v]).hex()


def _t_addr(a: str) -> str:
    return "0x" + encode(["address"], [a]).hex()


def revert_custom(name: str) -> dict:
    return {"code": 3, "message": "execution reverted", "data": "0x" + abi.selector(name + "()").hex()}


def revert_string(msg: str) -> dict:
    data = "0x08c379a0" + encode(["string"], [msg]).hex()
    return {"code": 3, "message": "execution reverted: " + msg, "data": data}


class FakeChain:
    def __init__(self, chain_id=5042002, head=1_000_000, head_ts=1_800_000_000, base_fee=20 * GWEI,
                 priority=5 * GWEI, contract=CONTRACT, max_range=10_000):
        self.chain_id = chain_id
        self.head = head
        self.head_ts = head_ts
        self.base_fee = base_fee
        self.priority = priority
        self.contract = contract
        self.max_range = max_range           # Arc: 10,000 blocks inclusive
        self.bills = {}                      # id -> dict of Bill fields
        self.logs = []
        self._log_index = {}
        self.code = "0x6080"
        self.refund_revert = {}              # id -> JSON-RPC error dict for eth_call/estimateGas of refund(id)
        self.gas_estimate = 61_234
        self.balance = 10 ** 18              # keeper native balance, wei (1 USDC)
        self.nonce = 7
        self.sent = []                       # raw txs
        self.receipts = {}                   # hash -> receipt
        self.mine_sent = True                # produce a receipt for sent txs
        self.receipt_status = 1
        self.effective_gas_price = 25 * GWEI
        self.calls = []                      # (method, params)
        self.get_logs_calls = []
        self.fail_methods = {}               # method -> error dict (raised as JSON-RPC error)
        self.bill_count_override = None
        self.max_results = None              # Arc-style result cap (-32602 "exceeds max results"), any range size
        self.cap_counts_all_topics = False   # pessimistic node: counts every log of the address, not just matches
        self.topic_filters = []              # topic0 sets the keeper asked for

    # --- building history ---
    def _emit(self, block, topic0, topics, data_types, data_values):
        idx = self._log_index.get(block, 0)
        self._log_index[block] = idx + 1
        self.logs.append({
            "address": self.contract.lower(),
            "topics": [topic0] + topics,
            "data": "0x" + (encode(data_types, data_values).hex() if data_types else ""),
            "blockNumber": hex(block),
            "logIndex": hex(idx),
            "transactionHash": "0x" + ("%064x" % (block * 1000 + idx)),
            "removed": False,
        })

    def issue(self, bid, block, payee=None, amount=150_000_000, pay_by=None, claim_window=3600,
              allowed=ZERO, ref=b"\x11" * 32):
        payee = payee or addr(0xA11CE)
        pay_by = pay_by or self.head_ts + 86_400
        self.bills[bid] = dict(payee=payee, amount=amount, payer=ZERO, payBy=pay_by, claimWindow=claim_window,
                               allowedPayer=allowed, claimBy=0, status=abi.OPEN, ref=ref)
        self._emit(block, abi.EVENTS["Issued"][0], [_t_uint(bid), _t_addr(payee), _t_addr(allowed)],
                   ["uint96", "uint64", "uint32", "bytes32"], [amount, pay_by, claim_window, ref])

    def pay(self, bid, block, payer=None, claim_by=None):
        payer = payer or addr(0xFA4117)
        b = self.bills[bid]
        claim_by = claim_by if claim_by is not None else self.head_ts - 10
        b.update(payer=payer, claimBy=claim_by, status=abi.PAID)
        self._emit(block, abi.EVENTS["Paid"][0], [_t_uint(bid), _t_addr(payer)], ["uint64"], [claim_by])

    def spam_cancel_block(self, first_id, n, issue_from_block, block, per_block=100):
        """n bills issued `per_block` per block from issue_from_block, then all cancelled in one block."""
        for k in range(n):
            self.issue(first_id + k, issue_from_block + k // per_block, amount=1)
        for k in range(n):
            self.cancel(first_id + k, block)

    def cancel(self, bid, block):
        self.bills[bid]["status"] = abi.CANCELLED
        self._emit(block, abi.EVENTS["Cancelled"][0], [_t_uint(bid)], [], [])

    def claim(self, bid, block):
        b = self.bills[bid]
        b["status"] = abi.CLAIMED
        self._emit(block, abi.EVENTS["Claimed"][0], [_t_uint(bid), _t_addr(b["payee"])], ["uint96"], [b["amount"]])

    def decline(self, bid, block):
        b = self.bills[bid]
        b["status"] = abi.DECLINED
        self._emit(block, abi.EVENTS["Declined"][0], [_t_uint(bid), _t_addr(b["payer"])], ["uint96"], [b["amount"]])

    def refund(self, bid, block, caller=None):
        b = self.bills[bid]
        b["status"] = abi.REFUNDED
        self._emit(block, abi.EVENTS["Refunded"][0], [_t_uint(bid), _t_addr(b["payer"])], ["uint96", "address"],
                   [b["amount"], caller or addr(0xCA11E5)])

    # --- node behaviour ---
    def bill_word(self, bid) -> str:
        b = self.bills.get(bid)
        if b is None:
            vals = [ZERO, 0, ZERO, 0, 0, ZERO, 0, 0, b"\x00" * 32]
        else:
            vals = [b["payee"], b["amount"], b["payer"], b["payBy"], b["claimWindow"], b["allowedPayer"],
                    b["claimBy"], b["status"], b["ref"]]
        return "0x" + encode([abi.BILL_TUPLE], [tuple(vals)]).hex()

    def _refund_check(self, data):
        bid = int(data[10:], 16)
        if bid in self.refund_revert:
            raise RpcError("eth_call", self.refund_revert[bid])
        b = self.bills.get(bid)
        if not b or b["status"] != abi.PAID:
            raise RpcError("eth_call", revert_custom("WrongStatus"))
        if self.head_ts < b["claimBy"]:
            raise RpcError("eth_call", revert_custom("ClaimWindowOpen"))
        return bid

    def handle(self, method, params):
        self.calls.append((method, params))
        if method in self.fail_methods:
            raise RpcError(method, self.fail_methods[method])
        if method == "eth_chainId":
            return hex(self.chain_id)
        if method == "eth_getCode":
            return self.code if params[0].lower() == self.contract.lower() else "0x"
        if method == "eth_getBlockByNumber":
            return {"number": hex(self.head), "timestamp": hex(self.head_ts), "baseFeePerGas": hex(self.base_fee)}
        if method == "eth_getLogs":
            q = params[0]
            frm, to = int(q["fromBlock"], 16), int(q["toBlock"], 16)
            self.get_logs_calls.append((frm, to))
            if to - frm + 1 > self.max_range:
                raise RpcError(method, {"code": -32012, "message": "requested range too large"})
            t0 = q["topics"][0]
            want = set(t.lower() for t in ([t0] if isinstance(t0, str) else t0))
            self.topic_filters.append(frozenset(want))
            in_range = [lg for lg in self.logs if frm <= int(lg["blockNumber"], 16) <= to
                        and lg["address"] == q["address"].lower()]
            out = [dict(lg) for lg in in_range if lg["topics"][0] in want]
            if self.max_results is not None:
                counted = in_range if self.cap_counts_all_topics else out
                if len(counted) > self.max_results:
                    raise RpcError(method, {"code": -32602, "message": (
                        "request exceeded max allowed range: query exceeds max results %d" % self.max_results)})
            return out
        if method == "eth_call":
            tx = params[0]
            if tx["to"].lower() != self.contract.lower():
                return "0x"
            sel = tx["data"][2:10]
            if sel == abi.SEL_BILL_COUNT.hex():
                n = self.bill_count_override if self.bill_count_override is not None else max(self.bills or [0])
                return "0x" + encode(["uint256"], [n]).hex()
            if sel == abi.SEL_GET_BILL.hex():
                return self.bill_word(int(tx["data"][10:], 16))
            if sel == abi.SEL_REFUND.hex():
                self._refund_check(tx["data"])
                return "0x"
            raise RpcError(method, {"code": 3, "message": "execution reverted"})
        if method == "eth_estimateGas":
            if params[0]["data"][2:10] == abi.SEL_REFUND.hex():
                self._refund_check(params[0]["data"])
            return hex(self.gas_estimate)
        if method == "eth_maxPriorityFeePerGas":
            return hex(self.priority)
        if method == "eth_getBalance":
            return hex(self.balance)
        if method == "eth_getTransactionCount":
            return hex(self.nonce)
        if method == "eth_sendRawTransaction":
            raw = params[0]
            self.sent.append(raw)
            h = "0x" + abi.keccak(bytes.fromhex(raw[2:])).hex()
            self.nonce += 1
            if self.mine_sent:
                self.head += 1                   # next block; same timestamp (Arc blocks are ~0.5 s apart)
                tx = self.decode_tx(raw)
                used = 50_000
                self.receipts[h] = {"transactionHash": h, "status": hex(self.receipt_status),
                                    "blockNumber": hex(self.head), "gasUsed": hex(used),
                                    "effectiveGasPrice": hex(self.effective_gas_price)}
                if self.receipt_status == 1:
                    bid = int(tx["data"].hex()[-64:], 16)
                    self.refund(bid, self.head)
            return h
        if method == "eth_getTransactionReceipt":
            return self.receipts.get(params[0])
        if method == "eth_getTransactionByHash":
            return None
        raise AssertionError("unexpected RPC method " + method)

    @staticmethod
    def decode_tx(raw: str) -> dict:
        return TypedTransaction.from_bytes(HexBytes(raw)).as_dict()

    @staticmethod
    def sender(raw: str) -> str:
        return Account.recover_transaction(raw)


class FakeRpc(Rpc):
    def __init__(self, chain: FakeChain):
        super().__init__("http://fake", retries=0, sleep=lambda s: None)
        self.chain = chain

    def call(self, method, params):
        return self.chain.handle(method, params)
