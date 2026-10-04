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
- **Sign, then pay.** Arc's USDC implements EIP-3009. The payer signs typed data for the bill, and one `payWithAuthorization` transaction moves the money (the page sends it from the payer's wallet, so the browser shows two prompts and no approval transaction). `payWithAuthorization` uses `receiveWithAuthorization`, which only this contract can execute, with a nonce derived from `(chainId, contract, billId)`. A signature for one bill cannot pay any other bill, and no approval is left behind.
- **References are fingerprints.** Every amount and address on Arc is public. The contract stores `sha256("paidthrough:v1:" + salt + ":" + text)`; the plaintext and salt travel only in the link's `#fragment`.
- **Per-bill cap.** `maxAmount` (10,000 USDC on mainnet) bounds the loss from an unknown bug in unaudited code. It also catches the classic Arc mistake of passing an 18-decimal native amount where 6-decimal USDC units are expected (1 USDC × 10¹² is far above the cap).
- **No libraries in `src/`.** The contract is small enough to read in one sitting; checks-effects-interactions plus a balance-delta check on the way in.

## Arc specifics handled

| Trap | Where handled |
|---|---|
| Native gas balance is 18 decimals, USDC ERC-20 view is 6 | contract works only in 6-decimal units; page and keeper convert gas (`/1e18`) and amounts (`/1e6`) separately; fuzz test for the 10¹² mistake |
| Base fee floor 20 gwei; lower tx dropped silently | keeper sets `maxFeePerGas >= max(2 × baseFee, 20 gwei)` |
| Native value sent to a contract also shows up as USDC | no payable function, no `receive`/`fallback` |
| USDC blocklist (Arc checks sender, recipient and transaction sender through a precompile) | any revert leaves the bill `Paid`; refund stays open to every unblocked address (see limits) |
| Addresses with code (EIP-7702 delegations, smart accounts) sign through ERC-1271 | page checks `eth_getCode(payer)` and falls back to approve + pay |
| Everything public | reference fingerprints; page warns at issue time |

## Review findings and fixes

Two internal adversarial reviews ran on 2026-10-01, one on security and one on spec and test quality. Neither found a way to lose or redirect funds in the contract. The security review found three issues outside the contract and the spec and test review found eight test gaps (F1-F8). All eleven are fixed.

Security review:

- **Keeper log-spam DoS (medium).** More than 2,000 cheap `BillCancelled` events in one block made the keeper's log query fail on that block every run, which would stop automatic refunds. The keeper no longer reads `BillCancelled` and falls back to `getBill` when a block still overflows (`keeper/tests/test_spam.py`).
- **Dust griefing (low).** Thousands of 1-unit bills could queue ahead of real refunds and burn keeper gas. Refunds now go largest first, and bills below 0.05 USDC are left for the payer, or anyone else, to refund from the bill page.
- **Look-alike biller display (low).** The page showed the biller as a short address (32 bits) next to a green reference check, so a vanity address with the same short form could pass for the school. The page now shows the full checksummed address in 4-character groups, and the check says it does not prove who the biller is.

Spec and test review:

- **F1 (medium).** The "native value is rejected" test passed for the wrong reason (the caller had no USDC). Every function is now called with value from a funded caller, and again without.
- **F2 (medium).** No test paid a bill already Claimed, Declined or Refunded. Both pay paths are now shown to revert there with `WrongStatus`.
- **F3 (medium).** The Arc fork test returned silently when funding failed. It fails instead, and every stub logs `STUBBED:`.
- **F4 (info).** A blocklist test assumed a blocklisted payee can still send a transaction. The real-node rehearsal showed Arc refuses it, and the test now expects that.
- **F5 (low).** The invariant run tolerated reverts and hid idle actions. `fail_on_revert` is on, action counts cover all runs, and the handler makes bad calls that must fail with exact errors.
- **F6 (low).** A pay-by fuzz test reached its accept branch in about 0.3% of runs. The input is now bounded so both branches get real coverage.
- **F7 (low).** The re-entrancy test token hooked only `transfer`. It now also hooks `transferFrom` and `receiveWithAuthorization` and re-enters pay and cancel.
- **F8 (info).** No test covered a token that over-credits, or `cancelAuthorization`. Both are added.

The security review also noted two untested paths that were already correct: a signature submitted straight to USDC, and a payer set to the contract or to USDC. The rehearsal now runs both on the real node, and `PaidThroughAuth.t.sol` covers them with the mock token.

## Known limits

- **Unaudited.** Tests are listed in the README and review findings above; no external audit.
- **Blocklisted addresses** (checked on the real Arc node, `status/rehearsal-2026-10-02.md` steps 23-37, including a really blocklisted mainnet address). Arc's USDC rejects a transfer with "Blocked address" when the sender, the recipient, or the address that sends the transaction is blocklisted. So a blocked payee can neither claim nor decline, and after `claimBy` any unblocked address refunds the payer. A blocked payer cannot pay; if a payer is blocked after paying, the refund reverts and waits until the block is lifted, and if it is permanent the money stays frozen in the contract, as the token would freeze it anyway. The keeper's own address must not be blocked. There is no pull fallback.
- **Smart-wallet payers.** Arc's USDC checks signatures from an address that has code (an EIP-7702 delegated wallet or a smart account) through ERC-1271. Such a payer can sign only if its wallet implements ERC-1271; otherwise the page uses approve + pay. Found by the fork test: small public test keys on Arc mainnet already carry sweeper delegations.
- **A payer can burn the signature path for a bill.** Arc's USDC has `cancelAuthorization`; a payer who cancels `authNonce(billId)` can no longer pay that bill by signature. Approve + pay still works. Only the payer can do this to their own nonce.
- **Open bills can be paid by anyone.** With no `allowedPayer`, a stranger can pay first; the money still goes to the bill, and the intended payer's transaction fails with `WrongStatus`. The biller desk asks for the payer's address when it is known.
- **Payee identity.** The fingerprint proves the reference text, not who issued the bill. A fake bill with a copied reference is possible until billers publish their addresses (a biller directory is future work). Refund protects against a biller who never collects, not against a dishonest one.
- **Long holds.** A bill can wait up to 365 days to be paid and then up to 365 days to be collected.
- **Stray tokens.** USDC sent to the contract other than through `pay*` is stuck; there is no sweep, by design.
- **Biller trust is outside the contract.** The contract proves the biller's address collected the money, not that the school credited the student. The link to a real school is the biller publishing its address.
- **Expired-but-unpaid bills** stay `Open` forever on chain; they hold nothing.
- **No partial or multi-payer bills, no installments** in this version.
