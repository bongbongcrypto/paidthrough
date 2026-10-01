import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import kpath  # noqa: F401
from eth_account import Account

import abi
import notify
import paidthrough_keeper as K
from fakechain import CONTRACT, GWEI, FakeChain, FakeRpc, addr, revert_string
from fmt import fee_usdc, usdc6, utc

KEEPER_TEST_ENV = "PAIDTHROUGH_KEEPER_ENV"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pt-keeper-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.dep = self.tmp / "deployments.json"
        self.dep.write_text(json.dumps({"mainnet": None, "testnet": {"address": CONTRACT, "fromBlock": 90}}))
        self.chain = FakeChain()
        self.factory_calls = 0
        self.cards = []
        env = mock.patch.dict(os.environ, {"PAIDTHROUGH_KEEPER_STATE": str(self.tmp / "state"),
                                           KEEPER_TEST_ENV: str(self.tmp / "missing.env")})
        env.start()
        self.addCleanup(env.stop)

    def factory(self, url):
        self.factory_calls += 1
        return FakeRpc(self.chain)

    def run_cli(self, *argv, key_loader=None):
        out, err = io.StringIO(), io.StringIO()
        code = K.main(["--network", "testnet"] + list(argv), rpc_factory=self.factory, out=out, err=err,
                      key_loader=key_loader, notify_sender=self.capture_card, sleep=lambda s: None,
                      clock=self.fake_clock(), deployments=self.dep)
        return code, out.getvalue(), err.getvalue()

    def capture_card(self, text):
        self.cards.append(text)
        return True, "sent"

    @staticmethod
    def fake_clock():
        t = [0.0]

        def clock():
            t[0] += 1.0
            return t[0]
        return clock

    def write_key(self):
        acct = Account.create()            # throwaway key generated inside the test
        key_hex = "0x" + bytes(acct.key).hex()
        kf = self.tmp / "keeper.env"
        kf.write_text("# test only\nKEEPER_PRIVATE_KEY=%s\n" % key_hex)
        os.environ[KEEPER_TEST_ENV] = str(kf)
        return acct, key_hex

    def history(self):
        c = self.chain
        ts = c.head_ts
        c.issue(1, 100)
        c.issue(2, 101); c.cancel(2, 102)
        c.issue(3, 103); c.pay(3, 104, claim_by=ts + 100)
        c.issue(4, 105); c.pay(4, 106, claim_by=ts - 1); c.claim(4, 107)
        c.issue(5, 108); c.pay(5, 109, claim_by=ts - 1); c.decline(5, 110)
        c.issue(6, 111); c.pay(6, 112, claim_by=ts - 1); c.refund(6, 113)
        c.issue(7, 114, amount=150_000_000); c.pay(7, 115, payer=addr(0xF7), claim_by=ts)   # due exactly now
        c.issue(8, 116); c.pay(8, 117, claim_by=ts + 1)                                    # due in 1 s
        c.issue(9, 118, amount=2_500_001); c.pay(9, 119, payer=addr(0xF9), claim_by=ts - 3600)


class ConfigTests(Base):
    def test_no_deployment_exits_zero_without_network(self):
        code, out, err = self.run_cli("--network", "mainnet", "scan")
        self.assertEqual(code, 0)
        self.assertIn("no deployment configured for mainnet", out)
        self.assertEqual(self.factory_calls, 0)

    def test_global_options_before_or_after_command(self):
        self.assertEqual(K.parse(["scan", "--network", "testnet"]).network, "testnet")
        self.assertEqual(K.parse(["--network", "testnet", "scan"]).network, "testnet")
        self.assertEqual(K.parse(["scan"]).network, "mainnet")
        a = K.parse(["run", "--send", "--network", "testnet", "--from-block", "5"])
        self.assertEqual((a.network, a.from_block, a.send, a.no_cache), ("testnet", 5, True, False))

    def test_real_deployments_file_schema(self):
        # null until deploy, then {"address": "0x..", "fromBlock": N}; must stay loadable either way
        data = json.loads((kpath.KEEPER / "deployments.json").read_text())
        self.assertEqual(set(data), {"mainnet", "testnet"})
        for net, entry in data.items():
            if entry is None:
                with self.assertRaises(K.NoDeployment):
                    K.load_deployment(net, path=kpath.KEEPER / "deployments.json")
                continue
            self.assertIsInstance(entry, dict, net)
            self.assertEqual(abi.checksum(entry["address"]).lower(), entry["address"].lower())
            self.assertIsInstance(entry["fromBlock"], int)
            self.assertGreater(entry["fromBlock"], 0)

    def test_contract_without_from_block_refused(self):
        self.dep.write_text(json.dumps({"mainnet": None, "testnet": None}))
        code, out, err = self.run_cli("--contract", CONTRACT, "scan")
        self.assertEqual(code, 2)
        self.assertIn("no from-block", err)
        self.assertEqual(self.factory_calls, 0)

    def test_chain_id_mismatch_refused(self):
        self.history()
        self.chain.chain_id = 5042        # mainnet id served while we asked for testnet
        code, out, err = self.run_cli("run")
        self.assertEqual(code, 2)
        self.assertIn("chain id", err)
        self.assertFalse(any(m == "eth_getLogs" for m, _ in self.chain.calls))

    def test_send_refused_without_key_file(self):
        self.history()
        code, out, err = self.run_cli("run", "--send")
        self.assertEqual(code, 2)
        self.assertIn("key file not found", err)
        self.assertEqual(self.factory_calls, 0)

    def test_bad_key_file_refused_without_echo(self):
        kf = self.tmp / "bad.env"
        kf.write_text("KEEPER_PRIVATE_KEY=0xnot-a-key-SECRETVALUE\n")
        os.environ[KEEPER_TEST_ENV] = str(kf)
        code, out, err = self.run_cli("run", "--send")
        self.assertEqual(code, 2)
        self.assertNotIn("SECRETVALUE", out + err)


