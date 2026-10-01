// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;

import {PaidThroughBase} from "./utils/PaidThroughBase.sol";
import {PaidThrough} from "../src/PaidThrough.sol";

/// @notice Fuzzed amounts, windows and time offsets; exact base-unit conservation; 6 vs 18 decimals.
contract PaidThroughFuzzTest is PaidThroughBase {
    struct Snap {
        uint256 payer;
        uint256 payee;
        uint256 contractBal;
    }

    function _snap() internal view returns (Snap memory) {
        return Snap(usdc.balanceOf(payer), usdc.balanceOf(payee), usdc.balanceOf(address(pt)));
    }

    function _issueFuzz(uint96 amount, uint64 payDelay, uint32 claimWindow) internal returns (uint256 id) {
        vm.prank(payee);
        id = pt.issue(amount, uint64(block.timestamp) + payDelay, claimWindow, address(0), REF);
    }

    function _bounds(uint96 amount, uint64 payDelay, uint32 claimWindow)
        internal
        pure
        returns (uint96, uint64, uint32)
    {
        amount = uint96(bound(amount, 1, MAX_AMOUNT));
        payDelay = uint64(bound(payDelay, 1, 365 days));
        claimWindow = uint32(bound(claimWindow, 1 hours, 365 days));
        return (amount, payDelay, claimWindow);
    }

    // ------------------------------------------------------------------ conservation

    function testFuzz_payThenClaim_conserves(uint96 amount, uint64 payDelay, uint32 claimWindow, uint256 payAt, uint256 claimAt)
        public
    {
        (amount, payDelay, claimWindow) = _bounds(amount, payDelay, claimWindow);
        uint256 id = _issueFuzz(amount, payDelay, claimWindow);
        Snap memory s0 = _snap();

        vm.warp(block.timestamp + bound(payAt, 0, payDelay - 1));
        _pay(id, payer);
        assertEq(usdc.balanceOf(address(pt)), amount);
        assertEq(pt.getBill(id).claimBy, block.timestamp + claimWindow);

        vm.warp(block.timestamp + bound(claimAt, 0, claimWindow - 1));
        vm.prank(payee);
        pt.claim(id);

        Snap memory s1 = _snap();
        assertEq(s1.payer + s1.payee, s0.payer + s0.payee, "conserved");
        assertEq(s1.payee - s0.payee, amount, "payee got exact amount");
        assertEq(s0.payer - s1.payer, amount, "payer paid exact amount");
        assertEq(s1.contractBal, 0, "nothing left behind");
    }

    function testFuzz_payThenDecline_conserves(uint96 amount, uint64 payDelay, uint32 claimWindow, uint256 declineAt)
        public
    {
        (amount, payDelay, claimWindow) = _bounds(amount, payDelay, claimWindow);
        uint256 id = _issueFuzz(amount, payDelay, claimWindow);
        Snap memory s0 = _snap();
        _pay(id, payer);
        vm.warp(block.timestamp + bound(declineAt, 0, claimWindow - 1));
        vm.prank(payee);
        pt.decline(id);

        Snap memory s1 = _snap();
        assertEq(s1.payer, s0.payer, "payer whole");
        assertEq(s1.payee, s0.payee);
        assertEq(s1.contractBal, 0);
    }

    function testFuzz_payThenRefund_conserves(uint96 amount, uint64 payDelay, uint32 claimWindow, uint256 lateBy)
        public
    {
        (amount, payDelay, claimWindow) = _bounds(amount, payDelay, claimWindow);
        uint256 id = _issueFuzz(amount, payDelay, claimWindow);
        Snap memory s0 = _snap();
        _pay(id, payer);
        vm.warp(pt.getBill(id).claimBy + bound(lateBy, 0, 3650 days));
        vm.prank(stranger);
        pt.refund(id);

        Snap memory s1 = _snap();
        assertEq(s1.payer, s0.payer, "payer whole");
        assertEq(s1.payee, s0.payee);
        assertEq(s1.contractBal, 0);
        assertEq(usdc.balanceOf(stranger), START_BALANCE, "caller gains nothing");
    }

    function testFuzz_payWithAuthorization_conserves(uint96 amount, uint256 validFor, uint256 claimAt) public {
        amount = uint96(bound(amount, 1, MAX_AMOUNT));
        uint256 id = _issueAs(payee, amount, address(0));
        uint256 validBefore = block.timestamp + bound(validFor, 1, 1 hours);
        (uint8 v, bytes32 r, bytes32 s) = _sign(PAYER_PK, address(pt), amount, 0, validBefore, pt.authNonce(id));
        Snap memory s0 = _snap();
        vm.prank(relayer);
        pt.payWithAuthorization(id, payer, 0, validBefore, v, r, s);
        vm.warp(block.timestamp + bound(claimAt, 0, CLAIM_WINDOW - 1));
        vm.prank(payee);
        pt.claim(id);
        Snap memory s1 = _snap();
        assertEq(s1.payer + s1.payee, s0.payer + s0.payee);
        assertEq(s1.payee - s0.payee, amount);
        assertEq(s1.contractBal, 0);
        assertEq(usdc.balanceOf(relayer), 0);
    }

    function testFuzz_manyBills_heldEqualsSumOfPaid(uint96[8] memory amounts, uint8 claimMask) public {
        uint256 held;
        uint256[8] memory ids;
        for (uint256 i; i < 8; ++i) {
            uint96 a = uint96(bound(amounts[i], 1, MAX_AMOUNT));
            ids[i] = _issueAs(payee, a, address(0));
            _pay(ids[i], i % 2 == 0 ? payer : other);
            held += a;
        }
        assertEq(usdc.balanceOf(address(pt)), held);
        for (uint256 i; i < 8; ++i) {
            if ((uint256(claimMask) >> i) & 1 == 1) {
                uint96 a = pt.getBill(ids[i]).amount;
                vm.prank(payee);
                pt.claim(ids[i]);
                held -= a;
                assertEq(usdc.balanceOf(address(pt)), held);
            }
        }
        vm.warp(block.timestamp + CLAIM_WINDOW);
        for (uint256 i; i < 8; ++i) {
            if (_status(ids[i]) == PaidThrough.Status.Paid) pt.refund(ids[i]);
        }
        assertEq(usdc.balanceOf(address(pt)), 0);
        assertEq(usdc.balanceOf(payer) + usdc.balanceOf(other) + usdc.balanceOf(payee), 2 * START_BALANCE);
    }

    // ------------------------------------------------------------------ bounds and boundaries

    function testFuzz_issue_amountAcceptedIffInRange(uint96 amount) public {
        vm.prank(payee);
        if (amount == 0 || amount > MAX_AMOUNT) {
            vm.expectRevert(PaidThrough.BadAmount.selector);
            pt.issue(amount, uint64(block.timestamp) + PAY_WINDOW, CLAIM_WINDOW, address(0), REF);
        } else {
            uint256 id = pt.issue(amount, uint64(block.timestamp) + PAY_WINDOW, CLAIM_WINDOW, address(0), REF);
            assertEq(pt.getBill(id).amount, amount);
        }
    }

    function testFuzz_issue_payByAcceptedIffInWindow(uint64 payBy) public {
        vm.prank(payee);
        if (payBy <= block.timestamp || payBy > block.timestamp + 365 days) {
            vm.expectRevert(PaidThrough.BadPayBy.selector);
            pt.issue(AMOUNT, payBy, CLAIM_WINDOW, address(0), REF);
        } else {
            pt.issue(AMOUNT, payBy, CLAIM_WINDOW, address(0), REF);
        }
    }

    function testFuzz_issue_claimWindowAcceptedIffInRange(uint32 claimWindow) public {
        vm.prank(payee);
        if (claimWindow < 1 hours || claimWindow > 365 days) {
            vm.expectRevert(PaidThrough.BadClaimWindow.selector);
            pt.issue(AMOUNT, uint64(block.timestamp) + PAY_WINDOW, claimWindow, address(0), REF);
        } else {
            pt.issue(AMOUNT, uint64(block.timestamp) + PAY_WINDOW, claimWindow, address(0), REF);
        }
    }

    function testFuzz_pay_onlyBeforePayBy(uint256 offset) public {
        uint256 id = _issue();
        uint64 payBy = pt.getBill(id).payBy;
        vm.warp(block.timestamp + bound(offset, 0, 2 * PAY_WINDOW));
        vm.startPrank(payer);
        usdc.approve(address(pt), AMOUNT);
        if (block.timestamp >= payBy) vm.expectRevert(PaidThrough.PayWindowClosed.selector);
        pt.pay(id);
        vm.stopPrank();
    }

    function testFuzz_claimDeclineRefund_noGapNoOverlap(uint256 offset, bool useDecline) public {
        uint256 id = _issueAndPay();
        uint64 claimBy = pt.getBill(id).claimBy;
        vm.warp(block.timestamp + bound(offset, 0, 2 * uint256(CLAIM_WINDOW)));
        bool payeeWindow = block.timestamp < claimBy;

        // Exactly one of {payee action, refund} is possible at any moment.
        if (payeeWindow) {
            vm.expectRevert(PaidThrough.ClaimWindowOpen.selector);
            pt.refund(id);
            vm.prank(payee);
            if (useDecline) pt.decline(id);
            else pt.claim(id);
        } else {
            vm.prank(payee);
            vm.expectRevert(PaidThrough.ClaimWindowClosed.selector);
            if (useDecline) pt.decline(id);
            else pt.claim(id);
            pt.refund(id);
            _assertStatus(id, PaidThrough.Status.Refunded);
        }
        assertEq(usdc.balanceOf(address(pt)), 0);
    }

    // ------------------------------------------------------------------ 6 vs 18 decimals

    function test_decimals_sixDecimalBaseUnits() public {
        assertEq(usdc.decimals(), 6);
        uint96 amount = 25_500_000; // 25.50 USDC = 25.50 * 10^6
        assertEq(uint256(amount), 2550 * 10 ** 6 / 100);
        uint256 id = _issueAs(payee, amount, address(0));
        Snap memory s0 = _snap();
        _pay(id, payer);
        vm.prank(payee);
        pt.claim(id);
        Snap memory s1 = _snap();
        assertEq(s0.payer - s1.payer, 25_500_000);
        assertEq(s1.payee - s0.payee, 25_500_000);
    }

    function test_decimals_eighteenDecimalMistakeRejectedByCap() public {
        uint96 mistaken = uint96(25_500_000 * 1e12); // 25.50 scaled as if USDC had 18 decimals (native gas units)
        assertEq(uint256(mistaken), 25.5e18);
        vm.prank(payee);
        vm.expectRevert(PaidThrough.BadAmount.selector);
        pt.issue(mistaken, uint64(block.timestamp) + PAY_WINDOW, CLAIM_WINDOW, address(0), REF);
    }

    function testFuzz_decimals_anyEighteenDecimalWholeAmountRejected(uint256 wholeUsdc) public {
        // Any amount of at least 0.01 USDC written in 18 decimals exceeds the 10,000 USDC (6-decimal) cap.
        wholeUsdc = bound(wholeUsdc, 1, 79_000_000_000); // keeps value * 1e16 inside uint96
        uint256 cents18 = wholeUsdc * 1e16; // wholeUsdc cents in 18-decimal units
        vm.prank(payee);
        vm.expectRevert(PaidThrough.BadAmount.selector);
        pt.issue(uint96(cents18), uint64(block.timestamp) + PAY_WINDOW, CLAIM_WINDOW, address(0), REF);
    }
}
