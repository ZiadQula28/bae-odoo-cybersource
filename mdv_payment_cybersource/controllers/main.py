import json
import logging
import re
from html import escape

from werkzeug.exceptions import Forbidden

from odoo import http
from odoo.exceptions import UserError, ValidationError
from odoo.http import request

from odoo.addons.payment import utils as payment_utils
from odoo.addons.mdv_payment_cybersource import const
from odoo.addons.mdv_payment_cybersource.cybersource_client import verify_webhook

_logger = logging.getLogger(__name__)

# A transient token is a JWT (header.payload.signature, base64url).
JWT_RE = re.compile(r'^[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]*$')
AUTH_TX_RE = re.compile(r'^[A-Za-z0-9]{1,64}$')

RETURN_PAGE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>3-D Secure</title></head>
<body style="font-family:sans-serif;text-align:center;padding-top:40px">
<p>%(message)s</p>
<p><a href="/payment/status" target="_top">%(link)s</a></p>
<script>
try { window.parent.postMessage({mdvCybersource: 'done'}, window.location.origin); } catch (e) {}
try { window.top.location.href = '/payment/status'; } catch (e) {}
</script>
</body></html>"""


class CyberSourceController(http.Controller):
    _context_url = '/payment/mdv_cybersource/capture_context'
    _pay_url = '/payment/mdv_cybersource/pay'
    _authenticate_url = '/payment/mdv_cybersource/authenticate'
    _return_url = '/payment/mdv_cybersource/3ds/return'
    _webhook_url = '/payment/mdv_cybersource/webhook'

    @staticmethod
    def _get_tx(tx_id, access_token):
        try:
            tx_id = int(tx_id)
        except (TypeError, ValueError):
            raise Forbidden()
        tx_sudo = request.env['payment.transaction'].sudo().browse(tx_id).exists()
        if (
            not tx_sudo or tx_sudo.provider_code != const.PROVIDER_CODE
            or not payment_utils.check_access_token(access_token, tx_sudo.reference, tx_sudo.id)
        ):
            raise Forbidden()
        lang = request.cookies.get('frontend_lang')
        active = dict(request.env['res.lang'].get_installed())
        if lang not in active:
            lang = tx_sudo.partner_lang if tx_sudo.partner_lang in active else request.env.lang
        return tx_sudo.with_context(lang=lang)

    @staticmethod
    def _clean_browser(browser):
        return browser if isinstance(browser, dict) else {}

    @http.route(_context_url, type='jsonrpc', auth='public')
    def mdv_cybersource_capture_context(self, provider_id, **kwargs):
        """ A Microform capture context for this page (valid 15 minutes). """
        try:
            provider_id = int(provider_id)
        except (TypeError, ValueError):
            raise Forbidden()
        provider_sudo = request.env['payment.provider'].sudo().browse(provider_id).exists()
        if not provider_sudo or provider_sudo.code != const.PROVIDER_CODE \
                or provider_sudo.state == 'disabled':
            raise Forbidden()
        try:
            return provider_sudo._mdv_cs_capture_context(request.httprequest.host_url)
        except ValidationError as error:
            _logger.warning("CyberSource capture context failed: %s", error)
            raise ValidationError(request.env._(
                "The card form could not be loaded. Please try again later."))

    @http.route(_pay_url, type='jsonrpc', auth='public')
    def mdv_cybersource_pay(self, tx_id, access_token, transient_token=None, browser=None,
                            **kwargs):
        """ Pay with the transient token created by Microform. """
        tx_sudo = self._get_tx(tx_id, access_token)
        if not isinstance(transient_token, str) or len(transient_token) > 20000 \
                or not JWT_RE.match(transient_token):
            raise Forbidden()
        if tx_sudo.state != 'draft' or tx_sudo.provider_reference:
            return {'redirect_url': '/payment/status'}  # Double click, browser back.
        return tx_sudo._mdv_cs_pay(transient_token, browser=self._clean_browser(browser))

    @http.route(_authenticate_url, type='jsonrpc', auth='public')
    def mdv_cybersource_authenticate(self, tx_id, access_token, browser=None, **kwargs):
        """ After the device data collection: check enrollment and authorize. """
        tx_sudo = self._get_tx(tx_id, access_token)
        if tx_sudo.state != 'draft' or tx_sudo.provider_reference:
            return {'redirect_url': '/payment/status'}
        md = tx_sudo._mdv_cs_sign_md()
        return_url = request.httprequest.host_url.rstrip('/') + self._return_url + '?md=' + md
        return tx_sudo._mdv_cs_authenticate(browser=self._clean_browser(browser),
                                            return_url=return_url)

    @http.route(_return_url, type='http', auth='public', methods=['GET', 'POST'], csrf=False,
                save_session=False)
    def mdv_cybersource_3ds_return(self, md=None, **data):
        """ The bank sends the customer back here (inside the challenge window) after the
        3-D Secure challenge. `md` is signed by Odoo; the result is fetched from CyberSource. """
        tx_sudo = request.env['payment.transaction'].sudo()._mdv_cs_tx_from_md(
            md or data.get('MD'))
        env = request.env
        if not tx_sudo:
            message = env._("The payment could not be found.")
        else:
            auth_ref = data.get('TransactionId') or data.get('transactionId') or ''
            if not AUTH_TX_RE.match(auth_ref or ''):
                auth_ref = tx_sudo.mdv_cs_3ds_auth_ref or ''
            if tx_sudo.state == 'draft' and auth_ref:
                try:
                    tx_sudo._mdv_cs_validate_challenge(auth_ref)
                except (ValidationError, UserError) as error:
                    tx_sudo._set_error(str(error))
            message = env._("Verification finished. You are redirected to the payment status.")
        html = RETURN_PAGE % {'message': escape(message),
                              'link': escape(env._("Continue"))}
        return request.make_response(html, headers=[('Content-Type', 'text/html; charset=utf-8')])

    @http.route(_webhook_url, type='http', auth='public', methods=['GET', 'POST'], csrf=False,
                save_session=False)
    def mdv_cybersource_webhook(self, **kwargs):
        """ Receive a CyberSource webhook notification.

        The `v-c-signature` header (`t=<timestamp>;keyId=<id>;sig=<HMAC-SHA256>`) is checked with
        the webhook security key; unsigned or badly signed calls get 401. The body is never
        trusted: the transaction it names is read back with GET /tss/v2/transactions/{id}.
        GET is the health check of the subscription.
        """
        if request.httprequest.method == 'GET':
            return request.make_json_response({'status': 'ok'})
        raw = request.httprequest.get_data() or b''
        providers = request.env['payment.provider'].sudo().search(
            [('code', '=', const.PROVIDER_CODE), ('state', '!=', 'disabled')])
        keys = providers._mdv_cs_webhook_keys()
        header = request.httprequest.headers.get('v-c-signature', '')
        ok, reason = verify_webhook(header, raw, keys)
        if not ok:
            _logger.warning("CyberSource webhook refused: %s", reason)
            return request.make_json_response({'status': 'refused'}, status=401)
        try:
            notification = json.loads(raw)
        except ValueError:
            return request.make_json_response({'status': 'bad request'}, status=400)
        key_id = dict(p.split('=', 1) for p in header.split(';') if '=' in p).get('keyId', '')
        provider = providers.filtered(
            lambda p: (p.sudo().mdv_cs_webhook_key_id or '').strip() == key_id.strip())
        for prov in provider[:1]:
            request.env['mdv.cybersource.log'].sudo()._mdv_log(
                provider=prov, method='POST', path=self._webhook_url, operation='webhook',
                response=notification if isinstance(notification, dict) else None,
                status=200, direction='in')
        notification = notification if isinstance(notification, dict) else {}
        retry = False
        try:
            _updated, retry = request.env['payment.transaction'].sudo()\
                ._mdv_cs_handle_notification(notification, provider=provider)
        except (ValidationError, UserError):
            _logger.exception("CyberSource webhook could not be processed.")
        try:
            retry_number = int(notification.get('retryNumber') or 0)
        except (TypeError, ValueError):
            retry_number = 0
        if retry and retry_number < 2:
            # Ask CyberSource to send it again in a moment (its retry policy), without risking
            # the deactivation of the subscription after repeated failures.
            return request.make_json_response({'status': 'retry later'}, status=503)
        return request.make_json_response({'status': 'received'})
