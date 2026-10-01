// SPDX-License-Identifier: MIT
pragma solidity 0.8.30;

import {PaidThroughBase} from "./utils/PaidThroughBase.sol";
import {PaidThrough} from "../src/PaidThrough.sol";
import {EcdsaSelfDelegate, NoErc1271Delegate} from "./mocks/Delegates.sol";

/// @notice EIP-3009 payment path with real secp256k1 signatures (vm.sign) over the USDC EIP-712 domain.
contract PaidThroughAuthTest is PaidThroughBase {
    uint256 internal validAfter;
    uint256 internal validBefore;

    function setUp() public override {
        super.setUp();
        validAfter = block.timestamp - 1;
        validBefore = block.timestamp + 1 hours;
    }

    function _signBill(uint256 pk, uint256 billId) internal view returns (uint8, bytes32, bytes32) {
        return _sign(pk, address(pt), pt.getBill(billId).amount, validAfter, validBefore, pt.authNonce(billId));
    }

    function test_typehashMatchesSpec() public view {
        assertEq(
            RECEIVE_TYPEHASH,
            keccak256(
                "ReceiveWithAuthorization(address from,address to,uint256 value,uint256 validAfter,uint256 validBefore,bytes32 nonce)"
            )
        );
        assertEq(usdc.RECEIVE_WITH_AUTHORIZATION_TYPEHASH(), RECEIVE_TYPEHASH);
    }

    function test_mockDomainMatchesUsdcShape() public view {
        bytes32 expected = keccak256(
            abi.encode(
                keccak256("EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"),
                keccak256("USDC"),
                keccak256("2"),
                block.chainid,
                address(usdc)
            )
        );
        assertEq(usdc.DOMAIN_SEPARATOR(), expected);
        assertEq(usdc.decimals(), 6);
    }

    function test_authNonce_formula() public view {
        assertEq(pt.authNonce(1), keccak256(abi.encode(block.chainid, address(pt), uint256(1))));
        assertTrue(pt.authNonce(1) != pt.authNonce(2));
    }

    function test_authNonce_differsAcrossDeploymentsAndChains() public {
        PaidThrough pt2 = new PaidThrough(address(usdc), MAX_AMOUNT);
        assertTrue(pt.authNonce(1) != pt2.authNonce(1));
        bytes32 before = pt.authNonce(1);
        vm.chainId(5042);
        assertTrue(pt.authNonce(1) != before);
        assertEq(pt.authNonce(1), keccak256(abi.encode(uint256(5042), address(pt), uint256(1))));
    }

    function test_relayerSubmits_payerPays_noApprovalNeeded() public {
        uint256 id = _issue();
        (uint8 v, bytes32 r, bytes32 s) = _signBill(PAYER_PK, id);
        uint64 claimBy = uint64(block.timestamp) + CLAIM_WINDOW;

        assertEq(usdc.allowance(payer, address(pt)), 0);
        vm.expectEmit(true, true, false, true, address(pt));
        emit BillPaid(id, payer, claimBy);
        vm.prank(relayer);
        pt.payWithAuthorization(id, payer, validAfter, validBefore, v, r, s);

        PaidThrough.Bill memory b = pt.getBill(id);
        assertEq(b.payer, payer);
        assertEq(b.claimBy, claimBy);
        assertEq(uint8(b.status), uint8(PaidThrough.Status.Paid));
        assertEq(usdc.balanceOf(address(pt)), AMOUNT);
        assertEq(usdc.balanceOf(payer), START_BALANCE - AMOUNT);
        assertEq(usdc.balanceOf(relayer), 0);
        assertEq(usdc.allowance(payer, address(pt)), 0, "no standing allowance");
        assertTrue(usdc.authorizationState(payer, pt.authNonce(id)));
    }

    function test_payerSubmitsOwnSignature_thenClaim() public {
        uint256 id = _issue();
        (uint8 v, bytes32 r, bytes32 s) = _signBill(PAYER_PK, id);
        vm.prank(payer);
        pt.payWithAuthorization(id, payer, validAfter, validBefore, v, r, s);
        vm.prank(payee);
        pt.claim(id);
        assertEq(usdc.balanceOf(payee), AMOUNT);
        assertEq(usdc.balanceOf(address(pt)), 0);
    }

    function test_revert_signatureForBillACannotPayBillB() public {
        uint256 a = _issueAs(payee, AMOUNT, address(0));
        uint256 b = _issueAs(payee2, AMOUNT, address(0)); // same amount, different payee
        (uint8 v, bytes32 r, bytes32 s) = _signBill(PAYER_PK, a);
        vm.prank(relayer);
        vm.expectRevert(bytes("FiatTokenV2: invalid signature"));
        pt.payWithAuthorization(b, payer, validAfter, validBefore, v, r, s);
        _assertStatus(b, PaidThrough.Status.Open);
        _assertStatus(a, PaidThrough.Status.Open);
        assertEq(usdc.balanceOf(payer), START_BALANCE);
    }

    function test_revert_replayAfterSuccess() public {
        uint256 id = _issue();
        (uint8 v, bytes32 r, bytes32 s) = _signBill(PAYER_PK, id);
        pt.payWithAuthorization(id, payer, validAfter, validBefore, v, r, s);

        // Same bill: no longer Open.
        vm.expectRevert(PaidThrough.WrongStatus.selector);
        pt.payWithAuthorization(id, payer, validAfter, validBefore, v, r, s);

        // Even after the bill is declined (payer refunded), the token refuses the used nonce.
        vm.prank(payee);
        pt.decline(id);
        bytes32 nonce = pt.authNonce(id);
        vm.prank(address(pt));
        vm.expectRevert(bytes("FiatTokenV2: authorization is used or canceled"));
        usdc.receiveWithAuthorization(payer, address(pt), AMOUNT, validAfter, validBefore, nonce, v, r, s);
        assertEq(usdc.balanceOf(payer), START_BALANCE);
    }

    function test_revert_validBeforePassed() public {
        uint256 id = _issue();
        (uint8 v, bytes32 r, bytes32 s) = _signBill(PAYER_PK, id);
        vm.warp(validBefore + 1);
        vm.expectRevert(bytes("FiatTokenV2: authorization is expired"));
        pt.payWithAuthorization(id, payer, validAfter, validBefore, v, r, s);
        _assertStatus(id, PaidThrough.Status.Open);
    }

    function test_revert_validBeforeEqualsNow() public {
        uint256 id = _issue();
        (uint8 v, bytes32 r, bytes32 s) = _signBill(PAYER_PK, id);
        vm.warp(validBefore);
        vm.expectRevert(bytes("FiatTokenV2: authorization is expired"));
        pt.payWithAuthorization(id, payer, validAfter, validBefore, v, r, s);
    }

    function test_revert_validAfterInFuture() public {
        uint256 id = _issue();
        validAfter = block.timestamp + 10 minutes;
        (uint8 v, bytes32 r, bytes32 s) = _signBill(PAYER_PK, id);
        vm.expectRevert(bytes("FiatTokenV2: authorization is not yet valid"));
        pt.payWithAuthorization(id, payer, validAfter, validBefore, v, r, s);
        _assertStatus(id, PaidThrough.Status.Open);
    }

    function test_validAfter_becomesValidOnlyAfterTime() public {
        uint256 id = _issue();
        validAfter = block.timestamp + 10 minutes;
        (uint8 v, bytes32 r, bytes32 s) = _signBill(PAYER_PK, id);
        vm.warp(validAfter);
        vm.expectRevert(bytes("FiatTokenV2: authorization is not yet valid"));
        pt.payWithAuthorization(id, payer, validAfter, validBefore, v, r, s);
        vm.warp(validAfter + 1);
        pt.payWithAuthorization(id, payer, validAfter, validBefore, v, r, s);
        _assertStatus(id, PaidThrough.Status.Paid);
    }

    function test_revert_signerIsNotPayerArg() public {
        uint256 id = _issue();
        (uint8 v, bytes32 r, bytes32 s) = _signBill(OTHER_PK, id); // other signs
        vm.expectRevert(bytes("FiatTokenV2: invalid signature"));
        pt.payWithAuthorization(id, payer, validAfter, validBefore, v, r, s); // claims payer
        assertEq(usdc.balanceOf(payer), START_BALANCE);
        assertEq(usdc.balanceOf(other), START_BALANCE);
    }

    function test_revert_zeroPayerWithGarbageSignature() public {
        uint256 id = _issue();
        vm.expectRevert(bytes("ECRecover: invalid signature"));
        pt.payWithAuthorization(id, address(0), validAfter, validBefore, 27, bytes32(0), bytes32(uint256(1)));
        _assertStatus(id, PaidThrough.Status.Open);
    }

    function test_revert_allowedPayerMismatch() public {
        uint256 id = _issueAs(payee, AMOUNT, payer);
        (uint8 v, bytes32 r, bytes32 s) = _signBill(OTHER_PK, id);
        vm.expectRevert(PaidThrough.NotAllowedPayer.selector);
        pt.payWithAuthorization(id, other, validAfter, validBefore, v, r, s);
    }

    function test_allowedPayer_matches_ok() public {
        uint256 id = _issueAs(payee, AMOUNT, payer);
        (uint8 v, bytes32 r, bytes32 s) = _signBill(PAYER_PK, id);
        vm.prank(relayer);
        pt.payWithAuthorization(id, payer, validAfter, validBefore, v, r, s);
        assertEq(pt.getBill(id).payer, payer);
    }

    function test_revert_wrongValueSigned() public {
        uint256 id = _issue();
        (uint8 v, bytes32 r, bytes32 s) =
            _sign(PAYER_PK, address(pt), AMOUNT - 1, validAfter, validBefore, pt.authNonce(id));
        vm.expectRevert(bytes("FiatTokenV2: invalid signature"));
        pt.payWithAuthorization(id, payer, validAfter, validBefore, v, r, s);
    }

    function test_revert_randomNonceSigned() public {
        uint256 id = _issue();
        (uint8 v, bytes32 r, bytes32 s) =
            _sign(PAYER_PK, address(pt), AMOUNT, validAfter, validBefore, keccak256("some other nonce"));
        vm.expectRevert(bytes("FiatTokenV2: invalid signature"));
        pt.payWithAuthorization(id, payer, validAfter, validBefore, v, r, s);
    }

    function test_revert_signatureForOtherDeployment() public {
        PaidThrough pt2 = new PaidThrough(address(usdc), MAX_AMOUNT);
        uint256 id = _issue();
        vm.prank(payee);
        pt2.issue(AMOUNT, uint64(block.timestamp) + PAY_WINDOW, CLAIM_WINDOW, address(0), REF);
        // Signed for pt2's bill 1 (to = pt2, nonce bound to pt2); replayed on pt's bill 1.
        (uint8 v, bytes32 r, bytes32 s) =
            _sign(PAYER_PK, address(pt2), AMOUNT, validAfter, validBefore, pt2.authNonce(1));
        vm.expectRevert(bytes("FiatTokenV2: invalid signature"));
        pt.payWithAuthorization(id, payer, validAfter, validBefore, v, r, s);
    }

    function test_revert_signatureOnOtherChainDomain() public {
        uint256 id = _issue();
        bytes32 foreignDomain = keccak256(
            abi.encode(
                keccak256("EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"),
                keccak256("USDC"),
                keccak256("2"),
                uint256(5042002),
                address(usdc)
            )
        );
        (uint8 v, bytes32 r, bytes32 s) =
            _signFor(foreignDomain, PAYER_PK, address(pt), AMOUNT, validAfter, validBefore, pt.authNonce(id));
        vm.expectRevert(bytes("FiatTokenV2: invalid signature"));
        pt.payWithAuthorization(id, payer, validAfter, validBefore, v, r, s);
    }

    function test_revert_highSMalleatedSignature() public {
        uint256 id = _issue();
        (uint8 v, bytes32 r, bytes32 s) = _signBill(PAYER_PK, id);
        uint256 n = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141;
        bytes32 sHigh = bytes32(n - uint256(s));
        uint8 vFlip = v == 27 ? 28 : 27;
        vm.expectRevert(bytes("ECRecover: invalid signature 's' value"));
        pt.payWithAuthorization(id, payer, validAfter, validBefore, vFlip, r, sHigh);
    }

    function test_revert_signatureCannotBeUsedDirectlyOnToken() public {
        uint256 id = _issue();
        (uint8 v, bytes32 r, bytes32 s) = _signBill(PAYER_PK, id);
        // An attacker cannot pull the signed funds anywhere: receiveWithAuthorization requires msg.sender == to.
        bytes32 nonce = pt.authNonce(id);
        vm.prank(stranger);
        vm.expectRevert(bytes("FiatTokenV2: caller must be the payee"));
        usdc.receiveWithAuthorization(payer, address(pt), AMOUNT, validAfter, validBefore, nonce, v, r, s);
        assertFalse(usdc.authorizationState(payer, nonce), "nonce not consumed");
        // The signature still works through the contract afterwards.
        pt.payWithAuthorization(id, payer, validAfter, validBefore, v, r, s);
        _assertStatus(id, PaidThrough.Status.Paid);
    }

    function test_revert_payByPassed() public {
        uint256 id = _issue();
        vm.warp(pt.getBill(id).payBy);
        validBefore = block.timestamp + 1 hours;
        (uint8 v, bytes32 r, bytes32 s) = _signBill(PAYER_PK, id);
        vm.expectRevert(PaidThrough.PayWindowClosed.selector);
        pt.payWithAuthorization(id, payer, validAfter, validBefore, v, r, s);
    }

    function test_revert_cancelledBill() public {
        uint256 id = _issue();
        (uint8 v, bytes32 r, bytes32 s) = _signBill(PAYER_PK, id);
        vm.prank(payee);
        pt.cancel(id);
        vm.expectRevert(PaidThrough.WrongStatus.selector);
        pt.payWithAuthorization(id, payer, validAfter, validBefore, v, r, s);
        assertEq(usdc.balanceOf(payer), START_BALANCE);
    }

    function test_revert_insufficientBalance_stateRolledBack() public {
        uint256 pk = 0xD0D;
        address poor = vm.addr(pk);
        usdc.mint(poor, AMOUNT - 1);
        uint256 id = _issue();
        (uint8 v, bytes32 r, bytes32 s) = _signBill(pk, id);
        vm.expectRevert(bytes("ERC20: transfer amount exceeds balance"));
        pt.payWithAuthorization(id, poor, validAfter, validBefore, v, r, s);
        PaidThrough.Bill memory b = pt.getBill(id);
        assertEq(uint8(b.status), uint8(PaidThrough.Status.Open));
        assertEq(b.payer, address(0));
        assertEq(b.claimBy, 0);
    }

    // ---- payer argument pointing at a contract: the token cannot be made to pull from PaidThrough or itself ----

    function test_revert_payerIsPaidThroughItself() public {
        usdc.mint(address(pt), AMOUNT); // even with stray tokens sitting in the contract
        uint256 id = _issue();
        (uint8 v, bytes32 r, bytes32 s) = _signBill(PAYER_PK, id);
        vm.expectRevert(bytes("FiatTokenV2: invalid signature"));
        pt.payWithAuthorization(id, address(pt), validAfter, validBefore, v, r, s);
        _assertStatus(id, PaidThrough.Status.Open);
        assertEq(usdc.balanceOf(address(pt)), AMOUNT);
    }

    function test_revert_payerIsTheTokenContract() public {
        uint256 id = _issue();
        (uint8 v, bytes32 r, bytes32 s) = _signBill(PAYER_PK, id);
        vm.expectRevert(bytes("FiatTokenV2: invalid signature"));
        pt.payWithAuthorization(id, address(usdc), validAfter, validBefore, v, r, s);
        _assertStatus(id, PaidThrough.Status.Open);
    }

    function test_revert_signatureRedirectedToAttacker() public {
        uint256 id = _issue();
        (uint8 v, bytes32 r, bytes32 s) = _signBill(PAYER_PK, id); // signed for to = PaidThrough
        bytes32 nonce = pt.authNonce(id);
        vm.prank(stranger); // stranger names itself as `to`: passes the caller check, fails the signature
        vm.expectRevert(bytes("FiatTokenV2: invalid signature"));
        usdc.receiveWithAuthorization(payer, stranger, AMOUNT, validAfter, validBefore, nonce, v, r, s);
        assertFalse(usdc.authorizationState(payer, nonce));
        assertEq(usdc.balanceOf(stranger), START_BALANCE);
    }

    // ---- EIP-7702-delegated payers: USDC checks addresses with code only through ERC-1271 ----

    function test_delegatedPayer_withoutErc1271_cannotUseSignaturePath_butCanPay() public {
        vm.etch(payer, type(NoErc1271Delegate).runtimeCode); // payer EOA now carries delegate code
        uint256 id = _issue();
        (uint8 v, bytes32 r, bytes32 s) = _signBill(PAYER_PK, id);
        vm.expectRevert(bytes("FiatTokenV2: invalid signature"));
        pt.payWithAuthorization(id, payer, validAfter, validBefore, v, r, s);
        _assertStatus(id, PaidThrough.Status.Open);

        // Fallback path (approve + pay) still works for the same payer.
        _pay(id, payer);
        _assertStatus(id, PaidThrough.Status.Paid);
        assertEq(usdc.balanceOf(address(pt)), AMOUNT);
    }

    function test_delegatedPayer_withErc1271_canUseSignaturePath() public {
        vm.etch(payer, type(EcdsaSelfDelegate).runtimeCode);
        uint256 id = _issue();
        (uint8 v, bytes32 r, bytes32 s) = _signBill(PAYER_PK, id);
        vm.prank(relayer);
        pt.payWithAuthorization(id, payer, validAfter, validBefore, v, r, s);
        _assertStatus(id, PaidThrough.Status.Paid);
        assertEq(usdc.balanceOf(payer), START_BALANCE - AMOUNT);

        // A signature by a different key is rejected by the delegate.
        uint256 id2 = _issue();
        (v, r, s) = _signBill(OTHER_PK, id2);
        vm.expectRevert(bytes("FiatTokenV2: invalid signature"));
        pt.payWithAuthorization(id2, payer, validAfter, validBefore, v, r, s);
    }

    // ---- paying a bill that is already Claimed, Declined or Refunded ----

    function _terminalBills() internal returns (uint256[3] memory ids) {
        ids[0] = _issueAndPay();
        vm.prank(payee);
        pt.claim(ids[0]);
        ids[1] = _issueAndPay();
        vm.prank(payee);
        pt.decline(ids[1]);
        ids[2] = _issueAndPay();
        vm.warp(pt.getBill(ids[2]).claimBy);
        pt.refund(ids[2]);
    }

    function test_revert_payAndPayWithAuthorization_onClaimedDeclinedRefunded() public {
        uint256[3] memory ids = _terminalBills();
        validAfter = block.timestamp - 1;
        validBefore = block.timestamp + 1 hours;
        uint256 heldBefore = usdc.balanceOf(address(pt));
        for (uint256 i; i < 3; ++i) {
            vm.startPrank(other);
            usdc.approve(address(pt), AMOUNT);
            vm.expectRevert(PaidThrough.WrongStatus.selector);
            pt.pay(ids[i]);
            vm.stopPrank();

            (uint8 v, bytes32 r, bytes32 s) = _signBill(OTHER_PK, ids[i]);
            vm.prank(relayer);
            vm.expectRevert(PaidThrough.WrongStatus.selector);
            pt.payWithAuthorization(ids[i], other, validAfter, validBefore, v, r, s);
        }
        assertEq(usdc.balanceOf(address(pt)), heldBefore);
        assertEq(usdc.balanceOf(other), START_BALANCE);
    }

    // ---- EIP-3009 cancelAuthorization: a payer can withdraw the signature path for one bill ----

    function _signCancel(uint256 pk, bytes32 nonce) internal view returns (uint8, bytes32, bytes32) {
        bytes32 structHash = keccak256(abi.encode(usdc.CANCEL_AUTHORIZATION_TYPEHASH(), vm.addr(pk), nonce));
        bytes32 digest = keccak256(abi.encodePacked("\x19\x01", usdc.DOMAIN_SEPARATOR(), structHash));
        return vm.sign(pk, digest);
    }

    function test_cancelAuthorization_blocksSignaturePathOnly() public {
        uint256 id = _issue();
        uint256 other_ = _issue();
        (uint8 v, bytes32 r, bytes32 s) = _signBill(PAYER_PK, id);

        (uint8 cv, bytes32 cr, bytes32 cs) = _signCancel(PAYER_PK, pt.authNonce(id));
        usdc.cancelAuthorization(payer, pt.authNonce(id), cv, cr, cs);
        assertTrue(usdc.authorizationState(payer, pt.authNonce(id)));

        vm.prank(relayer);
        vm.expectRevert(bytes("FiatTokenV2: authorization is used or canceled"));
        pt.payWithAuthorization(id, payer, validAfter, validBefore, v, r, s);
        _assertStatus(id, PaidThrough.Status.Open);

        // The other bill's signature path is untouched, and approve + pay still pays the cancelled one.
        (v, r, s) = _signBill(PAYER_PK, other_);
        pt.payWithAuthorization(other_, payer, validAfter, validBefore, v, r, s);
        _pay(id, payer);
        _assertStatus(id, PaidThrough.Status.Paid);
        _assertStatus(other_, PaidThrough.Status.Paid);
    }
}
