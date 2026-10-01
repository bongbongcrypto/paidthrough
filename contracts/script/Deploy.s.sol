// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;

import {Script} from "forge-std/Script.sol";
import {console2} from "forge-std/console2.sol";
import {PaidThrough} from "../src/PaidThrough.sol";

/// @notice Deploys PaidThrough. Reads USDC and MAX_AMOUNT (USDC base units, 6 decimals) from the environment.
///         No key in code: the signer comes from the forge CLI at broadcast time.
contract Deploy is Script {
    function run() external returns (PaidThrough pt) {
        address usdc = vm.envAddress("USDC");
        uint256 maxAmount = vm.envUint("MAX_AMOUNT");
        require(usdc.code.length > 0, "USDC has no code on this chain");
        require(maxAmount > 0 && maxAmount <= type(uint96).max, "MAX_AMOUNT out of range");
        // Guard against an 18-decimal value: more than 1,000,000 USDC per bill is almost surely a unit mistake.
        require(maxAmount <= 1_000_000e6, "MAX_AMOUNT looks like 18 decimals; use 6-decimal base units");

        vm.startBroadcast();
        pt = new PaidThrough(usdc, uint96(maxAmount));
        vm.stopBroadcast();

        console2.log("PaidThrough deployed at", address(pt));
        console2.log("usdc", usdc);
        console2.log("maxAmount (base units)", maxAmount);
    }
}
