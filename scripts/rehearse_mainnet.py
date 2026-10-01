"""Real-node rehearsal of PaidThrough on Arc mainnet. Read-only: nothing is broadcast, nothing is spent.

Every PaidThrough path runs against Arc mainnet's real USDC (0x3600...0000, a FiatToken proxy) and Arc's real
precompiles (native transfer 0x1800...0000, blocklist 0x1800...0001) through `eth_call` with state overrides, and
`eth_estimateGas` measures real gas. PaidThrough's runtime code is placed at a throwaway address by override; bill
storage, native balances (which Arc mirrors into the 6-decimal USDC view), the FiatToken allowance slot and, for the
blocklist cases, the blocklist precompile's storage are set the same way. Each step is sent from a fresh in-memory EOA
(`Account.create()`, checked to have no code on chain). Keys are never written or printed and hold nothing.

RPC methods used: eth_getBlockByNumber, eth_getCode, eth_call, eth_estimateGas. No eth_sendRawTransaction.

Inputs: Foundry artifacts with storage layout (contracts/foundry.toml sets extra_output = ["storageLayout"]):
    <out>/PaidThrough.sol/PaidThrough.json and <out>/RehearsalProbe.sol/RehearsalProbe.json

Run in CI: job `arc-rehearsal` in .github/workflows/ci.yml (builds, runs this, writes the table to the job summary).
Run locally (Python 3.11+ with eth-account, eth-abi, pycryptodome), using the `contracts-out` artifact of a CI run:
    gh run download <run-id> -n contracts-out -D contracts/out
    python scripts/rehearse_mainnet.py --out contracts/out --markdown rehearsal.md --json rehearsal.json

Exit code 0 when every step matched its expectation, 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

from Crypto.Hash import keccak
from eth_abi import decode, encode
from eth_account import Account

DEFAULT_RPC = "https://rpc.mainnet.arc.io"
CHAIN_ID = 5042
USDC = "0x3600000000000000000000000000000000000000"
BLOCKLIST_PRECOMPILE = "0x1800000000000000000000000000000000000001"
USDC_DOMAIN_SEPARATOR = "0x940506929bba468048a19b567f4f0d534714bc06604b5c3017e5d16785ccdf84"
RECEIVE_TYPEHASH = "0xd099cc98ef71107a616c4f0f941f04c322d8e254fe26b3c6668db87aae413de8"

MAX_AMOUNT = 10_000 * 10**6  # USDC base units (6 decimals)
AMOUNT = 25_500_000  # 25.50 USDC
BASE_FEE_WEI = 20 * 10**9  # Arc base-fee floor, 20 gwei; gas is paid in native USDC (18 decimals)
NATIVE_PER_UNIT = 10**12  # 1 USDC base unit (6 dec) = 1e12 native wei (18 dec)
PAYER_START = 30 * 10**18  # 30 USDC in native units
GAS_MONEY = 1 * 10**18  # 1 USDC in native units for callers

# FiatToken v1 storage layout: mapping(address => mapping(address => uint256)) allowed at slot 10.
ALLOWANCE_SLOT = 10
# Arc blocklist precompile: mapping(address => uint256) at slot 2 (found by probing; verified again at runtime).
BLOCKLIST_SLOT = 2

STATUS = ["None", "Open", "Paid", "Claimed", "Refunded", "Declined", "Cancelled"]
BILL_ABI = "(address,uint96,address,uint64,uint32,address,uint64,uint8,bytes32)"
BILL_FIELDS = ["payee", "amount", "payer", "payBy", "claimWindow", "allowedPayer", "claimBy", "status", "ref"]

# ---------------------------------------------------------------------------------------------- helpers


def kec(data: bytes) -> bytes:
    h = keccak.new(digest_bits=256)
    h.update(data)
    return h.digest()


def sel(sig: str) -> bytes:
    return kec(sig.encode())[:4]


def calldata(sig: str, types: list[str] | None = None, args: list | None = None) -> str:
    return "0x" + (sel(sig) + (encode(types, args) if types else b"")).hex()


def h32(v: int) -> str:
    return "0x" + v.to_bytes(32, "big").hex()


def addr_int(a: str) -> int:
    return int(a, 16)


def usdc_str(base_units: int) -> str:
    sign = "-" if base_units < 0 else ""
    v = abs(base_units)
    return f"{sign}{v // 10**6}.{v % 10**6:06d}"


def cost_str(gas: int | None) -> str:
    # gas x 20 gwei = wei of native USDC (18 decimals); exact, 8 decimals since 20e9 / 1e18 = 2e-8.
    if gas is None:
        return "-"
    wei = gas * BASE_FEE_WEI
    return f"{wei // 10**18}.{(wei % 10**18) // 10**10:08d}"


class Rpc:
    def __init__(self, url: str):
        self.url = url
        self.calls = 0

    def raw(self, method: str, params: list) -> dict:
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
        for attempt in range(8):
            time.sleep(0.3)
            req = urllib.request.Request(
                self.url, body, {"content-type": "application/json", "user-agent": "paidthrough-rehearsal/1.0"}
            )
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    self.calls += 1
                    return json.loads(r.read())
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    time.sleep(10 + 5 * attempt)
                    continue
                raise
            except (urllib.error.URLError, TimeoutError):
                time.sleep(3 + 2 * attempt)
        raise RuntimeError(f"RPC {method} failed after retries")

    def result(self, method: str, params: list):
        out = self.raw(method, params)
        if "error" in out:
            raise RuntimeError(f"{method}: {out['error']}")
        return out["result"]


def merge(*parts: dict) -> dict:
    out: dict = {}
    for p in parts:
        for a, o in p.items():
            a = a.lower()
            cur = out.setdefault(a, {})
            for key, val in o.items():
                if key == "stateDiff":
                    cur.setdefault("stateDiff", {}).update(val)
                else:
                    cur[key] = val
    return out


def balance(addr: str, wei: int) -> dict:
    return {addr: {"balance": hex(wei)}}


def code(addr: str, runtime: str) -> dict:
    return {addr: {"code": runtime}}


# ---------------------------------------------------------------------------------------------- artifacts


def load_artifact(out_dir: str, name: str) -> dict:
    path = os.path.join(out_dir, f"{name}.sol", f"{name}.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


class Layout:
    """Bill storage slots, read from the compiler's storage layout (never hard-coded)."""

    def __init__(self, sl: dict):
        storage = {e["label"]: e for e in sl["storage"]}
        self.count_slot = int(storage["_billCount"]["slot"])
        bills = storage["_bills"]
        self.bills_slot = int(bills["slot"])
        types = sl["types"]
        struct = types[types[bills["type"]]["value"]]
        if int(struct["numberOfBytes"]) != 128:
            raise SystemExit("Bill struct is not 4 slots; layout changed")
        self.members = {
            m["label"]: (int(m["slot"]), int(m["offset"]), int(types[m["type"]]["numberOfBytes"]))
            for m in struct["members"]
        }
        if list(self.members) != BILL_FIELDS:
            raise SystemExit(f"unexpected Bill members {list(self.members)}")

    def bill(self, bill_id: int, **fields) -> dict:
        base = int.from_bytes(kec(encode(["uint256", "uint256"], [bill_id, self.bills_slot])), "big")
        words = {0: 0, 1: 0, 2: 0, 3: 0}
        for label, value in fields.items():
            slot, offset, size = self.members[label]
            if isinstance(value, str):
                value = int(value, 16)
            if value >= 1 << (8 * size):
                raise ValueError(f"{label} too large")
            words[slot] |= value << (8 * offset)
        return {h32(base + s): h32(v) for s, v in words.items()}

    def count(self, n: int) -> dict:
        return {h32(self.count_slot): h32(n)}


