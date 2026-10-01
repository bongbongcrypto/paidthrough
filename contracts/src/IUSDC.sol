// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;

/// @title IUSDC
/// @notice The subset of Circle's FiatToken (USDC, 6 decimals) that PaidThrough calls.
interface IUSDC {
    /// @notice Moves `value` base units from the caller to `to`.
    function transfer(address to, uint256 value) external returns (bool);

    /// @notice Moves `value` base units from `from` to `to` using the caller's allowance.
    function transferFrom(address from, address to, uint256 value) external returns (bool);

    /// @notice Returns the balance of `account` in base units (6 decimals).
    function balanceOf(address account) external view returns (uint256);

    /// @notice EIP-3009: moves `value` from `from` to `to` with `from`'s signature; caller must be `to`.
    function receiveWithAuthorization(
        address from,
        address to,
        uint256 value,
        uint256 validAfter,
        uint256 validBefore,
        bytes32 nonce,
        uint8 v,
        bytes32 r,
        bytes32 s
    ) external;
}
