"""Server run shape: the per-run time budget, state-file write failures and the systemd unit."""
import contextlib
import errno
import io
import json
import re
import time
import unittest

import kpath  # noqa: F401

import abi
import notify
import paidthrough_keeper as K
from fakechain import CONTRACT, FakeRpc
from test_keeper import Base

INSTALL = "/home/ubuntu/bots/paidthrough-keeper"
UNIT = kpath.KEEPER / "systemd" / "paidthrough-keeper.service"
TIMER = kpath.KEEPER / "systemd" / "paidthrough-keeper.timer"
GET_BILL = abi.SEL_GET_BILL.hex()


class SlowRpc(FakeRpc):
    """Every call costs `per_call` seconds: on a shared fake clock `now` ([t]), or for real with time.sleep."""

    def __init__(self, chain, per_call, now=None):
        super().__init__(chain)
        self.per_call = per_call
        self.now = now
        self.log = []          # (clock at call start, method, params)

    def call(self, method, params):
        self.log.append((self.now[0] if self.now is not None else time.monotonic(), method, params))
        if self.now is not None:
            self.now[0] += self.per_call
        else:
            time.sleep(self.per_call)
        return super().call(method, params)


class TimeBudgetTests(Base):
    N = 10

    def many_due(self, n=None):
        """n due bills of 150 USDC near the chain head, so the log scan is a single getLogs call."""
        self.dep.write_text(json.dumps({"mainnet": None, "testnet": {"address": CONTRACT, "fromBlock": 999_000}}))
        c = self.chain
        for i in range(1, (n or self.N) + 1):
            c.issue(i, 999_100 + 2 * i)
            c.pay(i, 999_101 + 2 * i, claim_by=c.head_ts - 10 * i)

    def main(self, rpc, *argv, clock=None):
        out, err = io.StringIO(), io.StringIO()
        kw = {"clock": clock} if clock else {}
        code = K.main(["--network", "testnet"] + list(argv), rpc_factory=lambda url: rpc, out=out, err=err,
                      notify_sender=self.capture_card, sleep=lambda s: None, deployments=self.dep, **kw)
        return code, out.getvalue(), err.getvalue()

    def saved(self):
        files = list((self.tmp / "state").glob("*.json"))
        self.assertEqual(len(files), 1)
        return json.loads(files[0].read_text())

    def test_slow_rpc_stops_starting_sends_at_the_budget(self):
        self.many_due()
        self.write_key()
        now = [0.0]
        rpc = SlowRpc(self.chain, 3.0, now)
        code, out, err = self.main(rpc, "run", "--send", "--notify", "--time-budget", "60", clock=lambda: now[0])
        self.assertEqual(code, 0, err)
        # 3 s per call. Reads: chainId, getCode, getBlock, getLogs, billCount, maxPriorityFee = 6 calls (18 s).
        # Each refund: getBill, eth_call, estimateGas, getBalance, getTransactionCount, send, receipt = 7 calls
        # (21 s). Refunds start at 18 and 39; the check at 60 stops the rest. A new read call shifts these.
        self.assertEqual(len(self.chain.sent), 2)
        starts = [t for t, m, p in rpc.log if m == "eth_call" and p[0]["data"][2:10] == GET_BILL and p[1] == "latest"]
        sends = [t for t, m, p in rpc.log if m == "eth_sendRawTransaction"]
        self.assertEqual(len(starts), 2)
        self.assertTrue(all(t < 60 for t in starts + sends))            # nothing started after the deadline
        self.assertIn("time budget (60s) used up", out)
        self.assertEqual(out.count("SKIP time_budget"), self.N - 2)
        self.assertEqual(len(self.cards), 1)
        self.assertIn("2건 완료", self.cards[0])
        self.assertIn(notify.SKIP_KO["time_budget"], self.cards[0])
        self.assertEqual(notify.problems(self.cards[0], allow={"USDC"}), [])
        state = self.saved()
        self.assertEqual(state["inflight"], {})
        self.assertEqual(sum(k.endswith(":time_budget") for k in state["notified"]), self.N - 2)
        # the next run continues where this one stopped
        now[0] = 0.0
        code, out, err = self.main(rpc, "run", "--send", "--time-budget", "60", clock=lambda: now[0])
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.chain.sent), 4)

    def test_dry_run_also_stops_planning_at_the_budget(self):
        self.many_due()
        now = [0.0]
        rpc = SlowRpc(self.chain, 3.0, now)
        code, out, err = self.main(rpc, "run", "--time-budget", "20", clock=lambda: now[0])
        self.assertEqual(code, 0, err)
        self.assertEqual(out.count(" refund 150.000000 USDC"), 1)     # one plan started at 18 s, none at 27 s
        self.assertEqual(out.count("SKIP time_budget"), self.N - 1)
        self.assertFalse(any(m == "eth_sendRawTransaction" for _, m, _ in rpc.log))

    def test_real_clock_with_sleeping_rpc(self):
        # default clock (time.monotonic), real sleeps: the run ends near the budget, not after all bills
        n = 30
        self.many_due(n)
        self.write_key()
        rpc = SlowRpc(self.chain, 0.01)
        t0 = time.monotonic()
        code, out, err = self.main(rpc, "run", "--send", "--time-budget", "0.3")
        took = time.monotonic() - t0
        self.assertEqual(code, 0, err)
        self.assertLess(len(self.chain.sent), n)
        self.assertIn("SKIP time_budget", out)
        self.assertLess(took, 5.0)
        self.assertEqual(self.saved()["inflight"], {})

    def test_budget_flag_validation(self):
        self.assertEqual(K.parse(["run"]).time_budget, K.DEFAULT_TIME_BUDGET)
        self.assertEqual(K.parse(["run", "--time-budget", "2.5"]).time_budget, 2.5)
        for bad in ("0", "-5", "nan", "inf", "soon"):
            with self.assertRaises(SystemExit, msg=bad), contextlib.redirect_stderr(io.StringIO()):
                K.parse(["run", "--time-budget", bad])


