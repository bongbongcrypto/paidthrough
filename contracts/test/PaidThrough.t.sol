// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;

import {PaidThroughBase} from "./utils/PaidThroughBase.sol";
import {PaidThrough} from "../src/PaidThrough.sol";

contract PaidThroughTest is PaidThroughBase {
    // ------------------------------------------------------------------ constructor / views

    function test_constructor_setsImmutables() public view {
        assertEq(address(pt.usdc()), address(usdc));
        assertEq(pt.maxAmount(), MAX_AMOUNT);
    }

    function test_constants() public view {
        assertEq(pt.MIN_CLAIM_WINDOW(), 1 hours);
        assertEq(pt.MAX_CLAIM_WINDOW(), 365 days);
        assertEq(pt.MAX_PAY_WINDOW(), 365 days);
    }

    function test_billCount_startsAtZero_andUnknownBillIsEmpty() public view {
        assertEq(pt.billCount(), 0);
        PaidThrough.Bill memory b = pt.getBill(1);
        assertEq(b.payee, address(0));
        assertEq(b.amount, 0);
        assertEq(uint8(b.status), uint8(PaidThrough.Status.None));
    }

    // ------------------------------------------------------------------ issue

    function test_issue_storesBillAndEmits() public {
        uint64 payBy = uint64(block.timestamp) + PAY_WINDOW;
        vm.expectEmit(true, true, true, true, address(pt));
        emit BillIssued(1, payee, payer, AMOUNT, payBy, CLAIM_WINDOW, REF);
        vm.prank(payee);
        uint256 id = pt.issue(AMOUNT, payBy, CLAIM_WINDOW, payer, REF);

        assertEq(id, 1);
        assertEq(pt.billCount(), 1);
        PaidThrough.Bill memory b = pt.getBill(id);
        assertEq(b.payee, payee);
        assertEq(b.amount, AMOUNT);
        assertEq(b.payer, address(0));
        assertEq(b.payBy, payBy);
        assertEq(b.claimWindow, CLAIM_WINDOW);
        assertEq(b.allowedPayer, payer);
        assertEq(b.claimBy, 0);
        assertEq(uint8(b.status), uint8(PaidThrough.Status.Open));
        assertEq(b.ref, REF);
    }

    function test_issue_idsStartAtOneAndIncrement() public {
        assertEq(_issue(), 1);
        assertEq(_issueAs(payee2, 1, address(0)), 2);
        assertEq(_issue(), 3);
        assertEq(pt.billCount(), 3);
        assertEq(pt.getBill(2).payee, payee2);
    }

    function test_issue_movesNoTokens() public {
        _issue();
        assertEq(usdc.balanceOf(address(pt)), 0);
    }

    function test_issue_revert_amountZero() public {
        vm.prank(payee);
        vm.expectRevert(PaidThrough.BadAmount.selector);
        pt.issue(0, uint64(block.timestamp) + PAY_WINDOW, CLAIM_WINDOW, address(0), REF);
    }

    function test_issue_amountOne_ok() public {
        uint256 id = _issueAs(payee, 1, address(0));
        assertEq(pt.getBill(id).amount, 1);
    }

    function test_issue_atMaxAmount_ok() public {
        uint256 id = _issueAs(payee, MAX_AMOUNT, address(0));
        assertEq(pt.getBill(id).amount, MAX_AMOUNT);
    }

    function test_issue_revert_aboveMaxAmount() public {
        vm.prank(payee);
        vm.expectRevert(PaidThrough.BadAmount.selector);
        pt.issue(MAX_AMOUNT + 1, uint64(block.timestamp) + PAY_WINDOW, CLAIM_WINDOW, address(0), REF);
    }

    function test_issue_revert_payByNow() public {
        vm.prank(payee);
        vm.expectRevert(PaidThrough.BadPayBy.selector);
        pt.issue(AMOUNT, uint64(block.timestamp), CLAIM_WINDOW, address(0), REF);
    }

    function test_issue_revert_payByInPast() public {
        vm.prank(payee);
        vm.expectRevert(PaidThrough.BadPayBy.selector);
        pt.issue(AMOUNT, uint64(block.timestamp) - 1, CLAIM_WINDOW, address(0), REF);
    }

    function test_issue_payByNowPlusOne_ok() public {
        vm.prank(payee);
        pt.issue(AMOUNT, uint64(block.timestamp) + 1, CLAIM_WINDOW, address(0), REF);
    }

    function test_issue_payByAtMaxPayWindow_ok() public {
        vm.prank(payee);
        uint256 id = pt.issue(AMOUNT, uint64(block.timestamp) + 365 days, CLAIM_WINDOW, address(0), REF);
        assertEq(pt.getBill(id).payBy, block.timestamp + 365 days);
    }

    function test_issue_revert_payByBeyondMaxPayWindow() public {
        vm.prank(payee);
        vm.expectRevert(PaidThrough.BadPayBy.selector);
        pt.issue(AMOUNT, uint64(block.timestamp) + 365 days + 1, CLAIM_WINDOW, address(0), REF);
    }

    function test_issue_revert_claimWindowBelowMin() public {
        vm.prank(payee);
        vm.expectRevert(PaidThrough.BadClaimWindow.selector);
        pt.issue(AMOUNT, uint64(block.timestamp) + PAY_WINDOW, 1 hours - 1, address(0), REF);
    }

    function test_issue_claimWindowAtMin_ok() public {
        vm.prank(payee);
        uint256 id = pt.issue(AMOUNT, uint64(block.timestamp) + PAY_WINDOW, 1 hours, address(0), REF);
        assertEq(pt.getBill(id).claimWindow, 1 hours);
    }

    function test_issue_claimWindowAtMax_ok() public {
        vm.prank(payee);
        uint256 id = pt.issue(AMOUNT, uint64(block.timestamp) + PAY_WINDOW, 365 days, address(0), REF);
        assertEq(pt.getBill(id).claimWindow, 365 days);
    }

    function test_issue_revert_claimWindowAboveMax() public {
        vm.prank(payee);
        vm.expectRevert(PaidThrough.BadClaimWindow.selector);
        pt.issue(AMOUNT, uint64(block.timestamp) + PAY_WINDOW, 365 days + 1, address(0), REF);
    }

    // ------------------------------------------------------------------ cancel

    function test_cancel_byPayee_emits() public {
        uint256 id = _issue();
        vm.expectEmit(true, false, false, true, address(pt));
        emit BillCancelled(id);
        vm.prank(payee);
        pt.cancel(id);
        _assertStatus(id, PaidThrough.Status.Cancelled);
    }

    function test_cancel_afterPayBy_ok() public {
        uint256 id = _issue();
        vm.warp(block.timestamp + PAY_WINDOW + 1 days);
        vm.prank(payee);
        pt.cancel(id);
        _assertStatus(id, PaidThrough.Status.Cancelled);
    }

    function test_cancel_revert_notPayee() public {
        uint256 id = _issue();
        vm.prank(stranger);
        vm.expectRevert(PaidThrough.NotPayee.selector);
        pt.cancel(id);
    }

    function test_cancel_revert_unknownBill() public {
        vm.prank(payee);
        vm.expectRevert(PaidThrough.NotPayee.selector);
        pt.cancel(42);
    }

    function test_cancel_revert_whenPaid() public {
        uint256 id = _issueAndPay();
        vm.prank(payee);
        vm.expectRevert(PaidThrough.WrongStatus.selector);
        pt.cancel(id);
    }

    function test_cancel_revert_twice() public {
        uint256 id = _issue();
        vm.startPrank(payee);
        pt.cancel(id);
        vm.expectRevert(PaidThrough.WrongStatus.selector);
        pt.cancel(id);
        vm.stopPrank();
    }

    // ------------------------------------------------------------------ pay

    function test_pay_movesExactAmountAndEmits() public {
        uint256 id = _issue();
        uint64 claimBy = uint64(block.timestamp) + CLAIM_WINDOW;
        vm.startPrank(payer);
        usdc.approve(address(pt), AMOUNT);
        vm.expectEmit(true, true, false, true, address(pt));
        emit BillPaid(id, payer, claimBy);
        pt.pay(id);
        vm.stopPrank();

        PaidThrough.Bill memory b = pt.getBill(id);
        assertEq(b.payer, payer);
        assertEq(b.claimBy, claimBy);
        assertEq(uint8(b.status), uint8(PaidThrough.Status.Paid));
        assertEq(usdc.balanceOf(address(pt)), AMOUNT);
        assertEq(usdc.balanceOf(payer), START_BALANCE - AMOUNT);
        assertEq(usdc.allowance(payer, address(pt)), 0);
    }

    function test_pay_claimByIsPaidAtPlusClaimWindow() public {
        uint256 id = _issue();
        vm.warp(block.timestamp + 3 days);
        uint256 paidAt = block.timestamp;
        _pay(id, payer);
        assertEq(pt.getBill(id).claimBy, paidAt + CLAIM_WINDOW);
    }

    function test_pay_atPayByMinusOne_ok() public {
        uint256 id = _issue();
        vm.warp(pt.getBill(id).payBy - 1);
        _pay(id, payer);
        _assertStatus(id, PaidThrough.Status.Paid);
    }

    function test_pay_revert_atPayBy() public {
        uint256 id = _issue();
        vm.warp(pt.getBill(id).payBy);
        vm.startPrank(payer);
        usdc.approve(address(pt), AMOUNT);
        vm.expectRevert(PaidThrough.PayWindowClosed.selector);
        pt.pay(id);
        vm.stopPrank();
    }

    function test_pay_revert_unknownBill() public {
        vm.prank(payer);
        vm.expectRevert(PaidThrough.WrongStatus.selector);
        pt.pay(7);
    }

    function test_pay_revert_cancelled() public {
        uint256 id = _issue();
        vm.prank(payee);
        pt.cancel(id);
        vm.startPrank(payer);
        usdc.approve(address(pt), AMOUNT);
        vm.expectRevert(PaidThrough.WrongStatus.selector);
        pt.pay(id);
        vm.stopPrank();
    }

    function test_pay_revert_alreadyPaid() public {
        uint256 id = _issueAndPay();
        vm.startPrank(other);
        usdc.approve(address(pt), AMOUNT);
        vm.expectRevert(PaidThrough.WrongStatus.selector);
        pt.pay(id);
        vm.stopPrank();
        assertEq(pt.getBill(id).payer, payer);
    }

    function test_pay_allowedPayer_ok() public {
        uint256 id = _issueAs(payee, AMOUNT, payer);
        _pay(id, payer);
        assertEq(pt.getBill(id).payer, payer);
    }

    function test_pay_revert_notAllowedPayer() public {
        uint256 id = _issueAs(payee, AMOUNT, payer);
        vm.startPrank(other);
        usdc.approve(address(pt), AMOUNT);
        vm.expectRevert(PaidThrough.NotAllowedPayer.selector);
        pt.pay(id);
        vm.stopPrank();
    }

    function test_pay_openBill_anyoneMayPay() public {
        uint256 id = _issue();
        _pay(id, stranger);
        assertEq(pt.getBill(id).payer, stranger);
    }

    function test_pay_revert_noApproval() public {
        uint256 id = _issue();
        vm.prank(payer);
        vm.expectRevert(bytes("ERC20: transfer amount exceeds allowance"));
        pt.pay(id);
        _assertStatus(id, PaidThrough.Status.Open);
    }

    function test_pay_revert_insufficientBalance() public {
        address poor = makeAddr("poor");
        usdc.mint(poor, AMOUNT - 1);
        uint256 id = _issue();
        vm.startPrank(poor);
        usdc.approve(address(pt), AMOUNT);
        vm.expectRevert(bytes("ERC20: transfer amount exceeds balance"));
        pt.pay(id);
        vm.stopPrank();
        _assertStatus(id, PaidThrough.Status.Open);
    }

    // ------------------------------------------------------------------ claim

    function test_claim_paysPayeeAndEmits() public {
        uint256 id = _issueAndPay();
        vm.expectEmit(true, true, false, true, address(pt));
        emit BillClaimed(id, payee, AMOUNT);
        vm.prank(payee);
        pt.claim(id);
        _assertStatus(id, PaidThrough.Status.Claimed);
        assertEq(usdc.balanceOf(payee), AMOUNT);
        assertEq(usdc.balanceOf(address(pt)), 0);
    }

    function test_claim_atClaimByMinusOne_ok() public {
        uint256 id = _issueAndPay();
        vm.warp(pt.getBill(id).claimBy - 1);
        vm.prank(payee);
        pt.claim(id);
        _assertStatus(id, PaidThrough.Status.Claimed);
    }

    function test_claim_revert_atClaimBy() public {
        uint256 id = _issueAndPay();
        vm.warp(pt.getBill(id).claimBy);
        vm.prank(payee);
        vm.expectRevert(PaidThrough.ClaimWindowClosed.selector);
        pt.claim(id);
    }

    function test_claim_revert_notPayee() public {
        uint256 id = _issueAndPay();
        vm.prank(payer);
        vm.expectRevert(PaidThrough.NotPayee.selector);
        pt.claim(id);
    }

    function test_claim_revert_openBill() public {
        uint256 id = _issue();
        vm.prank(payee);
        vm.expectRevert(PaidThrough.WrongStatus.selector);
        pt.claim(id);
    }

    function test_claim_revert_unknownBill() public {
        vm.prank(payee);
        vm.expectRevert(PaidThrough.NotPayee.selector);
        pt.claim(9);
    }

    function test_claim_revert_twice() public {
        uint256 id = _issueAndPay();
        vm.startPrank(payee);
        pt.claim(id);
        vm.expectRevert(PaidThrough.WrongStatus.selector);
        pt.claim(id);
        vm.stopPrank();
        assertEq(usdc.balanceOf(payee), AMOUNT);
    }

    function test_claim_revert_otherPayeesBill() public {
        uint256 id = _issueAndPay();
        vm.prank(payee2);
        vm.expectRevert(PaidThrough.NotPayee.selector);
        pt.claim(id);
    }

    // ------------------------------------------------------------------ decline

    function test_decline_returnsToPayerAndEmits() public {
        uint256 id = _issueAndPay();
        vm.expectEmit(true, true, false, true, address(pt));
        emit BillDeclined(id, payer, AMOUNT);
        vm.prank(payee);
        pt.decline(id);
        _assertStatus(id, PaidThrough.Status.Declined);
        assertEq(usdc.balanceOf(payer), START_BALANCE);
        assertEq(usdc.balanceOf(payee), 0);
        assertEq(usdc.balanceOf(address(pt)), 0);
    }

    function test_decline_atClaimByMinusOne_ok() public {
        uint256 id = _issueAndPay();
        vm.warp(pt.getBill(id).claimBy - 1);
        vm.prank(payee);
        pt.decline(id);
        _assertStatus(id, PaidThrough.Status.Declined);
    }

    function test_decline_revert_atClaimBy() public {
        uint256 id = _issueAndPay();
        vm.warp(pt.getBill(id).claimBy);
        vm.prank(payee);
        vm.expectRevert(PaidThrough.ClaimWindowClosed.selector);
        pt.decline(id);
    }

    function test_decline_revert_notPayee() public {
        uint256 id = _issueAndPay();
        vm.prank(stranger);
        vm.expectRevert(PaidThrough.NotPayee.selector);
        pt.decline(id);
    }

    function test_decline_revert_openBill() public {
        uint256 id = _issue();
        vm.prank(payee);
        vm.expectRevert(PaidThrough.WrongStatus.selector);
        pt.decline(id);
    }

    function test_decline_revert_afterClaim() public {
        uint256 id = _issueAndPay();
        vm.startPrank(payee);
        pt.claim(id);
        vm.expectRevert(PaidThrough.WrongStatus.selector);
        pt.decline(id);
        vm.stopPrank();
    }

    // ------------------------------------------------------------------ refund

    function test_refund_revert_atClaimByMinusOne() public {
        uint256 id = _issueAndPay();
        vm.warp(pt.getBill(id).claimBy - 1);
        vm.expectRevert(PaidThrough.ClaimWindowOpen.selector);
        pt.refund(id);
    }

    function test_refund_atClaimBy_ok() public {
        uint256 id = _issueAndPay();
        vm.warp(pt.getBill(id).claimBy);
        vm.expectEmit(true, true, false, true, address(pt));
        emit BillRefunded(id, payer, AMOUNT, address(this));
        pt.refund(id);
        _assertStatus(id, PaidThrough.Status.Refunded);
        assertEq(usdc.balanceOf(payer), START_BALANCE);
        assertEq(usdc.balanceOf(address(pt)), 0);
    }

    function test_refund_byThirdParty_callerRecorded() public {
        uint256 id = _issueAndPay();
        vm.warp(pt.getBill(id).claimBy + 10 days);
        vm.expectEmit(true, true, false, true, address(pt));
        emit BillRefunded(id, payer, AMOUNT, stranger);
        vm.prank(stranger);
        pt.refund(id);
        assertEq(usdc.balanceOf(payer), START_BALANCE);
        assertEq(usdc.balanceOf(stranger), START_BALANCE);
    }

    function test_refund_byPayee_ok() public {
        uint256 id = _issueAndPay();
        vm.warp(pt.getBill(id).claimBy);
        vm.prank(payee);
        pt.refund(id);
        _assertStatus(id, PaidThrough.Status.Refunded);
    }

    function test_refund_revert_openBill() public {
        uint256 id = _issue();
        vm.warp(block.timestamp + 400 days);
        vm.expectRevert(PaidThrough.WrongStatus.selector);
        pt.refund(id);
    }

    function test_refund_revert_unknownBill() public {
        vm.expectRevert(PaidThrough.WrongStatus.selector);
        pt.refund(1);
    }

    function test_refund_revert_twice() public {
        uint256 id = _issueAndPay();
        vm.warp(pt.getBill(id).claimBy);
        pt.refund(id);
        vm.expectRevert(PaidThrough.WrongStatus.selector);
        pt.refund(id);
        assertEq(usdc.balanceOf(payer), START_BALANCE);
    }

    function test_refund_revert_afterClaim() public {
        uint256 id = _issueAndPay();
        vm.prank(payee);
        pt.claim(id);
        vm.warp(block.timestamp + CLAIM_WINDOW);
        vm.expectRevert(PaidThrough.WrongStatus.selector);
        pt.refund(id);
    }

    function test_refund_revert_afterDecline() public {
        uint256 id = _issueAndPay();
        vm.prank(payee);
        pt.decline(id);
        vm.warp(block.timestamp + CLAIM_WINDOW);
        vm.expectRevert(PaidThrough.WrongStatus.selector);
        pt.refund(id);
    }

    // ------------------------------------------------------------------ native value

    function _withValue(address from, bytes memory data) internal returns (bool ok) {
        vm.deal(from, 1 ether);
        vm.prank(from);
        (ok,) = address(pt).call{value: 1}(data);
    }

    function _withoutValue(address from, bytes memory data) internal returns (bool ok) {
        vm.prank(from);
        (ok,) = address(pt).call(data);
    }

    /// Each state-changing call fails with 1 wei attached and succeeds without it, from a caller that is otherwise
    /// fully able to make it (funded, approved, right role, right time), so the value is the only reason.
    function test_native_everyFunctionRejectsValue_butWorksWithout() public {
        uint256 a = _issue(); // claim
        uint256 b = _issue(); // decline
        uint256 c = _issue(); // cancel
        uint256 d = _issue(); // refund
        uint256 e = _issue(); // payWithAuthorization
        vm.prank(payer);
        usdc.approve(address(pt), 4 * uint256(AMOUNT));

        bytes memory payA = abi.encodeCall(PaidThrough.pay, (a));
        assertFalse(_withValue(payer, payA), "pay with value");
        assertTrue(_withoutValue(payer, payA), "pay");
        assertTrue(_withoutValue(payer, abi.encodeCall(PaidThrough.pay, (b))));
        assertTrue(_withoutValue(payer, abi.encodeCall(PaidThrough.pay, (d))));

        uint256 validBefore = block.timestamp + 1 hours;
        (uint8 v, bytes32 r, bytes32 s) = _sign(PAYER_PK, address(pt), AMOUNT, 0, validBefore, pt.authNonce(e));
        bytes memory pwa = abi.encodeCall(PaidThrough.payWithAuthorization, (e, payer, 0, validBefore, v, r, s));
        assertFalse(_withValue(relayer, pwa), "payWithAuthorization with value");
        assertTrue(_withoutValue(relayer, pwa), "payWithAuthorization");

        bytes memory claimA = abi.encodeCall(PaidThrough.claim, (a));
        assertFalse(_withValue(payee, claimA), "claim with value");
        assertTrue(_withoutValue(payee, claimA), "claim");

        bytes memory declineB = abi.encodeCall(PaidThrough.decline, (b));
        assertFalse(_withValue(payee, declineB), "decline with value");
        assertTrue(_withoutValue(payee, declineB), "decline");

        bytes memory cancelC = abi.encodeCall(PaidThrough.cancel, (c));
        assertFalse(_withValue(payee, cancelC), "cancel with value");
        assertTrue(_withoutValue(payee, cancelC), "cancel");

        vm.warp(pt.getBill(d).claimBy);
        bytes memory refundD = abi.encodeCall(PaidThrough.refund, (d));
        assertFalse(_withValue(stranger, refundD), "refund with value");
        assertTrue(_withoutValue(stranger, refundD), "refund");

        assertEq(address(pt).balance, 0);
        assertEq(usdc.balanceOf(address(pt)), AMOUNT); // bill e is still Paid
    }

    function test_native_callWithValueToIssueReverts() public {
        vm.deal(address(this), 1 ether);
        (bool ok,) = address(pt).call{value: 1}(
            abi.encodeCall(
                PaidThrough.issue, (AMOUNT, uint64(block.timestamp) + PAY_WINDOW, CLAIM_WINDOW, address(0), REF)
            )
        );
        assertFalse(ok);
        assertEq(pt.billCount(), 0);
    }

    function test_native_plainSendReverts() public {
        vm.deal(address(this), 1 ether);
        bool sent = payable(address(pt)).send(1);
        assertFalse(sent);
        (bool ok,) = address(pt).call{value: 1}("");
        assertFalse(ok);
        assertEq(address(pt).balance, 0);
    }

    function test_unknownSelectorReverts() public {
        (bool ok,) = address(pt).call(abi.encodeWithSignature("sweep(address)", address(this)));
        assertFalse(ok);
    }

    // ------------------------------------------------------------------ full lifecycles

    function test_lifecycle_twoBillsIndependent() public {
        uint256 a = _issueAndPay();
        uint256 b = _issueAs(payee2, 1_000_000, address(0));
        _pay(b, other);
        assertEq(usdc.balanceOf(address(pt)), AMOUNT + 1_000_000);

        vm.prank(payee);
        pt.claim(a);
        vm.warp(pt.getBill(b).claimBy);
        pt.refund(b);

        assertEq(usdc.balanceOf(payee), AMOUNT);
        assertEq(usdc.balanceOf(other), START_BALANCE);
        assertEq(usdc.balanceOf(address(pt)), 0);
        _assertStatus(a, PaidThrough.Status.Claimed);
        _assertStatus(b, PaidThrough.Status.Refunded);
    }
}
