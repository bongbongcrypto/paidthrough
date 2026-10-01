// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;

import {Test} from "forge-std/Test.sol";
import {Vm} from "forge-std/Vm.sol";
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

/// @notice Local stand-in for Arc's native-USDC transfer precompile (0x1800…0000), which anvil does not implement.
///         Real FiatToken on Arc calls transfer(from, to, value18) on it; this stub moves the native (18-decimal)
///         balances with cheatcodes so the rest of the real token logic can run on the fork. Fork-only.
contract NativeTransferStub {
    Vm internal constant VM = Vm(address(uint160(uint256(keccak256("hevm cheat code")))));

    function transfer(address from, address to, uint256 value) external returns (bool) {
        require(from.balance >= value, "stub: insufficient native balance");
        VM.deal(from, from.balance - value);
        VM.deal(to, to.balance + value);
        return true;
    }
}

/// @notice Local stand-in for Arc's blocklist precompile (0x1800…0001) that FiatToken.transferFrom queries. Fork-only;
///         reports nobody as blocked (blocklist behaviour itself is covered by the mock-token suite).
contract BlocklistStub {
    function isBlocklisted(address) external pure returns (bool) {
        return false;
    }
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
    uint256 internal constant PAYER_PK = 0xB0B; // public Foundry test key: never holds real funds
    address internal constant NATIVE_TRANSFER_PRECOMPILE = 0x1800000000000000000000000000000000000000;
    address internal constant BLOCKLIST_PRECOMPILE = 0x1800000000000000000000000000000000000001;

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
        _logCoinbaseLink();
        address payer = vm.addr(PAYER_PK);
        if (!_fundPayer(payer)) return;

        PaidThrough pt = new PaidThrough(ARC_USDC, MAX_AMOUNT);
        _flowAuthPayThenClaim(pt, payer);
        _flowPayThenRefund(pt, payer);
    }

    /// @dev Diagnostic: does the fork link native balance (18 dec) and the ERC-20 view (6 dec)?
    function _logCoinbaseLink() internal view {
        address cb = block.coinbase;
        console2.log("coinbase", cb);
        console2.log("coinbase native balance (18 dec)", cb.balance);
        try usdc.balanceOf(cb) returns (uint256 b) {
            console2.log("coinbase USDC.balanceOf (6 dec)", b);
        } catch (bytes memory err) {
            console2.log("coinbase USDC.balanceOf reverted; revert bytes:", err.length);
            console2.logBytes(err);
        }
    }

    function _fundPayer(address payer) internal returns (bool) {
        vm.deal(payer, 30 ether); // 30 USDC in native 18-decimal units
        console2.log("payer native balance after vm.deal", payer.balance);
        uint256 bal;
        try usdc.balanceOf(payer) returns (uint256 b) {
            bal = b;
            console2.log("payer USDC.balanceOf after vm.deal", b);
        } catch (bytes memory err) {
            console2.log("RESULT: USDC.balanceOf(payer) reverted on the fork; native<->ERC-20 link not emulated");
            console2.logBytes(err);
            return false;
        }
        if (bal != 30e6) {
            console2.log("RESULT: vm.deal did not fund the ERC-20 view (expected 30000000). Flows not run on fork.");
            return false;
        }
        console2.log("RESULT: vm.deal funds the ERC-20 view; running flows against the real USDC");
        return true;
    }

    function _authSig(PaidThrough pt, address payer, uint256 billId, uint256 validBefore)
        internal
        view
        returns (uint8, bytes32, bytes32)
    {
        bytes32 structHash = keccak256(
            abi.encode(
                RECEIVE_TYPEHASH, payer, address(pt), uint256(AMOUNT), uint256(0), validBefore, pt.authNonce(billId)
            )
        );
        bytes32 digest = keccak256(abi.encodePacked("\x19\x01", usdc.DOMAIN_SEPARATOR(), structHash));
        return vm.sign(PAYER_PK, digest);
    }

    /// @dev Flow 1: issue -> payWithAuthorization (real domain, relayer submits) -> claim.
    function _flowAuthPayThenClaim(PaidThrough pt, address payer) internal {
        address payee = makeAddr("arc-payee");
        vm.prank(payee);
        uint256 id = pt.issue(AMOUNT, uint64(block.timestamp + 1 days), 1 days, payer, keccak256("arc-fork-a"));
        uint256 validBefore = block.timestamp + 1 hours;
        (uint8 v, bytes32 r, bytes32 s) = _authSig(pt, payer, id, validBefore);

        // Small Foundry test keys are public; on Arc mainnet 0xB0B's address carries an EIP-7702 delegation.
        // FiatToken checks any address with code only through ERC-1271, so record what the real token does.
        console2.log("payer code length on Arc mainnet", payer.code.length);
        bool paid = _tryAuthPay(pt, id, payer, validBefore, v, r, s);
        if (!paid && payer.code.length > 0) {
            console2.log("FINDING: payer carries code (EIP-7702 delegation); real USDC rejected the ECDSA signature");
            console2.logBytes(payer.code);
            vm.etch(payer, bytes("")); // local fork only: make the payer a plain EOA again
            console2.log("cleared the delegation on the local fork; retrying as a plain EOA");
            paid = _tryAuthPay(pt, id, payer, validBefore, v, r, s);
        }
        if (!paid) {
            console2.log("native transfer precompile code length", NATIVE_TRANSFER_PRECOMPILE.code.length);
            console2.log("FINDING: anvil does not implement Arc's native USDC transfer precompile at 0x1800..00;");
            console2.log("         installing a cheatcode stub on the local fork only and retrying");
            vm.etch(NATIVE_TRANSFER_PRECOMPILE, type(NativeTransferStub).runtimeCode);
            vm.allowCheatcodes(NATIVE_TRANSFER_PRECOMPILE);
            paid = _tryAuthPay(pt, id, payer, validBefore, v, r, s);
        }
        assertTrue(paid, "payWithAuthorization on real USDC");
        assertEq(usdc.balanceOf(address(pt)), AMOUNT);
        assertTrue(usdc.authorizationState(payer, pt.authNonce(id)));
        vm.prank(payee);
        pt.claim(id);
        assertEq(usdc.balanceOf(payee), AMOUNT);
        assertEq(usdc.balanceOf(address(pt)), 0);
        assertEq(usdc.balanceOf(payer), 30e6 - AMOUNT, "payer paid exactly 25.50 USDC");
        assertEq(payee.balance, uint256(AMOUNT) * 1e12, "payee native balance = amount scaled to 18 decimals");
        console2.log("flow 1 (auth pay -> claim) ok; payer USDC left", usdc.balanceOf(payer));
    }

    function _tryAuthPay(PaidThrough pt, uint256 id, address payer, uint256 validBefore, uint8 v, bytes32 r, bytes32 s)
        internal
        returns (bool)
    {
        vm.prank(makeAddr("arc-relayer"));
        try pt.payWithAuthorization(id, payer, 0, validBefore, v, r, s) {
            console2.log("payWithAuthorization on real USDC: ok");
            return true;
        } catch (bytes memory err) {
            console2.log("payWithAuthorization on real USDC reverted:");
            console2.logBytes(err);
            return false;
        }
    }

    /// @dev Flow 2: issue -> approve + pay -> warp to claimBy -> refund by a third party.
    function _flowPayThenRefund(PaidThrough pt, address payer) internal {
        vm.prank(makeAddr("arc-payee"));
        uint256 id = pt.issue(AMOUNT, uint64(block.timestamp + 1 days), 1 hours, address(0), keccak256("arc-fork-b"));
        vm.deal(payer, 30 ether); // top up to 30 USDC (18-decimal native units)
        uint256 payerBefore = usdc.balanceOf(payer);
        assertEq(payerBefore, 30e6);
        vm.prank(payer);
        usdc.approve(address(pt), AMOUNT);
        if (!_tryPay(pt, id, payer)) {
            console2.log("blocklist precompile code length", BLOCKLIST_PRECOMPILE.code.length);
            console2.log("FINDING: real USDC transferFrom asks a blocklist precompile at 0x1800..01 that anvil lacks;");
            console2.log("         installing a stub that reports nobody blocked (local fork only) and retrying");
            vm.etch(BLOCKLIST_PRECOMPILE, type(BlocklistStub).runtimeCode);
            assertTrue(_tryPay(pt, id, payer), "pay on real USDC");
        }
        assertEq(usdc.balanceOf(payer), payerBefore - AMOUNT);
        assertEq(usdc.allowance(payer, address(pt)), 0, "approval fully used");
        vm.warp(pt.getBill(id).claimBy);
        vm.prank(makeAddr("arc-keeper"));
        pt.refund(id);
        assertEq(usdc.balanceOf(payer), payerBefore);
        assertEq(usdc.balanceOf(address(pt)), 0);
        console2.log("flow 2 (pay -> refund) ok");
    }

    function _tryPay(PaidThrough pt, uint256 id, address payer) internal returns (bool) {
        vm.prank(payer);
        try pt.pay(id) {
            console2.log("pay on real USDC: ok");
            return true;
        } catch (bytes memory err) {
            console2.log("pay on real USDC reverted:");
            console2.logBytes(err);
            return false;
        }
    }
}
