import base64
import hashlib
import hmac
import json
import time

from odoo.tests import BaseCase, tagged

from odoo.addons.mdv_payment_cybersource.cybersource_client import (
    CyberSourceClient, CyberSourceError, dumps, error_message, jwt_payload, scrub,
    verify_webhook, webhook_signatures,
)
from odoo.addons.mdv_payment_cybersource.tests.common import (
    KEY_ID, MERCHANT_ID, SECRET_KEY, TRANSIENT_TOKEN, WEBHOOK_KEY_ID, WEBHOOK_SECRET,
)

DATE = 'Sat, 17 Oct 2026 01:37:26 GMT'


def reference_signature(secret, lines):
    """ Independent implementation of the HTTP Signature of the CyberSource docs. """
    key = base64.b64decode(secret)
    return base64.b64encode(hmac.new(key, '\n'.join(lines).encode(), hashlib.sha256)
                            .digest()).decode()


@tagged('post_install', '-at_install')
class TestCyberSourceClient(BaseCase):

    def setUp(self):
        super().setUp()
        self.client = CyberSourceClient(MERCHANT_ID, KEY_ID, SECRET_KEY, environment='test')

    def test_post_headers_follow_http_signature(self):
        body = dumps({'clientReferenceInformation': {'code': 'S00042'}})
        headers = self.client.signed_headers('POST', '/pts/v2/payments', body, date=DATE)
        digest = 'SHA-256=' + base64.b64encode(hashlib.sha256(body).digest()).decode()
        self.assertEqual(headers['digest'], digest)
        self.assertEqual(headers['v-c-merchant-id'], MERCHANT_ID)
        self.assertEqual(headers['host'], 'apitest.cybersource.com')
        expected = reference_signature(SECRET_KEY, [
            'host: apitest.cybersource.com', f'date: {DATE}',
            'request-target: post /pts/v2/payments', f'digest: {digest}',
            f'v-c-merchant-id: {MERCHANT_ID}',
        ])
        self.assertEqual(
            headers['signature'],
            f'keyid="{KEY_ID}", algorithm="HmacSHA256", '
            f'headers="host date request-target digest v-c-merchant-id", '
            f'signature="{expected}"')

    def test_get_headers_have_no_digest(self):
        headers = self.client.signed_headers('GET', '/tss/v2/transactions/123', b'', date=DATE)
        self.assertNotIn('digest', headers)
        self.assertIn('headers="host date request-target v-c-merchant-id"', headers['signature'])
        expected = reference_signature(SECRET_KEY, [
            'host: apitest.cybersource.com', f'date: {DATE}',
            'request-target: get /tss/v2/transactions/123', f'v-c-merchant-id: {MERCHANT_ID}',
        ])
        self.assertIn(f'signature="{expected}"', headers['signature'])

    def test_production_host(self):
        client = CyberSourceClient(MERCHANT_ID, KEY_ID, SECRET_KEY, environment='prod')
        self.assertEqual(client.host, 'api.cybersource.com')

    def test_bad_secret_and_missing_credentials(self):
        client = CyberSourceClient(MERCHANT_ID, KEY_ID, 'not base64 !!', environment='test')
        with self.assertRaises(CyberSourceError):
            client.signed_headers('GET', '/x', b'')
        with self.assertRaises(CyberSourceError):
            CyberSourceClient('', KEY_ID, SECRET_KEY)

    def test_error_messages(self):
        body = {'status': 'INVALID_REQUEST', 'reason': 'MISSING_FIELD',
                'message': 'Declined - The request is missing one or more fields',
                'details': [{'field': 'orderInformation.billTo.locality'}]}
        self.assertEqual(
            error_message(body, 400),
            'Declined - The request is missing one or more fields '
            '(orderInformation.billTo.locality)')
        self.assertIn('credentials', error_message({}, 401))
        self.assertEqual(error_message({'response': {'rmsg': 'Authentication Failed'}}, 401),
                         'Authentication Failed')

    def test_scrub_hides_secrets(self):
        data = scrub({
            'tokenInformation': {'transientTokenJwt': TRANSIENT_TOKEN},
            'paymentInformation': {'card': {'number': '4111111111111111', 'securityCode': '123'}},
            'consumerAuthenticationInformation': {'accessToken': 'eyJ...', 'cavv': 'AJkB'},
            'keyInformation': {'key': 'c2VjcmV0'},
            'orderInformation': {'amountDetails': {'totalAmount': '10.00'}},
        })
        self.assertEqual(data['tokenInformation']['transientTokenJwt'], '***')
        self.assertEqual(data['paymentInformation']['card']['number'], '************1111')
        self.assertEqual(data['paymentInformation']['card']['securityCode'], '***')
        self.assertEqual(data['consumerAuthenticationInformation']['accessToken'], '***')
        self.assertEqual(data['keyInformation']['key'], '***')
        self.assertEqual(data['orderInformation']['amountDetails']['totalAmount'], '10.00')
        self.assertEqual(scrub('a' * 150 + '.' + 'b' * 150 + '.' + 'c'), '***')

    def test_jwt_payload(self):
        claims = jwt_payload(TRANSIENT_TOKEN)
        self.assertEqual(claims['content']['paymentInformation']['card']['number']['maskedValue'],
                         'XXXXXXXXXXXX1111')
        with self.assertRaises(ValueError):
            jwt_payload('not-a-jwt')


@tagged('post_install', '-at_install')
class TestWebhookSignature(BaseCase):

    def _header(self, raw, timestamp=None, key=WEBHOOK_SECRET, key_id=WEBHOOK_KEY_ID,
                mode='raw'):
        timestamp = str(timestamp or int(time.time() * 1000))
        payload = json.loads(raw).get('payload') if mode == 'payload' else None
        signatures = webhook_signatures(key, timestamp, raw, payload)
        return f't={timestamp};keyId={key_id};sig={signatures[-1]}'

    def setUp(self):
        super().setUp()
        self.raw = json.dumps({
            'eventType': 'payments.payments.accept', 'webhookId': 'w1',
            'payload': [{'data': {'id': '7922010459376548504807', 'type': 'payments'}}],
        }).encode()
        self.keys = {WEBHOOK_KEY_ID: WEBHOOK_SECRET}

    def test_valid_signature_of_raw_body(self):
        self.assertEqual(verify_webhook(self._header(self.raw), self.raw, self.keys), (True, ''))

    def test_valid_signature_of_payload_array(self):
        header = self._header(self.raw, mode='payload')
        self.assertEqual(verify_webhook(header, self.raw, self.keys), (True, ''))

    def test_seconds_timestamp_accepted(self):
        header = self._header(self.raw, timestamp=int(time.time()))
        self.assertTrue(verify_webhook(header, self.raw, self.keys)[0])

    def test_refused_signatures(self):
        self.assertEqual(verify_webhook('', self.raw, self.keys), (False, 'missing signature'))
        header = self._header(self.raw, key_id='other')
        self.assertEqual(verify_webhook(header, self.raw, self.keys), (False, 'unknown key id'))
        header = self._header(self.raw, key=base64.b64encode(b'wrong key').decode())
        self.assertEqual(verify_webhook(header, self.raw, self.keys), (False, 'bad signature'))
        header = self._header(self.raw, timestamp=int((time.time() - 3600) * 1000))
        self.assertEqual(verify_webhook(header, self.raw, self.keys), (False, 'expired'))
        tampered = self.raw.replace(b'accept', b'reject')
        self.assertEqual(verify_webhook(self._header(self.raw), tampered, self.keys),
                         (False, 'bad signature'))
