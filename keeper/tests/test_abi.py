import unittest

import kpath  # noqa: F401
from eth_abi import encode
from eth_hash.auto import keccak as eth_hash_keccak

import abi
from fakechain import FakeChain, addr


class AbiTest(unittest.TestCase):
    def test_keccak_known_selector(self):
        # ERC-20 transfer is a fixed, well-known selector: proves the keccak (not sha3-256) in use.
        self.assertEqual(abi.selector("transfer(address,uint256)").hex(), "a9059cbb")

    def test_selectors_match_second_keccak_implementation(self):
        for sig, sel in ((abi.SIG_REFUND, abi.SEL_REFUND), (abi.SIG_GET_BILL, abi.SEL_GET_BILL),
                         (abi.SIG_BILL_COUNT, abi.SEL_BILL_COUNT)):
            self.assertEqual(sel, eth_hash_keccak(sig.encode())[:4], sig)
        self.assertEqual(abi.SEL_REFUND.hex(), "278ecde1")

    def test_event_topics_from_spec_signatures(self):
        sigs = {"Issued": "BillIssued(uint256,address,address,uint96,uint64,uint32,bytes32)",
                "Cancelled": "BillCancelled(uint256)", "Paid": "BillPaid(uint256,address,uint64)",
                "Claimed": "BillClaimed(uint256,address,uint96)", "Declined": "BillDeclined(uint256,address,uint96)",
                "Refunded": "BillRefunded(uint256,address,uint96,address)"}
        for name, sig in sigs.items():
            self.assertEqual(abi.EVENTS[name][0], "0x" + eth_hash_keccak(sig.encode()).hex(), name)
        self.assertEqual(len(set(abi.ALL_TOPICS)), 6)

    def test_status_enum_order(self):
        self.assertEqual(abi.STATUS_NAMES,
                         ("None", "Open", "Paid", "Claimed", "Refunded", "Declined", "Cancelled"))

    def test_get_bill_decode_static_tuple(self):
        payee, payer, allowed = addr(1), addr(2), addr(3)
        word = encode([abi.BILL_TUPLE], [(payee, 2 ** 96 - 1, payer, 2 ** 64 - 1, 3600, allowed, 1_800_000_000,
                                          abi.REFUNDED, b"\xab" * 32)])
        self.assertEqual(len(word), 288)   # 9 inline words, no offset
        b = abi.decode_bill(5, "0x" + word.hex())
        self.assertEqual((b.payee, b.payer, b.allowedPayer), (payee, payer, allowed))
        self.assertEqual(b.amount, 2 ** 96 - 1)
        self.assertEqual(b.status_name, "Refunded")
        self.assertEqual(b.ref, "0x" + "ab" * 32)
        with self.assertRaises(abi.DecodeError):
            abi.decode_bill(5, "0x" + word.hex()[:-64])

    def test_decode_each_event(self):
        c = FakeChain()
        c.issue(1, 10, amount=1_234_567, claim_window=7200, allowed=addr(9))
        c.pay(1, 11, payer=addr(9), claim_by=c.head_ts + 5)
        c.claim(1, 12)
        c.issue(2, 13)
        c.cancel(2, 14)
        c.issue(3, 15)
        c.pay(3, 16)
        c.decline(3, 17)
        c.issue(4, 18)
        c.pay(4, 19)
        c.refund(4, 20, caller=addr(77))
        evs = [abi.decode_log(lg) for lg in c.logs]
        self.assertEqual([e["event"] for e in evs], ["Issued", "Paid", "Claimed", "Issued", "Cancelled", "Issued",
                                                     "Paid", "Declined", "Issued", "Paid", "Refunded"])
        self.assertEqual(evs[0]["amount"], 1_234_567)
        self.assertEqual(evs[0]["claimWindow"], 7200)
        self.assertEqual(evs[0]["allowedPayer"], addr(9))
        self.assertEqual(evs[1]["claimBy"], c.head_ts + 5)
        self.assertEqual(evs[2]["payee"], addr(0xA11CE))
        self.assertEqual(evs[-1]["caller"], addr(77))
        self.assertEqual(evs[-1]["billId"], 4)

    def test_decode_rejects_wrong_shape(self):
        c = FakeChain()
        c.issue(1, 10)
        lg = dict(c.logs[0])
        lg["data"] = lg["data"][:-64]
        with self.assertRaises(abi.DecodeError):
            abi.decode_log(lg)
        lg = dict(c.logs[0])
        lg["topics"] = lg["topics"][:2]
        with self.assertRaises(abi.DecodeError):
            abi.decode_log(lg)

    def test_refund_calldata(self):
        self.assertEqual(abi.encode_refund(7), "0x278ecde1" + "0" * 63 + "7")
        self.assertTrue(abi.is_refund_calldata(abi.encode_refund(7), 7))
        self.assertFalse(abi.is_refund_calldata(abi.encode_refund(7), 8))

    def test_decode_revert(self):
        self.assertEqual(abi.decode_revert("0x" + abi.selector("ClaimWindowOpen()").hex()), "ClaimWindowOpen")
        # real Arc shape (USDC revert, probed 2026-10-01)
        data = "0x08c379a0" + encode(["string"], ["Blacklistable: account is blacklisted"]).hex()
        self.assertIn("blacklisted", abi.decode_revert(data))
        self.assertTrue(abi.decode_revert("0xdeadbeef").startswith("custom error"))
        self.assertEqual(abi.decode_revert(None), "reverted (no reason)")


if __name__ == "__main__":
    unittest.main()