class HookRpc(FakeRpc):
    def __init__(self, chain, on_call):
        super().__init__(chain)
        self.on_call = on_call

    def call(self, method, params):
        self.on_call(method)
        return super().call(method, params)


class StateSaveTests(Base):
    def blocked_dir(self):
        f = self.tmp / "a-regular-file"
        f.write_text("not a directory")
        return str(f / "state")      # mkdir fails: FileExistsError on Windows, NotADirectoryError on Linux

    def test_dry_run_reports_unwritable_state_with_card(self):
        self.history()
        code, out, err = self.run_cli("--state-dir", self.blocked_dir(), "run", "--notify")
        self.assertEqual(code, 1)
        self.assertIn("could not save the keeper state file", err)
        self.assertNotIn("Traceback", err)
        self.assertIn(abi.encode_refund(7), out)          # the dry-run plan itself still ran
        self.assertEqual(len(self.cards), 1)
        self.assertIn("키퍼 기록 저장 실패", self.cards[0])
        self.assertEqual(notify.problems(self.cards[0], allow={"USDC"}), [])

    def test_send_refused_before_any_chain_call(self):
        self.history()
        self.write_key()
        code, out, err = self.run_cli("--state-dir", self.blocked_dir(), "run", "--send", "--notify")
        self.assertEqual(code, 2)
        self.assertIn("--send refused: cannot write the keeper state file", err)
        self.assertEqual(self.chain.calls, [])
        self.assertEqual(self.chain.sent, [])
        self.assertEqual(len(self.cards), 1)
        self.assertIn("하나도 보내지 않았습니다", self.cards[0])

    def test_save_failure_after_a_send_stops_sending_and_reports(self):
        self.history()
        self.write_key()
        real = K.save_state
        n = [0]

        def flaky(path, data):
            n[0] += 1
            if n[0] == 1:            # the pre-flight check passes, then the disk goes read-only
                return real(path, data)
            raise OSError(errno.EROFS, "Read-only file system", str(path))
        K.save_state = flaky
        self.addCleanup(setattr, K, "save_state", real)
        code, out, err = self.run_cli("run", "--send", "--notify")
        self.assertEqual(code, 1)
        self.assertEqual(len(self.chain.sent), 1)         # bill 7 sent; bill 9 not started without a saved record
        self.assertIn("in-flight record NOT saved", out)
        self.assertIn("bill 9  SKIP state_unsaved", out)
        self.assertIn("could not save the keeper state file", err)
        self.assertEqual(len(self.cards), 1)
        self.assertIn("환불 1건, 합계 150.000000 USDC를 보냈습니다", self.cards[0])
        self.assertEqual(notify.problems(self.cards[0], allow={"USDC"}), [])

    def test_inflight_record_is_on_disk_before_the_receipt_wait(self):
        self.history()
        self.write_key()
        seen = []

        def on_call(method):
            if method == "eth_getTransactionReceipt":
                files = list((self.tmp / "state").glob("*.json"))
                seen.append(json.loads(files[0].read_text())["inflight"] if files else None)
        self.factory = lambda url: HookRpc(self.chain, on_call)
        code, out, err = self.run_cli("run", "--send")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(seen), 2)
        self.assertEqual(list(seen[0]), ["7"])
        self.assertEqual(list(seen[1]), ["9"])

    def test_scan_with_unwritable_cache_still_answers(self):
        self.history()
        code, out, err = self.run_cli("--state-dir", self.blocked_dir(), "scan")
        self.assertEqual(code, 0)
        self.assertIn("9 bills", out)
        self.assertIn("warning: scan cache not saved", err)

    def test_state_cards_are_readable(self):
        sent = [{"id": 7, "amount": 150_000_000, "fee_wei": 10 ** 15}]
        failed = [{"id": i, "why": "no receipt"} for i in range(100, 108)]
        for c in (K.state_card("mainnet", refused=True),
                  K.state_card("mainnet", {"sent": sent, "failed": failed}),
                  K.state_card("testnet", {"sent": [], "failed": failed[:1]}),
                  K.state_card("mainnet", {})):
            self.assertEqual(notify.problems(c, allow={"USDC"}), [], c)
            self.assertTrue(c.rstrip().endswith("#paidthrough"))


