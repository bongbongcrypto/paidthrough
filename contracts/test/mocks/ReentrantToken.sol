// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;

import {MockFiatToken} from "./MockFiatToken.sol";

/// @notice Hostile token: right before it moves a balance (transfer, transferFrom or receiveWithAuthorization) it
///         re-enters a target once with prepared calldata and records whether that inner call succeeded.
///         USDC has no hooks; this only proves PaidThrough writes its state before every token call.
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

    function _beforeMove() internal override {
        if (!armed) return;
        armed = false;
        reentered = true;
        (bool ok, bytes memory ret) = target.call(reentryData);
        innerSuccess = ok;
        innerRevertData = ret;
    }
}
