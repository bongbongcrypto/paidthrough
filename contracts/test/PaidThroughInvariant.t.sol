// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;

import {Test} from "forge-std/Test.sol";
import {PaidThrough} from "../src/PaidThrough.sol";
import {MockFiatToken} from "./mocks/MockFiatToken.sol";

/// @notice Drives PaidThrough with several actors; ghost state mirrors what each successful call must have done.
contract PaidThroughHandler is Test {
    PaidThrough public immutable pt;
    MockFiatToken public immutable usdc;
    uint96 public immutable maxAmount;

    uint256[] internal payerKeys;
    address[] public payers;
    address[] public payees;

    uint256 public ghostNow;
    uint256 public ghostTotalIn;
    uint256 public ghostTotalOut;
    uint256 public ghostHeld; // sum of amounts over Paid bills
    mapping(uint256 => PaidThrough.Status) public expectedStatus;
    mapping(uint256 => uint256) public ghostPaidAt;
    mapping(uint256 => address) public ghostPayer;

    mapping(bytes32 => uint256) public calls; // successful calls per action

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
            usdc.mint(a, 1_000_000e6);
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
        if (id == 0) return;
        PaidThrough.Bill memory b = pt.getBill(id);
        if (b.status != PaidThrough.Status.Open || ghostNow >= b.payBy) return;
        address payer = payers[_payablePayer(b, payerSeed)];
        vm.prank(payer);
        pt.pay(id);
        _afterPaid(id, payer, b.amount);
        calls["pay"]++;
    }

    function payWithAuthorization(uint256 billSeed, uint256 payerSeed, uint256 relayerSeed) external atGhostTime {
        uint256 id = _billId(billSeed);
        if (id == 0) return;
        PaidThrough.Bill memory b = pt.getBill(id);
        if (b.status != PaidThrough.Status.Open || ghostNow >= b.payBy) return;
        uint256 idx = _payablePayer(b, payerSeed);
        address payer = payers[idx];
        uint256 validBefore = ghostNow + 1 hours;
        bytes32 structHash = keccak256(
            abi.encode(
                usdc.RECEIVE_WITH_AUTHORIZATION_TYPEHASH(),
                payer,
                address(pt),
                uint256(b.amount),
                uint256(0),
                validBefore,
                pt.authNonce(id)
            )
        );
        bytes32 digest = keccak256(abi.encodePacked("\x19\x01", usdc.DOMAIN_SEPARATOR(), structHash));
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(payerKeys[idx], digest);
        vm.prank(address(uint160(0x3000 + relayerSeed % 5)));
        pt.payWithAuthorization(id, payer, 0, validBefore, v, r, s);
        _afterPaid(id, payer, b.amount);
        calls["payWithAuthorization"]++;
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
        if (id == 0) return;
        PaidThrough.Bill memory b = pt.getBill(id);
        if (b.status != PaidThrough.Status.Paid || ghostNow >= b.claimBy) return;
        vm.prank(b.payee);
        pt.claim(id);
        _afterOut(id, PaidThrough.Status.Claimed, b.amount);
        calls["claim"]++;
    }

    function decline(uint256 billSeed) external atGhostTime {
        uint256 id = _billId(billSeed);
        if (id == 0) return;
        PaidThrough.Bill memory b = pt.getBill(id);
        if (b.status != PaidThrough.Status.Paid || ghostNow >= b.claimBy) return;
        vm.prank(b.payee);
        pt.decline(id);
        _afterOut(id, PaidThrough.Status.Declined, b.amount);
        calls["decline"]++;
    }

    function refund(uint256 billSeed, uint256 callerSeed) external atGhostTime {
        uint256 id = _billId(billSeed);
        if (id == 0) return;
        PaidThrough.Bill memory b = pt.getBill(id);
        if (b.status != PaidThrough.Status.Paid || ghostNow < b.claimBy) return;
        vm.prank(address(uint160(0x4000 + callerSeed % 7)));
        pt.refund(id);
        _afterOut(id, PaidThrough.Status.Refunded, b.amount);
        calls["refund"]++;
    }

    function cancel(uint256 billSeed) external atGhostTime {
        uint256 id = _billId(billSeed);
        if (id == 0) return;
        PaidThrough.Bill memory b = pt.getBill(id);
        if (b.status != PaidThrough.Status.Open) return;
        vm.prank(b.payee);
        pt.cancel(id);
        expectedStatus[id] = PaidThrough.Status.Cancelled;
        calls["cancel"]++;
    }

    /// @dev Calls that must fail; if any succeeds the ghost status check catches it.
    function attemptBadCalls(uint256 billSeed, uint256 who) external atGhostTime {
        uint256 id = _billId(billSeed);
        if (id == 0) return;
        address caller = address(uint160(0x5000 + who % 3));
        vm.startPrank(caller);
        try pt.claim(id) {
            calls["unexpected"]++;
        } catch {}
        try pt.decline(id) {
            calls["unexpected"]++;
        } catch {}
        try pt.cancel(id) {
            calls["unexpected"]++;
        } catch {}
        vm.stopPrank();
        PaidThrough.Bill memory b = pt.getBill(id);
        if (b.status != PaidThrough.Status.Paid) {
            try pt.refund(id) {
                calls["unexpected"]++;
            } catch {}
        }
        calls["attemptBadCalls"]++;
    }

    function warp(uint256 dt) external {
        ghostNow += bound(dt, 1, 20 days);
        vm.warp(ghostNow);
        calls["warp"]++;
    }

    function _afterOut(uint256 id, PaidThrough.Status s, uint96 amount) internal {
        expectedStatus[id] = s;
        ghostTotalOut += amount;
        ghostHeld -= amount;
    }

    function callCount(string calldata action) external view returns (uint256) {
        return calls[bytes32(bytes(action))];
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

    function invariant_inEqualsOutPlusHeld() public view {
        assertEq(handler.ghostTotalIn(), handler.ghostTotalOut() + handler.ghostHeld());
        assertEq(usdc.balanceOf(address(pt)), handler.ghostTotalIn() - handler.ghostTotalOut());
    }

    /// Every bill's status is exactly what the successful calls imply; terminal statuses never change.
    function invariant_statusMatchesGhost_terminalNeverChanges() public view {
        uint256 n = pt.billCount();
        for (uint256 id = 1; id <= n; ++id) {
            assertEq(uint8(pt.getBill(id).status), uint8(handler.expectedStatus(id)), "status drift");
        }
        assertEq(handler.callCount("unexpected"), 0, "a guarded call succeeded");
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
