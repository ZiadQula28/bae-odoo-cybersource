import json
import re
from contextlib import contextmanager
from unittest.mock import MagicMock, patch
from urllib.parse import urlsplit

from odoo.addons.payment.tests.common import PaymentCommon
from odoo.addons.mdv_payment_cybersource.cybersource_client import b64url_json

REQUEST_PATH = 'odoo.addons.mdv_payment_cybersource.cybersource_client.requests.request'

# Placeholder credentials of the unit tests. The live bench used the sandbox credentials that
# CyberSource publishes in its own sample code (cybersource-rest-samples-python,
# data/Configuration.py); they are never needed by these tests.
MERCHANT_ID = 'odoo_unit_test'
KEY_ID = '00000000-0000-4000-8000-000000000001'
SECRET_KEY = 'b2Rvby11bml0LXRlc3Qtc2hhcmVkLXNlY3JldC1rZXk='  # base64 of a dummy string
WEBHOOK_KEY_ID = '11111111-0000-4000-8000-000000000002'
WEBHOOK_SECRET = 'b2Rvby11bml0LXRlc3Qtd2ViaG9vay1rZXk='

PAYMENT_ID = '7922010459376548504807'
CAPTURE_ID = '7922010714636012204805'
REFUND_ID = '7922010736656597404807'
REVERSAL_ID = '7922010760156602304807'
CUSTOMER_ID = '5E004EDCEC5BC54FE063A2598D0A6D65'
INSTRUMENT_ID = '5E004E449727C661E063A2598D0A2F95'


def make_jwt(claims):
    """ An unsigned JWT with the given claims (the module never verifies them itself). """
    import base64

    def enc(data):
        return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip('=')
    return '%s.%s.%s' % (enc({'kid': 'test', 'alg': 'RS256'}), enc(claims), 'c2ln')


TRANSIENT_TOKEN = make_jwt({
    'iss': 'Flex/08', 'jti': '1E3GQY1RNKBG6IBD2EP93C43PIZ2NQ6SQLUIM3S16BGLHTY4IIEK5EB1AE5D73A4',
    'content': {'paymentInformation': {'card': {
        'number': {'detectedCardTypes': ['001'], 'maskedValue': 'XXXXXXXXXXXX1111',
                   'bin': '411111'},
        'expirationMonth': {'value': '12'}, 'expirationYear': {'value': '2031'},
    }}},
})

CAPTURE_CONTEXT = make_jwt({
    'flx': {'path': '/flex/v2/tokens', 'origin': 'https://testflex.cybersource.com'},
    'ctx': [{'data': {
        'clientLibrary': 'https://testflex.cybersource.com/microform/bundle/v2.0.2/'
                         'flex-microform.min.js',
        'clientLibraryIntegrity': 'sha256-4TUKBd3VMIGGNs1ZLzfU6bG0YG4kUScSOtPu5ec7Ygo=',
        'targetOrigins': ['http://localhost:8069'], 'clientVersion': 'v2.0',
    }, 'type': 'mf-2.0.0'}],
    'iss': 'Flex API', 'jti': 'abc',
})


def authorized(amount='1111.11', currency='USD', status='AUTHORIZED', payment_id=PAYMENT_ID,
               **extra):
    """ The answer of POST /pts/v2/payments (shape of the live sandbox answer). """
    body = {
        '_links': {'self': {'method': 'GET', 'href': f'/pts/v2/payments/{payment_id}'}},
        'clientReferenceInformation': {'code': 'Test Transaction'},
        'id': payment_id,
        'orderInformation': {'amountDetails': {'authorizedAmount': amount, 'currency': currency}},
        'paymentAccountInformation': {'card': {'type': '001'}},
        'paymentInformation': {'tokenizedCard': {'type': '001'}, 'card': {'type': '001'}},
        'processorInformation': {
            'approvalCode': '322516', 'networkTransactionId': '016153570198200',
            'responseCode': '00', 'avs': {'code': 'Y', 'codeRaw': 'Y'},
            'cardVerification': {'resultCode': 'M', 'resultCodeRaw': 'M'},
        },
        'reconciliationId': payment_id,
        'status': status,
        'submitTimeUtc': '2026-10-17T01:37:26Z',
    }
    body.update(extra)
    return body


