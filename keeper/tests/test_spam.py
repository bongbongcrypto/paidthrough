"""Security review 2026-10-01: log-spam DoS (MEDIUM) and dust griefing (LOW)."""
import argparse
import io
import json
import unittest

import kpath  # noqa: F401

import abi
import notify
import paidthrough_keeper as K
import state as st
from fakechain import CONTRACT, FakeChain, FakeRpc, addr
from rpc import RpcError
from test_keeper import Base


def sent_ids(chain):
    return [int(FakeChain.decode_tx(raw)["data"].hex()[-64:], 16) for raw in chain.sent]


class LogSpamTests(Base):
    """3,000 BillCancelled logs in one block exceed Arc's 2,000-result cap even for a 1-block query."""

    def spam_setup(self, real_paid_in_spam_block=False):
        c = self.chain
        c.max_results = 2_000
        c.issue(1, 200, amount=75_000_000)                       # the real bill
        c.spam_cancel_block(2, 3_000, issue_from_block=300, block=900)
        c.pay(1, 900 if real_paid_in_spam_block else 250, payer=addr(0xF1), claim_by=c.head_ts - 60)
        self.state_file = None

    def state(self):
        files = list((self.tmp / "state").glob("*.json"))
        self.assertEqual(len(files), 1)
        return json.loads(files[0].read_text())

    def test_keeper_never_queries_bill_cancelled(self):
        self.spam_setup()
        self.write_key()
        code, out, err = self.run_cli("run", "--send")
        self.assertEqual(code, 0, err)
        cancelled = abi.EVENTS["Cancelled"][0]
        self.assertTrue(self.chain.topic_filters)
        self.assertTrue(all(cancelled not in f for f in self.chain.topic_filters))
        self.assertEqual(sent_ids(self.chain), [1])               # the real due bill is refunded
        st_ = self.state()
        self.assertEqual(st_["scannedTo"], 1_000_000)          # head at the start of the run        # cache advanced past the spam block
        self.assertEqual(st_["lost"], [])

    def test_overflow_even_per_topic_falls_back_to_getbill(self):
        # pessimistic node: the cap counts every log of the contract in the block, so even a single-topic,
        # single-block query for BillPaid fails at block 900 - and the real bill was paid in that block.
        self.spam_setup(real_paid_in_spam_block=True)
        self.chain.cap_counts_all_topics = True
        self.write_key()
        code, out, err = self.run_cli("run", "--send")
        self.assertEqual(code, 0, err)
        self.assertIn("block 900 holds more logs", err)
        self.assertEqual(sent_ids(self.chain), [1])                # still refunded, via getBill
        st_ = self.state()
        self.assertEqual(st_["scannedTo"], 1_000_000)          # head at the start of the run
        self.assertIn(["Paid", 900], st_["lost"])
        self.assertTrue(st_["degraded"])
        # next run: no crash, nothing left to do, bill 1 known as Refunded (terminal) from getBill
        self.chain.head += 10
        code, out, err = self.run_cli("run", "--send")
        self.assertEqual(code, 0, err)
        self.assertEqual(sent_ids(self.chain), [1])
        self.assertIn("due bills: 0", out)
        self.assertEqual(self.state()["terminal"].get("1"), abi.REFUNDED)

    def test_issued_overflow_seeds_from_getbill(self):
        c = self.chain
        c.max_results = 2_000
        for k in range(2_100):                                     # 2,100 BillIssued in one block
            c.issue(1 + k, 500, amount=1 if k else 9_000_000)
        c.pay(1, 600, payer=addr(0xF1), claim_by=c.head_ts - 1)
        self.write_key()
        code, out, err = self.run_cli("run", "--send")
        self.assertEqual(code, 0, err)
        self.assertIn("lost its Issued logs", err)
        self.assertEqual(sent_ids(self.chain), [1])
        st_ = self.state()
        self.assertEqual(len(st_["seeds"]), 2_100)
        self.assertFalse(st_["degraded"])                          # Issued loss alone is not degraded
        calls_before = len(self.chain.calls)
        code, out, err = self.run_cli("due")                       # seeds cached: no 2,100 getBill again
        self.assertEqual(code, 0, err)
        self.assertLess(len(self.chain.calls) - calls_before, 50)

    def test_scan_and_bill_show_cancelled_from_getbill(self):
        self.history()                                              # bill 2 is cancelled
        code, out, err = self.run_cli("scan")
        self.assertEqual(code, 0, err)
        self.assertIn("Cancelled=1", out)
        row2 = [l for l in out.splitlines() if l.strip().startswith("2 ")][0]
        self.assertIn("Cancelled", row2)
        self.assertNotIn("*getBill", row2)
        code, out, err = self.run_cli("bill", "2")
        self.assertEqual(code, 0, err)
        self.assertIn("cancelled: status from getBill", out)


