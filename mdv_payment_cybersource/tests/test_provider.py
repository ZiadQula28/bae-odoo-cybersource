from unittest.mock import patch

from odoo.exceptions import UserError, ValidationError
from odoo.tests import tagged

from odoo.addons.mdv_payment_cybersource.tests.common import (
    CAPTURE_CONTEXT, WEBHOOK_KEY_ID, CyberSourceCommon, make_jwt,
)


@tagged('post_install', '-at_install')
class TestCyberSourceProvider(CyberSourceCommon):

    def test_feature_support(self):
        self.assertTrue(self.cybersource.support_tokenization)
        self.assertEqual(self.cybersource.support_manual_capture, 'full_only')
        self.assertEqual(self.cybersource.support_refund, 'partial')

    def test_default_payment_methods_and_networks(self):
        self.assertEqual(self.cybersource._get_default_payment_method_codes(),
                         {'card', 'visa', 'mastercard', 'amex', 'discover'})
        card = self.env.ref('payment.payment_method_card')
        self.cybersource.payment_method_ids = [(6, 0, [card.id])]
        visa = self.env.ref('payment.payment_method_visa')
        jcb = self.env.ref('payment.payment_method_jcb')
        (card.brand_ids - visa - jcb).active = False
        (visa | jcb).active = True
        self.assertEqual(sorted(self.cybersource._mdv_cs_networks()), ['JCB', 'VISA'])

    def test_capture_context_request(self):
        with self.mock_api(('POST', '/microform/v2/sessions', CAPTURE_CONTEXT, 201)) as api:
            result = self.cybersource._mdv_cs_capture_context('https://shop.example.com/shop/x')
        sent = api.last('/microform')['json']
        self.assertEqual(sent['targetOrigins'], ['https://shop.example.com'])
        self.assertEqual(sent['clientVersion'], 'v2.0')
        self.assertEqual(sent['allowedPaymentTypes'], ['CARD'])
        self.assertTrue(sent['allowedCardNetworks'])
        self.assertEqual(result['capture_context'], CAPTURE_CONTEXT)
        self.assertTrue(result['client_library'].startswith('https://testflex.cybersource.com/'))
        self.assertTrue(result['client_library_integrity'].startswith('sha256-'))
        # The capture context is a secret-like JWT: it is not stored in the API log.
        log = self.env['mdv.cybersource.log'].search([('provider_id', '=', self.cybersource.id)],
                                                     limit=1)
        self.assertEqual(log.operation, 'capture_context')
        self.assertNotIn(CAPTURE_CONTEXT, log.response_body or '')

    def test_capture_context_rejects_foreign_library(self):
        evil = make_jwt({'ctx': [{'data': {'clientLibrary': 'https://evil.example.com/x.js'}}]})
        with self.mock_api(('POST', '/microform/v2/sessions', evil, 201)):
            with self.assertRaises(ValidationError):
                self.cybersource._mdv_cs_capture_context('https://shop.example.com')
        with self.assertRaises(ValidationError):
            self.cybersource._mdv_cs_capture_context('javascript:alert(1)')

    def test_test_connection(self):
        with self.mock_api(('POST', '/microform/v2/sessions', CAPTURE_CONTEXT, 201)):
            action = self.cybersource.action_mdv_cs_test_connection()
        self.assertEqual(action['params']['type'], 'success')
        with self.mock_api(('POST', '/microform/v2/sessions',
                            {'response': {'rmsg': 'Authentication Failed'}}, 401)):
            with self.assertRaisesRegex(ValidationError, 'Authentication Failed'):
                self.cybersource.action_mdv_cs_test_connection()

    def test_environment_follows_state(self):
        self.cybersource.state = 'test'
        self.assertEqual(self.cybersource._mdv_cs_client().host, 'apitest.cybersource.com')
        self.cybersource.state = 'enabled'
        self.assertEqual(self.cybersource._mdv_cs_client().host, 'api.cybersource.com')

    def test_register_webhook_needs_https(self):
        with patch.object(type(self.cybersource), 'get_base_url',
                          return_value='http://localhost:8069'):
            with self.assertRaises(UserError):
                self.cybersource.action_mdv_cs_register_webhook()

    def test_register_webhook(self):
        self.cybersource.write({'mdv_cs_webhook_key_id': False, 'mdv_cs_webhook_secret': False})
        products = [
            {'productId': 'payments', 'eventTypes': [
                {'eventName': 'payments.payments.accept'}, {'eventName': 'payments.refunds.accept'}]},
            {'productId': 'tokenManagement', 'eventTypes': ['tms.token.created']},
        ]
        routes = [
            ('POST', '/kms/egress/v2/keys-sym', {'status': 'SUCCESS', 'keyInformation': {
                'keyId': 'new-key-id', 'key': 'bmV3LWtleQ=='}}, 201),
            ('GET', '/notification-subscriptions/v2/products/.*', [products], 200),
            ('POST', '/notification-subscriptions/v2/webhooks', {'webhookId': 'wh-123',
                                                                 'status': 'INACTIVE'}, 201),
        ]
        with patch.object(type(self.cybersource), 'get_base_url',
                          return_value='https://shop.example.com'):
            with self.mock_api(*routes) as api:
                self.cybersource.action_mdv_cs_register_webhook()
        self.assertEqual(self.cybersource.mdv_cs_webhook_key_id, 'new-key-id')
        self.assertEqual(self.cybersource.mdv_cs_webhook_secret, 'bmV3LWtleQ==')
        self.assertEqual(self.cybersource.mdv_cs_webhook_ref, 'wh-123')
        sent = api.last('/webhooks')['json']
        self.assertEqual(sent['webhookUrl'],
                         'https://shop.example.com/payment/mdv_cybersource/webhook')
        self.assertEqual(sent['products'], [{'productId': 'payments', 'eventTypes': [
            'payments.payments.accept', 'payments.refunds.accept']}])
        self.assertEqual(sent['organizationId'], self.cybersource.mdv_cs_merchant_id)
        key_request = api.last('/keys-sym')['json']
        self.assertEqual(key_request['keyInformation']['keyType'], 'sharedSecret')

    def test_webhook_keys(self):
        self.assertIn(WEBHOOK_KEY_ID, self.cybersource._mdv_cs_webhook_keys())

    def test_descriptor_must_be_ascii(self):
        with self.assertRaises(ValidationError):
            self.cybersource.mdv_cs_descriptor = 'Café Northwind'
        self.cybersource.mdv_cs_descriptor = 'NORTHWIND'

    def test_expiry_years(self):
        years = self.cybersource._mdv_cs_expiry_years()
        self.assertEqual(len(years), 16)
        self.assertEqual(int(years[1]) - int(years[0]), 1)