def service(text: str) -> dict:
    """[Service] key -> list of values (repeated keys such as Environment= kept)."""
    sec, out = None, {}
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        m = re.fullmatch(r"\[(\w+)\]", s)
        if m:
            sec = m.group(1)
            continue
        if sec == "Service":
            k, v = s.split("=", 1)
            out.setdefault(k.strip(), []).append(v.strip())
    return out


class SystemdUnitTests(unittest.TestCase):
    def setUp(self):
        self.text = UNIT.read_text(encoding="utf-8")
        self.svc = service(self.text)

    def test_single_install_directory(self):
        self.assertEqual(self.svc["WorkingDirectory"], [INSTALL])
        self.assertEqual(self.svc["ExecStart"],
                         [INSTALL + "/venv/bin/python keeper/paidthrough_keeper.py run --send --notify --network mainnet"])
        self.assertEqual(kpath.KEEPER.name, "keeper")                     # the repo's keeper/ is copied as keeper/
        self.assertTrue((kpath.KEEPER / "paidthrough_keeper.py").is_file())
        args = K.parse(self.svc["ExecStart"][0].split()[2:])
        self.assertEqual((args.cmd, args.send, args.notify, args.network), ("run", True, True, "mainnet"))
        self.assertEqual(self.svc["Type"], ["oneshot"])
        self.assertEqual(self.svc["MemoryMax"], ["200M"])

    def test_state_directory_is_writable_inside_the_sandbox(self):
        env = dict(v.split("=", 1) for v in self.svc["Environment"])
        self.assertIn(env["PAIDTHROUGH_KEEPER_STATE"], self.svc["ReadWritePaths"][0].split())
        self.assertEqual(self.svc["ProtectSystem"], ["strict"])
        self.assertTrue(env["PAIDTHROUGH_KEEPER_ENV"].startswith("/home/ubuntu/."))   # key outside the install dir
        self.assertFalse(env["PAIDTHROUGH_KEEPER_ENV"].startswith(INSTALL))

    def test_timeout_is_above_the_worst_case_run(self):
        args = K.parse(self.svc["ExecStart"][0].split()[2:])            # flags added to ExecStart count too
        worst = K.worst_case_seconds(args.time_budget, args.receipt_timeout, args.poll)
        self.assertGreater(int(self.svc["TimeoutStartSec"][0]), worst)
        self.assertIn("= %d s" % worst, self.text)                       # the comment's arithmetic is current
        self.assertIn("OnUnitActiveSec=5min", TIMER.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
