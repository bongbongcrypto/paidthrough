// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;

import {MockFiatToken} from "./MockFiatToken.sol";

/// @notice Hostile token: on an outgoing transfer it re-enters a target with prepared calldata once and
///         records whether the inner call succeeded. USDC has no hooks; this only proves checks-effects-interactions.
contract ReentrantToken is MockFiatToken {
    address public target;
    bytes public reentryData;
    bool public armed;
    bool public reentered;
    bool public innerSuccess;
    bytes public innerRevertData;

    function arm(address target_, bytes calldata data) external {
        target = target_;
        reentryData = data;
        armed = true;
    }

    function transfer(address to, uint256 value) public override returns (bool) {
        if (armed) {
            armed = false;
            reentered = true;
            (bool ok, bytes memory ret) = target.call(reentryData);
            innerSuccess = ok;
            innerRevertData = ret;
        }
        return super.transfer(to, value);
    }
}
