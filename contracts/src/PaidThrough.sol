// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;

import {IUSDC} from "./IUSDC.sol";

/// @title PaidThrough
/// @notice A biller (payee) issues a USDC bill; a payer pays it into this contract; the payee claims it before
///         `claimBy`, or declines it (money back to the payer); after `claimBy` anyone can refund the payer.
/// @dev No owner, no fee, no pause, no upgrade, no sweep. Amounts are USDC base units (6 decimals).
///      Transfers out are pushed with `transfer`; if the token blocks the recipient the call reverts and the
///      bill stays Paid. Tokens sent here outside `pay*` are stuck. Unaudited.
contract PaidThrough {
    enum Status {
        None,
        Open,
        Paid,
        Claimed,
        Refunded,
        Declined,
        Cancelled
    }

    /// @dev Packed into 4 storage slots.
    struct Bill {
        address payee; // issuer; receives the money on claim
        uint96 amount; // USDC base units (6 decimals)
        address payer; // zero until paid
        uint64 payBy; // bill can be paid while block.timestamp < payBy
        uint32 claimWindow; // seconds the payee has after payment
        address allowedPayer; // zero = anyone may pay
        uint64 claimBy; // set on payment: paidAt + claimWindow
        Status status;
        bytes32 ref; // fingerprint of the biller's reference (never plaintext)
    }

    uint32 public constant MIN_CLAIM_WINDOW = 1 hours;
    uint32 public constant MAX_CLAIM_WINDOW = 365 days;
    uint64 public constant MAX_PAY_WINDOW = 365 days;

    /// @notice The USDC token (ERC-20 view, 6 decimals).
    IUSDC public immutable usdc;
    /// @notice Largest amount a single bill may carry, in USDC base units.
    uint96 public immutable maxAmount;

    uint256 private _billCount;
    mapping(uint256 billId => Bill) private _bills;

    event BillIssued(
        uint256 indexed billId,
        address indexed payee,
        address indexed allowedPayer,
        uint96 amount,
        uint64 payBy,
        uint32 claimWindow,
        bytes32 ref
    );
    event BillCancelled(uint256 indexed billId);
    event BillPaid(uint256 indexed billId, address indexed payer, uint64 claimBy);
    event BillClaimed(uint256 indexed billId, address indexed payee, uint96 amount);
    event BillDeclined(uint256 indexed billId, address indexed payer, uint96 amount);
    event BillRefunded(uint256 indexed billId, address indexed payer, uint96 amount, address caller);

    error NotPayee();
    error WrongStatus();
    error PayWindowClosed();
    error ClaimWindowClosed();
    error ClaimWindowOpen();
    error NotAllowedPayer();
    error BadAmount();
    error BadPayBy();
    error BadClaimWindow();
    error TransferFailed();
    error BalanceMismatch();

    /// @param usdc_ USDC token address (Arc: 0x3600000000000000000000000000000000000000).
    /// @param maxAmount_ Per-bill cap in USDC base units (mainnet: 10_000e6).
    constructor(address usdc_, uint96 maxAmount_) {
        usdc = IUSDC(usdc_);
        maxAmount = maxAmount_;
    }

    /// @notice Issue a bill payable to the caller.
    /// @param amount USDC base units, 0 < amount <= maxAmount.
    /// @param payBy Unix time; payable while now < payBy; now < payBy <= now + 365 days.
    /// @param claimWindow Seconds the payee has to claim after payment, 1 hour to 365 days.
    /// @param allowedPayer Only this address may pay; zero means anyone.
    /// @param ref Fingerprint of the biller's reference (hash, never plaintext).
    /// @return billId The new bill id (ids start at 1).
    function issue(uint96 amount, uint64 payBy, uint32 claimWindow, address allowedPayer, bytes32 ref)
        external
        returns (uint256 billId)
    {
        if (amount == 0 || amount > maxAmount) revert BadAmount();
        if (payBy <= block.timestamp || payBy > block.timestamp + MAX_PAY_WINDOW) revert BadPayBy();
        if (claimWindow < MIN_CLAIM_WINDOW || claimWindow > MAX_CLAIM_WINDOW) revert BadClaimWindow();

        billId = ++_billCount;
        _bills[billId] = Bill({
            payee: msg.sender,
            amount: amount,
            payer: address(0),
            payBy: payBy,
            claimWindow: claimWindow,
            allowedPayer: allowedPayer,
            claimBy: 0,
            status: Status.Open,
            ref: ref
        });
        emit BillIssued(billId, msg.sender, allowedPayer, amount, payBy, claimWindow, ref);
    }

    /// @notice Payee withdraws an unpaid bill (also allowed after payBy).
    function cancel(uint256 billId) external {
        Bill storage b = _bills[billId];
        if (msg.sender != b.payee) revert NotPayee();
        if (b.status != Status.Open) revert WrongStatus();
        b.status = Status.Cancelled;
        emit BillCancelled(billId);
    }

    /// @notice Pay a bill from the caller's balance; the caller must have approved `amount` first.
    function pay(uint256 billId) external {
        uint96 amount = _markPaid(billId, msg.sender);
        uint256 balanceBefore = usdc.balanceOf(address(this));
        if (!usdc.transferFrom(msg.sender, address(this), amount)) revert TransferFailed();
        _checkReceived(balanceBefore, amount);
    }

    /// @notice Pay a bill with the payer's EIP-3009 ReceiveWithAuthorization signature; anyone may submit.
    /// @dev The signed nonce must be `authNonce(billId)` and the signed value the bill amount.
    /// @param payer The signer whose USDC pays the bill.
    /// @param validAfter Signature valid only when now > validAfter.
    /// @param validBefore Signature valid only when now < validBefore.
    function payWithAuthorization(
        uint256 billId,
        address payer,
        uint256 validAfter,
        uint256 validBefore,
        uint8 v,
        bytes32 r,
        bytes32 s
    ) external {
        uint96 amount = _markPaid(billId, payer);
        uint256 balanceBefore = usdc.balanceOf(address(this));
        usdc.receiveWithAuthorization(
            payer, address(this), amount, validAfter, validBefore, authNonce(billId), v, r, s
        );
        _checkReceived(balanceBefore, amount);
    }

    /// @notice Payee takes the money; only while now < claimBy.
    function claim(uint256 billId) external {
        Bill storage b = _bills[billId];
        address payee = b.payee;
        if (msg.sender != payee) revert NotPayee();
        if (b.status != Status.Paid) revert WrongStatus();
        if (block.timestamp >= b.claimBy) revert ClaimWindowClosed();
        b.status = Status.Claimed;
        uint96 amount = b.amount;
        emit BillClaimed(billId, payee, amount);
        _send(payee, amount);
    }

    /// @notice Payee refuses the payment and sends it back to the payer; only while now < claimBy.
    function decline(uint256 billId) external {
        Bill storage b = _bills[billId];
        if (msg.sender != b.payee) revert NotPayee();
        if (b.status != Status.Paid) revert WrongStatus();
        if (block.timestamp >= b.claimBy) revert ClaimWindowClosed();
        b.status = Status.Declined;
        address payer = b.payer;
        uint96 amount = b.amount;
        emit BillDeclined(billId, payer, amount);
        _send(payer, amount);
    }

    /// @notice Anyone returns an unclaimed payment to the payer; only when now >= claimBy.
    function refund(uint256 billId) external {
        Bill storage b = _bills[billId];
        if (b.status != Status.Paid) revert WrongStatus();
        if (block.timestamp < b.claimBy) revert ClaimWindowOpen();
        b.status = Status.Refunded;
        address payer = b.payer;
        uint96 amount = b.amount;
        emit BillRefunded(billId, payer, amount, msg.sender);
        _send(payer, amount);
    }

    /// @notice The EIP-3009 nonce a payer signs for this bill: keccak256(abi.encode(chainid, this, billId)).
    function authNonce(uint256 billId) public view returns (bytes32) {
        return keccak256(abi.encode(block.chainid, address(this), billId));
    }

    /// @notice Returns the stored bill (all zero for an unknown id).
    function getBill(uint256 billId) external view returns (Bill memory) {
        return _bills[billId];
    }

    /// @notice Number of bills issued; ids run 1..billCount.
    function billCount() external view returns (uint256) {
        return _billCount;
    }

    /// @dev Checks a bill can be paid by `payer` and writes the Paid state before any token call.
    function _markPaid(uint256 billId, address payer) private returns (uint96 amount) {
        Bill storage b = _bills[billId];
        if (b.status != Status.Open) revert WrongStatus();
        if (block.timestamp >= b.payBy) revert PayWindowClosed();
        address allowed = b.allowedPayer;
        if (allowed != address(0) && payer != allowed) revert NotAllowedPayer();
        uint64 claimBy = uint64(block.timestamp) + b.claimWindow;
        b.payer = payer;
        b.claimBy = claimBy;
        b.status = Status.Paid;
        emit BillPaid(billId, payer, claimBy);
        return b.amount;
    }

    /// @dev The contract's USDC balance must have risen by exactly `amount`.
    function _checkReceived(uint256 balanceBefore, uint96 amount) private view {
        if (usdc.balanceOf(address(this)) != balanceBefore + amount) revert BalanceMismatch();
    }

    /// @dev Pushes `amount` to `to`; reverts if the token returns false (a token revert bubbles up).
    function _send(address to, uint96 amount) private {
        if (!usdc.transfer(to, amount)) revert TransferFailed();
    }
}
