# PaidThrough — spec of record

Every component (contract, keeper, web, docs) builds against this file. Change it here first, then in code.

## Chain facts (read 2026-10-01 by `scripts/probe_usdc.py`)

| | Arc mainnet | Arc testnet |
|---|---|---|
| chain id | 5042 | 5042002 |
| RPC | https://rpc.mainnet.arc.io | https://rpc.testnet.arc.network |
| USDC (ERC-20 view of native USDC) | `0x3600000000000000000000000000000000000000` | same address |
| USDC `DOMAIN_SEPARATOR` | `0x940506929bba468048a19b567f4f0d534714bc06604b5c3017e5d16785ccdf84` | `0x361191522483d32a83e70ae7183b4b9629442c13a78bc9921d6f707911c8c6b0` |
| base fee | 20 gwei floor (tx below it are dropped silently) | 20 gwei |

- USDC token: FiatToken-style proxy (implementation `0xc6ad664ac6679f4ce74e10e91449c93ec1ae3ca6` on mainnet) with `decimals() = 6`, `name() = "USDC"`, `version() = "2"`, EIP-3009 (`receiveWithAuthorization`, `transferWithAuthorization`, `authorizationState`, `cancelAuthorization`), EIP-2612 `permit`, `isBlacklisted`.
- EIP-712 domain for signatures: `{name: "USDC", version: "2", chainId, verifyingContract: 0x3600…0000}`. Computed separator matches the on-chain value above.
- `ReceiveWithAuthorization` typehash `0xd099cc98ef71107a616c4f0f941f04c322d8e254fe26b3c6668db87aae413de8`: `ReceiveWithAuthorization(address from,address to,uint256 value,uint256 validAfter,uint256 validBefore,bytes32 nonce)`.
- Native balance (gas) is 18 decimals; the ERC-20 view is 6 decimals of the same money. Gas cost in USDC = gasUsed × effectiveGasPrice / 1e18.

## Contract `PaidThrough` (Solidity 0.8.30, no external libraries in `src/`)

```solidity
constructor(address usdc, uint96 maxAmount)   // both immutable; mainnet deploy: usdc = 0x3600…0000, maxAmount = 10_000e6

enum Status { None, Open, Paid, Claimed, Refunded, Declined, Cancelled }

struct Bill {               // 4 storage slots
    address payee;          // issuer; receives the money on claim
    uint96  amount;         // USDC base units (6 decimals)
    address payer;          // zero until paid
    uint64  payBy;          // bill can be paid while block.timestamp < payBy
    uint32  claimWindow;    // seconds the payee has after payment
    address allowedPayer;   // zero = anyone may pay
    uint64  claimBy;        // set on payment: paidAt + claimWindow
    Status  status;
    bytes32 ref;            // fingerprint of the biller's reference (never plaintext)
}

uint32 constant MIN_CLAIM_WINDOW = 1 hours;
uint32 constant MAX_CLAIM_WINDOW = 365 days;
uint64 constant MAX_PAY_WINDOW   = 365 days;   // payBy <= now + MAX_PAY_WINDOW

function issue(uint96 amount, uint64 payBy, uint32 claimWindow, address allowedPayer, bytes32 ref) external returns (uint256 billId);
function cancel(uint256 billId) external;                       // payee; Open -> Cancelled (also after payBy)
function pay(uint256 billId) external;                          // payer = msg.sender; needs approve(amount)
function payWithAuthorization(uint256 billId, address payer, uint256 validAfter, uint256 validBefore,
                              uint8 v, bytes32 r, bytes32 s) external;   // anyone may submit
function claim(uint256 billId) external;                        // payee; Paid and now < claimBy -> Claimed, pays payee
function decline(uint256 billId) external;                      // payee; Paid and now < claimBy -> Declined, pays payer back
function refund(uint256 billId) external;                       // anyone; Paid and now >= claimBy -> Refunded, pays payer back

function authNonce(uint256 billId) public view returns (bytes32);   // keccak256(abi.encode(block.chainid, address(this), billId))
function getBill(uint256 billId) external view returns (Bill memory);
function billCount() external view returns (uint256);               // ids run 1..billCount
```

