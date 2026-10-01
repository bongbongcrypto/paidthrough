// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;

interface IBalanceOf {
    function balanceOf(address) external view returns (uint256);
}

/// @notice Read-only rehearsal helper. Never deployed: `scripts/rehearse_mainnet.py` places its runtime code at a
///         throwaway address through an `eth_call` state override, runs a short sequence of calls from that address,
///         and reads the balances the sequence leaves behind. Nothing is broadcast.
contract RehearsalProbe {
    struct Call {
        address target;
        bytes data;
    }

    struct Result {
        bool ok;
        bytes ret;
    }

    /// @notice Reads token balances of `watch`, runs `calls` in order (a failed call does not stop the rest),
    ///         then reads the balances again.
    function run(Call[] calldata calls, address token, address[] calldata watch)
        external
        returns (Result[] memory results, uint256[] memory before, uint256[] memory afterwards)
    {
        before = _balances(token, watch);
        results = new Result[](calls.length);
        for (uint256 i; i < calls.length; ++i) {
            (bool ok, bytes memory ret) = calls[i].target.call(calls[i].data);
            results[i] = Result(ok, ret);
        }
        afterwards = _balances(token, watch);
    }

    function _balances(address token, address[] calldata watch) private view returns (uint256[] memory bals) {
        bals = new uint256[](watch.length);
        for (uint256 i; i < watch.length; ++i) {
            bals[i] = IBalanceOf(token).balanceOf(watch[i]);
        }
    }
}
