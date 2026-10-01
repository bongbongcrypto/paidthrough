// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;

import {Test} from "forge-std/Test.sol";
import {PaidThrough} from "../../src/PaidThrough.sol";
import {MockFiatToken} from "../mocks/MockFiatToken.sol";

/// @notice Shared setup: a mock USDC, PaidThrough with a 10_000 USDC cap, and funded actors.
abstract contract PaidThroughBase is Test {
    event BillIssued(
        uint256 indexed billId,
        address indexed payee,
        address indexed allowedPayer,
        uint96 amount,
        uint64 payBy,
        uint32 claimWindow,
        bytes32 ref
    );
    event BillCancelled(uint256 indexed billId);
    event BillPaid(uint256 indexed billId, address indexed payer, uint64 claimBy);
    event BillClaimed(uint256 indexed billId, address indexed payee, uint96 amount);
    event BillDeclined(uint256 indexed billId, address indexed payer, uint96 amount);
    event BillRefunded(uint256 indexed billId, address indexed payer, uint96 amount, address caller);

    uint96 internal constant MAX_AMOUNT = 10_000e6; // 10,000 USDC in 6-decimal base units
    uint96 internal constant AMOUNT = 25_500_000; // 25.50 USDC
    uint32 internal constant CLAIM_WINDOW = 7 days;
    uint64 internal constant PAY_WINDOW = 30 days;
    bytes32 internal constant REF = keccak256("paidthrough:v1:test-ref");
    uint256 internal constant START_TIME = 1_800_000_000;

    bytes32 internal constant RECEIVE_TYPEHASH = 0xd099cc98ef71107a616c4f0f941f04c322d8e254fe26b3c6668db87aae413de8;

    // Foundry test keys only (small literal ints).
    uint256 internal constant PAYER_PK = 0xB0B;
    uint256 internal constant OTHER_PK = 0xC0C;

    MockFiatToken internal usdc;
    PaidThrough internal pt;

    address internal payee = makeAddr("payee");
    address internal payee2 = makeAddr("payee2");
    address internal payer;
    address internal other;
    address internal stranger = makeAddr("stranger");
    address internal relayer = makeAddr("relayer");

    uint256 internal constant START_BALANCE = 100_000e6;

    function setUp() public virtual {
        vm.warp(START_TIME);
        usdc = new MockFiatToken();
        pt = new PaidThrough(address(usdc), MAX_AMOUNT);
        payer = vm.addr(PAYER_PK);
        other = vm.addr(OTHER_PK);
        usdc.mint(payer, START_BALANCE);
        usdc.mint(other, START_BALANCE);
        usdc.mint(stranger, START_BALANCE);
    }

    // ---- helpers ----

    function _issue() internal returns (uint256) {
        return _issueAs(payee, AMOUNT, address(0));
    }

    function _issueAs(address who, uint96 amount, address allowedPayer) internal returns (uint256) {
        vm.prank(who);
        return pt.issue(amount, uint64(block.timestamp) + PAY_WINDOW, CLAIM_WINDOW, allowedPayer, REF);
    }

    function _pay(uint256 billId, address from) internal {
        uint96 amount = pt.getBill(billId).amount;
        vm.startPrank(from);
        usdc.approve(address(pt), amount);
        pt.pay(billId);
        vm.stopPrank();
    }

    function _issueAndPay() internal returns (uint256 billId) {
        billId = _issue();
        _pay(billId, payer);
    }

    function _sign(uint256 pk, address to, uint256 value, uint256 validAfter, uint256 validBefore, bytes32 nonce)
        internal
        view
        returns (uint8 v, bytes32 r, bytes32 s)
    {
        return _signFor(usdc.DOMAIN_SEPARATOR(), pk, to, value, validAfter, validBefore, nonce);
    }

    function _signFor(
        bytes32 domainSeparator,
        uint256 pk,
        address to,
        uint256 value,
        uint256 validAfter,
        uint256 validBefore,
        bytes32 nonce
    ) internal pure returns (uint8 v, bytes32 r, bytes32 s) {
        address from = vm.addr(pk);
        bytes32 structHash = keccak256(abi.encode(RECEIVE_TYPEHASH, from, to, value, validAfter, validBefore, nonce));
        bytes32 digest = keccak256(abi.encodePacked("\x19\x01", domainSeparator, structHash));
        return vm.sign(pk, digest);
    }

    function _status(uint256 billId) internal view returns (PaidThrough.Status) {
        return pt.getBill(billId).status;
    }

    function _assertStatus(uint256 billId, PaidThrough.Status expected) internal view {
        assertEq(uint8(pt.getBill(billId).status), uint8(expected), "status");
    }
}