# ---------------------------------------------------------------------------------------------- rehearsal


class Rehearsal:
    def __init__(self, rpc: Rpc, out_dir: str):
        self.rpc = rpc
        pt = load_artifact(out_dir, "PaidThrough")
        probe = load_artifact(out_dir, "RehearsalProbe")
        if "storageLayout" not in pt:
            raise SystemExit("artifact has no storageLayout; build with extra_output = [\"storageLayout\"]")
        self.layout = Layout(pt["storageLayout"])
        self.initcode = pt["bytecode"]["object"]
        self.probe_code = probe["deployedBytecode"]["object"]
        self.errors = {}
        for item in pt["abi"]:
            if item["type"] == "error":
                sig = item["name"] + "(" + ",".join(i["type"] for i in item["inputs"]) + ")"
                self.errors[sel(sig)] = item["name"]
        self.steps: list[dict] = []
        self.checks: list[dict] = []

        blk = rpc.result("eth_getBlockByNumber", ["latest", False])
        self.block_tag = blk["number"]
        self.block_number = int(blk["number"], 16)
        self.now = int(blk["timestamp"], 16)
        self.base_fee = int(blk.get("baseFeePerGas", "0x0"), 16)

        self.deployer = self.fresh_eoa()
        self.payee = self.fresh_eoa()
        self.payee2 = self.fresh_eoa()
        self.payer = self.fresh_eoa()
        self.relayer = self.fresh_eoa()
        self.keeper = self.fresh_eoa()
        self.pt = self.fresh_eoa().address  # throwaway address that will carry PaidThrough's code
        self.probe = self.fresh_eoa().address
        self.runtime = None

    # ---- primitives

    def fresh_eoa(self):
        for _ in range(10):
            acct = Account.create()
            if self.rpc.result("eth_getCode", [acct.address, "latest"]) in ("0x", "0x0", ""):
                return acct
        raise SystemExit("could not find a code-free address")

    def decode_revert(self, data: str | None, message: str) -> str:
        if not data or data == "0x":
            return message or "revert (no data)"
        b = bytes.fromhex(data[2:])
        if b[:4] == bytes.fromhex("08c379a0"):
            return decode(["string"], b[4:])[0]
        if b[:4] in self.errors:
            return self.errors[b[:4]]
        return "0x" + b.hex()

    def eth_call(self, tx: dict, overrides: dict, block_ov: dict | None = None):
        params = [tx, self.block_tag, overrides]
        if block_ov:
            params.append(block_ov)
        out = self.rpc.raw("eth_call", params)
        if "error" in out:
            err = out["error"]
            return False, err.get("data"), self.decode_revert(err.get("data"), err.get("message", ""))
        return True, out["result"], ""

    def estimate(self, tx: dict, overrides: dict):
        out = self.rpc.raw("eth_estimateGas", [tx, self.block_tag, overrides])
        if "error" in out:
            err = out["error"]
            return None, self.decode_revert(err.get("data"), err.get("message", ""))
        return int(out["result"], 16), ""

    def base(self, *extra: dict) -> dict:
        actors = [self.payee, self.payee2, self.payer, self.relayer, self.keeper, self.deployer]
        parts = [code(self.pt, self.runtime)] + [balance(a.address, GAS_MONEY) for a in actors]
        return merge(*parts, *extra)

    def pt_state(self, *slot_dicts: dict, native: int = 0) -> dict:
        diff: dict = {}
        for d in slot_dicts:
            diff.update(d)
        o = {self.pt: {"stateDiff": diff}} if diff else {}
        if native:
            o = merge(o, balance(self.pt, native))
        return o

    def bill(self, bill_id: int, status: str, payee=None, payer=None, allowed=None, claim_by=0, pay_by=None):
        return self.layout.bill(
            bill_id,
            payee=(payee or self.payee.address),
            amount=AMOUNT,
            payer=(payer or "0x0"),
            payBy=pay_by or self.now + 7 * 86400,
            claimWindow=3 * 86400,
            allowedPayer=(allowed or "0x0"),
            claimBy=claim_by,
            status=STATUS.index(status),
            ref=int.from_bytes(kec(b"paidthrough:v1:rehearsal"), "big"),
        )

    def auth_nonce(self, bill_id: int) -> bytes:
        return kec(encode(["uint256", "address", "uint256"], [CHAIN_ID, self.pt, bill_id]))

    def sign_auth(self, acct, bill_id: int, valid_before: int, value: int = AMOUNT):
        struct_hash = kec(
            encode(
                ["bytes32", "address", "address", "uint256", "uint256", "uint256", "bytes32"],
                [bytes.fromhex(RECEIVE_TYPEHASH[2:]), acct.address, self.pt, value, 0, valid_before,
                 self.auth_nonce(bill_id)],
            )
        )
        digest = kec(b"\x19\x01" + bytes.fromhex(USDC_DOMAIN_SEPARATOR[2:]) + struct_hash)
        sig = acct.unsafe_sign_hash(digest)
        return sig.v, sig.r.to_bytes(32, "big"), sig.s.to_bytes(32, "big")

    def pwa_data(self, bill_id: int, payer, v, r, s, valid_before: int) -> str:
        return calldata(
            "payWithAuthorization(uint256,address,uint256,uint256,uint8,bytes32,bytes32)",
            ["uint256", "address", "uint256", "uint256", "uint8", "bytes32", "bytes32"],
            [bill_id, payer.address, 0, valid_before, v, r, s],
        )

    # ---- step runner

    def step(self, name: str, caller, data: str, overrides: dict, expect: str | None, *, to: str | None = None,
             block_ov: dict | None = None, est_overrides: dict | None = None, probe: dict | None = None,
             note: str = ""):
        tx = {"from": caller.address, "to": to or self.pt, "data": data}
        ok, ret, reason = self.eth_call(tx, overrides, block_ov)
        result = "ok" if ok else f"revert: {reason}"
        gas = None
        gas_note = ""
        if ok:
            gas, gas_err = self.estimate(tx, est_overrides if est_overrides is not None else overrides)
            if gas is None:
                gas_note = f"estimateGas failed: {gas_err}"
        if expect is None:
            match = "info"
        elif expect == "ok":
            match = "yes" if ok else "NO"
        elif expect == "revert":
            match = "yes" if not ok else "NO"
        else:
            match = "yes" if (not ok and expect.lower() in reason.lower()) else "NO"
        post = self.run_probe(**probe) if probe else ""
        row = {
            "step": name, "caller": caller.address, "expected": expect or "(observe)", "result": result,
            "match": match, "gas": gas, "cost_usdc": cost_str(gas), "post_state": post,
            "note": "; ".join(x for x in (note, gas_note) if x), "return": ret if ok else None,
        }
        self.steps.append(row)
        print(f"[{match:>4}] {name}: {result} gas={gas} {post}", flush=True)
        return ok, ret

    def run_probe(self, calls: list[tuple[str, str]], watch: list[tuple[str, str]], overrides: dict,
                  block_ov: dict | None = None) -> str:
        """Runs `calls` from the probe address (which carries RehearsalProbe code) and reads balances after."""
        data = calldata(
            "run((address,bytes)[],address,address[])",
            ["(address,bytes)[]", "address", "address[]"],
            [[(t, bytes.fromhex(d[2:])) for t, d in calls], USDC, [a for _, a in watch]],
        )
        ov = merge(overrides, code(self.probe, self.probe_code), balance(self.probe, GAS_MONEY))
        tx = {"from": self.keeper.address, "to": self.probe, "data": data}
        ok, ret, reason = self.eth_call(tx, ov, block_ov)
        if not ok:
            return f"probe failed: {reason}"
        results, token_bals, _native = decode(
            ["(bool,bytes)[]", "uint256[]", "uint256[]"], bytes.fromhex(ret[2:])
        )
        parts = []
        for i, (cok, cret) in enumerate(results):
            if not cok:
                parts.append(f"call{i + 1} reverted ({self.decode_revert('0x' + cret.hex(), '')})")
        last_ok, last_ret = results[-1]
        if last_ok and len(last_ret) == 9 * 32:
            b = decode([BILL_ABI], last_ret)[0]
            parts.append(f"bill status {STATUS[b[7]]}")
        parts += [f"{label} {usdc_str(v)} USDC" for (label, _), v in zip(watch, token_bals)]
        return ", ".join(parts)

    # ---- the rehearsal

    def run(self):
        rpc = self.rpc
        chain = int(rpc.result("eth_chainId", []), 16)
        self.check("chain id", chain == CHAIN_ID, str(chain))
        ds = rpc.result("eth_call", [{"to": USDC, "data": calldata("DOMAIN_SEPARATOR()")}, self.block_tag])
        self.check("USDC DOMAIN_SEPARATOR matches SPEC", ds.lower() == USDC_DOMAIN_SEPARATOR, ds)

        # Deployment: creation eth_call returns the runtime with immutables filled; estimateGas gives real gas.
        args = encode(["address", "uint96"], [USDC, MAX_AMOUNT]).hex()
        create = {"from": self.deployer.address, "data": self.initcode + args}
        ok, runtime, reason = self.eth_call(create, balance(self.deployer.address, GAS_MONEY))
        if not ok:
            raise SystemExit(f"creation eth_call failed: {reason}")
        self.runtime = runtime
        rt = bytes.fromhex(runtime[2:])
        self.check("runtime carries usdc immutable", bytes.fromhex(USDC[2:]) in rt, f"{len(rt)} bytes")
        deploy_gas, err = self.estimate(create, balance(self.deployer.address, GAS_MONEY))
        self.steps.append({
            "step": "deploy PaidThrough(usdc, 10_000e6)", "caller": self.deployer.address, "expected": "ok",
            "result": "ok" if deploy_gas else f"estimate failed: {err}", "match": "yes" if deploy_gas else "NO",
            "gas": deploy_gas, "cost_usdc": cost_str(deploy_gas), "post_state": f"runtime {len(rt)} bytes",
            "note": "creation eth_estimateGas", "return": None,
        })
        print(f"deploy gas {deploy_gas}", flush=True)

        nonce_ret = rpc.result("eth_call", [{"to": self.pt, "data": calldata("authNonce(uint256)", ["uint256"], [1])},
                                            self.block_tag, self.base()])
        self.check("authNonce(1) == keccak(chainid, this, 1)", nonce_ret[2:] == self.auth_nonce(1).hex(), nonce_ret)

        allowance_ok = self.verify_allowance_slot()
        blocklist_ok = self.verify_blocklist_slot()

        L = self.layout
        payee, payer, relayer, keeper = self.payee, self.payer, self.relayer, self.keeper
        pay_by = self.now + 7 * 86400
        issue_data = calldata(
            "issue(uint96,uint64,uint32,address,bytes32)",
            ["uint96", "uint64", "uint32", "address", "bytes32"],
            [AMOUNT, pay_by, 3 * 86400, payer.address, kec(b"paidthrough:v1:rehearsal")],
        )
        getbill = calldata("getBill(uint256)", ["uint256"], [1])
        bill_data = lambda fn, i=1: calldata(f"{fn}(uint256)", ["uint256"], [i])  # noqa: E731
        payer_funds = balance(payer.address, PAYER_START)

        # issue
        self.step("issue (first bill, id 1)", payee, issue_data, self.base(), "ok",
                  probe={"calls": [(self.pt, issue_data), (self.pt, getbill)], "watch": [("contract", self.pt)],
                         "overrides": self.base()})
        self.step("issue (later bill, id 42)", payee, issue_data, self.base(self.pt_state(L.count(41))), "ok")

        open1 = self.pt_state(L.count(1), self.bill(1, "Open", allowed=payer.address))
        open1_any = self.pt_state(L.count(1), self.bill(1, "Open"))

        # pay (approve state set through the real FiatToken allowance slot)
        if allowance_ok:
            allow = self.allowance_override(payer.address, self.pt, AMOUNT)
            self.step("pay (approve + pay; transferFrom)", payer, bill_data("pay"),
                      self.base(open1, payer_funds, allow), "ok",
                      probe={"calls": [(USDC, calldata("approve(address,uint256)", ["address", "uint256"],
                                                       [self.pt, AMOUNT])),
                                       (self.pt, bill_data("pay")), (self.pt, getbill)],
                             "watch": [("payer(probe)", self.probe), ("contract", self.pt)],
                             "overrides": self.base(open1_any, balance(self.probe, PAYER_START))},
                      note="probe pays as a contract payer: approve + pay in one eth_call")
        else:
            self.skip("pay (approve + pay)", "allowance slot assumption failed")

        # payWithAuthorization: real EIP-712 signature on the mainnet domain, submitted by a relayer
        valid_before = self.now + 3600
        v, r, s = self.sign_auth(payer, 1, valid_before)
        pwa = self.pwa_data(1, payer, v, r, s, valid_before)
        self.step("payWithAuthorization (relayer submits payer's signature)", relayer, pwa,
                  self.base(open1, payer_funds), "ok",
                  probe={"calls": [(self.pt, pwa), (self.pt, getbill)],
                         "watch": [("payer", payer.address), ("contract", self.pt), ("relayer(probe)", self.probe)],
                         "overrides": self.base(open1, payer_funds)})

        paid_at = self.now - 600
        claim_by = paid_at + 3 * 86400
        held = AMOUNT * NATIVE_PER_UNIT

        def paid(payee_addr=None, payer_addr=None, cb=claim_by):
            return self.pt_state(
                L.count(1),
                self.bill(1, "Paid", payee=payee_addr, payer=payer_addr or payer.address, allowed=payer.address,
                          claim_by=cb),
                native=held,
            )

        self.step("claim (payee, before claimBy)", payee, bill_data("claim"), self.base(paid()), "ok",
                  probe={"calls": [(self.pt, bill_data("claim")), (self.pt, getbill)],
                         "watch": [("payee(probe)", self.probe), ("contract", self.pt)],
                         "overrides": self.base(paid(payee_addr=self.probe))})
        self.step("decline (payee, before claimBy)", payee, bill_data("decline"), self.base(paid()), "ok",
                  probe={"calls": [(self.pt, bill_data("decline")), (self.pt, getbill)],
                         "watch": [("payer", payer.address), ("contract", self.pt)],
                         "overrides": self.base(paid(payee_addr=self.probe))})
        self.step("cancel (payee, open bill)", payee, bill_data("cancel"), self.base(open1), "ok",
                  probe={"calls": [(self.pt, bill_data("cancel")), (self.pt, getbill)], "watch": [],
                         "overrides": self.base(self.pt_state(L.count(1), self.bill(1, "Open", payee=self.probe)))})
        at_claim_by = {"time": hex(claim_by)}
        # estimateGas takes no block override: give it a bill whose claimBy is already the current block time.
        self.step("refund (third party, block time = claimBy)", keeper, bill_data("refund"), self.base(paid()),
                  "ok", block_ov=at_claim_by, est_overrides=self.base(paid(cb=self.now)),
                  probe={"calls": [(self.pt, bill_data("refund")), (self.pt, getbill)],
                         "watch": [("payer", payer.address), ("contract", self.pt), ("caller(probe)", self.probe)],
                         "overrides": self.base(paid()), "block_ov": at_claim_by})

        # negative checks
        self.step("claim at claimBy", payee, bill_data("claim"), self.base(paid()), "ClaimWindowClosed",
                  block_ov=at_claim_by)
        self.step("decline at claimBy", payee, bill_data("decline"), self.base(paid()), "ClaimWindowClosed",
                  block_ov=at_claim_by)
        self.step("refund at claimBy - 1", keeper, bill_data("refund"), self.base(paid()), "ClaimWindowOpen",
                  block_ov={"time": hex(claim_by - 1)})
        two_open = self.pt_state(L.count(2), self.bill(1, "Open"), self.bill(2, "Open", payee=self.payee2.address))
        pwa_b = self.pwa_data(2, payer, v, r, s, valid_before)  # signature made for bill 1
        self.step("signature for bill 1 used on bill 2 (same amount, other payee)", relayer, pwa_b,
                  self.base(two_open, payer_funds), "invalid signature")
        self.step("pay after payBy", payer, bill_data("pay"), self.base(open1, payer_funds), "PayWindowClosed",
                  block_ov={"time": hex(pay_by)})
        self.step("pay by non-allowed payer", relayer, bill_data("pay"), self.base(open1), "NotAllowedPayer")
        self.step("claim by non-payee", keeper, bill_data("claim"), self.base(paid()), "NotPayee")

        # EIP-7702: a payer address carrying a delegation is checked by USDC only through ERC-1271.
        delegated = {payer.address: {"code": "0xef0100" + self.keeper.address[2:].lower()}}
        self.step("payWithAuthorization, payer has an EIP-7702 delegation to a code-less address", relayer, pwa,
                  self.base(open1, payer_funds, delegated), None,
                  note="code override 0xef0100||address; observes the real token's ERC-1271 branch")

        # blocklist, simulated through the real blocklist precompile's storage
        if blocklist_ok:
            def blocked(a: str) -> dict:
                return {BLOCKLIST_PRECOMPILE: {"stateDiff": {self.blocklist_key(a): h32(1)}}}

            note = "blocklist set by state override of precompile 0x1800..01 storage"
            self.step("claim to a blocklisted payee", payee, bill_data("claim"),
                      self.base(paid(), blocked(payee.address)), "revert", note=note)
            self.step("refund to payer after claimBy while payee is blocklisted", keeper, bill_data("refund"),
                      self.base(paid(), blocked(payee.address)), "ok", block_ov=at_claim_by,
                      est_overrides=self.base(paid(cb=self.now), blocked(payee.address)), note=note,
                      probe={"calls": [(self.pt, bill_data("refund")), (self.pt, getbill)],
                             "watch": [("payer", payer.address), ("contract", self.pt)],
                             "overrides": self.base(paid(), blocked(payee.address)), "block_ov": at_claim_by})
            self.step("refund to a blocklisted payer", keeper, bill_data("refund"),
                      self.base(paid(), blocked(payer.address)), "revert", block_ov=at_claim_by, note=note)
            self.step("decline to a blocklisted payer", payee, bill_data("decline"),
                      self.base(paid(), blocked(payer.address)), "revert", note=note)
            self.step("payWithAuthorization by a blocklisted payer", relayer, pwa,
                      self.base(open1, payer_funds, blocked(payer.address)), "revert", note=note)
            if allowance_ok:
                self.step("pay by a blocklisted payer", payer, bill_data("pay"),
                          self.base(open1, payer_funds, self.allowance_override(payer.address, self.pt, AMOUNT),
                                    blocked(payer.address)), "revert", note=note)
            self.step("claim while the contract itself is blocklisted", payee, bill_data("claim"),
                      self.base(paid(), blocked(self.pt)), "revert", note=note)
        else:
            self.skip("blocklist cases", "blocklist slot assumption failed")

    # ---- slot checks

    def allowance_override(self, owner: str, spender: str, value: int) -> dict:
        inner = kec(encode(["address", "uint256"], [owner, ALLOWANCE_SLOT]))
        key = kec(encode(["address"], [spender]) + inner)
        return {USDC: {"stateDiff": {"0x" + key.hex(): h32(value)}}}

    def verify_allowance_slot(self) -> bool:
        o, s = self.payer.address, self.pt
        data = calldata("allowance(address,address)", ["address", "address"], [o, s])
        ret = self.rpc.result("eth_call", [{"to": USDC, "data": data}, self.block_tag,
                                           self.allowance_override(o, s, 123456)])
        ok = int(ret, 16) == 123456
        self.check(f"FiatToken allowance mapping at slot {ALLOWANCE_SLOT}", ok, str(int(ret, 16)))
        return ok

    def blocklist_key(self, a: str) -> str:
        return "0x" + kec(encode(["address", "uint256"], [a, BLOCKLIST_SLOT])).hex()

    def verify_blocklist_slot(self) -> bool:
        a = self.payee.address
        ov = {BLOCKLIST_PRECOMPILE: {"stateDiff": {self.blocklist_key(a): h32(1)}}}
        r1 = self.rpc.result("eth_call", [{"to": USDC, "data": calldata("isBlacklisted(address)", ["address"], [a])},
                                          self.block_tag, ov])
        r0 = self.rpc.result("eth_call", [{"to": USDC, "data": calldata("isBlacklisted(address)", ["address"], [a])},
                                          self.block_tag])
        ok = int(r1, 16) == 1 and int(r0, 16) == 0
        self.check(f"blocklist precompile mapping at slot {BLOCKLIST_SLOT} drives USDC.isBlacklisted", ok,
                   f"without override {int(r0, 16)}, with override {int(r1, 16)}")
        return ok

    def check(self, name: str, ok: bool, detail: str):
        self.checks.append({"check": name, "ok": ok, "detail": detail})
        print(f"[{'ok' if ok else 'FAIL'}] {name}: {detail}", flush=True)

    def skip(self, name: str, why: str):
        self.steps.append({"step": name, "caller": "-", "expected": "-", "result": f"skipped: {why}",
                           "match": "NO", "gas": None, "cost_usdc": "-", "post_state": "", "note": "",
                           "return": None})

    # ---- report

    def markdown(self) -> str:
        lines = [
            "## Arc mainnet real-node rehearsal (read-only: eth_call / eth_estimateGas with state overrides)",
            "",
            f"Block {self.block_number} (time {self.now}), base fee {self.base_fee / 1e9:g} gwei. "
            f"Cost = estimateGas x 20 gwei, paid in native USDC. Bill amount 25.50 USDC. RPC calls: {self.rpc.calls}.",
            "",
            "| Check | OK | Detail |",
            "|---|---|---|",
        ]
        lines += [f"| {c['check']} | {'yes' if c['ok'] else 'NO'} | {c['detail']} |" for c in self.checks]
        lines += [
            "",
            "| # | Step | Expected | Result | Match | estimateGas | USDC @20 gwei | Post-state (probe) | Note |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for i, s in enumerate(self.steps, 1):
            gas = f"{s['gas']:,}" if s["gas"] else "-"
            lines.append(
                f"| {i} | {s['step']} | {s['expected']} | {s['result']} | {s['match']} | {gas} | {s['cost_usdc']} "
                f"| {s['post_state']} | {s['note']} |"
            )
        return "\n".join(lines) + "\n"

    def passed(self) -> bool:
        return all(c["ok"] for c in self.checks) and all(s["match"] in ("yes", "info") for s in self.steps)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default=os.path.join("contracts", "out"), help="Foundry out/ directory")
    ap.add_argument("--rpc", default=os.environ.get("ARC_RPC_URL", DEFAULT_RPC))
    ap.add_argument("--markdown", help="write the result table here")
    ap.add_argument("--json", help="write raw results here")
    a = ap.parse_args()

    r = Rehearsal(Rpc(a.rpc), a.out)
    r.run()
    md = r.markdown()
    print(md)
    if a.markdown:
        with open(a.markdown, "w", encoding="utf-8", newline="\n") as f:
            f.write(md)
    if a.json:
        with open(a.json, "w", encoding="utf-8", newline="\n") as f:
            json.dump({"block": r.block_number, "time": r.now, "checks": r.checks, "steps": r.steps}, f, indent=2)
    ok = r.passed()
    print("ALL STEPS MATCHED" if ok else "SOME STEPS DID NOT MATCH", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