def declined(reason='INSUFFICIENT_FUND', message='Decline - Insufficient funds in the account.'):
    return {
        'id': '7922011130186010604806', 'status': 'DECLINED',
        'errorInformation': {'reason': reason, 'message': message},
        'submitTimeUtc': '2026-10-17T01:38:33Z',
    }


def invalid_request(fields=('orderInformation.billTo.locality',)):
    return {
        'id': '7922011130186010604806', 'submitTimeUtc': '2026-10-17T01:38:33Z',
        'status': 'INVALID_REQUEST', 'reason': 'MISSING_FIELD',
        'message': 'Declined - The request is missing one or more fields',
        'details': [{'field': f, 'reason': 'MISSING_FIELD'} for f in fields],
    }


def fake_response(payload, status=201):
    response = MagicMock()
    response.status_code = status
    response.ok = status < 400
    if payload is None:
        response.text = ''
        response.json.side_effect = ValueError('no json')
    elif isinstance(payload, str):
        response.text = payload
        response.json.side_effect = ValueError('no json')
    else:
        response.text = json.dumps(payload)
        response.json.return_value = payload
    return response


class FakeApi:
    """ Answers the requests of the client by method and path, records what was sent. """

    def __init__(self, routes):
        # routes: list of (method, path regex, body, status)
        self.routes = [(m, re.compile(p), b, s) for m, p, b, s in routes]
        self.calls = []

    def __call__(self, method, url, data=None, headers=None, timeout=None, **kwargs):
        path = urlsplit(url).path
        body = json.loads(data) if data else None
        self.calls.append({'method': method, 'url': url, 'path': path, 'json': body,
                           'headers': headers or {}})
        for index, (m, pattern, answer, status) in enumerate(self.routes):
            if m == method and pattern.fullmatch(path):
                if isinstance(answer, list):  # several answers, in order
                    answer = answer.pop(0) if len(answer) > 1 else answer[0]
                return fake_response(answer, status)
        return fake_response({'status': 'NOT_FOUND', 'message': f'no route {method} {path}'},
                             404)

    def paths(self):
        return [(c['method'], c['path']) for c in self.calls]

    def last(self, path_part=''):
        for call in reversed(self.calls):
            if path_part in call['path']:
                return call
        return None


class CyberSourceCommon(PaymentCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.currency_usd = cls._enable_currency('USD')
        cls.cybersource = cls._prepare_provider('mdv_cybersource', update_values={
            'mdv_cs_merchant_id': MERCHANT_ID,
            'mdv_cs_key_id': KEY_ID,
            'mdv_cs_secret_key': SECRET_KEY,
            'mdv_cs_webhook_key_id': WEBHOOK_KEY_ID,
            'mdv_cs_webhook_secret': WEBHOOK_SECRET,
            'mdv_cs_webhook_ref': False,
            'mdv_cs_payer_auth': False,
            'mdv_cs_descriptor': False,
            'mdv_cs_send_order_lines': True,
            'capture_manually': False,
        })
        cls.provider = cls.cybersource
        cls.currency = cls.currency_usd
        # Bench transactions must not reach the sync cron of the tests.
        cls.env['payment.transaction'].search([
            ('provider_code', '=', 'mdv_cybersource'), ('state', 'in', ('draft', 'pending')),
        ]).write({'state': 'cancel'})
        cls.partner.write({
            'name': 'Alex Morgan', 'street': '1 Market St', 'city': 'San Francisco',
            'zip': '94105', 'country_id': cls.env.ref('base.us').id,
            'state_id': cls.env.ref('base.state_us_5').id, 'email': 'alex.morgan@example.com',
            'phone': '+1 415 555 0100',
        })

    @contextmanager
    def mock_api(self, *routes):
        api = FakeApi(list(routes))
        with patch(REQUEST_PATH, side_effect=api):
            yield api

    def _create_cs_transaction(self, flow='direct', **values):
        return self._create_transaction(flow, **values)

    @staticmethod
    def jwt_claims(token):
        return b64url_json(token.split('.')[1])
