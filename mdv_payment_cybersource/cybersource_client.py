"""Minimal client of the CyberSource REST API with HTTP Signature authentication.

Authentication (developer.cybersource.com, "HTTP Signature Authentication"): every request carries
the headers `v-c-merchant-id`, `date` (RFC 1123), `host`, a `digest` (`SHA-256=` + base64 SHA-256
of the body) for requests with a body, and `signature`:

    keyid="<key id>", algorithm="HmacSHA256", headers="host date request-target digest
    v-c-merchant-id", signature="<base64 HMAC-SHA256 of the signature string>"

The HMAC key is the base64-decoded shared secret key. The signature string is one
`name: value` line per signed header, joined with a newline; `request-target` is the lower-case
method, a space and the path with its query string.

No dependency on the CyberSource SDK: `requests` only.
"""

import base64
import hashlib
import hmac
import json
import logging
import time
from email.utils import formatdate

import requests

from odoo.addons.mdv_payment_cybersource import const

_logger = logging.getLogger(__name__)

TIMEOUT = 30


class CyberSourceError(Exception):
    """A request that failed: transport error, HTTP error or a body CyberSource rejected."""

    def __init__(self, message, status=None, body=None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.body = body or {}

    @property
    def reason(self):
        return (self.body or {}).get('reason') or ''


def dumps(payload):
    """The exact bytes sent and signed (compact JSON)."""
    return json.dumps(payload, separators=(',', ':'), ensure_ascii=False).encode('utf-8')


def digest(body_bytes):
    return 'SHA-256=' + base64.b64encode(hashlib.sha256(body_bytes).digest()).decode()


def sign(secret_key, signature_string):
    """Base64 HMAC-SHA256 of the signature string with the base64-decoded shared secret."""
    try:
        key = base64.b64decode(secret_key, validate=True)
    except (ValueError, TypeError):
        raise CyberSourceError("The shared secret key is not a valid base64 value.")
    mac = hmac.new(key, signature_string.encode('utf-8'), hashlib.sha256).digest()
    return base64.b64encode(mac).decode()


def b64url_json(segment):
    segment = segment + '=' * (-len(segment) % 4)
    return json.loads(base64.urlsafe_b64decode(segment.encode()).decode('utf-8'))


def jwt_payload(token):
    """The claims of a JWT, *not verified*: only used to read what CyberSource sent us (the
    capture context we requested ourselves, or a transient token we pass back to CyberSource,
    which verifies it)."""
    if not isinstance(token, str) or token.count('.') != 2:
        raise ValueError("not a JWT")
    return b64url_json(token.split('.')[1])


def mask(value, keep=4):
    value = str(value or '')
    if len(value) <= keep:
        return '*' * len(value)
    return '*' * (len(value) - keep) + value[-keep:]


def scrub(data):
    """A copy of a request or response body safe to store in the API log."""
    if isinstance(data, dict):
        result = {}
        for key, value in data.items():
            if key in const.SECRET_KEYS and isinstance(value, (str, int)):
                result[key] = mask(value) if key == 'number' else '***'
            else:
                result[key] = scrub(value)
        return result
    if isinstance(data, list):
        return [scrub(item) for item in data]
    if isinstance(data, str) and data.count('.') == 2 and len(data) > 200:
        return '***'  # A JWT returned as the whole body (capture contexts).
    return data


class CyberSourceClient:

    def __init__(self, merchant_id, key_id, secret_key, environment='test', timeout=TIMEOUT):
        if not (merchant_id and key_id and secret_key):
            raise CyberSourceError("The merchant ID, key ID and shared secret key are required.")
        self.merchant_id = merchant_id.strip()
        self.key_id = key_id.strip()
        self.secret_key = secret_key.strip()
        self.host = const.HOSTS['prod' if environment == 'prod' else 'test']
        self.timeout = timeout

    # === SIGNATURE === #

    def signed_headers(self, method, path, body_bytes=b'', date=None):
        method = method.upper()
        date = date or formatdate(usegmt=True)
        values = {
            'host': self.host,
            'date': date,
            'request-target': f'{method.lower()} {path}',
            'v-c-merchant-id': self.merchant_id,
        }
        names = ['host', 'date', 'request-target']
        headers = {
            'host': self.host,
            'date': date,
            'v-c-merchant-id': self.merchant_id,
            'Accept': 'application/hal+json;charset=utf-8',
        }
        if method in ('POST', 'PUT', 'PATCH'):
            values['digest'] = headers['digest'] = digest(body_bytes)
            headers['Content-Type'] = 'application/json;charset=utf-8'
            names.append('digest')
        names.append('v-c-merchant-id')
        signature_string = '\n'.join(f'{name}: {values[name]}' for name in names)
        headers['signature'] = (
            f'keyid="{self.key_id}", algorithm="HmacSHA256", headers="{" ".join(names)}", '
            f'signature="{sign(self.secret_key, signature_string)}"'
        )
        return headers

    # === REQUEST === #

    def request(self, method, path, payload=None):
        """Send a signed request and return `(http_status, body)`.

        2xx bodies are returned as they are (a payment DECLINED by the bank is a 201). Other
        statuses raise `CyberSourceError` with the message of CyberSource.
        """
        method = method.upper()
        body_bytes = dumps(payload) if payload is not None and method != 'GET' else b''
        if method in ('POST', 'PUT', 'PATCH') and not body_bytes:
            body_bytes = b'{}'
        headers = self.signed_headers(method, path, body_bytes)
        url = f'https://{self.host}{path}'
        started = time.monotonic()
        try:
            response = requests.request(
                method, url, data=body_bytes or None, headers=headers, timeout=self.timeout,
            )
        except requests.exceptions.Timeout:
            raise CyberSourceError("CyberSource did not answer in time.")
        except requests.exceptions.RequestException as error:
            _logger.warning("CyberSource connection error on %s %s: %s", method, path, error)
            raise CyberSourceError("Could not connect to CyberSource.")
        _logger.info("CyberSource %s %s -> HTTP %s (%.2fs)", method, path, response.status_code,
                     time.monotonic() - started)
        text = response.text or ''
        try:
            body = response.json() if text.strip() else {}
        except ValueError:
            body = text
        if response.status_code >= 400:
            raise CyberSourceError(error_message(body, response.status_code),
                                   status=response.status_code,
                                   body=body if isinstance(body, dict) else {'message': text[:500]})
        return response.status_code, body


def error_message(body, status=None):
    """A readable message from an error body of CyberSource."""
    if isinstance(body, dict):
        if body.get('response') and isinstance(body['response'], dict):  # 401 of the gateway
            body = body['response']
        parts = [body.get('message') or body.get('rmsg') or '']
        info = body.get('errorInformation') or {}
        if not parts[0] and info:
            parts = [info.get('message') or info.get('reason') or '']
        details = body.get('details') or info.get('details') or []
        fields = [d.get('field') for d in details if isinstance(d, dict) and d.get('field')]
        if fields:
            parts.append('(%s)' % ', '.join(fields))
        message = ' '.join(p for p in parts if p)
        if message:
            return message
    if status == 401:
        return "CyberSource refused the credentials (HTTP 401). Check the merchant ID, key ID " \
               "and shared secret key, and the environment (test or production)."
    if isinstance(body, str) and body.strip():
        return body.strip()[:300]
    return f"CyberSource answered with HTTP {status}."


# === WEBHOOK SIGNATURE === #

def parse_signature_header(header):
    """`v-c-signature: t=<timestamp>;keyId=<key id>;sig=<base64 HMAC-SHA256>`."""
    parts = {}
    for item in (header or '').split(';'):
        if '=' in item:
            name, value = item.split('=', 1)
            parts[name.strip()] = value.strip()
    return parts


def webhook_signatures(secret_key, timestamp, raw_body, payload=None):
    """The signatures CyberSource may have sent: HMAC-SHA256 of `<t>.<raw body>` (the official
    Salesforce B2C cartridge) and of `<t>.<JSON of the payload array>` (the official Oracle CX
    Commerce extension)."""
    candidates = []
    try:
        key = base64.b64decode(secret_key, validate=True)
    except (ValueError, TypeError):
        return candidates
    raw = raw_body if isinstance(raw_body, bytes) else (raw_body or '').encode('utf-8')
    messages = [timestamp.encode() + b'.' + raw]
    if payload is not None:
        messages.append(timestamp.encode() + b'.' + json.dumps(
            payload, separators=(',', ':'), ensure_ascii=False).encode('utf-8'))
    for message in messages:
        candidates.append(base64.b64encode(hmac.new(key, message, hashlib.sha256).digest()).decode())
    return candidates


def verify_webhook(header, raw_body, keys, now=None, tolerance=const.WEBHOOK_TOLERANCE):
    """Check the `v-c-signature` header of a notification.

    :param dict keys: {key id: base64 shared secret} of the webhook security keys.
    :return: (ok, reason)
    """
    parts = parse_signature_header(header)
    timestamp, key_id, signature = parts.get('t'), parts.get('keyId'), parts.get('sig')
    if not (timestamp and key_id and signature):
        return False, 'missing signature'
    secret = keys.get(key_id)
    if not secret:
        return False, 'unknown key id'
    try:
        stamp = int(timestamp)
    except ValueError:
        return False, 'bad timestamp'
    if stamp > 10 ** 11:  # milliseconds
        stamp //= 1000
    now = time.time() if now is None else now
    if abs(now - stamp) > tolerance:
        return False, 'expired'
    try:
        payload = json.loads(raw_body or b'{}').get('payload')
    except (ValueError, AttributeError):
        payload = None
    for candidate in webhook_signatures(secret, timestamp, raw_body, payload):
        if hmac.compare_digest(candidate, signature):
            return True, ''
    return False, 'bad signature'
