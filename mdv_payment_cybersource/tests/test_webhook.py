import json
import time
from datetime import timedelta

from odoo import fields
from odoo.tests import tagged
from odoo.tools import mute_logger

from odoo.addons.mdv_payment_cybersource.cybersource_client import webhook_signatures
from odoo.addons.mdv_payment_cybersource.tests.common import (
    PAYMENT_ID, TRANSIENT_TOKEN, WEBHOOK_KEY_ID, WEBHOOK_SECRET, CyberSourceCommon, authorized,
)


def details(code, *apps, reason=100):
    """ GET /tss/v2/transactions/{id} (shape of the live sandbox answer). """
    return {
        'id': PAYMENT_ID,
        'clientReferenceInformation': {'code': code, 'applicationName': 'REST API'},
        'applicationInformation': {'reasonCode': reason, 'applications': [
            {'name': name, 'reasonCode': '100', 'rCode': '1', 'rFlag': 'SOK'} for name in apps]},
        'orderInformation': {'amountDetails': {'totalAmount': '1111.11', 'currency': 'USD'}},
    }


def notification(transaction_ref=PAYMENT_ID, event='payments.payments.accept', retry=0):
    return {
        'eventType': event, 'webhookId': 'wh-1', 'retryNumber': retry,
        'organizationId': 'odoo_unit_test', 'productId': 'payments',
        'payload': [{'data': {
            '_links': {'self': {'href': f'/pts/v2/payments/{transaction_ref}'}},
            'id': transaction_ref, 'type': 'payments'}}],
    }


@tagged('post_install', '-at_install')
class TestCyberSourceWebhookLogic(CyberSourceCommon):

    def _review_tx(self):
        tx = self._create_cs_transaction()
        with self.mock_api(('POST', '/pts/v2/payments',
                            authorized(status='AUTHORIZED_PENDING_REVIEW'), 201)):
            tx._mdv_cs_pay(TRANSIENT_TOKEN, browser={})
        self.assertEqual(tx.state, 'pending')
        return tx

    def test_outcome_mapping(self):
        Tx = self.env['payment.transaction']
        self.assertEqual(Tx._mdv_cs_outcome(details('x', 'ics_auth')), 'authorized')
        self.assertEqual(Tx._mdv_cs_outcome(details('x', 'ics_auth', 'ics_bill')), 'captured')
        self.assertEqual(Tx._mdv_cs_outcome(details('x', 'ics_credit')), 'refunded')
        self.assertEqual(Tx._mdv_cs_outcome(details('x', 'ics_auth_reversal')), 'reversed')
        self.assertEqual(Tx._mdv_cs_outcome(details('x', 'ics_void')), 'voided')
        self.assertEqual(Tx._mdv_cs_outcome(details('x', 'ics_auth', reason=480)), 'review')
        self.assertEqual(Tx._mdv_cs_outcome(details('x', 'ics_auth', reason=481)), 'declined')
        self.assertIsNone(Tx._mdv_cs_outcome({}))

    def test_review_accepted_is_captured(self):
        tx = self._review_tx()
        capture = {'id': 'C1', 'status': 'PENDING'}
        with self.mock_api(
            ('GET', f'/tss/v2/transactions/{PAYMENT_ID}', details(tx.reference, 'ics_auth'), 200),
            ('POST', f'/pts/v2/payments/{PAYMENT_ID}/captures', capture, 201),
        ) as api:
            updated, retry = self.env['payment.transaction']._mdv_cs_handle_notification(
                notification())
        self.assertEqual(updated, tx)
        self.assertFalse(retry)
        self.assertIn(('POST', f'/pts/v2/payments/{PAYMENT_ID}/captures'), api.paths())
        self.assertEqual(tx.state, 'done')

    def test_review_rejected(self):
        tx = self._review_tx()
        with self.mock_api(('GET', f'/tss/v2/transactions/{PAYMENT_ID}',
                            details(tx.reference, 'ics_auth', reason=481), 200)):
            self.env['payment.transaction']._mdv_cs_handle_notification(notification())
        self.assertEqual(tx.state, 'error')

    def test_notification_of_another_order_is_ignored(self):
        tx = self._review_tx()
        with self.mock_api(('GET', f'/tss/v2/transactions/{PAYMENT_ID}',
                            details('SOMEONE-ELSE', 'ics_auth', 'ics_bill'), 200)):
            updated, _retry = self.env['payment.transaction']._mdv_cs_handle_notification(
                notification())
        self.assertFalse(updated)
        self.assertEqual(tx.state, 'pending')

    @mute_logger('odoo.addons.mdv_payment_cybersource.cybersource_client')
    def test_not_yet_readable_asks_for_retry(self):
        self._review_tx()
        with self.mock_api(('GET', f'/tss/v2/transactions/{PAYMENT_ID}',
                            {'message': 'The requested resource does not exist'}, 404)):
            updated, retry = self.env['payment.transaction']._mdv_cs_handle_notification(
                notification())
        self.assertFalse(updated)
        self.assertTrue(retry)

    def test_cron_settles_reviewed_payment(self):
        self.cybersource.capture_manually = True
        tx = self._review_tx()
        with self.mock_api(('GET', f'/tss/v2/transactions/{PAYMENT_ID}',
                            details(tx.reference, 'ics_auth'), 200)):
            self.env['payment.transaction']._cron_mdv_cs_sync()
        self.assertEqual(tx.state, 'authorized')

    def test_cron_drops_stale_transient_tokens(self):
        tx = self._create_cs_transaction()
        tx.sudo().mdv_cs_transient_token = TRANSIENT_TOKEN
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE payment_transaction SET write_date = %s WHERE id = %s",
            [fields.Datetime.now() - timedelta(hours=2), tx.id])
        tx.invalidate_recordset()
        with self.mock_api():
            self.env['payment.transaction']._cron_mdv_cs_sync()
        self.assertFalse(tx.sudo().mdv_cs_transient_token)


@tagged('post_install', '-at_install')
class TestCyberSourceWebhookSignatureFlow(CyberSourceCommon):
    """ The signature check of the controller, without HTTP: `verify_webhook` is what the route
    calls with the raw body and the keys of the providers. """

    def test_keys_of_enabled_providers(self):
        from odoo.addons.mdv_payment_cybersource.cybersource_client import verify_webhook
        raw = json.dumps(notification()).encode()
        stamp = str(int(time.time() * 1000))
        sig = webhook_signatures(WEBHOOK_SECRET, stamp, raw)[0]
        header = f't={stamp};keyId={WEBHOOK_KEY_ID};sig={sig}'
        keys = self.env['payment.provider'].search(
            [('code', '=', 'mdv_cybersource')])._mdv_cs_webhook_keys()
        self.assertTrue(verify_webhook(header, raw, keys)[0])
