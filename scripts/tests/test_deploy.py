"""Tests for scripts/deploy_mainnet.py against a fake Arc node (no network, no Foundry).

The fake answers the JSON-RPC calls the script makes, decodes the signed deploy transaction like a node would,
"creates" the contract with the artifact's runtime (immutables filled) and answers the read-back calls.
Test keys are made with Account.create() inside the test and live only in a temp directory.
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "keeper"))

from eth_abi import encode  # noqa: E402
from eth_account import Account  # noqa: E402
from eth_account.typed_transactions import TypedTransaction  # noqa: E402
from hexbytes import HexBytes  # noqa: E402

import deploy_mainnet as dm  # noqa: E402
from rpc import Rpc, RpcError  # noqa: E402

GWEI = 10**9
INITCODE = bytes.fromhex("6080604052")
# runtime: 2 opcode bytes, then the usdc immutable word, then the maxAmount immutable word, then 1 byte
RUNTIME = bytes.fromhex("6001") + bytes(32) + bytes(32) + bytes.fromhex("00")
IMMUTABLES = {"11": [{"start": 2, "length": 32}], "12": [{"start": 34, "length": 32}]}

WEB_CONFIG = """export const CONFIG = {
  network: 'mainnet',

  networks: {
    mainnet: {
      label: 'Arc mainnet',
      chainId: 5042,
      usdc: '0x3600000000000000000000000000000000000000',
      paidThrough: null, // set after deploy
      deployBlock: null, // block of the deploy tx
    },
    testnet: {
      label: 'Arc testnet',
      chainId: 5042002,
      paidThrough: null,
      deployBlock: null,
    },
  },
};
"""


def filled_runtime(max_amount=dm.DEFAULT_MAX_AMOUNT, usdc=dm.USDC) -> bytes:
    b = bytearray(RUNTIME)
    b[2:34] = encode(["address"], [usdc])
    b[34:66] = encode(["uint256"], [max_amount])
    return bytes(b)


class FakeArc(Rpc):
    def __init__(self, chain_id=5042, base_fee=20 * GWEI, priority=572, balance=10**18, code=None, pending_extra=0,
                 deploy_runtime=None, est=1_150_000):
        super().__init__("http://fake.invalid")
        self.chain_id_value = chain_id
        self.base_fee = base_fee
        self.priority = priority
        self.balance = balance
        self.codes = dict(code or {})
        self.nonces = {}
        self.pending_extra = pending_extra
        self.deploy_runtime = deploy_runtime
        self.est = est
        self.calls = []
        self.sent = []
        self.receipts = {}
        self.head = 777_000

    def call(self, method, params):
        self.calls.append((method, params))
        if method == "eth_chainId":
            return hex(self.chain_id_value)
        if method == "eth_getBlockByNumber":
            return {"number": hex(self.head), "timestamp": hex(1_790_000_000), "baseFeePerGas": hex(self.base_fee)}
        if method == "eth_maxPriorityFeePerGas":
            return hex(self.priority)
        if method == "eth_getCode":
            return "0x" + self.codes.get(params[0].lower(), b"").hex()
        if method == "eth_getTransactionCount":
            n = self.nonces.get(params[0].lower(), 5)
            return hex(n + (self.pending_extra if params[1] == "pending" else 0))
        if method == "eth_estimateGas":
            return hex(self.est)
        if method == "eth_getBalance":
            return hex(self.balance)
        if method == "eth_sendRawTransaction":
            raw = HexBytes(params[0])
            tx = TypedTransaction.from_bytes(raw).as_dict()
            sender = Account.recover_transaction(raw)
            self.sent.append({"tx": tx, "sender": sender})
            addr = dm.create_address(sender, tx["nonce"])
            self.codes[addr.lower()] = self.deploy_runtime if self.deploy_runtime is not None else filled_runtime()
            h = "0x" + dm.kec(bytes(raw)).hex()
            self.head += 1
            self.receipts[h] = {"status": "0x1", "contractAddress": addr, "blockNumber": hex(self.head),
                                "gasUsed": hex(1_100_000)}
            return h
        if method == "eth_getTransactionReceipt":
            return self.receipts.get(params[0])
        if method == "eth_call":
            to = params[0]["to"].lower()
            code = self.codes.get(to, b"")
            selector = params[0]["data"]
            if not code:
                return "0x"
            if selector == dm.sel("usdc()"):
                return "0x" + code[2:34].hex()
            if selector == dm.sel("maxAmount()"):
                return "0x" + code[34:66].hex()
            if selector == dm.sel("billCount()"):
                return "0x" + bytes(32).hex()
            raise RpcError(method, {"code": 3, "message": "execution reverted"})
        raise AssertionError("unexpected RPC method " + method)

    def methods(self):
        return [m for m, _ in self.calls]


class DeployTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        out = t / "out" / "PaidThrough.sol"
        out.mkdir(parents=True)
        (out / "PaidThrough.json").write_text(json.dumps({
            "bytecode": {"object": "0x" + INITCODE.hex()},
            "deployedBytecode": {"object": "0x" + RUNTIME.hex(), "immutableReferences": IMMUTABLES},
        }), encoding="utf-8")
        self.out = t / "out"
        self.deployments = t / "deployments.json"
        self.deployments.write_text('{"mainnet": null, "testnet": null}', encoding="utf-8")
        self.web = t / "config.js"
        self.web.write_text(WEB_CONFIG, encoding="utf-8")
        self.acct = Account.create()
        self.key_hex = self.acct.key.hex().removeprefix("0x")
        self.keyfile = t / "deployer.env"
        self.keyfile.write_text("DEPLOYER_PRIVATE_KEY=0x%s\n" % self.key_hex, encoding="utf-8")
        self._env = os.environ.get("PAIDTHROUGH_DEPLOYER_ENV")
        os.environ["PAIDTHROUGH_DEPLOYER_ENV"] = str(self.keyfile)

    def tearDown(self):
        if self._env is None:
            os.environ.pop("PAIDTHROUGH_DEPLOYER_ENV", None)
        else:
            os.environ["PAIDTHROUGH_DEPLOYER_ENV"] = self._env
        self.tmp.cleanup()

    def run_main(self, extra, rpc, key_loader=dm.load_key, confirm=lambda _: "DEPLOY"):
        buf = io.StringIO()
        argv = ["--out", str(self.out), "--deployments", str(self.deployments), "--web-config", str(self.web)]
        code = dm.main(argv + extra, rpc=rpc, out=buf, key_loader=key_loader, confirm=confirm,
                       sleep=lambda s: None)
        return code, buf.getvalue()

    # ---- dry run

    def test_dry_run_never_reads_key_or_signs(self):
        def no_key(_path):
            raise AssertionError("dry run must not read the key file")

        rpc = FakeArc()
        code, text = self.run_main(["--network", "mainnet"], rpc, key_loader=no_key)
        self.assertEqual(code, 0, text)
        self.assertIn("DRY RUN", text)
        self.assertNotIn("eth_sendRawTransaction", rpc.methods())
        self.assertEqual(rpc.sent, [])
        est = [p for m, p in rpc.calls if m == "eth_estimateGas"][0]
        self.assertNotIn("to", est[0])  # a creation
        self.assertTrue(est[0]["data"].startswith("0x" + INITCODE.hex()))
        self.assertTrue(est[0]["data"].endswith(encode(["address", "uint96"], [dm.USDC, 10_000 * 10**6]).hex()))
        self.assertIn("1380000", text)  # limit = ceil(1_150_000 x 1.2)
        self.assertEqual(self.deployments.read_text(encoding="utf-8"), '{"mainnet": null, "testnet": null}')
        self.assertEqual(self.web.read_text(encoding="utf-8"), WEB_CONFIG)

    def test_chain_id_mismatch_refused_before_estimate(self):
        rpc = FakeArc(chain_id=1)
        code, text = self.run_main(["--network", "mainnet"], rpc)
        self.assertEqual(code, 2)
        self.assertIn("chain id mismatch", text)
        self.assertNotIn("eth_estimateGas", rpc.methods())
        rpc = FakeArc(chain_id=5042)
        code, text = self.run_main(["--network", "testnet", "--send", "--yes"], rpc)
        self.assertEqual(code, 2)
        self.assertEqual(rpc.sent, [])

    def test_max_amount_sanity(self):
        for bad in (0, 10_000 * 10**18, dm.SANITY_MAX_AMOUNT + 1):
            rpc = FakeArc()
            code, text = self.run_main(["--network", "mainnet", "--max-amount", str(bad)], rpc)
            self.assertEqual(code, 2, bad)
            self.assertIn("6-decimal", text)
            self.assertEqual(rpc.calls, [])
        code, text = self.run_main(["--network", "mainnet", "--max-amount", str(500 * 10**6)], FakeArc())
        self.assertEqual(code, 0, text)
        self.assertIn("maxAmount = 500000000", text)

    # ---- send: refusals

    def test_send_refuses_without_key_file(self):
        os.environ["PAIDTHROUGH_DEPLOYER_ENV"] = str(Path(self.tmp.name) / "missing.env")
        rpc = FakeArc()
        code, text = self.run_main(["--network", "mainnet", "--send", "--yes"], rpc)
        self.assertEqual(code, 2)
        self.assertIn("key file not found", text)
        self.assertEqual(rpc.sent, [])

    def test_key_file_inside_repo_refused(self):
        with self.assertRaises(dm.Refused):
            dm.load_key(ROOT / "keeper" / "nonexistent-deployer.env")

    def test_bad_key_line_never_echoed(self):
        secret = "ab" * 31 + "c"  # 63 hex chars: invalid
        self.keyfile.write_text("DEPLOYER_PRIVATE_KEY=0x%s\n" % secret, encoding="utf-8")
        code, text = self.run_main(["--network", "mainnet", "--send", "--yes"], FakeArc())
        self.assertEqual(code, 2)
        self.assertNotIn(secret, text)

    def test_sender_with_code_refused(self):
        rpc = FakeArc(code={self.acct.address.lower(): bytes.fromhex("ef0100") + bytes(20)})
        code, text = self.run_main(["--network", "mainnet", "--send", "--yes"], rpc)
        self.assertEqual(code, 2)
        self.assertIn("EIP-7702", text)
        self.assertEqual(rpc.sent, [])

    def test_low_balance_refused(self):
        rpc = FakeArc(balance=10**15)  # 0.001 USDC
        code, text = self.run_main(["--network", "mainnet", "--send", "--yes"], rpc)
        self.assertEqual(code, 2)
        self.assertIn("balance", text)
        self.assertEqual(rpc.sent, [])

    def test_pending_transaction_refused(self):
        rpc = FakeArc(pending_extra=1)
        code, text = self.run_main(["--network", "mainnet", "--send", "--yes"], rpc)
        self.assertEqual(code, 2)
        self.assertIn("pending", text)

    def test_not_confirmed_nothing_sent(self):
        rpc = FakeArc()
        code, text = self.run_main(["--network", "mainnet", "--send"], rpc, confirm=lambda _: "yes")
        self.assertEqual(code, 2)
        self.assertEqual(rpc.sent, [])

    # ---- send: success and post-checks

    def test_send_deploys_checks_and_writes_configs(self):
        rpc = FakeArc(base_fee=1 * GWEI)  # below the floor: maxFee must still be 20 gwei
        code, text = self.run_main(["--network", "mainnet", "--send"], rpc)
        self.assertEqual(code, 0, text)
        self.assertEqual(len(rpc.sent), 1)
        tx = rpc.sent[0]["tx"]
        self.assertEqual(rpc.sent[0]["sender"], self.acct.address)
        self.assertEqual(bytes(tx["to"]), b"")
        self.assertEqual(tx["value"], 0)
        self.assertEqual(tx["chainId"], 5042)
        self.assertEqual(tx["maxFeePerGas"], 20 * GWEI)
        self.assertEqual(tx["nonce"], 5)
        self.assertEqual(tx["gas"], 1_380_000)
        addr = dm.create_address(self.acct.address, 5)
        dep = json.loads(self.deployments.read_text(encoding="utf-8"))
        self.assertEqual(dep["mainnet"]["address"], addr)
        self.assertEqual(dep["mainnet"]["fromBlock"], 777_001)
        self.assertTrue(dep["mainnet"]["txHash"].startswith("0x"))
        self.assertIsNone(dep["testnet"])
        web = self.web.read_text(encoding="utf-8")
        self.assertIn("paidThrough: '%s', // set after deploy" % addr, web)
        self.assertIn("deployBlock: 777001, // block of the deploy tx", web)
        self.assertEqual(web.count("paidThrough: null"), 1)  # testnet untouched
        self.assertIn("https://explorer.arc.io/address/%s" % addr, text)
        self.assertNotIn(self.key_hex, text)
        self.assertNotIn(self.key_hex.lower(), text.lower())

    def test_code_mismatch_does_not_write_configs(self):
        tampered = bytearray(filled_runtime())
        tampered[0] = 0x60
        tampered[1] = 0x02
        rpc = FakeArc(deploy_runtime=bytes(tampered))
        code, text = self.run_main(["--network", "mainnet", "--send", "--yes"], rpc)
        self.assertEqual(code, 1)
        self.assertIn("differs from the artifact", text)
        self.assertEqual(self.deployments.read_text(encoding="utf-8"), '{"mainnet": null, "testnet": null}')
        self.assertEqual(self.web.read_text(encoding="utf-8"), WEB_CONFIG)

    def test_wrong_immutable_does_not_write_configs(self):
        rpc = FakeArc(deploy_runtime=filled_runtime(max_amount=123))
        code, text = self.run_main(["--network", "mainnet", "--send", "--yes"], rpc)
        self.assertEqual(code, 1)
        self.assertIn("maxAmount()", text)
        self.assertEqual(self.web.read_text(encoding="utf-8"), WEB_CONFIG)

    def test_adopt_existing_deployment_testnet(self):
        addr = "0x00000000000000000000000000000000000000AA"
        rpc = FakeArc(chain_id=5042002, code={addr.lower(): filled_runtime()})
        code, text = self.run_main(["--network", "testnet", "--adopt", addr, "--from-block", "42"], rpc,
                                   key_loader=lambda _p: (_ for _ in ()).throw(AssertionError("no key")))
        self.assertEqual(code, 0, text)
        dep = json.loads(self.deployments.read_text(encoding="utf-8"))
        self.assertEqual(dep["testnet"], {"address": dm.checksum(addr), "fromBlock": 42})
        self.assertIsNone(dep["mainnet"])
        web = self.web.read_text(encoding="utf-8")
        self.assertIn("deployBlock: 42,", web)
        self.assertIn("https://explorer.testnet.arc.io/address/", text)

    def test_real_web_config_is_writable(self):
        real = ROOT / "web" / "config.js"
        if not real.exists():
            self.skipTest("web/config.js not present")
        copy = Path(self.tmp.name) / "real-config.js"
        copy.write_text(real.read_text(encoding="utf-8"), encoding="utf-8")
        addr = "0x00000000000000000000000000000000000000Bb"
        dm.write_web_config(copy, "mainnet", addr, 123)
        text = copy.read_text(encoding="utf-8")
        self.assertIn("paidThrough: '%s'," % dm.checksum(addr), text)
        self.assertIn("deployBlock: 123,", text)

    # ---- pure helpers

    def test_create_address_known_vectors(self):
        sender = "0x6ac7ea33f8831ea9dcc53393aaa88b25a785dbf0"
        self.assertEqual(dm.create_address(sender, 0).lower(), "0xcd234a471b72ba2f1ccf0a70fcaba648a5eecd8d")
        self.assertEqual(dm.create_address(sender, 1).lower(), "0x343c43a37d37dff08ae8c4a11544c718abb4fcf8")
        self.assertEqual(dm.create_address(sender, 2).lower(), "0xf778b86fa74e846c4f0a1fbd1335fe81c00a0c91")

    def test_fee_floor(self):
        self.assertEqual(dm.fee_params(1 * GWEI, 572), (20 * GWEI, 572))
        self.assertEqual(dm.fee_params(30 * GWEI, 1), (60 * GWEI, 1))

    def test_usdc18_rounds_up(self):
        self.assertEqual(dm.usdc18(1), "0.00000001")
        self.assertEqual(dm.usdc18(1_147_368 * 20 * GWEI), "0.02294736")


if __name__ == "__main__":
    unittest.main()