class ReadTests(Base):
    def test_scan_table(self):
        self.history()
        code, out, err = self.run_cli("scan")
        self.assertEqual(code, 0, err)
        self.assertIn("150.000000", out)          # 150 USDC from 6-decimal units
        self.assertIn("2.500001", out)
        self.assertIn(utc(self.chain.head_ts), out)
        self.assertIn("9 bills", out)
        self.assertIn("due now: 2", out)          # bills 7 and 9

    def test_due_boundary_against_block_timestamp(self):
        self.history()
        code, out, err = self.run_cli("due")
        self.assertEqual(code, 0)
        ids = [int(l.split()[0]) for l in out.splitlines() if " Paid " in l]
        self.assertEqual(ids, [7, 9])             # 7: claimBy == ts (due); 8: claimBy == ts + 1 (not due)

    def test_bill_command(self):
        self.history()
        code, out, err = self.run_cli("bill", "7")
        self.assertEqual(code, 0, err)
        self.assertIn("Paid", out)
        self.assertRegex(out, r"due now +yes")
        self.assertIn("Issued", out)

    def test_missing_events_filled_from_getbill(self):
        self.history()
        # the node silently drops bill 9's logs: an empty answer must not hide a due bill
        self.chain.logs = [lg for lg in self.chain.logs if int(lg["topics"][1], 16) != 9]
        code, out, err = self.run_cli("due")
        self.assertEqual(code, 0)
        self.assertIn("no BillIssued log", err)
        self.assertIn("*getBill", out)
        self.assertIn("2.500001", out)

    def test_events_beyond_bill_count_refused(self):
        self.history()
        self.chain.bill_count_override = 5        # chain says 5 bills, our decoded events show 9
        code, out, err = self.run_cli("run")
        self.assertEqual(code, 1)
        self.assertIn("billCount() is 5", err)
        self.assertFalse(any(m == "eth_estimateGas" for m, _ in self.chain.calls))

    def test_rpc_failure_is_unknown_not_empty(self):
        self.history()
        self.chain.fail_methods["eth_getLogs"] = {"code": -32000, "message": "internal error"}
        code, out, err = self.run_cli("due")
        self.assertEqual(code, 1)
        self.assertIn("state unknown", err)
        self.assertNotIn("no bill is due", out)

    def test_incremental_cache(self):
        self.history()
        self.assertEqual(self.run_cli("scan")[0], 0)
        first = list(self.chain.get_logs_calls)
        self.chain.head += 50_000
        self.chain.issue(10, self.chain.head - 5)
        self.chain.get_logs_calls.clear()
        code, out, err = self.run_cli("scan")
        self.assertEqual(code, 0, err)
        self.assertEqual(first[0][0], 90)
        self.assertEqual(self.chain.get_logs_calls[0][0], 1_000_000 - K.RESCAN_OVERLAP + 1)
        self.assertIn("10 bills", out)
        code, out2, _ = self.run_cli("--no-cache", "scan")
        self.assertIn("10 bills", out2)


