// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;

import {Test} from "forge-std/Test.sol";
import {console2} from "forge-std/console2.sol";
import {PaidThrough} from "../src/PaidThrough.sol";

interface IArcUSDC {
    function DOMAIN_SEPARATOR() external view returns (bytes32);
    function decimals() external view returns (uint8);
    function name() external view returns (string memory);
    function version() external view returns (string memory);
    function balanceOf(address) external view returns (uint256);
    function allowance(address, address) external view returns (uint256);
    function approve(address, uint256) external returns (bool);
    function authorizationState(address, bytes32) external view returns (bool);
}

/// @notice Runs only with ARC_RPC_URL set. Forks Arc mainnet and exercises PaidThrough against the real USDC.
///         Read-only against the network: nothing is broadcast; all state lives in the local fork.
contract ArcForkTest is Test {
    address internal constant ARC_USDC = 0x3600000000000000000000000000000000000000;
    bytes32 internal constant ARC_MAINNET_DOMAIN_SEPARATOR =
        0x940506929bba468048a19b567f4f0d534714bc06604b5c3017e5d16785ccdf84;
    bytes32 internal constant RECEIVE_TYPEHASH = 0xd099cc98ef71107a616c4f0f941f04c322d8e254fe26b3c6668db87aae413de8;
    uint96 internal constant MAX_AMOUNT = 10_000e6;
    uint96 internal constant AMOUNT = 25_500_000; // 25.50 USDC
    uint256 internal constant PAYER_PK = 0xB0B; // Foundry test key, never funded on any real network

    IArcUSDC internal usdc = IArcUSDC(ARC_USDC);

    function _fork() internal returns (bool) {
        string memory url = vm.envOr("ARC_RPC_URL", string(""));
        if (bytes(url).length == 0) {
            console2.log("ARC_RPC_URL not set: skipping Arc fork test");
            vm.skip(true);
            return false;
        }
        vm.createSelectFork(url);
        console2.log("forked chain id", block.chainid);
        console2.log("fork block", block.number);
        return true;
    }

    function test_arc_usdcDomainAndDecimals() public {
        if (!_fork()) return;
        assertEq(block.chainid, 5042, "Arc mainnet chain id");
        assertEq(usdc.DOMAIN_SEPARATOR(), ARC_MAINNET_DOMAIN_SEPARATOR, "USDC domain separator");
        assertEq(usdc.decimals(), 6, "USDC ERC-20 decimals");
        console2.log("USDC name", usdc.name());
        console2.log("USDC version", usdc.version());

        PaidThrough pt = new PaidThrough(ARC_USDC, MAX_AMOUNT);
        assertEq(address(pt.usdc()), ARC_USDC);
        console2.log("PaidThrough deployed on fork at", address(pt));
        console2.log("runtime size (bytes)", address(pt).code.length);
    }

    function test_arc_fundingAndFlows() public {
        if (!_fork()) return;
        address payer = vm.addr(PAYER_PK);
        address payee = makeAddr("arc-payee");

        // Diagnostic: does the fork link native balance (18 dec) and the ERC-20 view (6 dec)?
        address cb = block.coinbase;
        console2.log("coinbase", cb);
        console2.log("coinbase native balance (18 dec)", cb.balance);
        try usdc.balanceOf(cb) returns (uint256 b) {
            console2.log("coinbase USDC.balanceOf (6 dec)", b);
        } catch (bytes memory err) {
            console2.log("coinbase USDC.balanceOf reverted, bytes:", err.length);
            console2.logBytes(err);
        }

        vm.deal(payer, 30 ether); // 30 USDC in native 18-decimal units
        console2.log("payer native balance after vm.deal", payer.balance);
        uint256 bal;
        try usdc.balanceOf(payer) returns (uint256 b) {
            bal = b;
            console2.log("payer USDC.balanceOf after vm.deal", b);
        } catch (bytes memory err) {
            console2.log("RESULT: USDC.balanceOf(payer) reverted on the fork; native<->ERC-20 link not emulated");
            console2.logBytes(err);
            return;
        }
        if (bal != 30e6) {
            console2.log("RESULT: vm.deal did not fund the ERC-20 view (expected 30000000). Flows not run on fork.");
            return;
        }
        console2.log("RESULT: vm.deal funds the ERC-20 view; running flows against the real USDC");

        PaidThrough pt = new PaidThrough(ARC_USDC, MAX_AMOUNT);

        // Flow 1: issue -> payWithAuthorization (real domain) -> claim.
        vm.prank(payee);
        uint256 a = pt.issue(AMOUNT, uint64(block.timestamp + 1 days), 1 days, payer, keccak256("arc-fork-a"));
        uint256 validBefore = block.timestamp + 1 hours;
        bytes32 structHash = keccak256(
            abi.encode(RECEIVE_TYPEHASH, payer, address(pt), uint256(AMOUNT), uint256(0), validBefore, pt.authNonce(a))
        );
        bytes32 digest = keccak256(abi.encodePacked("\x19\x01", usdc.DOMAIN_SEPARATOR(), structHash));
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(PAYER_PK, digest);
        try pt.payWithAuthorization(a, payer, 0, validBefore, v, r, s) {
            console2.log("payWithAuthorization on real USDC: ok");
        } catch (bytes memory err) {
            console2.log("payWithAuthorization on real USDC reverted:");
            console2.logBytes(err);
            revert("payWithAuthorization failed on fork");
        }
        assertEq(usdc.balanceOf(address(pt)), AMOUNT);
        assertTrue(usdc.authorizationState(payer, pt.authNonce(a)));
        vm.prank(payee);
        pt.claim(a);
        assertEq(usdc.balanceOf(payee), AMOUNT);
        assertEq(usdc.balanceOf(address(pt)), 0);
        console2.log("flow 1 (auth pay -> claim) ok; payer USDC left", usdc.balanceOf(payer));

        // Flow 2: issue -> approve + pay -> warp past claimBy -> refund by a third party.
        vm.prank(payee);
        uint256 b2 = pt.issue(AMOUNT, uint64(block.timestamp + 1 days), 1 hours, address(0), keccak256("arc-fork-b"));
        uint256 payerBefore = usdc.balanceOf(payer);
        vm.startPrank(payer);
        usdc.approve(address(pt), AMOUNT);
        pt.pay(b2);
        vm.stopPrank();
        assertEq(usdc.balanceOf(payer), payerBefore - AMOUNT);
        vm.warp(pt.getBill(b2).claimBy);
        vm.prank(makeAddr("arc-keeper"));
        pt.refund(b2);
        assertEq(usdc.balanceOf(payer), payerBefore);
        assertEq(usdc.balanceOf(address(pt)), 0);
        console2.log("flow 2 (pay -> refund) ok");
    }
}
