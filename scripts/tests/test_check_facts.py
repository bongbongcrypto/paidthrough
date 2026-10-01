"""Tests for scripts/check_facts.py on synthetic docs (no network, no gh, stdlib only).

Proves the check bites: a bumped or spelled-out wrong number fails, a missing required fact fails, and the --bait
routine catches a bump in temp copies.
"""
from __future__ import annotations

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import check_facts as cf  # noqa: E402

ADDR = "0x1234567890AbcdEF1234567890aBcdef12345678"
TX = "0x" + "ab" * 32

CI_LOG = "\n".join([
    "scripts\tUNKNOWN STEP\t2026-10-01T13:57:12.8Z Ran 18 tests in 0.215s",
    "keeper\tUNKNOWN STEP\t2026-10-01T13:57:17.9Z Ran 68 tests in 3.777s",
    "arc-fork\tUNKNOWN STEP\t2026-10-01T13:57:19.0Z Ran 1 test suite in 843.80ms (840.45ms CPU time): "
    "2 tests passed, 0 failed, 0 skipped (2 total tests)",
    "test\tUNKNOWN STEP\t2026-10-01T13:57:18.7Z | PaidThrough        | 5,009            | 5,246             | 19,567 |",
    "test\tUNKNOWN STEP\t2026-10-01T13:57:36.3Z Ran 6 test suites in 17.37s (23.99s CPU time): "
    "138 tests passed, 0 failed, 2 skipped (140 total tests)",
    "test\tUNKNOWN STEP\t2026-10-01T13:57:39.4Z Ran 4 test suites in 2.97s (2.62s CPU time): "
    "136 tests passed, 0 failed, 0 skipped (136 total tests)",
])

REHEARSAL_MD = """## Arc mainnet real-node rehearsal

Block 23738783 (time 1790872718), base fee 20 gwei.

| Check | OK | Detail |
|---|---|---|
| chain id | yes | 5042 |

| # | Step | Expected | Result | Match | estimateGas | USDC @20 gwei | Post-state (probe) | Note |
|---|---|---|---|---|---|---|---|---|
| 1 | deploy | ok | ok | yes | 1 | 0 |  |  |
| 2 | claim to a blocklisted payee | Blocked address | revert: Blocked address | yes | - | - |  |  |
| 3 | decline sent by a blocklisted payee | (observe) | revert: Blocked address | info | - | - |  |  |
"""

GOOD_README = """All 37 steps matched their expected outcome (`status/rehearsal-2026-10-02.md`, block 23,738,783).
138 contract tests: 65 unit, 32 signature, 26 blocklist, 11 fuzz tests. The keeper has 68 tests; the deploy and
rehearsal scripts have 29 script tests. Two independent reviews found eight test gaps; runs every 5 minutes.
| `contracts/` | `PaidThrough.sol` (5,009-byte runtime, no libraries) |
| `keeper/` | Python refund keeper: runs every 5 minutes, alerts on failure (68 tests) |
Deployed: https://explorer.arc.io/address/%s (tx %s). USDC: https://explorer.arc.io/address/%s
""" % (ADDR, TX, cf.USDC)

GOOD_SUBMISSION = """| 6 | address | https://explorer.arc.io/address/%s |
| 12 | more | 138 contract tests in CI, a 37-step read-only rehearsal (`status/rehearsal-2026-10-02.md`). |
""" % ADDR.lower()

TRUTH = {
    "contract_tests": 138, "keeper_tests": 68, "scripts_tests": 29, "runtime_size": 5009, "initcode_size": 5246,
    "rehearsal": {"file": "rehearsal-2026-10-02.md", "steps": 37, "matched": 37, "block": 23738783},
    "deployment": {"address": ADDR, "fromBlock": 1, "txHash": TX},
}


def run(truth=None, readme=GOOD_README, submission=GOOD_SUBMISSION):
    facts = cf.build_facts(truth or TRUTH)
    return cf.check(facts, {"README.md": readme, "SUBMISSION.md": submission})[1]


def names(problems):
    return sorted({p.split(":", 1)[0] for p in problems})


