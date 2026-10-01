// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;

import {Test} from "forge-std/Test.sol";
import {console2} from "forge-std/console2.sol";
import {PaidThrough} from "../src/PaidThrough.sol";
import {MockFiatToken} from "./mocks/MockFiatToken.sol";

/// @notice Drives PaidThrough with several actors; ghost state mirrors what each successful call must have done.
///         Handler actions never revert (fail_on_revert = true): an action whose precondition does not hold is
///         counted as a no-op instead, so the metrics show real action counts.
contract PaidThroughHandler is Test {
    PaidThrough public immutable pt;
    MockFiatToken public immutable usdc;
    uint96 public immutable maxAmount;

    uint256 public constant PAYER_START = 1_000_000e6;

    uint256[] internal payerKeys;
    address[] public payers;
    address[] public payees;

    uint256 public ghostNow;
    uint256 public ghostTotalIn; // paid into the contract
    uint256 public ghostClaimed; // paid out to payees
    uint256 public ghostReturned; // paid back to payers (decline + refund)
    uint256 public ghostHeld; // sum of amounts over Paid bills
    mapping(uint256 => PaidThrough.Status) public expectedStatus;
    mapping(uint256 => uint256) public ghostPaidAt;
    mapping(uint256 => address) public ghostPayer;

    mapping(bytes32 => uint256) internal calls; // successful actions, no-ops and bad-call outcomes

    constructor(PaidThrough pt_, MockFiatToken usdc_, uint96 maxAmount_) {
        pt = pt_;
        usdc = usdc_;
        maxAmount = maxAmount_;
        ghostNow = block.timestamp;
        for (uint256 i = 1; i <= 4; ++i) {
            uint256 pk = 0x1000 + i;
            address a = vm.addr(pk);
            payerKeys.push(pk);
            payers.push(a);
            usdc.mint(a, PAYER_START);
            vm.prank(a);
            usdc.approve(address(pt), type(uint256).max);
        }
        for (uint256 i = 1; i <= 3; ++i) {
            payees.push(address(uint160(0x2000 + i)));
        }
    }

    modifier atGhostTime() {
        vm.warp(ghostNow);
        _;
    }

    function _billId(uint256 seed) internal view returns (uint256) {
        uint256 n = pt.billCount();
        if (n == 0) return 0;
        return bound(seed, 1, n);
    }

    function _noop(bytes32 action) internal {
        calls[keccak256(abi.encode("noop", action))]++;
    }

    // ---- actions ----

    function issue(uint256 actorSeed, uint96 amount, uint64 payDelay, uint32 claimWindow, uint256 allowSeed)
        external
        atGhostTime
    {
        address payee = payees[actorSeed % payees.length];
        amount = uint96(bound(amount, 1, maxAmount));
        payDelay = uint64(bound(payDelay, 1, 30 days));
        claimWindow = uint32(bound(claimWindow, 1 hours, 30 days));
        address allowed = allowSeed % 3 == 0 ? payers[allowSeed % payers.length] : address(0);
        vm.prank(payee);
        uint256 id = pt.issue(
            amount, uint64(ghostNow) + payDelay, claimWindow, allowed, keccak256(abi.encode(ghostNow, amount))
        );
        expectedStatus[id] = PaidThrough.Status.Open;
        calls["issue"]++;
    }

    function _payablePayer(PaidThrough.Bill memory b, uint256 seed) internal view returns (uint256 idx) {
        if (b.allowedPayer == address(0)) return seed % payers.length;
        for (uint256 i; i < payers.length; ++i) {
            if (payers[i] == b.allowedPayer) return i;
        }
        return seed % payers.length;
    }

    function pay(uint256 billSeed, uint256 payerSeed) external atGhostTime {
        uint256 id = _billId(billSeed);
        if (id == 0) {
            _noop("pay");
            return;
        }
        PaidThrough.Bill memory b = pt.getBill(id);
        if (b.status != PaidThrough.Status.Open || ghostNow >= b.payBy) {
            _noop("pay");
            return;
        }
        address payer = payers[_payablePayer(b, payerSeed)];
        vm.prank(payer);
        pt.pay(id);
        _afterPaid(id, payer, b.amount);
        calls["pay"]++;
    }

    function payWithAuthorization(uint256 billSeed, uint256 payerSeed, uint256 relayerSeed) external atGhostTime {
        uint256 id = _billId(billSeed);
        if (id == 0) {
            _noop("payWithAuthorization");
            return;
        }
        PaidThrough.Bill memory b = pt.getBill(id);
        if (b.status != PaidThrough.Status.Open || ghostNow >= b.payBy) {
            _noop("payWithAuthorization");
            return;
        }
        uint256 idx = _payablePayer(b, payerSeed);
        address payer = payers[idx];
        uint256 validBefore = ghostNow + 1 hours;
        (uint8 v, bytes32 r, bytes32 s) = _signAuth(idx, id, b.amount, validBefore);
        vm.prank(address(uint160(0x3000 + relayerSeed % 5)));
        pt.payWithAuthorization(id, payer, 0, validBefore, v, r, s);
        _afterPaid(id, payer, b.amount);
        calls["payWithAuthorization"]++;
    }

    function _signAuth(uint256 idx, uint256 id, uint96 amount, uint256 validBefore)
        internal
        view
        returns (uint8, bytes32, bytes32)
    {
        bytes32 structHash = keccak256(
            abi.encode(
                usdc.RECEIVE_WITH_AUTHORIZATION_TYPEHASH(),
                payers[idx],
                address(pt),
                uint256(amount),
                uint256(0),
                validBefore,
                pt.authNonce(id)
            )
        );
        bytes32 digest = keccak256(abi.encodePacked("\x19\x01", usdc.DOMAIN_SEPARATOR(), structHash));
        return vm.sign(payerKeys[idx], digest);
    }

    function _afterPaid(uint256 id, address payer, uint96 amount) internal {
        expectedStatus[id] = PaidThrough.Status.Paid;
        ghostPaidAt[id] = ghostNow;
        ghostPayer[id] = payer;
        ghostTotalIn += amount;
        ghostHeld += amount;
    }

    function claim(uint256 billSeed) external atGhostTime {
        uint256 id = _billId(billSeed);
        if (id == 0) {
            _noop("claim");
            return;
        }
        PaidThrough.Bill memory b = pt.getBill(id);
        if (b.status != PaidThrough.Status.Paid || ghostNow >= b.claimBy) {
            _noop("claim");
            return;
        }
        vm.prank(b.payee);
        pt.claim(id);
        _afterOut(id, PaidThrough.Status.Claimed, b.amount);
        ghostClaimed += b.amount;
        calls["claim"]++;
    }

    function decline(uint256 billSeed) external atGhostTime {
        uint256 id = _billId(billSeed);
        if (id == 0) {
            _noop("decline");
            return;
        }
        PaidThrough.Bill memory b = pt.getBill(id);
        if (b.status != PaidThrough.Status.Paid || ghostNow >= b.claimBy) {
            _noop("decline");
            return;
        }
        vm.prank(b.payee);
        pt.decline(id);
        _afterOut(id, PaidThrough.Status.Declined, b.amount);
        ghostReturned += b.amount;
        calls["decline"]++;
    }

    function refund(uint256 billSeed, uint256 callerSeed) external atGhostTime {
        uint256 id = _billId(billSeed);
        if (id == 0) {
            _noop("refund");
            return;
        }
        PaidThrough.Bill memory b = pt.getBill(id);
        if (b.status != PaidThrough.Status.Paid || ghostNow < b.claimBy) {
            _noop("refund");
            return;
        }
        vm.prank(address(uint160(0x4000 + callerSeed % 7)));
        pt.refund(id);
        _afterOut(id, PaidThrough.Status.Refunded, b.amount);
        ghostReturned += b.amount;
        calls["refund"]++;
    }

    function cancel(uint256 billSeed) external atGhostTime {
        uint256 id = _billId(billSeed);
        if (id == 0) {
            _noop("cancel");
            return;
        }
        PaidThrough.Bill memory b = pt.getBill(id);
        if (b.status != PaidThrough.Status.Open) {
            _noop("cancel");
            return;
        }
        vm.prank(b.payee);
        pt.cancel(id);
        expectedStatus[id] = PaidThrough.Status.Cancelled;
        calls["cancel"]++;
    }

    /// @dev Calls by the real parties (and strangers) that must fail in the bill's current state and time, each
    ///      with one exact custom error. A success counts as "unexpected", another error as "wrongError".
    function attemptBadCalls(uint256 billSeed, uint256 who) external atGhostTime {
        uint256 id = _billId(billSeed);
        if (id == 0) {
            _noop("attemptBadCalls");
            return;
        }
        PaidThrough.Bill memory b = pt.getBill(id);
        uint256 idx = _payablePayer(b, who);
        address payer = payers[idx];
        address stranger = address(uint160(0x5000 + who % 3));

        _mustFail(stranger, abi.encodeCall(PaidThrough.claim, (id)), PaidThrough.NotPayee.selector);
        _mustFail(stranger, abi.encodeCall(PaidThrough.cancel, (id)), PaidThrough.NotPayee.selector);

        if (b.status == PaidThrough.Status.Open) {
            if (ghostNow >= b.payBy) {
                _mustFail(payer, abi.encodeCall(PaidThrough.pay, (id)), PaidThrough.PayWindowClosed.selector);
            } else if (b.allowedPayer != address(0)) {
                address notAllowed = payers[(idx + 1) % payers.length];
                _mustFail(notAllowed, abi.encodeCall(PaidThrough.pay, (id)), PaidThrough.NotAllowedPayer.selector);
            }
            _mustFail(b.payee, abi.encodeCall(PaidThrough.claim, (id)), PaidThrough.WrongStatus.selector);
            _mustFail(b.payee, abi.encodeCall(PaidThrough.decline, (id)), PaidThrough.WrongStatus.selector);
            _mustFail(stranger, abi.encodeCall(PaidThrough.refund, (id)), PaidThrough.WrongStatus.selector);
        } else if (b.status == PaidThrough.Status.Paid) {
            _mustFail(payer, abi.encodeCall(PaidThrough.pay, (id)), PaidThrough.WrongStatus.selector);
            _mustFail(b.payee, abi.encodeCall(PaidThrough.cancel, (id)), PaidThrough.WrongStatus.selector);
            if (ghostNow < b.claimBy) {
                _mustFail(stranger, abi.encodeCall(PaidThrough.refund, (id)), PaidThrough.ClaimWindowOpen.selector);
            } else {
                _mustFail(b.payee, abi.encodeCall(PaidThrough.claim, (id)), PaidThrough.ClaimWindowClosed.selector);
                _mustFail(b.payee, abi.encodeCall(PaidThrough.decline, (id)), PaidThrough.ClaimWindowClosed.selector);
            }
        } else {
            // Claimed, Declined, Refunded or Cancelled: nothing may happen to the bill again.
            _mustFail(payer, abi.encodeCall(PaidThrough.pay, (id)), PaidThrough.WrongStatus.selector);
            _mustFail(b.payee, abi.encodeCall(PaidThrough.claim, (id)), PaidThrough.WrongStatus.selector);
            _mustFail(b.payee, abi.encodeCall(PaidThrough.decline, (id)), PaidThrough.WrongStatus.selector);
            _mustFail(b.payee, abi.encodeCall(PaidThrough.cancel, (id)), PaidThrough.WrongStatus.selector);
            _mustFail(stranger, abi.encodeCall(PaidThrough.refund, (id)), PaidThrough.WrongStatus.selector);
        }
        calls["attemptBadCalls"]++;
    }

    function _mustFail(address from, bytes memory data, bytes4 expected) internal {
        vm.prank(from);
        (bool ok, bytes memory ret) = address(pt).call(data);
        if (ok) {
            calls["unexpected"]++;
        } else if (ret.length < 4 || bytes4(ret) != expected) {
            calls["wrongError"]++;
        } else {
            calls["expectedFailure"]++;
        }
    }

    function warp(uint256 dt) external {
        ghostNow += bound(dt, 1, 20 days);
        vm.warp(ghostNow);
        calls["warp"]++;
    }

    function _afterOut(uint256 id, PaidThrough.Status s, uint96 amount) internal {
        expectedStatus[id] = s;
        ghostHeld -= amount;
    }

    function callCount(string calldata action) external view returns (uint256) {
        return calls[bytes32(bytes(action))];
    }

    function noopCount(string calldata action) external view returns (uint256) {
        return calls[keccak256(abi.encode("noop", bytes32(bytes(action))))];
    }

    function payerCount() external view returns (uint256) {
        return payers.length;
    }

    function payeeCount() external view returns (uint256) {
        return payees.length;
    }
}

