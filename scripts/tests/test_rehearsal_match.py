"""Tests for the step matching of scripts/rehearse_mainnet.py with a fake node (no network, no artifacts).

The fake answers eth_call with a chosen success or revert and eth_estimateGas with a fixed gas, so the same
`Rehearsal.step()` path CI runs decides 'yes' or 'NO'. A wrong expected string must give 'NO' and fail the run.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from eth_abi import encode  # noqa: E402

import rehearse_mainnet as rm  # noqa: E402

PT = "0x00000000000000000000000000000000000000aa"
CALLER = type("Caller", (), {"address": "0x00000000000000000000000000000000000000bb"})()
ERROR_STRING = "0x08c379a0"  # selector of Error(string)


def revert_string(msg: str) -> dict:
    data = ERROR_STRING + encode(["string"], [msg]).hex()
    return {"error": {"code": 3, "message": "execution reverted", "data": data}}


def revert_custom(name: str) -> dict:
    return {"error": {"code": 3, "message": "execution reverted", "data": "0x" + rm.sel(name + "()").hex()}}


class FakeRpc:
    """Answers eth_call with `call_answer` and eth_estimateGas with 21,000 gas; records every request."""

    def __init__(self, call_answer: dict):
        self.call_answer = call_answer
        self.requests: list[tuple[str, list]] = []
        self.calls = 0

    def raw(self, method: str, params: list) -> dict:
        self.requests.append((method, params))
        self.calls += 1
        if method == "eth_call":
            return self.call_answer
        if method == "eth_estimateGas":
            return {"result": hex(21_000)}
        raise AssertionError(f"unexpected RPC method {method}")


def rehearsal(call_answer: dict, custom_errors=("ClaimWindowClosed",)) -> rm.Rehearsal:
    """A Rehearsal with only what step() needs; __init__ (artifacts, live node) is skipped on purpose."""
    r = rm.Rehearsal.__new__(rm.Rehearsal)
    r.rpc = FakeRpc(call_answer)
    r.block_tag = "0x1"
    r.pt = PT
    r.errors = {rm.sel(n + "()"): n for n in custom_errors}
    r.steps = []
    r.checks = [{"check": "chain id", "ok": True, "detail": "5042", "required": True}]
    return r


class StepMatchTest(unittest.TestCase):
    def test_right_revert_string_matches(self):
        r = rehearsal(revert_string("Blocked address"))
        r.step("claim to a blocklisted payee", CALLER, "0x", {}, rm.BLOCKED)
        self.assertEqual(r.steps[-1]["match"], "yes")
        self.assertEqual(r.steps[-1]["result"], "revert: Blocked address")
        self.assertTrue(r.passed())
        ok, line = rm.verdict(r.steps, r.checks)
        self.assertTrue(ok)
        self.assertIn("1/1 asserted steps matched, 0 mismatched", line)

    def test_wrong_revert_string_is_no_and_fails_the_run(self):
        r = rehearsal(revert_string("Blocked address"))
        r.step("claim to a blocklisted payee", CALLER, "0x", {}, "NotPayee")
        self.assertEqual(r.steps[-1]["match"], "NO")
        self.assertFalse(r.passed())
        ok, line = rm.verdict(r.steps, r.checks)
        self.assertFalse(ok)
        self.assertTrue(line.startswith("VERDICT: FAIL"), line)
        self.assertIn("0/1 asserted steps matched, 1 mismatched", line)

    def test_one_wrong_step_among_right_ones_fails_the_run(self):
        r = rehearsal(revert_string("FiatTokenV2: invalid signature"))
        r.step("signature for bill 1 used on bill 2", CALLER, "0x", {}, "invalid signature")
        r.step("same, wrong expectation", CALLER, "0x", {}, "caller must be the payee")
        self.assertEqual([s["match"] for s in r.steps], ["yes", "NO"])
        self.assertFalse(r.passed())

    def test_custom_error_is_decoded_and_matched(self):
        r = rehearsal(revert_custom("ClaimWindowClosed"))
        r.step("claim at claimBy", CALLER, "0x", {}, "ClaimWindowClosed")
        self.assertEqual(r.steps[-1]["match"], "yes")
        r.step("claim at claimBy, wrong error", CALLER, "0x", {}, "ClaimWindowOpen")
        self.assertEqual(r.steps[-1]["match"], "NO")
        self.assertFalse(r.passed())

    def test_ok_expected_and_call_succeeds(self):
        r = rehearsal({"result": "0x"})
        r.step("claim by the payee while the payer is blocklisted", CALLER, "0x", {}, "ok")
        self.assertEqual(r.steps[-1]["match"], "yes")
        self.assertEqual(r.steps[-1]["gas"], 21_000)
        self.assertEqual([m for m, _ in r.rpc.requests], ["eth_call", "eth_estimateGas"])
        self.assertTrue(r.passed())

    def test_ok_expected_but_call_reverts_is_no(self):
        r = rehearsal(revert_string("Blocked address"))
        r.step("claim by the payee while the payer is blocklisted", CALLER, "0x", {}, "ok")
        self.assertEqual(r.steps[-1]["match"], "NO")
        self.assertFalse(r.passed())

    def test_revert_expected_but_call_succeeds_is_no(self):
        r = rehearsal({"result": "0x"})
        r.step("claim to a blocklisted payee", CALLER, "0x", {}, rm.BLOCKED)
        self.assertEqual(r.steps[-1]["match"], "NO")
        self.assertFalse(r.passed())

    def test_observe_only_and_any_revert_are_refused(self):
        for bad in (None, "", "revert", "REVERT", "(observe)"):
            r = rehearsal(revert_string("Blocked address"))
            with self.assertRaises(ValueError, msg=repr(bad)):
                r.step("observe", CALLER, "0x", {}, bad)
            self.assertEqual(r.rpc.requests, [], "no RPC request before the expectation is checked")
            self.assertEqual(r.steps, [])

    def test_failed_required_check_fails_the_run(self):
        r = rehearsal(revert_string("Blocked address"))
        r.step("claim to a blocklisted payee", CALLER, "0x", {}, rm.BLOCKED)
        r.checks.append({"check": "blocklist slot", "ok": False, "detail": "", "required": True})
        self.assertFalse(r.passed())

    def test_skips_are_not_counted_as_matched(self):
        r = rehearsal(revert_string("Blocked address"))
        r.step("claim to a blocklisted payee", CALLER, "0x", {}, rm.BLOCKED)
        r.skip("real blocklisted address", "none blocklisted", fail=False)
        ok, line = rm.verdict(r.steps, r.checks)
        self.assertTrue(ok)
        self.assertIn("1/1 asserted steps matched, 0 mismatched, 1 skipped", line)
        r.skip("blocklist cases", "slot assumption failed")  # fail=True: a missing asserted step
        self.assertFalse(r.passed())

    def test_markdown_ends_with_the_verdict(self):
        r = rehearsal(revert_string("Blocked address"))
        r.block_number, r.now, r.base_fee = 1, 0, 20 * 10**9
        r.step("claim to a blocklisted payee", CALLER, "0x", {}, rm.BLOCKED)
        md = r.markdown()
        self.assertIn("| 1 | claim to a blocklisted payee | Blocked address | revert: Blocked address | yes |", md)
        self.assertEqual(md.strip().splitlines()[-1], rm.verdict(r.steps, r.checks)[1])


if __name__ == "__main__":
    unittest.main()
