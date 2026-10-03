## PaidThrough mainnet test bills

Contract `0x05cf14cB82660942c272EaaD488C745490CB82aB` on Arc mainnet (chain 5042). Biller `0x7421346433d41fb6fA52317a8f0EEb5275bc2051`, payer `0x6fAc56a942714aEa400f521D4c870dFB12D9d83b`, funder `0x708B05A24D300c83f3D365DAD79a4c0b52D9038D`. Updated 2026-10-03T23:01:51Z.

| Funding | State | Value USDC | Tx | Block | Gas | Cost USDC |
|---|---|---|---|---|---|---|
| payer | confirmed | 1.10000000 | [0xbd75d395bf](https://explorer.arc.io/tx/0xbd75d395bf952fdbe110d4a838a1b027f9ccb743e0e1e2d3643fb62f927f098c) | 24115954 | 21000 | 0.00042001 |
| biller | confirmed | 0.05000000 | [0xdc228f1455](https://explorer.arc.io/tx/0xdc228f14557ee6db3fd1f37b2366b10d6c77972d36c01c9402bbdbb800aca26e) | 24115959 | 21000 | 0.00042001 |

| Bill | Purpose | Id | Amount | Claim window | Family link |
|---|---|---|---|---|---|
| A | collected | 1 | 0.500000 USDC | 604800s | [open](https://bongbongcrypto.github.io/paidthrough/#/bill/1?r=Test+bill+A+%28mainnet+check%29&s=56bdb5623b266a7574f729c162c7c71b) |
| C | declined | 2 | 0.500000 USDC | 604800s | [open](https://bongbongcrypto.github.io/paidthrough/#/bill/2?r=Test+bill+C+%28mainnet+check%29&s=1fa7b834f5db5f967ace6f4a143644fd) |
| B | refunded by the keeper | 3 | 0.500000 USDC | 3600s | [open](https://bongbongcrypto.github.io/paidthrough/#/bill/3?r=Test+bill+B+%28mainnet+check%29&s=9780bf5cc0e9339cfa89a3e73d416b63) |

| Step | State | Tx | Block | Gas used | Cost USDC |
|---|---|---|---|---|---|
| A.issue | done | [0xa5ded6e202](https://explorer.arc.io/tx/0xa5ded6e2020b3e98802d14282499e201c8c056cdd604e5e4eab13d5228a9f2ec) | 24115975 | 138046 | 0.00276093 |
| A.pay | done | [0x30adafa74d](https://explorer.arc.io/tx/0x30adafa74d41133372092983dde2d6056b02a403e31a54d2816ab598a888a617) | 24115981 | 106307 | 0.00212615 |
| A.claim | done | [0xe0bc1bb156](https://explorer.arc.io/tx/0xe0bc1bb1561a41235cecfd914b06965665e8f4290d58d219762ed597552c197d) | 24115986 | 58905 | 0.00117811 |
| C.issue | done | [0xecbe57311f](https://explorer.arc.io/tx/0xecbe57311f5d0d1dcb4e43d0082e8153477d935ad8c13e6b13f38f083462833d) | 24115991 | 120934 | 0.00241869 |
| C.approve | done | [0x1f725ead2c](https://explorer.arc.io/tx/0x1f725ead2c5e10561b58760e72ae942dd0a02e34aeea0c0938cae8493aebd0c2) | 24115997 | 55438 | 0.00110877 |
| C.pay | done | [0x0f35bb1bfb](https://explorer.arc.io/tx/0x0f35bb1bfbcb938d428763d661245a757e4c61f13d192cd7621e3fcf35d9db61) | 24116002 | 69406 | 0.00138813 |
| C.decline | done | [0x60e648502f](https://explorer.arc.io/tx/0x60e648502fa6ed08a12de72456822851285daeed0329364349cf68972f7ccd5b) | 24116007 | 65541 | 0.00131083 |
| B.issue | done | [0x86aefd2a3b](https://explorer.arc.io/tx/0x86aefd2a3b89b1d548428eb6508c6174bffda2a2e9ce3a7f59a6e28e969f3f4c) | 24116013 | 120934 | 0.00241869 |
| B.pay | done | [0xf38d003b2c](https://explorer.arc.io/tx/0xf38d003b2c19a3ee7fdce13c1296f41ff138d20acd51462333d7fc13b1bbc847) | 24116019 | 106307 | 0.00212615 |

Bill B refund: [0x8a2a540ac5](https://explorer.arc.io/tx/0x8a2a540ac5c90a6e384dc55d7b15a4a39b98be7b2d416fcd7401b93123347932) in block 24123342 by 0x708B05A24D300c83f3D365DAD79a4c0b52D9038D.
