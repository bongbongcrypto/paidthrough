// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;

/// @notice Test stand-in for Circle's FiatToken v2 (USDC): 6 decimals, EIP-712 domain {"USDC","2"},
///         real EIP-3009 receiveWithAuthorization, blocklist with FiatToken's modifier semantics,
///         plus fault switches (return false, skim 1 unit, over-credit 1 unit) to reach PaidThrough's defensive
///         errors. The blocklist revert message is the one Arc mainnet's real USDC returns ("Blocked address",
///         measured by scripts/rehearse_mainnet.py); Circle's generic FiatToken says "Blacklistable: ...".
///         Like Arc's real USDC (also measured there), a blocklisted transaction sender (tx.origin) cannot move
///         USDC even between two clean parties.
contract MockFiatToken {
    string public constant name = "USDC";
    string public constant symbol = "USDC";
    string public constant version = "2";
    uint8 public constant decimals = 6;

    bytes32 public constant RECEIVE_WITH_AUTHORIZATION_TYPEHASH = keccak256(
        "ReceiveWithAuthorization(address from,address to,uint256 value,uint256 validAfter,uint256 validBefore,bytes32 nonce)"
    );
    bytes32 public constant CANCEL_AUTHORIZATION_TYPEHASH =
        keccak256("CancelAuthorization(address authorizer,bytes32 nonce)");
    bytes32 private constant EIP712_DOMAIN_TYPEHASH =
        keccak256("EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)");

    bytes4 internal constant ERC1271_MAGIC = 0x1626ba7e; // isValidSignature(bytes32,bytes)

    /// @dev Where skimmed units go.
    address public constant SKIM_SINK = address(0x5C1A);

    uint256 public totalSupply;
    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;
    mapping(address => bool) public isBlacklisted;
    mapping(address => mapping(bytes32 => bool)) public authorizationState;

    bool public returnFalse;
    bool public skimOne;
    bool public overCreditOne;

    event Transfer(address indexed from, address indexed to, uint256 value);
    event Approval(address indexed owner, address indexed spender, uint256 value);
    event AuthorizationUsed(address indexed authorizer, bytes32 indexed nonce);
    event AuthorizationCanceled(address indexed authorizer, bytes32 indexed nonce);

    modifier notBlacklisted(address account) {
        require(!isBlacklisted[account], "Blocked address");
        _;
    }

    // ---- test controls ----

    function mint(address to, uint256 value) external {
        totalSupply += value;
        balanceOf[to] += value;
        emit Transfer(address(0), to, value);
    }

    function blacklist(address account) external {
        isBlacklisted[account] = true;
    }

    function unBlacklist(address account) external {
        isBlacklisted[account] = false;
    }

    function setReturnFalse(bool on) external {
        returnFalse = on;
    }

    function setSkimOne(bool on) external {
        skimOne = on;
    }

    /// @dev Credits the recipient one unit more than sent (minted from nowhere) to prove `!=` in the balance check.
    function setOverCreditOne(bool on) external {
        overCreditOne = on;
    }

    // ---- ERC-20 ----

    function approve(address spender, uint256 value)
        external
        notBlacklisted(msg.sender)
        notBlacklisted(spender)
        returns (bool)
    {
        allowance[msg.sender][spender] = value;
        emit Approval(msg.sender, spender, value);
        return true;
    }

    function transfer(address to, uint256 value)
        public
        virtual
        notBlacklisted(tx.origin)
        notBlacklisted(msg.sender)
        notBlacklisted(to)
        returns (bool)
    {
        if (returnFalse) return false;
        _transfer(msg.sender, to, value);
        return true;
    }

    function transferFrom(address from, address to, uint256 value)
        external
        notBlacklisted(tx.origin)
        notBlacklisted(msg.sender)
        notBlacklisted(from)
        notBlacklisted(to)
        returns (bool)
    {
        if (returnFalse) return false;
        require(value <= allowance[from][msg.sender], "ERC20: transfer amount exceeds allowance");
        allowance[from][msg.sender] -= value;
        _transfer(from, to, value);
        return true;
    }

    // ---- EIP-712 / EIP-3009 ----

    function DOMAIN_SEPARATOR() public view returns (bytes32) {
        return keccak256(
            abi.encode(
                EIP712_DOMAIN_TYPEHASH, keccak256(bytes(name)), keccak256(bytes(version)), block.chainid, address(this)
            )
        );
    }

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
    ) external {
        _requireNotBlocked(tx.origin);
        _requireNotBlocked(from);
        _requireNotBlocked(to);
        require(to == msg.sender, "FiatTokenV2: caller must be the payee");
        require(block.timestamp > validAfter, "FiatTokenV2: authorization is not yet valid");
        require(block.timestamp < validBefore, "FiatTokenV2: authorization is expired");
        require(!authorizationState[from][nonce], "FiatTokenV2: authorization is used or canceled");
        bytes32 digest = _receiveDigest(from, to, value, validAfter, validBefore, nonce);
        require(_isValidSignatureNow(from, digest, v, r, s), "FiatTokenV2: invalid signature");

        authorizationState[from][nonce] = true;
        emit AuthorizationUsed(from, nonce);
        _transfer(from, to, value);
    }

    /// @notice EIP-3009 cancelAuthorization: the authorizer burns an unused nonce with a signature.
    function cancelAuthorization(address authorizer, bytes32 nonce, uint8 v, bytes32 r, bytes32 s) external {
        require(!authorizationState[authorizer][nonce], "FiatTokenV2: authorization is used or canceled");
        bytes32 structHash = keccak256(abi.encode(CANCEL_AUTHORIZATION_TYPEHASH, authorizer, nonce));
        bytes32 digest = keccak256(abi.encodePacked("\x19\x01", DOMAIN_SEPARATOR(), structHash));
        require(_isValidSignatureNow(authorizer, digest, v, r, s), "FiatTokenV2: invalid signature");
        authorizationState[authorizer][nonce] = true;
        emit AuthorizationCanceled(authorizer, nonce);
    }

    // ---- internals ----

    function _requireNotBlocked(address account) internal view {
        require(!isBlacklisted[account], "Blocked address");
    }

    function _receiveDigest(
        address from,
        address to,
        uint256 value,
        uint256 validAfter,
        uint256 validBefore,
        bytes32 nonce
    ) internal view returns (bytes32) {
        bytes32 structHash =
            keccak256(abi.encode(RECEIVE_WITH_AUTHORIZATION_TYPEHASH, from, to, value, validAfter, validBefore, nonce));
        return keccak256(abi.encodePacked("\x19\x01", DOMAIN_SEPARATOR(), structHash));
    }

    /// @dev Hook run before every balance move (transfer, transferFrom, receiveWithAuthorization).
    function _beforeMove() internal virtual {}

    function _transfer(address from, address to, uint256 value) internal {
        _beforeMove();
        require(from != address(0), "ERC20: transfer from the zero address");
        require(to != address(0), "ERC20: transfer to the zero address");
        require(value <= balanceOf[from], "ERC20: transfer amount exceeds balance");
        balanceOf[from] -= value;
        if (skimOne && value > 0) {
            balanceOf[to] += value - 1;
            balanceOf[SKIM_SINK] += 1;
            emit Transfer(from, to, value - 1);
            emit Transfer(from, SKIM_SINK, 1);
        } else if (overCreditOne) {
            balanceOf[to] += value + 1;
            totalSupply += 1;
            emit Transfer(from, to, value + 1);
        } else {
            balanceOf[to] += value;
            emit Transfer(from, to, value);
        }
    }

    /// @dev Mirrors FiatToken v2.2 SignatureChecker: an address with code (a contract, or an EOA carrying an
    ///      EIP-7702 delegation) is checked only through ERC-1271 isValidSignature; otherwise ECDSA.
    function _isValidSignatureNow(address signer, bytes32 digest, uint8 v, bytes32 r, bytes32 s)
        internal
        view
        returns (bool)
    {
        if (signer.code.length == 0) return _recover(digest, v, r, s) == signer;
        (bool ok, bytes memory ret) =
            signer.staticcall(abi.encodeWithSelector(ERC1271_MAGIC, digest, abi.encodePacked(r, s, v)));
        return ok && ret.length >= 32 && abi.decode(ret, (bytes4)) == ERC1271_MAGIC;
    }

    /// @dev Same checks as FiatToken's ECRecover.recover: low-s, v in {27,28}, non-zero signer.
    function _recover(bytes32 digest, uint8 v, bytes32 r, bytes32 s) internal pure returns (address signer) {
        require(
            uint256(s) <= 0x7FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF5D576E7357A4501DDFE92F46681B20A0,
            "ECRecover: invalid signature 's' value"
        );
        require(v == 27 || v == 28, "ECRecover: invalid signature 'v' value");
        signer = ecrecover(digest, v, r, s);
        require(signer != address(0), "ECRecover: invalid signature");
    }
}
