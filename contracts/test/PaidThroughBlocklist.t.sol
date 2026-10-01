// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;

import {PaidThroughBase} from "./utils/PaidThroughBase.sol";
import {PaidThrough} from "../src/PaidThrough.sol";
import {ReentrantToken} from "./mocks/ReentrantToken.sol";

/// @notice Token-side failures: blocklisted parties (push model, bill stays Paid), false returns, short credits,
///         a hostile re-entering token, and stray transfers.
contract PaidThroughBlocklistTest is PaidThroughBase {
    string internal constant BLOCKED_MSG = "Blacklistable: account is blacklisted";

    // ------------------------------------------------------------------ blocked payee

    function test_blockedPayee_cannotClaim_billStaysPaid() public {
        uint256 id = _issueAndPay();
        usdc.blacklist(payee);
        vm.prank(payee);
        vm.expectRevert(bytes(BLOCKED_MSG));
        pt.claim(id);
        _assertStatus(id, PaidThrough.Status.Paid);
        assertEq(usdc.balanceOf(address(pt)), AMOUNT);
    }

    function test_blockedPayee_refundToPayerAfterClaimBy() public {
        uint256 id = _issueAndPay();
        usdc.blacklist(payee);
        vm.warp(pt.getBill(id).claimBy);
        vm.prank(stranger);
        pt.refund(id);
        _assertStatus(id, PaidThrough.Status.Refunded);
        assertEq(usdc.balanceOf(payer), START_BALANCE);
        assertEq(usdc.balanceOf(address(pt)), 0);
    }

    function test_blockedPayee_canStillDecline() public {
        uint256 id = _issueAndPay();
        usdc.blacklist(payee);
        vm.prank(payee);
        pt.decline(id); // money goes contract -> payer; the payee is not a party to that transfer
        _assertStatus(id, PaidThrough.Status.Declined);
        assertEq(usdc.balanceOf(payer), START_BALANCE);
    }

    function test_blockedPayee_unblockedInTime_canClaim() public {
        uint256 id = _issueAndPay();
        usdc.blacklist(payee);
        vm.prank(payee);
        vm.expectRevert(bytes(BLOCKED_MSG));
        pt.claim(id);
        usdc.unBlacklist(payee);
        vm.prank(payee);
        pt.claim(id);
        assertEq(usdc.balanceOf(payee), AMOUNT);
    }

    // ------------------------------------------------------------------ blocked payer

    function test_blockedPayer_refundReverts_thenSucceedsAfterUnblock() public {
        uint256 id = _issueAndPay();
        usdc.blacklist(payer);
        vm.warp(pt.getBill(id).claimBy);
        vm.expectRevert(bytes(BLOCKED_MSG));
        pt.refund(id);
        _assertStatus(id, PaidThrough.Status.Paid);
        assertEq(usdc.balanceOf(address(pt)), AMOUNT);

        vm.warp(block.timestamp + 90 days);
        usdc.unBlacklist(payer);
        vm.expectEmit(true, true, false, true, address(pt));
        emit BillRefunded(id, payer, AMOUNT, address(this));
        pt.refund(id);
        _assertStatus(id, PaidThrough.Status.Refunded);
        assertEq(usdc.balanceOf(payer), START_BALANCE);
    }

    function test_blockedPayer_declineReverts_billStaysPaid() public {
        uint256 id = _issueAndPay();
        usdc.blacklist(payer);
        vm.prank(payee);
        vm.expectRevert(bytes(BLOCKED_MSG));
        pt.decline(id);
        _assertStatus(id, PaidThrough.Status.Paid);
    }

    function test_blockedPayer_payeeCanStillClaim() public {
        uint256 id = _issueAndPay();
        usdc.blacklist(payer);
        vm.prank(payee);
        pt.claim(id);
        assertEq(usdc.balanceOf(payee), AMOUNT);
    }

    function test_blockedPayer_cannotPay() public {
        uint256 id = _issue();
        vm.prank(payer);
        usdc.approve(address(pt), AMOUNT);
        usdc.blacklist(payer);
        vm.prank(payer);
        vm.expectRevert(bytes(BLOCKED_MSG));
        pt.pay(id);
        _assertStatus(id, PaidThrough.Status.Open);
    }

    function test_blockedPayer_cannotPayWithAuthorization() public {
        uint256 id = _issue();
        uint256 validBefore = block.timestamp + 1 hours;
        (uint8 v, bytes32 r, bytes32 s) = _sign(PAYER_PK, address(pt), AMOUNT, 0, validBefore, pt.authNonce(id));
        usdc.blacklist(payer);
        vm.prank(relayer);
        vm.expectRevert(bytes(BLOCKED_MSG));
        pt.payWithAuthorization(id, payer, 0, validBefore, v, r, s);
        _assertStatus(id, PaidThrough.Status.Open);
    }

    function test_blockedContract_freezesEverything_untilUnblocked() public {
        uint256 id = _issueAndPay();
        usdc.blacklist(address(pt));
        vm.prank(payee);
        vm.expectRevert(bytes(BLOCKED_MSG));
        pt.claim(id);
        vm.warp(pt.getBill(id).claimBy);
        vm.expectRevert(bytes(BLOCKED_MSG));
        pt.refund(id);
        usdc.unBlacklist(address(pt));
        pt.refund(id);
        assertEq(usdc.balanceOf(payer), START_BALANCE);
    }

    // ------------------------------------------------------------------ TransferFailed

    function test_transferFailed_onPay() public {
        uint256 id = _issue();
        vm.prank(payer);
        usdc.approve(address(pt), AMOUNT);
        usdc.setReturnFalse(true);
        vm.prank(payer);
        vm.expectRevert(PaidThrough.TransferFailed.selector);
        pt.pay(id);
        _assertStatus(id, PaidThrough.Status.Open);
    }

    function test_transferFailed_onClaim() public {
        uint256 id = _issueAndPay();
        usdc.setReturnFalse(true);
        vm.prank(payee);
        vm.expectRevert(PaidThrough.TransferFailed.selector);
        pt.claim(id);
        _assertStatus(id, PaidThrough.Status.Paid);
    }

    function test_transferFailed_onDecline() public {
        uint256 id = _issueAndPay();
        usdc.setReturnFalse(true);
        vm.prank(payee);
        vm.expectRevert(PaidThrough.TransferFailed.selector);
        pt.decline(id);
        _assertStatus(id, PaidThrough.Status.Paid);
    }

    function test_transferFailed_onRefund() public {
        uint256 id = _issueAndPay();
        vm.warp(pt.getBill(id).claimBy);
        usdc.setReturnFalse(true);
        vm.expectRevert(PaidThrough.TransferFailed.selector);
        pt.refund(id);
        _assertStatus(id, PaidThrough.Status.Paid);
        usdc.setReturnFalse(false);
        pt.refund(id);
        _assertStatus(id, PaidThrough.Status.Refunded);
    }

    // ------------------------------------------------------------------ BalanceMismatch

    function test_balanceMismatch_onPay() public {
        uint256 id = _issue();
        vm.prank(payer);
        usdc.approve(address(pt), AMOUNT);
        usdc.setSkimOne(true);
        vm.prank(payer);
        vm.expectRevert(PaidThrough.BalanceMismatch.selector);
        pt.pay(id);
        _assertStatus(id, PaidThrough.Status.Open);
        assertEq(usdc.balanceOf(payer), START_BALANCE);
    }

    function test_balanceMismatch_onPayWithAuthorization() public {
        uint256 id = _issue();
        uint256 validBefore = block.timestamp + 1 hours;
        (uint8 v, bytes32 r, bytes32 s) = _sign(PAYER_PK, address(pt), AMOUNT, 0, validBefore, pt.authNonce(id));
        usdc.setSkimOne(true);
        vm.expectRevert(PaidThrough.BalanceMismatch.selector);
        pt.payWithAuthorization(id, payer, 0, validBefore, v, r, s);
        _assertStatus(id, PaidThrough.Status.Open);
        assertFalse(usdc.authorizationState(payer, pt.authNonce(id)));
    }

    // ------------------------------------------------------------------ stray tokens

    function test_strayTokens_areStuck_andDoNotBreakAccounting() public {
        vm.prank(stranger);
        usdc.transfer(address(pt), 5_000_000);
        uint256 id = _issueAndPay();
        assertEq(usdc.balanceOf(address(pt)), AMOUNT + 5_000_000);
        vm.prank(payee);
        pt.claim(id);
        assertEq(usdc.balanceOf(payee), AMOUNT);
        assertEq(usdc.balanceOf(address(pt)), 5_000_000, "stray tokens stay; no sweep exists");
    }

    // ------------------------------------------------------------------ re-entrancy (hostile token)

    function _hostileSetup() internal returns (ReentrantToken tok, PaidThrough hpt, uint256 id) {
        tok = new ReentrantToken();
        hpt = new PaidThrough(address(tok), MAX_AMOUNT);
        tok.mint(payer, START_BALANCE);
        vm.prank(payee);
        id = hpt.issue(AMOUNT, uint64(block.timestamp) + PAY_WINDOW, CLAIM_WINDOW, address(0), REF);
        vm.startPrank(payer);
        tok.approve(address(hpt), AMOUNT);
        hpt.pay(id);
        vm.stopPrank();
    }

    function test_reentrancy_refundDuringRefund_blocked() public {
        (ReentrantToken tok, PaidThrough hpt, uint256 id) = _hostileSetup();
        vm.warp(hpt.getBill(id).claimBy);
        tok.arm(address(hpt), abi.encodeCall(PaidThrough.refund, (id)));
        hpt.refund(id);
        assertTrue(tok.reentered());
        assertFalse(tok.innerSuccess());
        assertEq(bytes4(tok.innerRevertData()), PaidThrough.WrongStatus.selector);
        assertEq(tok.balanceOf(payer), START_BALANCE, "paid back exactly once");
        assertEq(tok.balanceOf(address(hpt)), 0);
    }

    function test_reentrancy_refundDuringClaim_blocked() public {
        (ReentrantToken tok, PaidThrough hpt, uint256 id) = _hostileSetup();
        vm.warp(hpt.getBill(id).claimBy - 1);
        tok.arm(address(hpt), abi.encodeCall(PaidThrough.refund, (id)));
        vm.prank(payee);
        hpt.claim(id);
        assertTrue(tok.reentered());
        assertFalse(tok.innerSuccess());
        assertEq(bytes4(tok.innerRevertData()), PaidThrough.WrongStatus.selector);
        assertEq(tok.balanceOf(payee), AMOUNT);
        assertEq(tok.balanceOf(address(hpt)), 0);
    }
}
