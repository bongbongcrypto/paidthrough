# PaidThrough — design notes

The interface of record is [`SPEC.md`](../SPEC.md). This file explains why it looks the way it does and what it does not protect against.

## One bill's life

```
            issue (biller)
                 │
                 ▼
   ┌──────────── Open ─────────────┐
   │ cancel (biller, any time)     │ pay / payWithAuthorization
   ▼                               │ (anyone, or the named payer, before payBy)
Cancelled                          ▼
                                  Paid ──────────────┐
                                   │                 │
            claim (biller,         │ decline         │ refund (anyone,
            before claimBy)        │ (biller, before │ at or after claimBy)
                   ▼               ▼ claimBy)        ▼
                Claimed         Declined          Refunded
              money → biller   money → payer     money → payer
```

`Open` with `now >= payBy` is shown as "expired" by the page and keeper; it holds no money, so nothing has to happen on chain.

## Who can do what

| Action | Who | When | Where the money goes |
|---|---|---|---|
| issue | anyone (becomes the bill's payee) | any time | — |
| cancel | payee | while Open | — (none held) |
| pay | anyone, or only `allowedPayer` if set | Open and before `payBy` | payer → contract |
| payWithAuthorization | anyone may submit; the payer signs | same | payer → contract |
| claim | payee | Paid and before `claimBy` | contract → payee |
| decline | payee | Paid and before `claimBy` | contract → payer |
| refund | anyone | Paid and at/after `claimBy` | contract → payer |

There is no other way for money to leave the contract. Nobody, including whoever deployed it, can redirect, pause, upgrade or take a fee.

## Decisions

- **The biller issues the bill.** Amount and reference come from the school or clinic, so the family pays exactly that bill, and collecting it is the biller's acknowledgement. A payer-initiated "send with refund" would need the biller to act anyway.
- **Claim, not push.** The biller must collect. If the address was wrong, lost, or abandoned, the money comes back instead of sitting at a dead address.
- **Strict boundary at `claimBy`.** Claim and decline end exactly when refund starts. There is no window in which both are possible and no gap in which neither is.
- **Anyone can trigger a refund.** The keeper is a convenience, not a dependency: the family, the biller or a stranger can call `refund` once it is due, and it can only pay the original payer.
- **One signature to pay.** Arc's USDC implements EIP-3009. `payWithAuthorization` uses `receiveWithAuthorization`, which only this contract can execute, with a nonce derived from `(chainId, contract, billId)`. A signature for one bill cannot pay any other bill, and no approval is left behind.
- **References are fingerprints.** Every amount and address on Arc is public. The contract stores `sha256("paidthrough:v1:" + salt + ":" + text)`; the plaintext and salt travel only in the link's `#fragment`.
- **Per-bill cap.** `maxAmount` (10,000 USDC on mainnet) bounds the loss from an unknown bug in unaudited code. It also catches the classic Arc mistake of passing an 18-decimal native amount where 6-decimal USDC units are expected (1 USDC × 10¹² is far above the cap).
- **No libraries in `src/`.** The contract is small enough to read in one sitting; checks-effects-interactions plus a balance-delta check on the way in.

## Arc specifics handled

| Trap | Where handled |
|---|---|
| Native gas balance is 18 decimals, USDC ERC-20 view is 6 | contract works only in 6-decimal units; page and keeper convert gas (`/1e18`) and amounts (`/1e6`) separately; fuzz test for the 10¹² mistake |
| Base fee floor 20 gwei; lower tx dropped silently | keeper sets `maxFeePerGas >= max(2 × baseFee, 20 gwei)` |
| Native value sent to a contract also shows up as USDC | no payable function, no `receive`/`fallback` |
| USDC blocklist (FiatToken) | push transfers revert for a blocked recipient; bill stays `Paid` (see limits) |
| Everything public | reference fingerprints; page warns at issue time |

## Known limits

- **Unaudited.** Tests and reviews are listed in the README; no external audit.
- **Blocklisted recipients.** If the payee is blocked, it cannot claim; after `claimBy` the payer is refunded. If the payer is blocked, the refund reverts and waits until the block is lifted; if the block is permanent, the money stays frozen in the contract, which is what the token's own freeze would do to it anyway. There is no pull fallback.
- **Smart-wallet payers.** Arc's USDC checks signatures from an address that has code (an EIP-7702 delegated wallet or a smart account) through ERC-1271. Such a payer can sign only if its wallet implements ERC-1271; otherwise the page uses approve + pay. Found by the fork test: small public test keys on Arc mainnet already carry sweeper delegations.
- **A payer can burn the signature path for a bill.** Arc's USDC has `cancelAuthorization`; a payer who cancels `authNonce(billId)` can no longer pay that bill by signature. Approve + pay still works. Only the payer can do this to their own nonce.
- **Unverified on Arc: blocklisted senders.** Tests assume a blocklisted payee can still send a `decline`. Arc's protocol-level blocklist may stop such an address from sending at all; then the payer is refunded after `claimBy` as usual.
- **Open bills can be paid by anyone.** With no `allowedPayer`, a stranger can pay first; the money still goes to the bill, and the intended payer's transaction fails with `WrongStatus`. The biller desk asks for the payer's address when it is known.
- **Payee identity.** The fingerprint proves the reference text, not who issued the bill. A fake bill with a copied reference is possible until billers publish their addresses (a biller directory is future work). Refund protects against a biller who never collects, not against a dishonest one.
- **Long holds.** A bill can wait up to 365 days to be paid and then up to 365 days to be collected.
- **Stray tokens.** USDC sent to the contract other than through `pay*` is stuck; there is no sweep, by design.
- **Biller trust is outside the contract.** The contract proves the biller's address collected the money, not that the school credited the student. The link to a real school is the biller publishing its address.
- **Expired-but-unpaid bills** stay `Open` forever on chain; they hold nothing.
- **No partial or multi-payer bills, no installments** in this version.
