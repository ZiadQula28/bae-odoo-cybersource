import json
from datetime import timedelta

from odoo import api, fields, models


class MdvCybersourceLog(models.Model):
    _name = 'mdv.cybersource.log'
    _description = "CyberSource API Log"
    _order = 'id desc'
    _rec_name = 'operation'

    provider_id = fields.Many2one('payment.provider', string="Provider", ondelete='cascade',
                                  index=True, readonly=True)
    company_id = fields.Many2one(related='provider_id.company_id', store=True)
    transaction_id = fields.Many2one('payment.transaction', string="Transaction",
                                     ondelete='set null', index=True, readonly=True)
    operation = fields.Char(readonly=True)
    method = fields.Char(readonly=True)
    path = fields.Char(string="Endpoint", readonly=True)
    status_code = fields.Integer(string="HTTP Status", readonly=True)
    result = fields.Char(string="Result", readonly=True,
                         help="The `status` of the CyberSource answer (AUTHORIZED, DECLINED...).")
    error = fields.Text(readonly=True)
    request_body = fields.Text(string="Request", readonly=True)
    response_body = fields.Text(string="Response", readonly=True)
    direction = fields.Selection([('out', "Sent"), ('in', "Webhook")], default='out',
                                 readonly=True)
    success = fields.Boolean(compute='_compute_success', store=True)

    @api.depends('status_code', 'error')
    def _compute_success(self):
        for log in self:
            log.success = not log.error and 200 <= (log.status_code or 0) < 300

    @api.model
    def _mdv_log(self, provider, method, path, *, tx=None, operation='', request=None,
                 response=None, status=None, error=None, direction='out'):
        result = response.get('status') if isinstance(response, dict) else False
        return self.create({
            'provider_id': provider.id,
            'transaction_id': tx.id if tx else False,
            'operation': operation,
            'method': method,
            'path': (path or '')[:250],
            'status_code': status or 0,
            'result': result if isinstance(result, str) else False,
            'error': error or False,
            'request_body': self._dump(request),
            'response_body': self._dump(response),
            'direction': direction,
        })

    @staticmethod
    def _dump(data):
        if data is None:
            return False
        if isinstance(data, str):
            return data[:20000]
        return json.dumps(data, indent=2, ensure_ascii=False)[:20000]

    @api.autovacuum
    def _gc_old_logs(self):
        """ Keep 90 days of API logs. """
        limit = fields.Datetime.now() - timedelta(days=90)
        self.sudo().search([('create_date', '<', limit)]).unlink()