contract PaidThroughInvariantTest is Test {
    uint96 internal constant MAX_AMOUNT = 10_000e6;

    MockFiatToken internal usdc;
    PaidThrough internal pt;
    PaidThroughHandler internal handler;

    function setUp() public {
        vm.warp(1_800_000_000);
        usdc = new MockFiatToken();
        pt = new PaidThrough(address(usdc), MAX_AMOUNT);
        handler = new PaidThroughHandler(pt, usdc, MAX_AMOUNT);

        bytes4[] memory selectors = new bytes4[](9);
        selectors[0] = PaidThroughHandler.issue.selector;
        selectors[1] = PaidThroughHandler.pay.selector;
        selectors[2] = PaidThroughHandler.payWithAuthorization.selector;
        selectors[3] = PaidThroughHandler.claim.selector;
        selectors[4] = PaidThroughHandler.decline.selector;
        selectors[5] = PaidThroughHandler.refund.selector;
        selectors[6] = PaidThroughHandler.cancel.selector;
        selectors[7] = PaidThroughHandler.warp.selector;
        selectors[8] = PaidThroughHandler.attemptBadCalls.selector;
        targetSelector(FuzzSelector({addr: address(handler), selectors: selectors}));
        targetContract(address(handler));
    }

    /// The handler's actions really change state (guards against a vacuous invariant run).
    function test_handlerActionsAreLive() public {
        handler.issue(0, 25_500_000, 1 days, 1 hours, 1); // bill 1, open to anyone
        handler.pay(1, 0);
        handler.issue(1, 7_000_000, 1 days, 1 hours, 1); // bill 2
        handler.payWithAuthorization(2, 1, 0);
        handler.claim(1);
        handler.decline(2);
        handler.issue(2, 1, 1 days, 1 hours, 1); // bill 3
        handler.cancel(3);
        handler.issue(0, MAX_AMOUNT, 1 days, 1 hours, 1); // bill 4
        handler.pay(4, 2);
        handler.attemptBadCalls(4, 0); // Paid, before claimBy: refund -> ClaimWindowOpen
        handler.warp(2 hours);
        handler.attemptBadCalls(4, 0); // Paid, after claimBy: claim/decline -> ClaimWindowClosed
        handler.refund(4, 0);
        handler.attemptBadCalls(1, 0); // Claimed: everything -> WrongStatus
        handler.issue(0, 1, 1 hours, 1 hours, 3); // bill 5, allowedPayer set
        handler.attemptBadCalls(5, 0); // Open: non-allowed payer -> NotAllowedPayer
        handler.warp(2 hours);
        handler.attemptBadCalls(5, 0); // Open after payBy -> PayWindowClosed
        handler.pay(5, 0); // no-op: pay window closed

        assertEq(handler.callCount("issue"), 5);
        assertEq(handler.callCount("pay"), 2);
        assertEq(handler.noopCount("pay"), 1);
        assertEq(handler.callCount("payWithAuthorization"), 1);
        assertEq(handler.callCount("claim"), 1);
        assertEq(handler.callCount("decline"), 1);
        assertEq(handler.callCount("cancel"), 1);
        assertEq(handler.callCount("refund"), 1);
        assertEq(handler.callCount("unexpected"), 0);
        assertEq(handler.callCount("wrongError"), 0);
        assertGt(handler.callCount("expectedFailure"), 20);
        assertEq(uint8(pt.getBill(1).status), uint8(PaidThrough.Status.Claimed));
        assertEq(uint8(pt.getBill(2).status), uint8(PaidThrough.Status.Declined));
        assertEq(uint8(pt.getBill(3).status), uint8(PaidThrough.Status.Cancelled));
        assertEq(uint8(pt.getBill(4).status), uint8(PaidThrough.Status.Refunded));
        assertEq(uint8(pt.getBill(5).status), uint8(PaidThrough.Status.Open));
        assertEq(handler.ghostClaimed(), 25_500_000);
        assertEq(handler.ghostReturned(), 7_000_000 + MAX_AMOUNT);
        assertEq(usdc.balanceOf(address(pt)), 0);
        invariant_balanceEqualsSumOfPaid();
        invariant_moneyOnlyMovesPayerContractPayee();
        invariant_statusMatchesGhost_terminalNeverChanges();
        invariant_paidBillsWellFormed();
    }

    /// Logs per-run counts and running totals across all runs (kept in process env vars, since every run starts
    /// from the post-setUp snapshot).
    function afterInvariant() public {
        string[8] memory actions =
            [string("issue"), "pay", "payWithAuthorization", "claim", "decline", "refund", "cancel", "expectedFailure"];
        uint256 runs = vm.envOr("PT_INV_RUNS", uint256(0)) + 1;
        vm.setEnv("PT_INV_RUNS", vm.toString(runs));
        console2.log("invariant runs so far", runs);
        for (uint256 i; i < actions.length; ++i) {
            string memory key = string.concat("PT_INV_TOTAL_", actions[i]);
            uint256 total = vm.envOr(key, uint256(0)) + handler.callCount(actions[i]);
            vm.setEnv(key, vm.toString(total));
            console2.log(string.concat("all runs: ", actions[i]), total);
        }
    }

    /// Token balance of the contract equals the sum of amounts over Paid bills (no stray tokens in this run).
    function invariant_balanceEqualsSumOfPaid() public view {
        uint256 sum;
        uint256 n = pt.billCount();
        for (uint256 id = 1; id <= n; ++id) {
            PaidThrough.Bill memory b = pt.getBill(id);
            if (b.status == PaidThrough.Status.Paid) sum += b.amount;
        }
        assertEq(usdc.balanceOf(address(pt)), sum, "balance == sum(Paid)");
        assertEq(sum, handler.ghostHeld(), "ghost held");
    }

    /// USDC is never created or lost, and only moves payer -> contract -> payee (claim) or back to payer.
    /// Checked against real token balances, not ghost arithmetic.
    function invariant_moneyOnlyMovesPayerContractPayee() public view {
        uint256 payerSum;
        uint256 payeeSum;
        for (uint256 i; i < handler.payerCount(); ++i) {
            payerSum += usdc.balanceOf(handler.payers(i));
        }
        for (uint256 i; i < handler.payeeCount(); ++i) {
            payeeSum += usdc.balanceOf(handler.payees(i));
        }
        uint256 held = usdc.balanceOf(address(pt));
        assertEq(payerSum + payeeSum + held, usdc.totalSupply(), "no USDC created, lost or sent elsewhere");
        assertEq(payeeSum, handler.ghostClaimed(), "payees hold exactly what they claimed");
        assertEq(
            payerSum,
            handler.payerCount() * handler.PAYER_START() - handler.ghostTotalIn() + handler.ghostReturned(),
            "payers hold start - paid + returned"
        );
    }

    /// Every bill's status is exactly what the successful calls imply; terminal statuses never change; every
    /// guarded bad call failed with its exact error.
    function invariant_statusMatchesGhost_terminalNeverChanges() public view {
        uint256 n = pt.billCount();
        for (uint256 id = 1; id <= n; ++id) {
            assertEq(uint8(pt.getBill(id).status), uint8(handler.expectedStatus(id)), "status drift");
        }
        assertEq(handler.callCount("unexpected"), 0, "a guarded call succeeded");
        assertEq(handler.callCount("wrongError"), 0, "a guarded call failed with the wrong error");
    }

    function invariant_paidBillsWellFormed() public view {
        uint256 n = pt.billCount();
        for (uint256 id = 1; id <= n; ++id) {
            PaidThrough.Bill memory b = pt.getBill(id);
            if (b.status == PaidThrough.Status.Open || b.status == PaidThrough.Status.Cancelled) {
                assertEq(b.payer, address(0));
                assertEq(b.claimBy, 0);
            } else {
                assertTrue(b.payer != address(0), "payer set");
                assertEq(b.payer, handler.ghostPayer(id));
                assertEq(uint256(b.claimBy), handler.ghostPaidAt(id) + b.claimWindow, "claimBy == paidAt + window");
            }
            if (b.allowedPayer != address(0) && b.payer != address(0)) assertEq(b.payer, b.allowedPayer);
        }
    }
}