class RunTests(Base):
    def test_dry_run_plans_without_sending(self):
        self.history()
        code, out, err = self.run_cli("run")
        self.assertEqual(code, 0, err)
        self.assertIn("dry-run", out)
        self.assertIn(abi.encode_refund(7), out)
        self.assertIn(abi.encode_refund(9), out)
        self.assertNotIn(abi.encode_refund(8), out)
        self.assertIn("fee ~", out)
        self.assertFalse(any(m == "eth_sendRawTransaction" for m, _ in self.chain.calls))
        sim = [p for m, p in self.chain.calls if m == "eth_call" and p[0]["data"].startswith("0x278ecde1")]
        self.assertTrue(sim and all(p[0]["from"] == K.ZERO for p in sim))

    def test_simulated_revert_is_skipped_and_reported_once(self):
        self.history()
        self.chain.refund_revert[7] = revert_string("Blacklistable: account is blacklisted")
        code, out, err = self.run_cli("run")
        self.assertEqual(code, 0)
        self.assertIn("bill 7  SKIP revert", out)
        self.assertIn("blacklisted", out)
        self.assertIn(abi.encode_refund(9), out)   # the other due bill still planned
        tries = [p for m, p in self.chain.calls if m == "eth_call" and p[0]["data"] == abi.encode_refund(7)]
        self.assertEqual(len(tries), 1)            # no retry loop

    def test_state_changed_since_events_is_skipped(self):
        self.history()
        self.chain.bills[9]["status"] = abi.REFUNDED   # someone refunded after our log scan
        code, out, err = self.run_cli("run")
        self.assertIn("bill 9  SKIP state_changed", out)

    def test_send_builds_correct_tx_and_never_prints_key(self):
        self.history()
        acct, key_hex = self.write_key()
        code, out, err = self.run_cli("run", "--send")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.chain.sent), 2)
        for raw, bid, nonce in zip(self.chain.sent, (7, 9), (7, 8)):
            tx = FakeChain.decode_tx(raw)
            self.assertEqual(tx["type"], 2)
            self.assertEqual(tx["chainId"], 5042002)
            self.assertEqual(tx["to"].hex().lower().replace("0x", ""), CONTRACT.lower()[2:])
            self.assertEqual("0x" + tx["data"].hex().replace("0x", ""), abi.encode_refund(bid))
            self.assertEqual(tx["value"], 0)
            self.assertEqual(tx["nonce"], nonce)                       # pending nonce, re-read per send
            self.assertEqual(tx["gas"], K.gas_limit(self.chain.gas_estimate))
            self.assertEqual(tx["maxFeePerGas"], 40 * GWEI)            # 2 x 20 gwei base fee
            self.assertEqual(tx["maxPriorityFeePerGas"], 5 * GWEI)
            self.assertEqual(FakeChain.sender(raw), acct.address)
        self.assertIn("REFUNDED", out)
        self.assertIn("fee 0.001250 USDC (1250000000000000 wei)", out)   # 50,000 gas x 25 gwei
        state_text = "".join(p.read_text() for p in (self.tmp / "state").glob("*.json"))
        bare = key_hex[2:]
        for blob in (out, err, state_text):
            for form in (key_hex, bare, bare.upper(), key_hex.upper()):
                self.assertNotIn(form, blob)
        self.assertNotIn(bare, repr(K.load_key(Path(os.environ[KEEPER_TEST_ENV]))))
        # after the run both bills are Refunded and nothing is due
        code, out, err = self.run_cli("due")
        self.assertIn("no bill is due", out)

    def test_receipt_timeout_stops_and_blocks_resend(self):
        self.history()
        self.write_key()
        self.chain.mine_sent = False
        code, out, err = self.run_cli("run", "--send", "--receipt-timeout", "5")
        self.assertEqual(code, 1)
        self.assertEqual(len(self.chain.sent), 1)          # second due bill not sent after an unknown result
        self.assertIn("status unknown", out)
        code, out, err = self.run_cli("--no-cache", "run", "--send", "--receipt-timeout", "5")
        self.assertEqual(len(self.chain.sent), 2)          # bill 7 blocked as in-flight; only bill 9 sent now
        self.assertIn("bill 7  SKIP inflight", out)
        self.assertEqual("0x" + FakeChain.decode_tx(self.chain.sent[1])["data"].hex().replace("0x", ""),
                         abi.encode_refund(9))

    def test_failed_receipt_reported(self):
        self.history()
        self.write_key()
        self.chain.receipt_status = 0
        code, out, err = self.run_cli("run", "--send")
        self.assertEqual(code, 1)
        self.assertIn("FAILED (reverted)", out)

    def test_no_gas_money_skips_all(self):
        self.history()
        self.write_key()
        self.chain.balance = 10 ** 12          # 0.000001 USDC
        code, out, err = self.run_cli("run", "--send", "--notify")
        self.assertEqual(code, 0)
        self.assertEqual(self.chain.sent, [])
        self.assertIn("no_gas_money", out)
        self.assertEqual(len(self.cards), 1)
        self.assertIn("가스비 USDC 충전", self.cards[0])

    def test_notify_summary_and_dedupe(self):
        self.history()
        self.chain.refund_revert[7] = revert_string("Blacklistable: account is blacklisted")
        self.chain.refund_revert[9] = revert_string("Blacklistable: account is blacklisted")
        self.run_cli("run", "--notify")
        self.assertEqual(len(self.cards), 1)
        self.assertEqual(notify.problems(self.cards[0], allow={"USDC"}), [])
        self.run_cli("run", "--notify")
        self.assertEqual(len(self.cards), 1)    # same skips within 24 h: no second message

    def test_notify_silent_when_nothing_due(self):
        self.chain.issue(1, 100)
        code, out, err = self.run_cli("run", "--notify")
        self.assertEqual(code, 0)
        self.assertEqual(self.cards, [])