class SourcesTest(unittest.TestCase):
    def test_ci_log_counts_and_sizes(self):
        got = cf.parse_ci_log(CI_LOG)
        self.assertEqual(got["contract_tests"], 138)  # the full run, not the 136 gas-report subset or arc-fork's 2
        self.assertEqual((got["keeper_tests"], got["scripts_tests"]), (68, 18))
        self.assertEqual((got["runtime_size"], got["initcode_size"]), (5009, 5246))

    def test_rehearsal_rows_and_matched(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "rehearsal-2026-10-02.md"
            p.write_text(REHEARSAL_MD, encoding="utf-8")
            got = cf.parse_rehearsal(p)
        self.assertEqual(got, {"file": "rehearsal-2026-10-02.md", "steps": 3, "matched": 2, "block": 23738783})

    def test_words_to_int(self):
        self.assertEqual(cf.to_int("one hundred thirty-eight"), 138)
        self.assertEqual(cf.to_int("thirty-seven"), 37)
        self.assertEqual(cf.to_int("one hundred and eighteen"), 118)
        self.assertEqual(cf.to_int("5,009"), 5009)


class CheckTest(unittest.TestCase):
    def test_consistent_docs_pass(self):
        self.assertEqual(run(), [])

    def test_bumped_test_count_fails(self):
        problems = run(readme=GOOD_README.replace("138 contract tests", "139 contract tests"))
        self.assertEqual(names(problems), ["contract_tests"])
        self.assertTrue(any("139" in p for p in problems))

    def test_bumped_count_in_one_doc_still_fails(self):
        # 138 still appears in the other doc, so only the conflict fires; that is enough to fail.
        problems = run(submission=GOOD_SUBMISSION.replace("138 contract", "136 contract"))
        self.assertEqual(names(problems), ["contract_tests"])

    def test_spelled_out_numbers(self):
        spelled = GOOD_README.replace("138 contract tests", "one hundred thirty-eight contract tests")
        self.assertEqual(run(readme=spelled), [])
        wrong = GOOD_README.replace("All 37 steps matched", "All thirty-six steps matched")
        self.assertIn("rehearsal_steps", names(run(readme=wrong)))

    def test_all_steps_matched_while_fewer_matched_fails(self):
        truth = dict(TRUTH, rehearsal=dict(TRUTH["rehearsal"], matched=31))
        problems = run(truth=truth)
        self.assertEqual(names(problems), ["rehearsal_matched"])
        self.assertTrue(any("'37 steps matched' gives 37" in p for p in problems))

    def test_honest_partial_match_wording_passes(self):
        truth = dict(TRUTH, rehearsal=dict(TRUTH["rehearsal"], matched=31))
        for wording in ("31 of 37 steps matched", "31/37 asserted steps matched"):
            readme = GOOD_README.replace("All 37 steps matched", wording)
            self.assertEqual(run(truth=truth, readme=readme), [], wording)
        readme = GOOD_README.replace("All 37 steps matched", "All 37 rehearsal steps matched")
        self.assertEqual(names(run(truth=truth, readme=readme)), ["rehearsal_matched"])

    def test_missing_required_fact_fails(self):
        problems = run(readme=GOOD_README.replace("29 script tests", "script tests"))
        self.assertEqual(names(problems), ["scripts_tests"])
        self.assertTrue(any("appears 0 times" in p for p in problems))

    def test_guard_only_fact_may_be_absent_but_not_wrong(self):
        self.assertEqual(run(), [])  # initcode size is not in the docs: fine
        problems = run(readme=GOOD_README + "\nInitcode size 5,000 bytes.\n")
        self.assertEqual(names(problems), ["initcode_size"])

    def test_stale_rehearsal_file_and_block_fail(self):
        stale = GOOD_README.replace("rehearsal-2026-10-02.md`, block 23,738,783",
                                    "rehearsal-2026-10-01.md`, block 23,641,690")
        self.assertEqual(names(run(readme=stale)), ["rehearsal_block", "rehearsal_file"])

    def test_unrelated_numbers_are_ignored(self):
        # eight test gaps, 11 fuzz tests, 5 minutes, two reviews, 65 unit: none sits in a registered context
        self.assertEqual(run(), [])

    def test_deploy_address_required_in_each_doc(self):
        problems = run(submission=GOOD_SUBMISSION.replace(ADDR.lower(), "<PaidThrough address>"))
        self.assertEqual(names(problems), ["deploy_address"])
        other = "0x" + "9" * 40
        problems = run(readme=GOOD_README.replace("address/" + ADDR, "address/" + other, 1))
        self.assertEqual(names(problems), ["deploy_address"])

    def test_not_deployed_registers_no_address(self):
        truth = dict(TRUTH, deployment=None)
        facts = cf.build_facts(truth)
        self.assertNotIn("deploy_address", [f.name for f in facts])
        self.assertEqual(run(truth=truth), [])


class MainTest(unittest.TestCase):
    """The CLI wiring, offline: counts by flag, rehearsal/artifact/deployments from temp files."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        rows = "".join(f"| {i} | step {i} | ok | ok | yes | - | - |  |  |\n" for i in range(1, 38))
        header = "| # | Step | Expected | Result | Match | estimateGas | USDC @20 gwei | Post-state (probe) | Note |\n"
        (t / "status").mkdir()
        (t / "status" / "rehearsal-2026-10-01.md").write_text("Block 1\n", encoding="utf-8")
        (t / "status" / "rehearsal-2026-10-02.md").write_text(
            "Block 23738783 (time 0)\n\n" + header + "|---|\n" + rows, encoding="utf-8")
        art = t / "out" / "PaidThrough.sol"
        art.mkdir(parents=True)
        art.joinpath("PaidThrough.json").write_text(
            '{"deployedBytecode": {"object": "0x%s"}, "bytecode": {"object": "0x%s"}}' % ("00" * 5009, "00" * 5246),
            encoding="utf-8")
        (t / "deployments.json").write_text(
            '{"mainnet": {"address": "%s", "fromBlock": 1, "txHash": "%s"}, "testnet": null}' % (ADDR, TX),
            encoding="utf-8")
        (t / "README.md").write_text(GOOD_README, encoding="utf-8")
        (t / "SUBMISSION.md").write_text(GOOD_SUBMISSION, encoding="utf-8")
        self.args = ["--docs", str(t / "README.md"), str(t / "SUBMISSION.md"), "--contract-tests", "138",
                     "--keeper-tests", "68", "--scripts-tests", "29", "--out", str(t / "out"),
                     "--status-dir", str(t / "status"), "--deployments", str(t / "deployments.json")]

    def tearDown(self):
        self.tmp.cleanup()

    def main(self, *extra):
        out = io.StringIO()
        with redirect_stdout(out):
            code = cf.main(self.args + list(extra))
        return code, out.getvalue()

    def test_consistent_tree_passes(self):
        code, out = self.main()
        self.assertEqual(code, 0, out)
        self.assertIn("FACTS: PASS - 11 facts, 0 problem(s)", out)

    def test_wrong_flag_value_fails(self):
        code, out = self.main("--keeper-tests", "70")
        self.assertEqual(code, 1)
        self.assertIn("PROBLEM keeper_tests:", out)

    def test_no_rehearsal_is_a_problem(self):
        code, out = self.main("--status-dir", str(Path(self.tmp.name) / "nowhere"))
        self.assertEqual(code, 1)
        self.assertIn("PROBLEM rehearsal_steps: no source of truth found", out)

    def test_bait_cli(self):
        code, out = self.main("--bait")
        self.assertEqual(code, 0, out)
        self.assertIn("BAIT: 10/10 bumped facts caught", out)  # 7 numbers + rehearsal file, address, tx hash
        self.assertIn("[bit] rehearsal_file: 'rehearsal-2026-10-02.md' -> 'rehearsal-2026-10-03.md'", out)
        self.assertIn("[bit] deploy_tx:", out)


class BaitTest(unittest.TestCase):
    def test_bait_catches_every_bump(self):
        with tempfile.TemporaryDirectory() as tmp:
            r, s = Path(tmp) / "README.md", Path(tmp) / "SUBMISSION.md"
            r.write_text(GOOD_README, encoding="utf-8")
            s.write_text(GOOD_SUBMISSION, encoding="utf-8")
            out = io.StringIO()
            with redirect_stdout(out):
                code = cf.bait(cf.build_facts(TRUTH), [r, s])
            self.assertEqual(r.read_text(encoding="utf-8"), GOOD_README, "bait must not touch the real docs")
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn("[bit] contract_tests: '138' -> '139'", out.getvalue())
        self.assertIn("[bit] runtime_size: '5,009' -> '5,010'", out.getvalue())
        self.assertNotIn("MISSED", out.getvalue())

    def test_bait_fails_when_nothing_to_bump(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = Path(tmp) / "README.md"
            r.write_text("no numbers here\n", encoding="utf-8")
            with redirect_stdout(io.StringIO()):
                self.assertEqual(cf.bait(cf.build_facts(TRUTH), [r]), 1)


if __name__ == "__main__":
    unittest.main()
