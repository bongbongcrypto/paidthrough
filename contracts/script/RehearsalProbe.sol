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

    /// @notice Runs `calls` in order (a failed call does not stop the rest), then reads token and native balances.
    function run(Call[] calldata calls, address token, address[] calldata watch)
        external
        returns (Result[] memory results, uint256[] memory tokenBalances, uint256[] memory nativeBalances)
    {
        results = new Result[](calls.length);
        for (uint256 i; i < calls.length; ++i) {
            (bool ok, bytes memory ret) = calls[i].target.call(calls[i].data);
            results[i] = Result(ok, ret);
        }
        tokenBalances = new uint256[](watch.length);
        nativeBalances = new uint256[](watch.length);
        for (uint256 i; i < watch.length; ++i) {
            tokenBalances[i] = IBalanceOf(token).balanceOf(watch[i]);
            nativeBalances[i] = watch[i].balance;
        }
    }
}
