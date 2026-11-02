import json
import time
from unittest.mock import patch

from odoo.tests import HttpCase, tagged
from odoo.tools import mute_logger

from odoo.addons.payment import utils as payment_utils
from odoo.addons.mdv_payment_cybersource.cybersource_client import webhook_signatures
from odoo.addons.mdv_payment_cybersource.tests.common import (
    CAPTURE_CONTEXT, PAYMENT_ID, REQUEST_PATH, TRANSIENT_TOKEN, WEBHOOK_KEY_ID, WEBHOOK_SECRET,
    CyberSourceCommon, FakeApi, authorized,
)
from odoo.addons.mdv_payment_cybersource.tests.test_webhook import details, notification

WEBHOOK = '/payment/mdv_cybersource/webhook'


@tagged('post_install', '-at_install')
class TestCyberSourceController(CyberSourceCommon, HttpCase):

    def _jsonrpc(self, url, params):
        response = self.url_open(url, data=json.dumps({
            'jsonrpc': '2.0', 'method': 'call', 'params': params}),
            headers={'Content-Type': 'application/json'})
        return response.json()

    def _post_webhook(self, body, header=None):
        raw = json.dumps(body).encode()
        if header is None:
            stamp = str(int(time.time() * 1000))
            header = (f't={stamp};keyId={WEBHOOK_KEY_ID};'
                      f'sig={webhook_signatures(WEBHOOK_SECRET, stamp, raw)[0]}')
        return self.url_open(WEBHOOK, data=raw, headers={
            'Content-Type': 'application/json', 'v-c-signature': header})

    def test_webhook_health_check(self):
        response = self.url_open(WEBHOOK)
        self.assertEqual(response.status_code, 200)

    @mute_logger('odoo.addons.mdv_payment_cybersource.controllers.main')
    def test_webhook_refuses_bad_signature(self):
        response = self._post_webhook(notification(), header='t=1;keyId=x;sig=y')
        self.assertEqual(response.status_code, 401)
        response = self._post_webhook(notification(), header='')
        self.assertEqual(response.status_code, 401)

    def test_webhook_updates_transaction(self):
        self.cybersource.capture_manually = True
        tx = self._create_cs_transaction()
        with self.mock_api(('POST', '/pts/v2/payments',
                            authorized(status='AUTHORIZED_PENDING_REVIEW'), 201)):
            tx._mdv_cs_pay(TRANSIENT_TOKEN, browser={})
        self.assertEqual(tx.state, 'pending')
        api = FakeApi([('GET', f'/tss/v2/transactions/{PAYMENT_ID}',
                        details(tx.reference, 'ics_auth'), 200)])
        with patch(REQUEST_PATH, side_effect=api):
            response = self._post_webhook(notification())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(api.paths(), [('GET', f'/tss/v2/transactions/{PAYMENT_ID}')])
        self.assertEqual(tx.state, 'authorized')
        log = self.env['mdv.cybersource.log'].search(
            [('direction', '=', 'in'), ('provider_id', '=', self.cybersource.id)], limit=1)
        self.assertEqual(log.operation, 'webhook')

    @mute_logger('odoo.addons.mdv_payment_cybersource.cybersource_client')
    def test_webhook_retry_when_not_readable(self):
        tx = self._create_cs_transaction()
        tx.provider_reference = PAYMENT_ID
        api = FakeApi([('GET', f'/tss/v2/transactions/{PAYMENT_ID}', {'message': 'no'}, 404)])
        with patch(REQUEST_PATH, side_effect=api):
            self.assertEqual(self._post_webhook(notification(retry=0)).status_code, 503)
            self.assertEqual(self._post_webhook(notification(retry=2)).status_code, 200)

    def test_pay_route_checks_access_token(self):
        tx = self._create_cs_transaction()
        result = self._jsonrpc('/payment/mdv_cybersource/pay', {
            'tx_id': tx.id, 'access_token': 'forged', 'transient_token': TRANSIENT_TOKEN})
        self.assertIn('error', result)
        token = payment_utils.generate_access_token(tx.reference, tx.id, env=self.env)
        result = self._jsonrpc('/payment/mdv_cybersource/pay', {
            'tx_id': tx.id, 'access_token': token, 'transient_token': '<script>'})
        self.assertIn('error', result)

    def test_pay_route(self):
        tx = self._create_cs_transaction()
        token = payment_utils.generate_access_token(tx.reference, tx.id, env=self.env)
        api = FakeApi([('POST', '/pts/v2/payments', authorized(), 201)])
        with patch(REQUEST_PATH, side_effect=api):
            result = self._jsonrpc('/payment/mdv_cybersource/pay', {
                'tx_id': tx.id, 'access_token': token, 'transient_token': TRANSIENT_TOKEN,
                'browser': {'httpBrowserLanguage': 'en-US'}})
        self.assertEqual(result['result'], {'redirect_url': '/payment/status'})
        self.assertEqual(tx.state, 'done')
        # A second click does not charge again.
        with patch(REQUEST_PATH, side_effect=api):
            result = self._jsonrpc('/payment/mdv_cybersource/pay', {
                'tx_id': tx.id, 'access_token': token, 'transient_token': TRANSIENT_TOKEN})
        self.assertEqual(len(api.calls), 1)

    def test_capture_context_route(self):
        api = FakeApi([('POST', '/microform/v2/sessions', CAPTURE_CONTEXT, 201)])
        with patch(REQUEST_PATH, side_effect=api):
            result = self._jsonrpc('/payment/mdv_cybersource/capture_context',
                                   {'provider_id': self.cybersource.id})
        self.assertEqual(result['result']['capture_context'], CAPTURE_CONTEXT)
        self.assertEqual(api.calls[0]['json']['targetOrigins'], [self.base_url()])
        self.cybersource.state = 'disabled'
        result = self._jsonrpc('/payment/mdv_cybersource/capture_context',
                               {'provider_id': self.cybersource.id})
        self.assertIn('error', result)

    def test_3ds_return(self):
        self.cybersource.mdv_cs_payer_auth = True
        tx = self._create_cs_transaction()
        tx.sudo().write({'mdv_cs_transient_token': TRANSIENT_TOKEN, 'mdv_cs_3ds_ref': 'ref',
                         'mdv_cs_3ds_auth_ref': 'z1HwdDZK23Fw29EhpXh0'})
        response = self.url_open('/payment/mdv_cybersource/3ds/return?md=1.forged',
                                 data={'TransactionId': 'abc'})
        self.assertIn('could not be found', response.text)
        api = FakeApi([('POST', '/pts/v2/payments', authorized(), 201)])
        with patch(REQUEST_PATH, side_effect=api):
            response = self.url_open(
                '/payment/mdv_cybersource/3ds/return?md=' + tx._mdv_cs_sign_md(),
                data={'TransactionId': 'z1HwdDZK23Fw29EhpXh0'})
        self.assertEqual(response.status_code, 200)
        self.assertIn('/payment/status', response.text)
        self.assertEqual(api.calls[0]['json']['consumerAuthenticationInformation'],
                         {'authenticationTransactionId': 'z1HwdDZK23Fw29EhpXh0'})
        self.assertEqual(tx.state, 'done')
