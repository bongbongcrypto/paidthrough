// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;

/// @notice Test stand-in for Circle's FiatToken v2 (USDC): 6 decimals, EIP-712 domain {"USDC","2"},
///         real EIP-3009 receiveWithAuthorization, blocklist with FiatToken's modifier semantics,
///         plus two fault switches (return false, skim 1 unit) to reach PaidThrough's defensive errors.
contract MockFiatToken {
    string public constant name = "USDC";
    string public constant symbol = "USDC";
    string public constant version = "2";
    uint8 public constant decimals = 6;

    bytes32 public constant RECEIVE_WITH_AUTHORIZATION_TYPEHASH = keccak256(
        "ReceiveWithAuthorization(address from,address to,uint256 value,uint256 validAfter,uint256 validBefore,bytes32 nonce)"
    );
    bytes32 private constant EIP712_DOMAIN_TYPEHASH =
        keccak256("EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)");

    /// @dev Where skimmed units go.
    address public constant SKIM_SINK = address(0x5C1A);

    uint256 public totalSupply;
    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;
    mapping(address => bool) public isBlacklisted;
    mapping(address => mapping(bytes32 => bool)) public authorizationState;

    bool public returnFalse;
    bool public skimOne;

    event Transfer(address indexed from, address indexed to, uint256 value);
    event Approval(address indexed owner, address indexed spender, uint256 value);
    event AuthorizationUsed(address indexed authorizer, bytes32 indexed nonce);

    modifier notBlacklisted(address account) {
        require(!isBlacklisted[account], "Blacklistable: account is blacklisted");
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
    ) external notBlacklisted(from) notBlacklisted(to) {
        require(to == msg.sender, "FiatTokenV2: caller must be the payee");
        require(block.timestamp > validAfter, "FiatTokenV2: authorization is not yet valid");
        require(block.timestamp < validBefore, "FiatTokenV2: authorization is expired");
        require(!authorizationState[from][nonce], "FiatTokenV2: authorization is used or canceled");

        bytes32 structHash =
            keccak256(abi.encode(RECEIVE_WITH_AUTHORIZATION_TYPEHASH, from, to, value, validAfter, validBefore, nonce));
        bytes32 digest = keccak256(abi.encodePacked("\x19\x01", DOMAIN_SEPARATOR(), structHash));
        require(_recover(digest, v, r, s) == from, "FiatTokenV2: invalid signature");

        authorizationState[from][nonce] = true;
        emit AuthorizationUsed(from, nonce);
        _transfer(from, to, value);
    }

    // ---- internals ----

    function _transfer(address from, address to, uint256 value) internal {
        require(from != address(0), "ERC20: transfer from the zero address");
        require(to != address(0), "ERC20: transfer to the zero address");
        require(value <= balanceOf[from], "ERC20: transfer amount exceeds balance");
        balanceOf[from] -= value;
        if (skimOne && value > 0) {
            balanceOf[to] += value - 1;
            balanceOf[SKIM_SINK] += 1;
            emit Transfer(from, to, value - 1);
            emit Transfer(from, SKIM_SINK, 1);
        } else {
            balanceOf[to] += value;
            emit Transfer(from, to, value);
        }
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