Rules:
- `issue`: `0 < amount <= maxAmount`; `block.timestamp < payBy <= block.timestamp + MAX_PAY_WINDOW`; `MIN_CLAIM_WINDOW <= claimWindow <= MAX_CLAIM_WINDOW`. Ids start at 1.
- `pay` / `payWithAuthorization`: status Open, `block.timestamp < payBy`, and `allowedPayer == 0 || payer == allowedPayer`. State is written (payer, claimBy, Paid) before the token call. `pay` uses `transferFrom` and requires `true`; both paths check the contract's USDC balance rose by exactly `amount`.
- `payWithAuthorization` calls `usdc.receiveWithAuthorization(payer, address(this), amount, validAfter, validBefore, authNonce(billId), v, r, s)`. The nonce is derived from the bill, so a signature for one bill cannot pay another bill, and the token itself rejects reuse. `receiveWithAuthorization` requires `msg.sender == to`, so nobody can redirect the signature outside this contract.
- `claim`/`decline`: only before `claimBy`. `refund`: only at or after `claimBy`. No overlap, no gap.
- Token transfers out use `transfer` and require `true`. If the recipient is blocklisted by the token, the call reverts and the bill stays `Paid` (push model, documented; no pull fallback): a blocked payee cannot claim, and after `claimBy` the payer gets the refund; a blocked payer's refund waits until the block is lifted.
- No `receive`/`fallback`; every function is non-payable. No owner, no fee, no pause, no upgrade, no sweep. Tokens sent to the contract outside `pay*` are stuck (documented).
- Invariant: `usdc.balanceOf(this) >= Σ amount of Paid bills`, with equality when nobody sends stray tokens.

Events (all ids indexed):
```solidity
event BillIssued(uint256 indexed billId, address indexed payee, address indexed allowedPayer, uint96 amount, uint64 payBy, uint32 claimWindow, bytes32 ref);
event BillCancelled(uint256 indexed billId);
event BillPaid(uint256 indexed billId, address indexed payer, uint64 claimBy);
event BillClaimed(uint256 indexed billId, address indexed payee, uint96 amount);
event BillDeclined(uint256 indexed billId, address indexed payer, uint96 amount);
event BillRefunded(uint256 indexed billId, address indexed payer, uint96 amount, address caller);
```
Custom errors, one per failed guard (e.g. `NotPayee`, `WrongStatus`, `PayWindowClosed`, `ClaimWindowClosed`, `ClaimWindowOpen`, `NotAllowedPayer`, `BadAmount`, `BadPayBy`, `BadClaimWindow`, `TransferFailed`, `BalanceMismatch`).

## Reference fingerprint (web and docs)
`ref = sha256(utf8("paidthrough:v1:" + saltHex + ":" + referenceText))` as bytes32. The salt is 16 random bytes made at issue time. The share link carries text and salt only in the URL fragment (`#/bill/<id>?r=<text>&s=<salt>`), which browsers never send to a server; the page recomputes the hash and shows whether it matches the chain.

## Keeper (`keeper/`, Python 3.11)
- Rebuilds every bill's status from the contract's events (chunked `eth_getLogs`, chunk halves on a range error), lists bills with `status == Paid && claimBy <= latest block timestamp`, simulates `refund(id)` with `eth_call`, and in `--send` mode sends it.
- Default is dry-run with no key. `--send` reads the key from a file outside the repo (path in env `PAIDTHROUGH_KEEPER_ENV`, default `~/.paidthrough-keeper.env`); never from the repo, never printed.
- EIP-1559 fees: `maxFeePerGas >= max(2 × baseFee, 20 gwei)`; priority fee from `eth_maxPriorityFeePerGas`. A simulated revert (e.g. blocklisted payer) is skipped and reported, not retried in a loop.
- Optional Telegram summary to the operator, only with `--notify`.

## Web (`web/`, static, no build step)
- Views (hash routes): `#/bill/<id>` family status (no wallet needed), `#/pay/<id>` payer, `#/desk` biller (issue, list own bills, claim/decline/cancel). Landing `#/` explains the product in one screen and links the three.
- Reads chain state from the browser over public RPC; writes through the injected wallet (EIP-1193): `wallet_addEthereumChain`/`wallet_switchEthereumChain`, `eth_signTypedData_v4` for `ReceiveWithAuthorization` (nonce = `authNonce(billId)`, `validBefore = min(payBy, now + 1 hour)`), then `payWithAuthorization` from the payer's own wallet: one transaction, no standing approval. Fallback: approve + pay.
- `web/config.js` holds per-network addresses; until the contract is deployed the page runs in a clearly labelled preview with one example bill drawn locally.
- Languages: English, Filipino, Korean. Filipino strings are machine-drafted and labelled so in the UI until a native speaker reviews them.
- Footer: "Independent project. Not affiliated with Circle or Arc. Unaudited."
- Issue form warns that everything on Arc is public and stores only the fingerprint.
