## Arc mainnet real-node rehearsal (read-only: eth_call / eth_estimateGas with state overrides)

Block 23641690 (time 1790823456), base fee 20 gwei. Cost = estimateGas x 20 gwei, paid in native USDC. Bill amount 25.50 USDC. RPC calls: 73.

| Check | OK | Detail |
|---|---|---|
| chain id | yes | 5042 |
| USDC DOMAIN_SEPARATOR matches SPEC | yes | 0x940506929bba468048a19b567f4f0d534714bc06604b5c3017e5d16785ccdf84 |
| runtime carries usdc immutable | yes | 5009 bytes |
| authNonce(1) == keccak(chainid, this, 1) | yes | 0x2fb35a0320ad4a0def43851ccc995737aaf6516972c7cfec25d6629a9ab88eff |
| FiatToken allowance mapping at slot 10 | yes | 123456 |
| blocklist precompile mapping at slot 2 drives USDC.isBlacklisted | yes | without override 0, with override 1 |
| real blocklisted address found | yes | 0x1999ef52700c34de7ec2b68a28aafb37db0c5ade |

| # | Step | Expected | Result | Match | estimateGas | USDC @20 gwei | Post-state (probe) | Note |
|---|---|---|---|---|---|---|---|---|
| 1 | deploy PaidThrough(usdc, 10_000e6) | ok | ok | yes | 1,147,368 | 0.02294736 | runtime 5009 bytes | creation eth_estimateGas |
| 2 | issue (first bill, id 1) | ok | ok | yes | 139,189 | 0.00278378 | bill status Open, contract +0.000000 (0.000000 -> 0.000000) |  |
| 3 | issue (later bill, id 42) | ok | ok | yes | 122,021 | 0.00244042 |  |  |
| 4 | approve (USDC; separate tx before pay) | ok | ok | yes | 56,253 | 0.00112506 |  | the signature path needs no approve |
| 5 | pay (approve + pay; transferFrom) | ok | ok | yes | 74,601 | 0.00149202 | bill status Paid, payer(probe) -25.500000 (30.000000 -> 4.500000), contract +25.500000 (0.000000 -> 25.500000) | probe pays as a contract payer: approve + pay in one eth_call |
| 6 | payWithAuthorization (relayer submits payer's signature) | ok | ok | yes | 111,850 | 0.00223700 | bill status Paid, payer -25.500000 (30.000000 -> 4.500000), contract +25.500000 (0.000000 -> 25.500000), relayer(probe) +0.000000 (1.000000 -> 1.000000) |  |
| 7 | claim (payee, before claimBy) | ok | ok | yes | 59,722 | 0.00119444 | bill status Claimed, payee(probe) +25.500000 (1.000000 -> 26.500000), contract -25.500000 (25.500000 -> 0.000000) |  |
| 8 | decline (payee, before claimBy) | ok | ok | yes | 66,384 | 0.00132768 | bill status Declined, payer +25.500000 (1.000000 -> 26.500000), contract -25.500000 (25.500000 -> 0.000000) |  |
| 9 | cancel (payee, open bill) | ok | ok | yes | 30,376 | 0.00060752 | bill status Cancelled |  |
| 10 | refund (third party, block time = claimBy) | ok | ok | yes | 66,463 | 0.00132926 | bill status Refunded, payer +25.500000 (1.000000 -> 26.500000), contract -25.500000 (25.500000 -> 0.000000), caller(probe) +0.000000 (1.000000 -> 1.000000) |  |
| 11 | claim at claimBy | ClaimWindowClosed | revert: ClaimWindowClosed | yes | - | - |  |  |
| 12 | decline at claimBy | ClaimWindowClosed | revert: ClaimWindowClosed | yes | - | - |  |  |
| 13 | refund at claimBy - 1 | ClaimWindowOpen | revert: ClaimWindowOpen | yes | - | - |  |  |
| 14 | signature for bill 1 used on bill 2 (same amount, other payee) | invalid signature | revert: FiatTokenV2: invalid signature | yes | - | - |  |  |
| 15 | pay after payBy | PayWindowClosed | revert: PayWindowClosed | yes | - | - |  |  |
| 16 | pay by non-allowed payer | NotAllowedPayer | revert: NotAllowedPayer | yes | - | - |  |  |
| 17 | claim by non-payee | NotPayee | revert: NotPayee | yes | - | - |  |  |
| 18 | signature (to = PaidThrough) submitted straight to USDC by another address | caller must be the payee | revert: FiatTokenV2: caller must be the payee | yes | - | - |  | real FiatToken to == msg.sender check; a revert leaves the nonce unused |
| 19 | same signature with to = the submitter (redirect attempt) | invalid signature | revert: FiatTokenV2: invalid signature | yes | - | - |  |  |
| 20 | payWithAuthorization with payer = PaidThrough itself (stray USDC present) | invalid signature | revert: FiatTokenV2: invalid signature | yes | - | - |  |  |
| 21 | payWithAuthorization with payer = the USDC contract | invalid signature | revert: FiatTokenV2: invalid signature | yes | - | - |  |  |
| 22 | payWithAuthorization, payer has an EIP-7702 delegation to a code-less address | (observe) | revert: FiatTokenV2: invalid signature | info | - | - |  | code override 0xef0100 + address; observes the real token's ERC-1271 branch |
| 23 | claim to a blocklisted payee | revert | revert: Blocked address | yes | - | - |  | blocklist set by state override of precompile 0x1800..01 storage |
| 24 | refund to payer after claimBy while payee is blocklisted | ok | ok | yes | 66,463 | 0.00132926 | bill status Refunded, payer +25.500000 (1.000000 -> 26.500000), contract -25.500000 (25.500000 -> 0.000000) | blocklist set by state override of precompile 0x1800..01 storage |
| 25 | refund to a blocklisted payer | revert | revert: Blocked address | yes | - | - |  | blocklist set by state override of precompile 0x1800..01 storage |
| 26 | decline to a blocklisted payer | revert | revert: Blocked address | yes | - | - |  | blocklist set by state override of precompile 0x1800..01 storage |
| 27 | payWithAuthorization by a blocklisted payer | revert | revert: Blocked address | yes | - | - |  | blocklist set by state override of precompile 0x1800..01 storage |
| 28 | pay by a blocklisted payer | revert | revert: Blocked address | yes | - | - |  | blocklist set by state override of precompile 0x1800..01 storage |
| 29 | claim while the contract itself is blocklisted | revert | revert: Blocked address | yes | - | - |  | blocklist set by state override of precompile 0x1800..01 storage |
| 30 | decline sent by a blocklisted payee (money goes to the payer) | (observe) | revert: Blocked address | info | - | - |  | blocklist set by state override of precompile 0x1800..01 storage |
| 31 | refund sent by a blocklisted third party (money goes to the payer) | (observe) | revert: Blocked address | info | - | - |  | blocklist set by state override of precompile 0x1800..01 storage |
| 32 | claim by the payee while the payer is blocklisted | (observe) | ok | info | 59,722 | 0.00119444 |  | blocklist set by state override of precompile 0x1800..01 storage |
| 33 | payWithAuthorization sent by a blocklisted relayer for a clean payer | (observe) | revert: Blocked address | info | - | - |  | blocklist set by state override of precompile 0x1800..01 storage |
| 34 | claim to a really blocklisted payee | revert | revert: Blocked address | yes | - | - |  | 0x1999ef52700c34de7ec2b68a28aafb37db0c5ade is blocklisted on Arc mainnet (USDC.isBlacklisted = true at this block); no override |
| 35 | refund to a really blocklisted payer | revert | revert: Blocked address | yes | - | - |  | 0x1999ef52700c34de7ec2b68a28aafb37db0c5ade is blocklisted on Arc mainnet (USDC.isBlacklisted = true at this block); no override |
| 36 | decline to a really blocklisted payer | revert | revert: Blocked address | yes | - | - |  | 0x1999ef52700c34de7ec2b68a28aafb37db0c5ade is blocklisted on Arc mainnet (USDC.isBlacklisted = true at this block); no override |
| 37 | decline sent by a really blocklisted payee (eth_call from that address) | (observe) | revert: Blocked address | info | - | - |  | 0x1999ef52700c34de7ec2b68a28aafb37db0c5ade is blocklisted on Arc mainnet (USDC.isBlacklisted = true at this block); no override; eth_call only, whether Arc admits a transaction from it is not tested |
