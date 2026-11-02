import logging
import re
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.tools import consteq, float_round

from odoo.addons.payment import utils as payment_utils
from odoo.addons.payment.const import CURRENCY_MINOR_UNITS
from odoo.addons.mdv_payment_cybersource import const
from odoo.addons.mdv_payment_cybersource.cybersource_client import jwt_payload

_logger = logging.getLogger(__name__)

# 3-D Secure 2 browser fields sent by the payment form (deviceInformation.*).
BROWSER_FIELDS = {
    'httpBrowserColorDepth': r'^\d{1,2}$',
    'httpBrowserJavaEnabled': r'^[YN]$',
    'httpBrowserJavaScriptEnabled': r'^[YN]$',
    'httpBrowserLanguage': r'^[A-Za-z]{2,3}(-[A-Za-z0-9]{1,8})*$',
    'httpBrowserScreenHeight': r'^\d{1,6}$',
    'httpBrowserScreenWidth': r'^\d{1,6}$',
    'httpBrowserTimeDifference': r'^-?\d{1,5}$',
}


def minor_digits(currency):
    return CURRENCY_MINOR_UNITS.get(currency.name, currency.decimal_places)


def format_amount(amount, currency):
    """ '10.50', '1050' for JPY, '10.500' for KWD: the string format of the REST API. """
    digits = minor_digits(currency)
    return '%.*f' % (digits, float_round(abs(amount), precision_digits=digits))


