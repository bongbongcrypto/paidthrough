// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;

/// @notice Code a payer EOA may carry through an EIP-7702 delegation (emulated in tests with vm.etch).
///         Accepts a signature only if it was made by this account's own key (address(this) is the EOA).
contract EcdsaSelfDelegate {
    function isValidSignature(bytes32 digest, bytes calldata sig) external view returns (bytes4) {
        if (sig.length != 65) return 0xffffffff;
        bytes32 r = bytes32(sig[0:32]);
        bytes32 s = bytes32(sig[32:64]);
        uint8 v = uint8(sig[64]);
        address signer = ecrecover(digest, v, r, s);
        return signer != address(0) && signer == address(this) ? bytes4(0x1626ba7e) : bytes4(0xffffffff);
    }
}

/// @notice Delegate code without ERC-1271 support (e.g. a sweeper delegation): every signature check fails.
contract NoErc1271Delegate {
    fallback() external payable {
        revert();
    }
}