class OverflowRpcTests(unittest.TestCase):
    def test_single_block_overflow_uses_callback_and_continues(self):
        c = FakeChain(head=5_000)
        c.max_results = 10
        for i in range(20):
            c.issue(1 + i, 3_000)
        c.issue(21, 4_000)
        seen = []
        logs = FakeRpc(c).get_logs_chunked(CONTRACT, [abi.KEEPER_TOPICS], 0, 5_000,
                                           on_overflow=lambda b, e: seen.append(b) or [])
        self.assertEqual(seen, [3_000])
        self.assertEqual([int(lg["topics"][1], 16) for lg in logs], [21])   # later blocks still read
        with self.assertRaisesRegex(RpcError, "even for one block"):
            FakeRpc(c).get_logs_chunked(CONTRACT, [abi.KEEPER_TOPICS], 0, 5_000)


class DustTests(Base):
    def dust_setup(self):
        c = self.chain
        ts = c.head_ts
        for i in range(1, 26):                                      # 25 one-unit bills, low ids, oldest
            c.issue(i, 100 + i, amount=1)
            c.pay(i, 200 + i, claim_by=ts - 10_000)
        c.issue(26, 300, amount=500_000_000); c.pay(26, 301, claim_by=ts - 5)      # 500 USDC
        c.issue(27, 302, amount=50_000); c.pay(27, 303, claim_by=ts - 50)         # exactly 0.05 USDC
        c.issue(28, 304, amount=500_000_000); c.pay(28, 305, claim_by=ts - 500)   # 500 USDC, waited longer

    def test_due_order_largest_first_then_oldest(self):
        self.dust_setup()
        bills = st.fold([abi.decode_log(lg) for lg in self.chain.logs])
        order = [b.id for b in st.due(bills, self.chain.head_ts)]
        self.assertEqual(order[:3], [28, 26, 27])                   # tie on 500 USDC -> older claimBy first
        self.assertEqual(order[3:], list(range(1, 26)))

    def test_min_amount_floor_skips_dust_without_simulating(self):
        self.dust_setup()
        self.write_key()
        code, out, err = self.run_cli("run", "--send", "--notify")
        self.assertEqual(code, 0, err)
        self.assertEqual(sent_ids(self.chain), [28, 26, 27])        # 0.05 USDC is at the floor: sent
        self.assertIn("bill 1  SKIP below keeper minimum (0.000001 < 0.050000 USDC) - payer can refund it themselves",
                      out)
        sims = {p[0]["data"] for m, p in self.chain.calls if m in ("eth_call", "eth_estimateGas")
                and p[0]["data"].startswith("0x" + abi.SEL_REFUND.hex())}
        self.assertEqual(sims, {abi.encode_refund(i) for i in (26, 27, 28)})   # dust never simulated or sent
        self.assertEqual(len(self.cards), 1)
        self.assertIn("아주 작은 청구서 25건", self.cards[0])
        self.assertEqual(notify.problems(self.cards[0], allow={"USDC"}), [])

    def test_dust_alone_sends_no_message(self):
        c = self.chain
        c.issue(1, 100, amount=10); c.pay(1, 101, claim_by=c.head_ts - 1)
        code, out, err = self.run_cli("run", "--notify")
        self.assertEqual(code, 0)
        self.assertEqual(self.cards, [])
        self.assertIn("1 below the 0.050000 USDC keeper minimum", out)

    def test_dust_cannot_push_real_refund_behind_max_sends(self):
        self.dust_setup()
        self.write_key()
        code, out, err = self.run_cli("run", "--send", "--min-amount", "0", "--max-sends", "2")
        self.assertEqual(code, 0, err)
        self.assertEqual(sent_ids(self.chain), [28, 26])            # real bills first, dust waits

    def test_min_amount_parsing(self):
        self.assertEqual(K.usdc_units("0.05"), 50_000)
        self.assertEqual(K.usdc_units("0"), 0)
        self.assertEqual(K.usdc_units("12.345678"), 12_345_678)
        for bad in ("0.0000001", "-1", "abc", "nan", "inf"):
            with self.assertRaises(argparse.ArgumentTypeError, msg=bad):
                K.usdc_units(bad)
        self.assertEqual(K.parse(["run"]).min_amount, 50_000)

    def test_card_with_full_body_still_counts_dust(self):
        sent = [{"id": 7, "amount": 1, "fee_wei": 1}]
        skipped = [{"id": 3, "reason": "revert"}]
        failed = [{"id": 4, "why": "x"}]
        c = notify.summary_card("mainnet", sent, skipped, failed, below_min=9)
        self.assertIn("소액 9건 제외", c)
        self.assertEqual(notify.problems(c, allow={"USDC"}), [])


class StateMigrationTests(unittest.TestCase):
    def test_old_state_keeps_inflight(self):
        import tempfile
        from pathlib import Path
        d = Path(tempfile.mkdtemp())
        p = d / "s.json"
        p.write_text(json.dumps({"version": 1, "chainId": 5042, "contract": CONTRACT, "fromBlock": 5,
                                 "scannedTo": 99, "logs": [], "inflight": {"3": {"tx": "0xab", "block": 9}},
                                 "notified": {}}))
        s = K.load_state(p, 5042, CONTRACT, 5)
        self.assertEqual(s["version"], K.STATE_VERSION)
        self.assertIsNone(s["scannedTo"])                           # logs re-read
        self.assertEqual(s["inflight"], {"3": {"tx": "0xab", "block": 9}})


if __name__ == "__main__":
    unittest.main()