class PaymentTransaction(models.Model):
    _inherit = 'payment.transaction'

    mdv_cs_status = fields.Char(string="CyberSource Status", readonly=True, copy=False)
    mdv_cs_reason = fields.Char(string="CyberSource Reason", readonly=True, copy=False)
    mdv_cs_card_type = fields.Char(string="Card Type", readonly=True, copy=False)
    mdv_cs_card_masked = fields.Char(string="Card Number", readonly=True, copy=False)
    mdv_cs_approval_code = fields.Char(string="Approval Code", readonly=True, copy=False)
    mdv_cs_avs_code = fields.Char(string="AVS Result", readonly=True, copy=False)
    mdv_cs_cvv_code = fields.Char(string="CVV Result", readonly=True, copy=False)
    mdv_cs_reconciliation_ref = fields.Char(string="Reconciliation ID", readonly=True, copy=False)
    mdv_cs_network_txn_ref = fields.Char(string="Network Transaction ID", readonly=True,
                                         copy=False)
    mdv_cs_capture_ref = fields.Char(string="Capture ID", readonly=True, copy=False,
                                     help="The id of the capture, which refunds are made on.")
    mdv_cs_eci = fields.Char(string="ECI", readonly=True, copy=False)
    mdv_cs_3ds_status = fields.Char(string="3-D Secure Result", readonly=True, copy=False,
                                    help="Y: authenticated, A: attempted, N: failed, "
                                         "U: unavailable, R: rejected, C: challenge.")
    mdv_cs_3ds_version = fields.Char(string="3-D Secure Version", readonly=True, copy=False)
    mdv_cs_3ds_ref = fields.Char(readonly=True, copy=False)  # Payer Auth setup referenceId
    mdv_cs_3ds_auth_ref = fields.Char(readonly=True, copy=False)  # authenticationTransactionId
    mdv_cs_transient_token = fields.Char(copy=False, groups='base.group_system')
    mdv_cs_log_ids = fields.One2many('mdv.cybersource.log', 'transaction_id',
                                     string="API Log")

    # === PROCESSING VALUES === #

    def _get_specific_processing_values(self, processing_values):
        """ Override of `payment` to give the payment form what it needs to send the transient
        token of Microform to the server. """
        res = super()._get_specific_processing_values(processing_values)
        if self.provider_code != const.PROVIDER_CODE:
            return res
        return {
            'mdv_cs_tx_id': self.id,
            'mdv_cs_access_token': payment_utils.generate_access_token(
                self.reference, self.id, env=self.env),
        }

    # === PAYLOAD === #

    def _mdv_cs_code(self):
        return (self.reference or '')[:50]

    def _mdv_cs_bill_to(self):
        name = (self.partner_name or self.partner_id.name or '').strip()
        first, _sep, last = name.partition(' ')
        bill_to = {
            'firstName': first[:60] or 'Customer',
            'lastName': (last or first)[:60] or 'Customer',
            'address1': (self.partner_address or '')[:60],
            'locality': (self.partner_city or '')[:50],
            'postalCode': (self.partner_zip or '')[:10],
            'country': self.partner_country_id.code or '',
            'email': self.partner_email or '',
        }
        if self.partner_state_id.code and self.partner_country_id.code in ('US', 'CA'):
            bill_to['administrativeArea'] = self.partner_state_id.code
        elif self.partner_state_id:
            bill_to['administrativeArea'] = (self.partner_state_id.code
                                             or self.partner_state_id.name)[:20]
        phone = re.sub(r'[^0-9+]', '', self.partner_phone or '')
        if phone:
            bill_to['phoneNumber'] = phone[:15]
        if self.partner_id.commercial_partner_id.is_company:
            bill_to['company'] = {'name': self.partner_id.commercial_partner_id.name[:60]}
        return {k: v for k, v in bill_to.items() if v}

    def _mdv_cs_line_items(self):
        """ The lines of the sales order or invoice (orderInformation.lineItems). """
        if not self.provider_id.mdv_cs_send_order_lines or self.operation == 'validation':
            return []
        digits = minor_digits(self.currency_id)
        items = []
        orders = self.sale_order_ids if 'sale_order_ids' in self._fields else []
        if len(orders) == 1 and orders.currency_id == self.currency_id \
                and orders.currency_id.compare_amounts(orders.amount_total, self.amount) == 0:
            for line in orders.order_line.filtered(lambda l: not l.display_type and l.product_uom_qty):
                items.append({
                    'productName': (line.product_id.name or line.name or '')[:255],
                    'productSku': (line.product_id.default_code or '')[:255] or None,
                    'quantity': max(int(round(line.product_uom_qty)), 1),
                    'unitPrice': '%.*f' % (digits, line.price_total / (
                        max(int(round(line.product_uom_qty)), 1))),
                    'totalAmount': '%.*f' % (digits, line.price_total),
                })
        return [{k: v for k, v in item.items() if v is not None} for item in items[:50]]

    def _mdv_cs_payment_payload(self, *, transient_token=None, capture=None):
        """ The body of POST /pts/v2/payments. """
        provider = self.provider_id
        if capture is None:
            capture = not provider.capture_manually and self.operation != 'validation'
        payload = {
            'clientReferenceInformation': {'code': self._mdv_cs_code(),
                                           'applicationName': 'Odoo'},
            'processingInformation': {
                'capture': bool(capture),
                'commerceIndicator': 'internet',
            },
            'orderInformation': {
                'amountDetails': {
                    'totalAmount': format_amount(self.amount, self.currency_id),
                    'currency': self.currency_id.name,
                },
                'billTo': self._mdv_cs_bill_to(),
            },
        }
        lines = self._mdv_cs_line_items()
        if lines:
            payload['orderInformation']['lineItems'] = lines
        if transient_token:
            payload['tokenInformation'] = {'transientTokenJwt': transient_token}
        if provider.mdv_cs_descriptor:
            payload['merchantInformation'] = {
                'merchantDescriptor': {'name': provider.mdv_cs_descriptor}}
        if self.tokenize or self.operation == 'validation':
            # Save the card with the Token Management Service while paying, as a stored
            # credential of the customer (first customer-initiated transaction).
            payload['processingInformation'].update({
                'actionList': ['TOKEN_CREATE'],
                'actionTokenTypes': ['customer', 'paymentInstrument'],
                'authorizationOptions': {
                    'initiator': {'type': 'customer', 'credentialStoredOnFile': True},
                },
            })
        return payload

    def _mdv_cs_browser_info(self, browser):
        """ deviceInformation for 3-D Secure 2, from the values read by the payment form and the
        headers of the request. """
        info = {'httpBrowserJavaScriptEnabled': 'Y'}
        for key, pattern in BROWSER_FIELDS.items():
            value = str((browser or {}).get(key, '') or '')
            if value and re.match(pattern, value):
                info[key] = value
        try:
            from odoo.http import request
            if request:
                http_request = request.httprequest
                info['ipAddress'] = (http_request.remote_addr or '')[:45]
                info['userAgentBrowserValue'] = (http_request.user_agent.string or '')[:2048]
                accept = http_request.headers.get('Accept') or 'text/html'
                info['httpAcceptBrowserValue'] = accept[:255]
                info['httpAcceptContent'] = accept[:256]
        except RuntimeError:
            pass
        return {k: v for k, v in info.items() if v}

    # === DIRECT FLOW (MICROFORM) === #

    def _mdv_cs_check_transient_token(self, transient_token):
        """ Read the (unverified) claims of the transient token: CyberSource verifies it when it is
        used. We only keep the masked number and the detected card type. """
        try:
            claims = jwt_payload(transient_token)
        except (ValueError, TypeError):
            raise ValidationError(_("The card details could not be read. Please try again."))
        card = (((claims.get('content') or {}).get('paymentInformation') or {}).get('card')
                or {})
        number = card.get('number') or {}
        types = number.get('detectedCardTypes') or []
        masked = number.get('maskedValue') or ''
        self.mdv_cs_card_masked = masked[-19:] if masked else False
        if types:
            self.write(self._mdv_cs_card_values(types[0]))
        return claims

    def _mdv_cs_card_values(self, card_type):
        """ The card type label and the brand payment method from a `card.type` code ('001') or a
        network name of Microform ('VISA'). """
        card_type = str(card_type or '')
        card_type = const.NETWORK_TO_TYPE.get(card_type.upper(), card_type)
        values = {'mdv_cs_card_type': const.CARD_TYPE_LABELS.get(card_type, card_type) or False}
        method_code = const.CARD_TYPES.get(card_type)
        if method_code:
            method = self.env['payment.method'].with_context(active_test=False).search(
                [('code', '=', method_code)], limit=1)
            if method:
                values['payment_method_id'] = method.id
        return values

    def _mdv_cs_pay(self, transient_token, browser=None):
        """ Pay (or save the card, for validations) with the transient token of Microform.

        Without Payer Authentication: one POST /pts/v2/payments. With it: first
        POST /risk/v1/authentication-setups, whose device data collection the payment form runs
        before calling `_mdv_cs_authenticate`.

        :return: {'redirect_url': ...} or {'action': 'collect', 'url', 'jwt'}
        """
        self.ensure_one()
        claims = self._mdv_cs_check_transient_token(transient_token)
        if self.provider_id.mdv_cs_payer_auth and self.operation != 'validation' \
                and claims.get('jti'):
            body = self.provider_id._mdv_cs_request(
                'POST', '/risk/v1/authentication-setups', {
                    'clientReferenceInformation': {'code': self._mdv_cs_code()},
                    # The id (jti) of the transient token, not the JWT itself.
                    'tokenInformation': {'transientToken': claims['jti']},
                }, tx=self, operation='payer_auth_setup', raise_on_error=False)
            info = body.get('consumerAuthenticationInformation') or {} \
                if isinstance(body, dict) else {}
            if body.get('status') == 'COMPLETED' and info.get('referenceId') \
                    and info.get('deviceDataCollectionUrl', '').startswith('https://'):
                self.sudo().write({
                    'mdv_cs_3ds_ref': info['referenceId'],
                    'mdv_cs_transient_token': transient_token,
                })
                return {
                    'action': 'collect',
                    'url': info['deviceDataCollectionUrl'],
                    'jwt': info.get('accessToken') or '',
                }
            # Payer Authentication not available on the account: pay without it.
            _logger.warning("CyberSource Payer Authentication setup failed for %s: %s",
                            self.reference, body.get('message') or body.get('status'))
        payload = self._mdv_cs_payment_payload(transient_token=transient_token)
        body = self.provider_id._mdv_cs_request('POST', '/pts/v2/payments', payload, tx=self,
                                                operation='payment', raise_on_error=False)
        self._mdv_cs_handle_payment_response(body)
        return {'redirect_url': '/payment/status'}

    def _mdv_cs_authenticate(self, browser=None, return_url=None):
        """ Check enrollment and authorize in one call (actionList CONSUMER_AUTHENTICATION).

        :return: {'redirect_url': ...} when the bank needs no challenge, else
                 {'action': 'challenge', 'url', 'jwt', 'md'}
        """
        self.ensure_one()
        transient_token = self.sudo().mdv_cs_transient_token
        if not (transient_token and self.mdv_cs_3ds_ref):
            raise ValidationError(_("The 3-D Secure verification expired. Please pay again."))
        payload = self._mdv_cs_payment_payload(transient_token=transient_token)
        processing = payload['processingInformation']
        processing['actionList'] = ['CONSUMER_AUTHENTICATION'] + processing.get('actionList', [])
        payload['consumerAuthenticationInformation'] = {
            'referenceId': self.mdv_cs_3ds_ref,
            'returnUrl': return_url,
        }
        payload['deviceInformation'] = self._mdv_cs_browser_info(browser)
        body = self.provider_id._mdv_cs_request('POST', '/pts/v2/payments', payload, tx=self,
                                                operation='payment_3ds', raise_on_error=False)
        info = body.get('consumerAuthenticationInformation') or {}
        if body.get('status') == const.STATUS_CHALLENGE and info.get('stepUpUrl', '').startswith(
                'https://'):
            self.mdv_cs_3ds_status = 'C'
            self.mdv_cs_status = const.STATUS_CHALLENGE
            self.mdv_cs_3ds_auth_ref = info.get('authenticationTransactionId') or False
            if info.get('specificationVersion'):
                self.mdv_cs_3ds_version = info['specificationVersion']
            return {
                'action': 'challenge',
                'url': info['stepUpUrl'],
                'jwt': info.get('accessToken') or '',
                'md': self._mdv_cs_sign_md(),
            }
        self._mdv_cs_handle_payment_response(body)
        return {'redirect_url': '/payment/status'}

    def _mdv_cs_validate_challenge(self, authentication_transaction_id):
        """ After the challenge: validate the authentication result and authorize
        (actionList VALIDATE_CONSUMER_AUTHENTICATION). """
        self.ensure_one()
        if self.state != 'draft':
            return
        transient_token = self.sudo().mdv_cs_transient_token
        if not transient_token:
            self._set_error(_("The 3-D Secure verification expired. Please pay again."))
            return
        payload = self._mdv_cs_payment_payload(transient_token=transient_token)
        processing = payload['processingInformation']
        processing['actionList'] = ['VALIDATE_CONSUMER_AUTHENTICATION'] + \
            processing.get('actionList', [])
        payload['consumerAuthenticationInformation'] = {
            'authenticationTransactionId': authentication_transaction_id,
        }
        body = self.provider_id._mdv_cs_request('POST', '/pts/v2/payments', payload, tx=self,
                                                operation='payment_3ds_validate',
                                                raise_on_error=False)
        self._mdv_cs_handle_payment_response(body)

    def _mdv_cs_sign_md(self):
        """ The merchant data echoed back by the 3-D Secure return: id + HMAC of the tx. """
        return '%s.%s' % (self.id, payment_utils.generate_access_token(
            'mdv_cs_3ds', self.reference, self.id, env=self.env))

    @api.model
    def _mdv_cs_tx_from_md(self, md):
        tx_id, _sep, token = (md or '').partition('.')
        if not tx_id.isdigit():
            return self.browse()
        tx = self.sudo().browse(int(tx_id)).exists()
        if not tx or tx.provider_code != const.PROVIDER_CODE or not token:
            return self.browse()
        expected = payment_utils.generate_access_token(
            'mdv_cs_3ds', tx.reference, tx.id, env=self.env)
        if not consteq(token, expected):
            return self.browse()
        return tx

    def _mdv_cs_handle_payment_response(self, body):
        """ Process the answer of POST /pts/v2/payments; the transient token is not kept. """
        if self.sudo().mdv_cs_transient_token:
            self.sudo().mdv_cs_transient_token = False
        if not isinstance(body, dict):
            body = {'status': 'SERVER_ERROR', 'message': str(body)[:200]}
        self._process(const.PROVIDER_CODE, {
            'reference': self.reference, 'operation': 'payment', 'response': body,
        })

    # === TOKEN FLOW === #

    def _send_payment_request(self):
        """ Override of `payment` to pay with a saved card (TMS customer + payment instrument). """
        if self.provider_code != const.PROVIDER_CODE:
            return super()._send_payment_request()
        token = self.token_id
        if not token or not token.provider_ref:
            raise UserError(_("The saved card is not linked to CyberSource."))
        payload = self._mdv_cs_payment_payload()
        payload['processingInformation'].pop('actionList', None)
        payload['processingInformation'].pop('actionTokenTypes', None)
        payment_information = {'paymentInstrument': {'id': token.provider_ref}}
        if token.mdv_cs_customer_ref:
            payment_information['customer'] = {'id': token.mdv_cs_customer_ref}
        payload['paymentInformation'] = payment_information
        if self.operation == 'offline':
            # Merchant-initiated (subscriptions, payment of an invoice by the seller).
            initiator = {'type': 'merchant', 'storedCredentialUsed': True}
            if token.mdv_cs_network_txn_ref:
                initiator['merchantInitiatedTransaction'] = {
                    'previousTransactionId': token.mdv_cs_network_txn_ref,
                    'reason': '7',
                }
            payload['processingInformation']['commerceIndicator'] = 'recurring' \
                if self.env.context.get('mdv_cs_recurring') else 'internet'
        else:
            initiator = {'type': 'customer', 'storedCredentialUsed': True}
        payload['processingInformation']['authorizationOptions'] = {'initiator': initiator}
        body = self.provider_id._mdv_cs_request('POST', '/pts/v2/payments', payload, tx=self,
                                                operation='payment_token', raise_on_error=False)
        self._mdv_cs_handle_payment_response(body)

    # === CAPTURE / VOID / REFUND === #

    def _send_capture_request(self):
        """ Override of `payment` to capture the authorized amount. """
        if self.provider_code != const.PROVIDER_CODE:
            return super()._send_capture_request()
        source = self.source_transaction_id
        payload = {
            'clientReferenceInformation': {'code': self._mdv_cs_code()},
            'orderInformation': {'amountDetails': {
                'totalAmount': format_amount(self.amount, self.currency_id),
                'currency': self.currency_id.name,
            }},
        }
        body = self.provider_id._mdv_cs_request(
            'POST', f'/pts/v2/payments/{source.provider_reference}/captures', payload, tx=self,
            operation='capture')
        self._process(const.PROVIDER_CODE, {
            'reference': self.reference, 'operation': 'capture', 'response': body})

    def _send_void_request(self):
        """ Override of `payment` to release the authorization (authorization reversal). """
        if self.provider_code != const.PROVIDER_CODE:
            return super()._send_void_request()
        source = self.source_transaction_id
        payload = {
            'clientReferenceInformation': {'code': self._mdv_cs_code()},
            'reversalInformation': {
                'amountDetails': {
                    'totalAmount': format_amount(self.amount, self.currency_id),
                },
                'reason': 'void',
            },
        }
        body = self.provider_id._mdv_cs_request(
            'POST', f'/pts/v2/payments/{source.provider_reference}/reversals', payload, tx=self,
            operation='void')
        self._process(const.PROVIDER_CODE, {
            'reference': self.reference, 'operation': 'void', 'response': body})

    def _send_refund_request(self):
        """ Override of `payment` to refund a captured payment (fully or partly). """
        if self.provider_code != const.PROVIDER_CODE:
            return super()._send_refund_request()
        source = self.source_transaction_id
        capture_ref = source.mdv_cs_capture_ref
        if capture_ref:
            path = f'/pts/v2/captures/{capture_ref}/refunds'
        else:
            path = f'/pts/v2/payments/{source.provider_reference}/refunds'
        payload = {
            'clientReferenceInformation': {'code': self._mdv_cs_code()},
            'orderInformation': {'amountDetails': {
                'totalAmount': format_amount(self.amount, self.currency_id),
                'currency': self.currency_id.name,
            }},
        }
        body = self.provider_id._mdv_cs_request('POST', path, payload, tx=self,
                                                operation='refund')
        self._process(const.PROVIDER_CODE, {
            'reference': self.reference, 'operation': 'refund', 'response': body})

    # === PROCESSING === #

    @api.model
    def _extract_reference(self, provider_code, payment_data):
        if provider_code != const.PROVIDER_CODE:
            return super()._extract_reference(provider_code, payment_data)
        return payment_data.get('reference')

    def _extract_amount_data(self, payment_data):
        """ Override of `payment`: check the authorized amount of successful payments. """
        if self.provider_code != const.PROVIDER_CODE:
            return super()._extract_amount_data(payment_data)
        response = payment_data.get('response') or {}
        if payment_data.get('operation') != 'payment' or self.operation == 'validation':
            return None
        status = response.get('status')
        amounts = (response.get('orderInformation') or {}).get('amountDetails') or {}
        if status not in const.STATUS_AUTHORIZED | const.STATUS_REVIEW \
                or not amounts.get('authorizedAmount'):
            return None
        try:
            amount = float(amounts['authorizedAmount'])
        except (TypeError, ValueError):
            return {'amount': 0, 'currency_code': ''}
        return {
            'amount': amount,
            'currency_code': amounts.get('currency') or self.currency_id.name,
            'precision_digits': minor_digits(self.currency_id),
        }

    def _apply_updates(self, payment_data):
        """ Override of `payment` to update the transaction from the answer of CyberSource. """
        if self.provider_code != const.PROVIDER_CODE:
            return super()._apply_updates(payment_data)
        response = payment_data.get('response') or {}
        operation = payment_data.get('operation')
        status = response.get('status') or ''
        error = response.get('errorInformation') or {}
        reason = error.get('reason') or response.get('reason') or ''
        self.mdv_cs_status = status
        self.mdv_cs_reason = reason or False
        if operation == 'payment':
            self._mdv_cs_store_payment_details(response)
        elif response.get('id'):
            self.provider_reference = response['id']

        if operation == 'payment':
            if status in const.STATUS_AUTHORIZED:
                capture = not self.provider_id.capture_manually and self.operation != 'validation'
                processing = response.get('processorInformation') or {}
                if self.operation == 'validation':
                    self._set_done()
                elif capture:
                    self._set_done()
                else:
                    self._set_authorized()
                if processing.get('networkTransactionId'):
                    self.mdv_cs_network_txn_ref = processing['networkTransactionId']
            elif status in const.STATUS_REVIEW:
                self._set_pending(state_message=_(
                    "The payment is authorized and held for review by the fraud screening "
                    "(Decision Manager)."))
            elif status == 'AUTHORIZED_RISK_DECLINED':
                self._mdv_cs_reverse_quietly(response)
                self._set_error(self._mdv_cs_decline_message(response))
            elif status == 'PARTIAL_AUTHORIZED':
                self._mdv_cs_reverse_quietly(response)
                self._set_error(_("The bank approved only part of the amount. The payment was "
                                  "canceled; please use another card."))
            elif status == const.STATUS_CHALLENGE:
                self._set_error(_("The bank asks for a 3-D Secure verification. Please pay "
                                  "again."))
            else:
                self._set_error(self._mdv_cs_decline_message(response))
        elif operation in ('capture', 'refund'):
            if status in const.STATUS_FOLLOW_ON_OK:
                if operation == 'capture':
                    self.mdv_cs_capture_ref = response.get('id')
                    self.source_transaction_id.mdv_cs_capture_ref = response.get('id')
                self._set_done()
            else:
                self._set_error(self._mdv_cs_decline_message(response))
        elif operation == 'void':
            if status in const.STATUS_CANCELED:
                self._set_canceled()
            else:
                self._set_error(self._mdv_cs_decline_message(response))

    def _mdv_cs_store_payment_details(self, response):
        if response.get('id'):
            self.provider_reference = response['id']
        processing = response.get('processorInformation') or {}
        auth_info = response.get('consumerAuthenticationInformation') or {}
        payment_info = response.get('paymentInformation') or {}
        account_info = response.get('paymentAccountInformation') or {}
        card_type = ((payment_info.get('card') or {}).get('type')
                     or (account_info.get('card') or {}).get('type')
                     or (payment_info.get('tokenizedCard') or {}).get('type'))
        values = {
            'mdv_cs_approval_code': processing.get('approvalCode') or False,
            'mdv_cs_avs_code': (processing.get('avs') or {}).get('code') or False,
            'mdv_cs_cvv_code': (processing.get('cardVerification') or {}).get('resultCode')
            or False,
            'mdv_cs_reconciliation_ref': response.get('reconciliationId') or False,
        }
        if auth_info.get('paresStatus') or auth_info.get('eci'):
            values.update({
                'mdv_cs_3ds_status': auth_info.get('paresStatus') or False,
                'mdv_cs_eci': auth_info.get('eci') or auth_info.get('eciRaw') or False,
                'mdv_cs_3ds_version': auth_info.get('specificationVersion') or False,
            })
        if card_type:
            values.update(self._mdv_cs_card_values(card_type))
        self.write(values)

    def _mdv_cs_decline_message(self, response):
        error = response.get('errorInformation') or {}
        reason = error.get('reason') or response.get('reason') or ''
        message = const.DECLINE_MESSAGES.get(reason)
        if message:
            message = message._translate(self.env.lang or 'en_US')
        else:
            message = error.get('message') or response.get('message') or \
                _("The payment was declined.")
        details = response.get('details') or error.get('details') or []
        fields_ = [d.get('field') for d in details if isinstance(d, dict) and d.get('field')]
        if fields_:
            message = '%s (%s)' % (message, ', '.join(fields_))
        return _("CyberSource: %(status)s - %(message)s",
                 status=response.get('status') or 'ERROR', message=message)

    def _mdv_cs_reverse_quietly(self, response):
        """ Release an authorization Odoo does not accept (partial or fraud-declined). """
        if not response.get('id'):
            return
        amounts = (response.get('orderInformation') or {}).get('amountDetails') or {}
        amount = amounts.get('authorizedAmount')
        if not amount:
            return
        self.provider_id._mdv_cs_request(
            'POST', f"/pts/v2/payments/{response['id']}/reversals", {
                'clientReferenceInformation': {'code': self._mdv_cs_code()},
                'reversalInformation': {'amountDetails': {'totalAmount': amount},
                                        'reason': 'void'},
            }, tx=self, operation='auto_reversal', raise_on_error=False)

    def _extract_token_values(self, payment_data):
        """ Override of `payment`: the TMS ids returned with actionList TOKEN_CREATE. """
        if self.provider_code != const.PROVIDER_CODE:
            return super()._extract_token_values(payment_data)
        response = payment_data.get('response') or {}
        info = response.get('tokenInformation') or {}
        instrument = (info.get('paymentInstrument') or {}).get('id')
        if not instrument:
            _logger.warning("CyberSource returned no payment instrument for %s.", self.reference)
            return {}
        masked = self.mdv_cs_card_masked or ''
        last4 = re.sub(r'\D', '', masked)[-4:]
        processing = response.get('processorInformation') or {}
        return {
            'provider_ref': instrument,
            'payment_details': last4 or self.mdv_cs_card_type or '',
            'mdv_cs_customer_ref': (info.get('customer') or {}).get('id') or False,
            'mdv_cs_network_txn_ref': processing.get('networkTransactionId') or False,
            'mdv_cs_card_type': self.mdv_cs_card_type or False,
        }

    # === WEBHOOKS AND STATUS SYNC === #

    def _mdv_cs_read_back(self, transaction_ref):
        """ GET /tss/v2/transactions/{id}: the record of CyberSource, never the webhook body. """
        self.ensure_one()
        return self.provider_id._mdv_cs_request(
            'GET', f'/tss/v2/transactions/{transaction_ref}', tx=self, operation='read_back',
            raise_on_error=False)

    @api.model
    def _mdv_cs_outcome(self, details):
        """ What a transaction-details record says happened: one of 'authorized', 'captured',
        'refunded', 'reversed', 'voided', 'review', 'declined', or None (unknown). """
        info = details.get('applicationInformation') or {}
        apps = {a.get('name'): a for a in info.get('applications') or [] if isinstance(a, dict)}
        reason = str(info.get('reasonCode') or '')

        def ok(name):
            app = apps.get(name)
            return bool(app) and str(app.get('reasonCode') or '') in ('100', '') \
                and (app.get('rFlag') or 'SOK') == 'SOK'

        if reason == '480':
            return 'review'
        if reason and reason not in ('100', '110'):
            return 'declined'
        if ok('ics_void'):
            return 'voided'
        if ok('ics_auth_reversal'):
            return 'reversed'
        if ok('ics_credit'):
            return 'refunded'
        if ok('ics_bill'):
            return 'captured'
        if ok('ics_auth'):
            return 'authorized'
        return None

    def _mdv_cs_apply_outcome(self, outcome, details=None):
        """ Move the transaction to the state the read-back record shows. """
        self.ensure_one()
        message = _("Updated from CyberSource.")
        if outcome == 'review':
            if self.state == 'draft':
                self._set_pending(state_message=_("Held for review by the fraud screening."))
            return
        if outcome == 'declined':
            if self.state in ('draft', 'pending', 'authorized'):
                self._set_error(_("CyberSource: the payment was declined or rejected after "
                                  "review."))
            return
        if self.operation == 'refund':
            if outcome == 'refunded':
                self._set_done(state_message=message)
            return
        if outcome in ('reversed', 'voided'):
            self._set_canceled(state_message=message)
        elif outcome == 'captured':
            self._set_done(state_message=message)
        elif outcome == 'authorized':
            if self.state in ('draft', 'pending'):
                if self.provider_id.capture_manually:
                    self._set_authorized(state_message=_("Accepted by CyberSource."))
                else:
                    # Accepted after a review: the payment was not captured yet.
                    self._set_authorized(state_message=_("Accepted after review."))
                    if self.state == 'authorized':
                        self._capture()

    def _mdv_cs_sync(self):
        """ Read the transactions back from CyberSource and update them. """
        for tx in self.filtered(lambda t: t.provider_code == const.PROVIDER_CODE
                                and t.provider_reference):
            details = tx._mdv_cs_read_back(tx.provider_reference)
            if not isinstance(details, dict) or details.get('http_status'):
                continue
            code = (details.get('clientReferenceInformation') or {}).get('code')
            if code and code != tx._mdv_cs_code():
                _logger.warning("CyberSource record %s does not belong to %s.",
                                tx.provider_reference, tx.reference)
                continue
            outcome = self._mdv_cs_outcome(details)
            if outcome:
                tx._mdv_cs_apply_outcome(outcome, details)

    def action_mdv_cs_sync(self):
        self._mdv_cs_sync()
        return True

    @api.model
    def _cron_mdv_cs_sync(self):
        """ Settle what the customer did not finish: payments held for review, captures and
        refunds still pending. """
        since = fields.Datetime.now() - timedelta(days=7)
        txs = self.search([
            ('provider_code', '=', const.PROVIDER_CODE),
            ('state', '=', 'pending'),
            ('provider_reference', '!=', False),
            ('create_date', '>=', since),
        ], limit=200)
        for tx in txs:
            try:
                with self.env.cr.savepoint():
                    tx._mdv_cs_sync()
            except (ValidationError, UserError) as error:
                _logger.warning("CyberSource sync of %s failed: %s", tx.reference, error)
        # Drop transient tokens of abandoned 3-D Secure attempts (valid 15 minutes).
        stale = self.sudo().search([
            ('provider_code', '=', const.PROVIDER_CODE),
            ('mdv_cs_transient_token', '!=', False),
            ('write_date', '<', fields.Datetime.now() - timedelta(minutes=30)),
        ])
        stale.write({'mdv_cs_transient_token': False})

    @api.model
    def _mdv_cs_handle_notification(self, notification, provider=None):
        """ Handle a verified webhook notification: find the CyberSource transaction it is about,
        read it back with the API, and update the matching Odoo transaction.

        :return: (updated transactions, whether CyberSource should send it again because the
                 transaction is not readable yet)
        """
        updated = self.browse()
        retry = False
        for item in notification.get('payload') or []:
            data = (item or {}).get('data') or {}
            transaction_ref = data.get('id')
            href = ((data.get('_links') or {}).get('self') or {}).get('href') or ''
            if not transaction_ref and href:
                transaction_ref = href.rstrip('/').rsplit('/', 1)[-1]
            if not transaction_ref or not re.match(r'^[A-Za-z0-9]{6,40}$', transaction_ref):
                continue
            code = (data.get('clientReferenceInformation') or {}).get('code')
            domain = [('provider_code', '=', const.PROVIDER_CODE)]
            tx = self.search(domain + [('provider_reference', '=', transaction_ref)], limit=1)
            if not tx and code:
                tx = self.search(domain + [('reference', '=', code)], limit=1)
            if provider and tx and tx.provider_id not in provider:
                continue
            if not tx:
                _logger.info("CyberSource notification for unknown transaction %s.",
                             transaction_ref)
                continue
            details = tx._mdv_cs_read_back(transaction_ref)
            if not isinstance(details, dict) or details.get('http_status'):
                # The transaction search of CyberSource lags a few seconds behind.
                retry = True
                continue
            ref_code = (details.get('clientReferenceInformation') or {}).get('code')
            if ref_code and ref_code != tx._mdv_cs_code():
                continue
            outcome = self._mdv_cs_outcome(details)
            if outcome:
                tx._mdv_cs_apply_outcome(outcome, details)
                updated |= tx
        return updated, retry
