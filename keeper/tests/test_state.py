import random
import unittest

import kpath  # noqa: F401

import abi
import state as st
from fakechain import FakeChain, addr


def decoded(chain):
    return [abi.decode_log(lg) for lg in chain.logs]


class StateTest(unittest.TestCase):
    def full_history(self):
        c = FakeChain()
        ts = c.head_ts
        c.issue(1, 100)                         # stays Open
        c.issue(2, 101); c.cancel(2, 102)       # Cancelled
        c.issue(3, 103); c.pay(3, 104, claim_by=ts + 100)                   # Paid, not due
        c.issue(4, 105); c.pay(4, 106, claim_by=ts - 1); c.claim(4, 107)    # Claimed
        c.issue(5, 108); c.pay(5, 109, claim_by=ts - 1); c.decline(5, 110)  # Declined
        c.issue(6, 111); c.pay(6, 112, claim_by=ts - 1); c.refund(6, 113)   # Refunded
        c.issue(7, 114); c.pay(7, 115, claim_by=ts)                         # Paid, due exactly at claimBy
        c.issue(8, 116); c.pay(8, 117, claim_by=ts + 1)                     # Paid, due one second later
        return c

    def test_rebuild_all_transitions(self):
        c = self.full_history()
        bills = st.fold(decoded(c))
        got = {k: v.status_name for k, v in bills.items()}
        self.assertEqual(got, {1: "Open", 2: "Cancelled", 3: "Paid", 4: "Claimed", 5: "Declined",
                               6: "Refunded", 7: "Paid", 8: "Paid"})
        # every field matches what getBill would return
        for bid, rec in bills.items():
            self.assertEqual(st.matches_chain(rec, abi.decode_bill(bid, c.bill_word(bid))), [], bid)
        self.assertEqual([e["event"] for e in bills[6].history], ["Issued", "Paid", "Refunded"])

    def test_order_independent_and_duplicates_ignored(self):
        evs = decoded(self.full_history())
        shuffled = evs + evs[:5]
        random.Random(1).shuffle(shuffled)
        a = {k: v.status for k, v in st.fold(evs).items()}
        b = {k: v.status for k, v in st.fold(shuffled).items()}
        self.assertEqual(a, b)

    def test_due_boundary_exact(self):
        c = self.full_history()
        bills = st.fold(decoded(c))
        due = [b.id for b in st.due(bills, c.head_ts)]
        self.assertIn(7, due)            # claimBy == block timestamp -> due
        self.assertNotIn(8, due)         # claimBy == block timestamp + 1 -> not due
        self.assertNotIn(3, due)
        self.assertEqual([b.id for b in st.due(bills, c.head_ts + 1)], [7, 8])

    def test_closed_bills_never_due(self):
        c = self.full_history()
        bills = st.fold(decoded(c))
        far_future = c.head_ts + 10 ** 9
        due = {b.id for b in st.due(bills, far_future)}
        self.assertEqual(due, {3, 7, 8})   # only Paid; Open, Claimed, Declined, Refunded, Cancelled never

    def test_paid_after_claimed_is_refused(self):
        c = FakeChain()
        c.issue(1, 10); c.pay(1, 11); c.claim(1, 12); c.pay(1, 13)
        with self.assertRaises(st.ImpossibleTransition):
            st.fold(decoded(c))

    def test_refund_of_open_bill_is_refused(self):
        c = FakeChain()
        c.issue(1, 10)
        c.bills[1]["payer"] = addr(5)
        c.refund(1, 11)
        with self.assertRaisesRegex(st.ImpossibleTransition, "Refunded while Open"):
            st.fold(decoded(c))

    def test_claim_or_decline_of_unpaid_bill_refused(self):
        for action in ("claim", "decline"):
            c = FakeChain()
            c.issue(1, 10)
            getattr(c, action)(1, 11)
            with self.assertRaisesRegex(st.ImpossibleTransition, "while Open"):
                st.fold(decoded(c))

    def test_event_before_issue_is_refused(self):
        c = FakeChain()
        c.issue(1, 10); c.pay(1, 11)
        with self.assertRaisesRegex(st.ImpossibleTransition, "before BillIssued"):
            st.fold(decoded(c)[1:])

    def test_double_issue_and_amount_mismatch_refused(self):
        c = FakeChain()
        c.issue(1, 10); c.issue(1, 11)
        with self.assertRaises(st.ImpossibleTransition):
            st.fold(decoded(c))
        c = FakeChain()
        c.issue(1, 10); c.pay(1, 11)
        c.bills[1]["amount"] += 1         # a Claimed event carrying a different amount = decode bug
        c.claim(1, 12)
        with self.assertRaisesRegex(st.ImpossibleTransition, "amount mismatch"):
            st.fold(decoded(c))

    def test_wrong_payer_for_restricted_bill_refused(self):
        c = FakeChain()
        c.issue(1, 10, allowed=addr(42))
        c.pay(1, 11, payer=addr(43))
        with self.assertRaisesRegex(st.ImpossibleTransition, "allowed payer"):
            st.fold(decoded(c))


if __name__ == "__main__":
    unittest.main()