class MoneyMathTests(unittest.TestCase):
    def test_fee_floor(self):
        self.assertEqual(K.fee_params(20 * GWEI, 572), (40 * GWEI, 572))
        self.assertEqual(K.fee_params(5 * GWEI, 0)[0], 20 * GWEI)       # impossible on Arc, floor anyway
        self.assertEqual(K.fee_params(20 * GWEI, 30 * GWEI)[0], 50 * GWEI)   # >= base + priority

    def test_gas_limit_rounds_up(self):
        self.assertEqual(K.gas_limit(61_234), 73_481)    # 73,480.8 -> 73,481
        self.assertEqual(K.gas_limit(50_000), 60_000)

    def test_decimals(self):
        self.assertEqual(usdc6(150_000_000), "150.000000")
        self.assertEqual(usdc6(1), "0.000001")
        self.assertEqual(fee_usdc(50_000 * 25 * GWEI), "0.001250")   # wei (18) -> USDC
        self.assertEqual(fee_usdc(1), "0.000001")                    # rounds up, never shows 0 for a cost
        self.assertEqual(fee_usdc(10 ** 18), "1.000000")
        self.assertEqual(fee_usdc(0), "0.000000")

    def test_tx_invariant_checked_before_signing(self):
        good = K.build_tx(CONTRACT, 7, 5042, 0, 70_000, 40 * GWEI, GWEI)
        self.assertEqual(good["to"], abi.checksum(CONTRACT))
        for patch in ({"to": addr(0xBAD)}, {"data": abi.encode_refund(8)}, {"value": 1}, {"chainId": 1},
                      {"data": "0xa9059cbb" + abi.encode_refund(7)[10:]}, {"maxFeePerGas": GWEI}):
            with self.assertRaises(AssertionError, msg=str(patch)):
                K.check_tx({**good, **patch}, CONTRACT, 7, 5042)
        with self.assertRaises(AssertionError):
            K.build_tx(CONTRACT, 7, 5042, 0, 70_000, 10 * GWEI, GWEI)      # below the 20 gwei floor


class NotifyCardTests(unittest.TestCase):
    def test_every_card_kind_is_readable(self):
        sent = [{"id": 7, "amount": 150_000_000, "fee_wei": 10 ** 15}]
        skipped = [{"id": i, "reason": r} for i, r in enumerate(notify.SKIP_KO, start=1)]
        failed = [{"id": 3, "why": "no receipt"}]
        cards = [notify.summary_card("mainnet", sent, [], []),
                 notify.summary_card("testnet", [], skipped[:1], []),
                 notify.summary_card("mainnet", sent, skipped, failed),
                 notify.summary_card("mainnet", [], [{"id": 1, "reason": "no_gas_money"}], []),
                 notify.summary_card("mainnet", [], [], [], error="RpcUnavailable")]
        for reason in notify.SKIP_KO:
            cards.append(notify.summary_card("mainnet", [], [{"id": 12345, "reason": reason}] * 8, []))
        for c in cards:
            self.assertEqual(notify.problems(c, allow={"USDC"}), [], c)
            self.assertTrue(c.startswith("<b>"))
            self.assertIn("할 일: ", c)
            self.assertTrue(c.rstrip().endswith("#paidthrough"))
        self.assertEqual(notify.summary_card("mainnet", [], [], []), "")

    def test_send_card_never_leaks_token(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        env = tmp / "alert.env"
        env.write_text("BOT_TOKEN=123:TESTTOKEN\nUSER_DIRECT_CHAT_ID=1\n")

        def boom(url, data, timeout=None):
            raise OSError("cannot reach " + url)
        ok, why = notify.send_card("<b>테스트</b>", env_path=env, opener=boom)
        self.assertFalse(ok)
        self.assertNotIn("TESTTOKEN", why)


if __name__ == "__main__":
    unittest.main()
