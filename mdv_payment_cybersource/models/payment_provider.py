import json
import logging
from urllib.parse import urlsplit

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.tools import urls

from odoo.addons.mdv_payment_cybersource import const
from odoo.addons.mdv_payment_cybersource.cybersource_client import (
    CyberSourceClient, CyberSourceError, jwt_payload, scrub,
)

_logger = logging.getLogger(__name__)


class PaymentProvider(models.Model):
    _inherit = 'payment.provider'

    code = fields.Selection(
        selection_add=[(const.PROVIDER_CODE, "CyberSource")],
        ondelete={const.PROVIDER_CODE: 'set default'},
    )

    # === CREDENTIALS === #
    mdv_cs_merchant_id = fields.Char(
        string="Merchant ID",
        help="Your CyberSource merchant ID (organization ID), as shown in the Business Center.",
        required_if_provider=const.PROVIDER_CODE,
        copy=False,
    )
    mdv_cs_key_id = fields.Char(
        string="Key ID",
        help="Business Center > Payment Configuration > Key Management > REST - Shared Secret: "
             "the key (the 'Key' column, a UUID).",
        required_if_provider=const.PROVIDER_CODE,
        copy=False,
    )
    mdv_cs_secret_key = fields.Char(
        string="Shared Secret Key",
        help="The base64 shared secret of the REST - Shared Secret key. It is shown once, when "
             "the key is generated.",
        required_if_provider=const.PROVIDER_CODE,
        copy=False,
        groups='base.group_system',
    )

    # === OPTIONS === #
    mdv_cs_payer_auth = fields.Boolean(
        string="3-D Secure (Payer Authentication)",
        default=True,
        help="Run 3-D Secure 2 with the bank of the card (Visa Secure, Mastercard Identity "
             "Check, Amex SafeKey...). Frictionless when the bank allows it, a challenge window "
             "otherwise. Payer Authentication must be enabled on your CyberSource account.",
    )
    mdv_cs_descriptor = fields.Char(
        string="Statement Descriptor",
        help="Optional name shown on the statement of the customer "
             "(merchantInformation.merchantDescriptor.name). Your processor must support it.",
        size=22,
    )
    mdv_cs_send_order_lines = fields.Boolean(
        string="Send Order Lines",
        default=True,
        help="Send the lines of the sales order or invoice (orderInformation.lineItems): they "
             "appear in the Business Center and improve the fraud screening.",
    )

    # === WEBHOOKS === #
    mdv_cs_webhook_key_id = fields.Char(
        string="Webhook Key ID",
        help="The key ID of the webhook security key (the keyId of the v-c-signature header).",
        copy=False,
    )
    mdv_cs_webhook_secret = fields.Char(
        string="Webhook Secret",
        help="The base64 webhook security key used to check the signature of notifications.",
        copy=False,
        groups='base.group_system',
    )
    mdv_cs_webhook_ref = fields.Char(string="Webhook Subscription ID", readonly=True, copy=False)
    mdv_cs_webhook_url = fields.Char(
        string="Notification URL", compute='_compute_mdv_cs_webhook_url',
        help="Where CyberSource sends its webhook notifications.",
    )
    mdv_cs_log_count = fields.Integer(compute='_compute_mdv_cs_log_count')

    # === COMPUTE METHODS === #

    def _compute_mdv_cs_webhook_url(self):
        for provider in self:
            base = provider.get_base_url() if provider.id else ''
            provider.mdv_cs_webhook_url = urls.urljoin(base, '/payment/mdv_cybersource/webhook') \
                if base else False

    def _compute_mdv_cs_log_count(self):
        groups = self.env['mdv.cybersource.log']._read_group(
            [('provider_id', 'in', self.ids)], ['provider_id'], ['__count'])
        counts = {provider.id: count for provider, count in groups}
        for provider in self:
            provider.mdv_cs_log_count = counts.get(provider.id, 0)

    def _compute_feature_support_fields(self):
        """ Override of `payment` to enable additional features. """
        super()._compute_feature_support_fields()
        self.filtered(lambda p: p.code == const.PROVIDER_CODE).update({
            'support_manual_capture': 'full_only',
            'support_refund': 'partial',
            'support_tokenization': True,
        })

    # === CONSTRAINT METHODS === #

    @api.constrains('mdv_cs_descriptor')
    def _check_mdv_cs_descriptor(self):
        for provider in self:
            descriptor = provider.mdv_cs_descriptor or ''
            if descriptor and not descriptor.isascii():
                raise ValidationError(_("The statement descriptor must use plain ASCII letters."))

    # === BUSINESS METHODS === #

    def _get_default_payment_method_codes(self):
        """ Override of `payment` to return the default payment method codes. """
        self.ensure_one()
        if self.code != const.PROVIDER_CODE:
            return super()._get_default_payment_method_codes()
        return const.DEFAULT_PAYMENT_METHOD_CODES

    def _mdv_cs_environment(self):
        self.ensure_one()
        return 'prod' if self.state == 'enabled' else 'test'

    def _mdv_cs_client(self):
        self.ensure_one()
        provider = self.sudo()
        try:
            return CyberSourceClient(
                provider.mdv_cs_merchant_id, provider.mdv_cs_key_id, provider.mdv_cs_secret_key,
                environment=provider._mdv_cs_environment(),
            )
        except CyberSourceError as error:
            raise ValidationError(_("CyberSource: %s", error.message))

    def _mdv_cs_request(self, method, path, payload=None, *, tx=None, operation='',
                        raise_on_error=True):
        """ Send a signed request and log it (bodies scrubbed of tokens and card data).

        :return: the body returned by CyberSource.
        :raise ValidationError: when the request fails and `raise_on_error` is set.
        """
        self.ensure_one()
        client = self._mdv_cs_client()
        status, body, error = None, None, None
        try:
            status, body = client.request(method, path, payload)
        except CyberSourceError as exc:
            error = exc
            status, body = exc.status, exc.body
        self.env['mdv.cybersource.log'].sudo()._mdv_log(
            provider=self, tx=tx, method=method, path=path, operation=operation,
            request=scrub(payload) if payload is not None else None,
            response=scrub(body), status=status, error=error and error.message,
        )
        if error:
            if raise_on_error:
                raise ValidationError(_("CyberSource: %s", error.message))
            result = dict(error.body) if isinstance(error.body, dict) else {}
            result.setdefault('status', 'NOT_FOUND' if error.status == 404 else 'INVALID_REQUEST')
            if not isinstance(result.get('status'), str):
                result['status'] = 'INVALID_REQUEST'
            result.setdefault('message', error.message)
            result['http_status'] = error.status
            return result
        return body

    def _mdv_cs_networks(self):
        """ The card networks of the enabled card brands, for Microform. """
        self.ensure_one()
        brands = self.payment_method_ids.brand_ids.filtered('active') | \
            self.payment_method_ids.filtered(lambda m: m.active and m.code in const.NETWORKS)
        networks = []
        for code in brands.mapped('code'):
            network = const.NETWORKS.get(code)
            if network and network not in networks:
                networks.append(network)
        return networks or list(const.DEFAULT_NETWORKS)

    def _mdv_cs_capture_context(self, origin):
        """ Ask CyberSource for a Microform capture context for the page at `origin`.

        POST /microform/v2/sessions returns a JWT naming the client library (and its subresource
        integrity hash) that renders the card number and security code fields in iframes served
        by CyberSource: the card data never reaches Odoo.

        :return: {'capture_context', 'client_library', 'client_library_integrity'}
        """
        self.ensure_one()
        origin = self._mdv_cs_clean_origin(origin)
        payload = {
            'clientVersion': 'v2.0',
            'targetOrigins': [origin],
            'allowedCardNetworks': self._mdv_cs_networks(),
            'allowedPaymentTypes': ['CARD'],
        }
        body = self._mdv_cs_request('POST', '/microform/v2/sessions', payload,
                                    operation='capture_context')
        if not isinstance(body, str):
            raise ValidationError(_("CyberSource did not return a capture context."))
        try:
            data = jwt_payload(body)['ctx'][0]['data']
        except (ValueError, KeyError, IndexError, TypeError):
            raise ValidationError(_("CyberSource returned an unreadable capture context."))
        library = data.get('clientLibrary') or ''
        if not library.startswith(tuple(o + '/' for o in const.MICROFORM_ORIGINS)):
            raise ValidationError(_("CyberSource returned an unexpected Microform library."))
        return {
            'capture_context': body,
            'client_library': library,
            'client_library_integrity': data.get('clientLibraryIntegrity') or '',
        }

    @staticmethod
    def _mdv_cs_clean_origin(origin):
        parts = urlsplit(origin or '')
        if parts.scheme not in ('http', 'https') or not parts.netloc:
            raise ValidationError(_("The address of the payment page is not valid."))
        return f'{parts.scheme}://{parts.netloc}'

    def _mdv_cs_inline_values(self):
        """ The values of the inline form read by the payment form script. """
        self.ensure_one()
        return json.dumps({
            'provider_id': self.id,
            'payer_auth': bool(self.mdv_cs_payer_auth),
            'test': self.state == 'test',
        })

    @api.model
    def _mdv_cs_expiry_years(self):
        year = fields.Date.context_today(self).year
        return [str(y) for y in range(year, year + 16)]

    # === ACTIONS === #

    def action_mdv_cs_test_connection(self):
        """ Check the credentials with a signed request (a Microform capture context). """
        self.ensure_one()
        self._mdv_cs_capture_context(self.get_base_url())
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'type': 'success',
                'title': _("Connection successful"),
                'message': _("CyberSource accepted the credentials (%s environment).",
                             _("production") if self.state == 'enabled' else _("test")),
                'sticky': False,
            },
        }

    def action_mdv_cs_register_webhook(self):
        """ Create a webhook security key (if none is set) and subscribe the notification URL to
        the payment and fraud events the merchant account offers.

        Endpoints: POST /kms/egress/v2/keys-sym, GET /notification-subscriptions/v2/products/{org},
        POST /notification-subscriptions/v2/webhooks.
        """
        self.ensure_one()
        provider = self.sudo()
        if not urls.urljoin(self.get_base_url(), '/').startswith('https://'):
            raise UserError(_("CyberSource only delivers webhooks to an HTTPS address. Set the "
                              "web.base.url of this database to its public HTTPS address first."))
        org = provider.mdv_cs_merchant_id
        if not (provider.mdv_cs_webhook_key_id and provider.mdv_cs_webhook_secret):
            body = self._mdv_cs_request('POST', '/kms/egress/v2/keys-sym', {
                'clientRequestAction': 'CREATE',
                'keyInformation': {
                    'provider': 'nrtd', 'tenant': org, 'keyType': 'sharedSecret',
                    'organizationId': org,
                },
            }, operation='webhook_key')
            info = (body or {}).get('keyInformation') or {}
            if isinstance(info, list):
                info = info[0] if info else {}
            if not (info.get('keyId') and info.get('key')):
                raise UserError(_("CyberSource did not return a webhook security key."))
            provider.write({
                'mdv_cs_webhook_key_id': info['keyId'],
                'mdv_cs_webhook_secret': info['key'],
            })
        available = self._mdv_cs_request(
            'GET', f'/notification-subscriptions/v2/products/{org}', operation='webhook_products')
        products = []
        for product in available if isinstance(available, list) else []:
            product_id = product.get('productId')
            if product_id not in const.WEBHOOK_PRODUCTS:
                continue
            events = [e.get('eventName') if isinstance(e, dict) else e
                      for e in product.get('eventTypes') or []]
            events = [e for e in events if e] or const.DEFAULT_WEBHOOK_EVENTS.get(product_id)
            if events:
                products.append({'productId': product_id, 'eventTypes': events})
        if not products:
            products = [{'productId': p, 'eventTypes': e}
                        for p, e in const.DEFAULT_WEBHOOK_EVENTS.items()]
        body = self._mdv_cs_request('POST', '/notification-subscriptions/v2/webhooks', {
            'name': ('Odoo %s' % (self.company_id.name or ''))[:60],
            'description': 'Odoo payment notifications',
            'organizationId': org,
            'products': products,
            'webhookUrl': self.mdv_cs_webhook_url,
            'healthCheckUrl': self.mdv_cs_webhook_url,
            'notificationScope': 'SELF',
            'retryPolicy': {
                'algorithm': 'ARITHMETIC', 'firstRetry': 1, 'interval': 1, 'numberOfRetries': 3,
                'deactivateFlag': False, 'repeatSequenceCount': 0, 'repeatSequenceWaitTime': 0,
            },
            'securityPolicy': {'securityType': 'key', 'proxyType': 'external'},
        }, operation='webhook_create')
        provider.mdv_cs_webhook_ref = (body or {}).get('webhookId') or False
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'type': 'success',
                'title': _("Webhook registered"),
                'message': _("CyberSource will send its notifications to %s.",
                             self.mdv_cs_webhook_url),
                'sticky': False,
            },
        }

    def action_mdv_cs_view_logs(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("CyberSource API Log"),
            'res_model': 'mdv.cybersource.log',
            'view_mode': 'list,form',
            'domain': [('provider_id', '=', self.id)],
        }

    def _mdv_cs_webhook_keys(self):
        """ {key id: secret} of the providers able to check a notification. """
        keys = {}
        for provider in self.sudo():
            if provider.mdv_cs_webhook_key_id and provider.mdv_cs_webhook_secret:
                keys[provider.mdv_cs_webhook_key_id.strip()] = provider.mdv_cs_webhook_secret.strip()
        return keys
