from odoo.exceptions import ValidationError
from odoo.tests import tagged
from odoo.tools import mute_logger

from odoo.addons.mdv_payment_cybersource.models.payment_transaction import format_amount
from odoo.addons.mdv_payment_cybersource.tests.common import (
    CAPTURE_ID, CUSTOMER_ID, INSTRUMENT_ID, PAYMENT_ID, REFUND_ID, REVERSAL_ID, TRANSIENT_TOKEN,
    CyberSourceCommon, authorized, declined, invalid_request,
)

PAY = ('POST', '/pts/v2/payments')


@tagged('post_install', '-at_install')
class TestCyberSourceTransaction(CyberSourceCommon):

    def _pay(self, tx, *answers):
        with self.mock_api(*answers) as api:
            result = tx._mdv_cs_pay(TRANSIENT_TOKEN, browser={})
        return result, api

    # === PAYLOAD === #

    def test_amount_format(self):
        jpy = self._enable_currency('JPY')
        kwd = self._enable_currency('KWD')
        self.assertEqual(format_amount(10.5, self.currency_usd), '10.50')
        self.assertEqual(format_amount(1050, jpy), '1050')
        self.assertEqual(format_amount(10.5, kwd), '10.500')
        self.assertEqual(format_amount(-3.333, self.currency_usd), '3.33')

    def test_payment_payload(self):
        tx = self._create_cs_transaction()
        payload = tx._mdv_cs_payment_payload(transient_token=TRANSIENT_TOKEN)
        self.assertEqual(payload['clientReferenceInformation']['code'], tx.reference)
        self.assertEqual(payload['orderInformation']['amountDetails'],
                         {'totalAmount': '1111.11', 'currency': 'USD'})
        self.assertTrue(payload['processingInformation']['capture'])
        self.assertEqual(payload['tokenInformation'], {'transientTokenJwt': TRANSIENT_TOKEN})
        bill_to = payload['orderInformation']['billTo']
        self.assertEqual((bill_to['firstName'], bill_to['lastName']), ('Alex', 'Morgan'))
        self.assertEqual(bill_to['administrativeArea'], 'CA')
        self.assertEqual(bill_to['country'], 'US')
        self.assertEqual(bill_to['postalCode'], '94105')
        self.assertEqual(bill_to['phoneNumber'], '+14155550100')
        self.assertNotIn('actionList', payload['processingInformation'])

    def test_manual_capture_and_tokenize_payload(self):
        self.cybersource.capture_manually = True
        tx = self._create_cs_transaction(tokenize=True)
        payload = tx._mdv_cs_payment_payload(transient_token=TRANSIENT_TOKEN)
        processing = payload['processingInformation']
        self.assertFalse(processing['capture'])
        self.assertEqual(processing['actionList'], ['TOKEN_CREATE'])
        self.assertEqual(processing['actionTokenTypes'], ['customer', 'paymentInstrument'])
        self.assertEqual(processing['authorizationOptions']['initiator'],
                         {'type': 'customer', 'credentialStoredOnFile': True})

    def test_descriptor_is_sent(self):
        self.cybersource.mdv_cs_descriptor = 'NORTHWIND'
        tx = self._create_cs_transaction()
        payload = tx._mdv_cs_payment_payload()
        self.assertEqual(payload['merchantInformation']['merchantDescriptor']['name'],
                         'NORTHWIND')

    # === DIRECT PAYMENT === #

    def test_sale_is_done(self):
        tx = self._create_cs_transaction()
        result, api = self._pay(tx, PAY + (authorized(), 201))
        self.assertEqual(result, {'redirect_url': '/payment/status'})
        self.assertEqual(tx.state, 'done')
        self.assertEqual(tx.provider_reference, PAYMENT_ID)
        self.assertEqual(tx.mdv_cs_status, 'AUTHORIZED')
        self.assertEqual(tx.mdv_cs_approval_code, '322516')
        self.assertEqual(tx.mdv_cs_avs_code, 'Y')
        self.assertEqual(tx.mdv_cs_cvv_code, 'M')
        self.assertEqual(tx.mdv_cs_card_type, 'Visa')
        self.assertEqual(tx.mdv_cs_card_masked, 'XXXXXXXXXXXX1111')
        self.assertEqual(tx.payment_method_id.code, 'visa')
        self.assertFalse(tx.sudo().mdv_cs_transient_token)
        self.assertEqual(api.paths(), [PAY])

    def test_authorization_only(self):
        self.cybersource.capture_manually = True
        tx = self._create_cs_transaction()
        self._pay(tx, PAY + (authorized(), 201))
        self.assertEqual(tx.state, 'authorized')

    def test_decline(self):
        tx = self._create_cs_transaction()
        self._pay(tx, PAY + (declined(), 201))
        self.assertEqual(tx.state, 'error')
        self.assertIn('insufficient funds', tx.state_message)
        self.assertEqual(tx.mdv_cs_reason, 'INSUFFICIENT_FUND')

    @mute_logger('odoo.addons.mdv_payment_cybersource.cybersource_client')
    def test_invalid_request(self):
        tx = self._create_cs_transaction()
        self._pay(tx, PAY + (invalid_request(), 400))
        self.assertEqual(tx.state, 'error')
        self.assertIn('orderInformation.billTo.locality', tx.state_message)

    def test_review_is_pending(self):
        tx = self._create_cs_transaction()
        self._pay(tx, PAY + (authorized(status='AUTHORIZED_PENDING_REVIEW'), 201))
        self.assertEqual(tx.state, 'pending')
        self.assertIn('review', tx.state_message)

    def test_risk_declined_is_reversed(self):
        tx = self._create_cs_transaction()
        body = authorized(status='AUTHORIZED_RISK_DECLINED',
                          errorInformation={'reason': 'DECISION_PROFILE_REJECT'})
        _result, api = self._pay(tx, PAY + (body, 201), (
            'POST', f'/pts/v2/payments/{PAYMENT_ID}/reversals', {'status': 'REVERSED'}, 201))
        self.assertEqual(tx.state, 'error')
        self.assertIn('fraud screening', tx.state_message)
        self.assertIn(('POST', f'/pts/v2/payments/{PAYMENT_ID}/reversals'), api.paths())

    def test_amount_mismatch(self):
        tx = self._create_cs_transaction()
        self._pay(tx, PAY + (authorized(amount='1.00'), 201))
        self.assertEqual(tx.state, 'error')

    def test_save_card_creates_token(self):
        tx = self._create_cs_transaction(tokenize=True)
        body = authorized(tokenInformation={
            'customer': {'id': CUSTOMER_ID}, 'paymentInstrument': {'id': INSTRUMENT_ID},
            'instrumentIdentifier': {'id': '7010000000016241111', 'state': 'ACTIVE'}})
        self._pay(tx, PAY + (body, 201))
        self.assertEqual(tx.state, 'done')
        token = tx.token_id
        self.assertTrue(token)
        self.assertEqual(token.provider_ref, INSTRUMENT_ID)
        self.assertEqual(token.mdv_cs_customer_ref, CUSTOMER_ID)
        self.assertEqual(token.payment_details, '1111')
        self.assertEqual(token.mdv_cs_network_txn_ref, '016153570198200')

    def test_validation_saves_card_with_zero_auth(self):
        tx = self._create_cs_transaction(operation='validation', amount=0.0, tokenize=True)
        body = authorized(amount='0.00', tokenInformation={
            'customer': {'id': CUSTOMER_ID}, 'paymentInstrument': {'id': INSTRUMENT_ID}})
        _result, api = self._pay(tx, PAY + (body, 201))
        sent = api.last('/pts/v2/payments')['json']
        self.assertFalse(sent['processingInformation']['capture'])
        self.assertEqual(sent['orderInformation']['amountDetails']['totalAmount'], '0.00')
        self.assertEqual(tx.state, 'done')
        self.assertEqual(tx.token_id.provider_ref, INSTRUMENT_ID)

    def test_card_type_from_network_name(self):
        tx = self._create_cs_transaction()
        self.assertEqual(tx._mdv_cs_card_values('MASTERCARD')['mdv_cs_card_type'], 'Mastercard')
        self.assertEqual(tx._mdv_cs_card_values('003')['mdv_cs_card_type'], 'American Express')
        values = tx._mdv_cs_card_values('VISA')
        self.assertEqual(self.env['payment.method'].browse(values['payment_method_id']).code,
                         'visa')
        self.assertEqual(tx._mdv_cs_card_values('999'), {'mdv_cs_card_type': '999'})

    def test_unreadable_transient_token(self):
        tx = self._create_cs_transaction()
        with self.assertRaises(ValidationError):
            tx._mdv_cs_pay('abc.def', browser={})

    # === 3-D SECURE === #

    def _setup_answer(self):
        return ('POST', '/risk/v1/authentication-setups', {
            'status': 'COMPLETED', 'id': '7922010849156974804806',
            'consumerAuthenticationInformation': {
                'accessToken': 'eyJhbGciOiJIUzI1NiJ9.e30.sig',
                'deviceDataCollectionUrl':
                    'https://centinelapistag.cardinalcommerce.com/V1/Cruise/Collect',
                'referenceId': 'e5fb5507-d1be-4e9b-b626-49ccd12182a6',
            }}, 201)

    def test_payer_auth_frictionless(self):
        self.cybersource.mdv_cs_payer_auth = True
        tx = self._create_cs_transaction()
        result, api = self._pay(tx, self._setup_answer())
        self.assertEqual(result['action'], 'collect')
        self.assertTrue(result['url'].startswith('https://centinelapistag'))
        setup = api.last('/authentication-setups')['json']
        # Payer Authentication takes the id (jti) of the transient token.
        self.assertEqual(setup['tokenInformation']['transientToken'],
                         self.jwt_claims(TRANSIENT_TOKEN)['jti'])
        self.assertEqual(tx.mdv_cs_3ds_ref, 'e5fb5507-d1be-4e9b-b626-49ccd12182a6')
        self.assertEqual(tx.state, 'draft')
        body = authorized(consumerAuthenticationInformation={
            'paresStatus': 'Y', 'eci': '05', 'eciRaw': '05', 'specificationVersion': '2.2.0',
            'cavv': 'AJkBBkhgQQAAAE4gSEJydQAAAAA='})
        with self.mock_api(PAY + (body, 201)) as api:
            result = tx._mdv_cs_authenticate(
                browser={'httpBrowserScreenWidth': '1440', 'httpBrowserLanguage': 'en-US',
                         'httpBrowserColorDepth': 'evil<script>'},
                return_url='https://shop.example.com/payment/mdv_cybersource/3ds/return?md=x')
        sent = api.last('/pts/v2/payments')['json']
        self.assertEqual(sent['processingInformation']['actionList'], ['CONSUMER_AUTHENTICATION'])
        self.assertEqual(sent['consumerAuthenticationInformation']['referenceId'],
                         'e5fb5507-d1be-4e9b-b626-49ccd12182a6')
        self.assertEqual(sent['deviceInformation']['httpBrowserScreenWidth'], '1440')
        self.assertNotIn('httpBrowserColorDepth', sent['deviceInformation'])
        self.assertEqual(result, {'redirect_url': '/payment/status'})
        self.assertEqual(tx.state, 'done')
        self.assertEqual((tx.mdv_cs_3ds_status, tx.mdv_cs_eci, tx.mdv_cs_3ds_version),
                         ('Y', '05', '2.2.0'))
        self.assertFalse(tx.sudo().mdv_cs_transient_token)

    def test_payer_auth_challenge(self):
        self.cybersource.mdv_cs_payer_auth = True
        tx = self._create_cs_transaction(tokenize=True)
        self._pay(tx, self._setup_answer())
        challenge = {
            'status': 'PENDING_AUTHENTICATION', 'id': '7922011845486152004805',
            'errorInformation': {'reason': 'CONSUMER_AUTHENTICATION_REQUIRED'},
            'consumerAuthenticationInformation': {
                'accessToken': 'eyJhbGciOiJIUzI1NiJ9.e30.sig',
                'stepUpUrl': 'https://centinelapistag.cardinalcommerce.com/V2/Cruise/StepUp',
                'authenticationTransactionId': 'z1HwdDZK23Fw29EhpXh0',
                'specificationVersion': '2.2.0',
            }}
        with self.mock_api(PAY + (challenge, 201)) as api:
            result = tx._mdv_cs_authenticate(browser={}, return_url='https://x/r')
        self.assertEqual(result['action'], 'challenge')
        self.assertTrue(result['url'].endswith('/StepUp'))
        self.assertEqual(tx.mdv_cs_3ds_auth_ref, 'z1HwdDZK23Fw29EhpXh0')
        self.assertEqual(tx.state, 'draft')
        self.assertEqual(tx._mdv_cs_tx_from_md(result['md']), tx)
        self.assertEqual(api.last('/pts')['json']['processingInformation']['actionList'],
                         ['CONSUMER_AUTHENTICATION', 'TOKEN_CREATE'])
        body = authorized(consumerAuthenticationInformation={'paresStatus': 'Y', 'eci': '05'},
                          tokenInformation={'customer': {'id': CUSTOMER_ID},
                                            'paymentInstrument': {'id': INSTRUMENT_ID}})
        with self.mock_api(PAY + (body, 201)) as api:
            tx._mdv_cs_validate_challenge('z1HwdDZK23Fw29EhpXh0')
        sent = api.last('/pts')['json']
        self.assertEqual(sent['processingInformation']['actionList'],
                         ['VALIDATE_CONSUMER_AUTHENTICATION', 'TOKEN_CREATE'])
        self.assertEqual(sent['consumerAuthenticationInformation'],
                         {'authenticationTransactionId': 'z1HwdDZK23Fw29EhpXh0'})
        self.assertEqual(tx.state, 'done')
        self.assertTrue(tx.token_id)

    def test_md_cannot_be_forged(self):
        tx = self._create_cs_transaction()
        md = tx._mdv_cs_sign_md()
        Tx = self.env['payment.transaction']
        self.assertEqual(Tx._mdv_cs_tx_from_md(md), tx)
        self.assertFalse(Tx._mdv_cs_tx_from_md(md[:-2] + 'xx'))
        self.assertFalse(Tx._mdv_cs_tx_from_md('%s.%s' % (tx.id + 1, md.split('.')[1])))
        self.assertFalse(Tx._mdv_cs_tx_from_md('garbage'))

    @mute_logger('odoo.addons.mdv_payment_cybersource.models.payment_transaction')
    def test_payer_auth_unavailable_falls_back(self):
        self.cybersource.mdv_cs_payer_auth = True
        tx = self._create_cs_transaction()
        result, api = self._pay(
            tx, ('POST', '/risk/v1/authentication-setups', invalid_request(), 400),
            PAY + (authorized(), 201))
        self.assertEqual(result, {'redirect_url': '/payment/status'})
        self.assertEqual(tx.state, 'done')
        self.assertEqual(api.paths()[-1], PAY)

    def test_failed_authentication(self):
        self.cybersource.mdv_cs_payer_auth = True
        tx = self._create_cs_transaction()
        self._pay(tx, self._setup_answer())
        with self.mock_api(PAY + (declined('CONSUMER_AUTHENTICATION_FAILED', 'x'), 201)):
            tx._mdv_cs_authenticate(browser={}, return_url='https://x/r')
        self.assertEqual(tx.state, 'error')
        self.assertIn('3-D Secure', tx.state_message)

    # === SAVED CARDS === #

    def _token(self):
        return self._create_token(provider_ref=INSTRUMENT_ID, mdv_cs_customer_ref=CUSTOMER_ID,
                                  mdv_cs_network_txn_ref='016153570198200')

    def test_pay_with_saved_card(self):
        token = self._token()
        tx = self._create_cs_transaction('token', token_id=token.id)
        with self.mock_api(PAY + (authorized(), 201)) as api:
            tx._charge_with_token()
        sent = api.last('/pts')['json']
        self.assertEqual(sent['paymentInformation'], {
            'paymentInstrument': {'id': INSTRUMENT_ID}, 'customer': {'id': CUSTOMER_ID}})
        self.assertEqual(sent['processingInformation']['authorizationOptions']['initiator'],
                         {'type': 'customer', 'storedCredentialUsed': True})
        self.assertNotIn('tokenInformation', sent)
        self.assertEqual(tx.state, 'done')

    def test_merchant_initiated_payment(self):
        token = self._token()
        tx = self._create_cs_transaction('token', token_id=token.id, operation='offline')
        with self.mock_api(PAY + (authorized(), 201)) as api:
            tx._charge_with_token()
        initiator = api.last('/pts')['json']['processingInformation'][
            'authorizationOptions']['initiator']
        self.assertEqual(initiator['type'], 'merchant')
        self.assertEqual(initiator['merchantInitiatedTransaction']['previousTransactionId'],
                         '016153570198200')
        self.assertEqual(tx.state, 'done')

    def test_removing_saved_card_deletes_instrument(self):
        token = self._token()
        path = f'/tms/v2/customers/{CUSTOMER_ID}/payment-instruments/{INSTRUMENT_ID}'
        with self.mock_api(('DELETE', path, None, 204)) as api:
            token.active = False
        self.assertEqual(api.paths(), [('DELETE', path)])

    # === CAPTURE / VOID / REFUND === #

    def _authorized_tx(self):
        self.cybersource.capture_manually = True
        tx = self._create_cs_transaction()
        self._pay(tx, PAY + (authorized(), 201))
        self.assertEqual(tx.state, 'authorized')
        return tx

    def test_capture(self):
        tx = self._authorized_tx()
        capture = {'id': CAPTURE_ID, 'status': 'PENDING',
                   'orderInformation': {'amountDetails': {'totalAmount': '1111.11',
                                                          'currency': 'USD'}}}
        with self.mock_api(('POST', f'/pts/v2/payments/{PAYMENT_ID}/captures', capture, 201)) \
                as api:
            child = tx._capture()
        self.assertEqual(api.last('/captures')['json']['orderInformation']['amountDetails'],
                         {'totalAmount': '1111.11', 'currency': 'USD'})
        self.assertEqual(child.state, 'done')
        self.assertEqual(child.provider_reference, CAPTURE_ID)
        self.assertEqual(tx.state, 'done')
        self.assertEqual(tx.mdv_cs_capture_ref, CAPTURE_ID)
        # A refund of a captured authorization goes to the capture.
        refund = {'id': REFUND_ID, 'status': 'PENDING',
                  'refundAmountDetails': {'refundAmount': '100.00', 'currency': 'USD'}}
        with self.mock_api(('POST', f'/pts/v2/captures/{CAPTURE_ID}/refunds', refund, 201)) \
                as api:
            refund_tx = tx._refund(amount_to_refund=100.0)
        self.assertEqual(refund_tx.state, 'done')
        self.assertEqual(refund_tx.amount, -100.0)
        self.assertEqual(api.last('/refunds')['json']['orderInformation']['amountDetails'][
            'totalAmount'], '100.00')

    def test_void(self):
        tx = self._authorized_tx()
        reversal = {'id': REVERSAL_ID, 'status': 'REVERSED',
                    'reversalAmountDetails': {'reversedAmount': '1111.11', 'currency': 'USD'}}
        with self.mock_api(('POST', f'/pts/v2/payments/{PAYMENT_ID}/reversals', reversal, 201)) \
                as api:
            child = tx._void()
        self.assertEqual(api.last('/reversals')['json']['reversalInformation'],
                         {'amountDetails': {'totalAmount': '1111.11'}, 'reason': 'void'})
        self.assertEqual(child.state, 'cancel')
        self.assertEqual(tx.state, 'cancel')

    def test_partial_refund_of_sale(self):
        tx = self._create_cs_transaction()
        self._pay(tx, PAY + (authorized(), 201))
        refund = {'id': REFUND_ID, 'status': 'PENDING'}
        with self.mock_api(('POST', f'/pts/v2/payments/{PAYMENT_ID}/refunds', refund, 201)):
            refund_tx = tx._refund(amount_to_refund=11.11)
        self.assertEqual(refund_tx.state, 'done')
        self.assertEqual(refund_tx.provider_reference, REFUND_ID)

    @mute_logger('odoo.addons.mdv_payment_cybersource.cybersource_client')
    def test_refund_refused(self):
        tx = self._create_cs_transaction()
        self._pay(tx, PAY + (authorized(), 201))
        with self.mock_api(('POST', f'/pts/v2/payments/{PAYMENT_ID}/refunds',
                            {'status': 'INVALID_REQUEST', 'reason': 'INVALID_AMOUNT',
                             'message': 'The amount exceeds the captured amount'}, 400)):
            refund_tx = tx._refund(amount_to_refund=5000.0)
        self.assertEqual(refund_tx.state, 'error')
        self.assertIn('exceeds', refund_tx.state_message)

    # === API LOG === #

    def test_api_log_hides_token(self):
        tx = self._create_cs_transaction()
        self._pay(tx, PAY + (authorized(), 201))
        log = tx.mdv_cs_log_ids
        self.assertEqual(len(log), 1)
        self.assertEqual(log.result, 'AUTHORIZED')
        self.assertTrue(log.success)
        self.assertNotIn(TRANSIENT_TOKEN, log.request_body)
        self.assertIn('"transientTokenJwt": "***"', log.request_body)
