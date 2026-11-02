import logging

from odoo import fields, models

from odoo.addons.mdv_payment_cybersource import const

_logger = logging.getLogger(__name__)


class PaymentToken(models.Model):
    _inherit = 'payment.token'

    mdv_cs_customer_ref = fields.Char(
        string="CyberSource Customer", readonly=True,
        help="The customer token of the Token Management Service.")
    mdv_cs_network_txn_ref = fields.Char(
        string="Network Transaction ID", readonly=True,
        help="Network transaction id of the first payment, sent with merchant-initiated "
             "payments of this card.")
    mdv_cs_card_type = fields.Char(string="Card Type", readonly=True)

    def _handle_archiving(self):
        """ Override of `payment`: delete the payment instrument from the Token Management Service
        when the customer (or the seller) removes the saved card. Best effort: a failure is only
        logged, the token is archived in Odoo anyway. """
        super()._handle_archiving()
        for token in self.filtered(lambda t: t.provider_code == const.PROVIDER_CODE
                                   and t.provider_ref and t.mdv_cs_customer_ref):
            path = (f'/tms/v2/customers/{token.mdv_cs_customer_ref}'
                    f'/payment-instruments/{token.provider_ref}')
            body = token.provider_id._mdv_cs_request('DELETE', path, operation='token_delete',
                                                     raise_on_error=False)
            if isinstance(body, dict) and body.get('http_status'):
                _logger.warning("CyberSource kept payment instrument of token %s: %s",
                                token.id, body.get('message'))
