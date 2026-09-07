"""Wizard showing the result of the e-invoice data check.

It is opened either explicitly (button "Check e-Invoice Data" on the invoice)
or automatically when the user tries to sign an invoice whose data would be
refused by NGSign / TTN. Each line points at the record to fix and can open it
directly, so the user does not have to guess where the value comes from.
"""

import json

from odoo import models, fields, api, _

from .ngsign_spec import ERROR, WARNING, INFO, SEVERITY_ORDER, SEVERITY_RANK


class NGSignValidationResult(models.TransientModel):
    _name = 'ngsign.validation.result'
    _description = 'NGSign e-Invoice Data Check'

    move_ids = fields.Many2many('account.move', string='Invoices')
    issue_ids = fields.One2many('ngsign.validation.issue', 'result_id', string='Issues')

    error_count = fields.Integer(string='Critical Count', compute='_compute_counts')
    warning_count = fields.Integer(compute='_compute_counts')
    info_count = fields.Integer(compute='_compute_counts')
    has_errors = fields.Boolean(compute='_compute_counts')
    is_clean = fields.Boolean(compute='_compute_counts')

    # Set when the wizard interrupted a signature: allows resuming it.
    resume_signature = fields.Boolean(default=False)
    resume_context = fields.Text()

    @api.depends('issue_ids.severity')
    def _compute_counts(self):
        for wizard in self:
            # Count each level explicitly: deducing one from the others breaks
            # as soon as a new severity exists.
            wizard.error_count = len(wizard.issue_ids.filtered(lambda i: i.severity == ERROR))
            wizard.warning_count = len(wizard.issue_ids.filtered(lambda i: i.severity == WARNING))
            wizard.info_count = len(wizard.issue_ids.filtered(lambda i: i.severity == INFO))
            wizard.has_errors = bool(wizard.error_count)
            wizard.is_clean = not wizard.issue_ids

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @api.model
    def open_for_moves(self, moves, issues=None, resume_signature=False):
        """Create the wizard for ``moves`` and return the action opening it."""
        if issues is None:
            issues = self.env['ngsign.validator'].validate_moves(moves)
        resume_keys = ('ngsign_action_type', 'ngsign_send_to_user_id')
        wizard = self.create({
            'move_ids': [(6, 0, moves.ids)],
            'resume_signature': resume_signature,
            'resume_context': json.dumps({
                key: self.env.context[key] for key in resume_keys if key in self.env.context
            }),
            'issue_ids': [(0, 0, self._issue_values(issue, sequence))
                          for sequence, issue in enumerate(self._sorted(issues))],
        })
        return wizard._action_open()

    @api.model
    def _sorted(self, issues):
        return self.env['ngsign.validator'].sort_by_severity(issues)

    @api.model
    def _issue_values(self, issue, sequence=0):
        return {
            'sequence': sequence,
            'code': issue['code'],
            'severity': issue['severity'],
            'label': issue['label'],
            'message': issue['message'],
            'hint': issue['hint'],
            'path': issue['path'],
            'value': issue['value'],
            'context_label': issue['context_label'],
            'move_id': issue['move_id'] or False,
            'res_model': issue['res_model'] or False,
            'res_id': issue['res_id'] or 0,
            'res_name': issue['res_name'],
        }

    def _action_open(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('e-Invoice Data Check'),
            'res_model': 'ngsign.validation.result',
            'res_id': self.id,
            'view_mode': 'form',
            # `views` is mandatory: this action is returned to a JS client action
            # that calls it through orm.call (/web/dataset/call_kw), which does
            # NOT run clean_action() - unlike a button (/web/dataset/call_button).
            # Without it the web client crashes on action.views.map().
            'views': [(False, 'form')],
            'target': 'new',
        }

    # ------------------------------------------------------------------
    # Buttons
    # ------------------------------------------------------------------

    def action_recheck(self):
        """Re-run the check after the user fixed the data."""
        self.ensure_one()
        issues = self.env['ngsign.validator'].validate_moves(self.move_ids)
        self.issue_ids = [(5, 0, 0)] + [
            (0, 0, self._issue_values(issue, sequence))
            for sequence, issue in enumerate(self._sorted(issues))
        ]
        return self._action_open()

    def action_proceed(self):
        """Sign anyway. Only offered when nothing blocking was found."""
        self.ensure_one()
        if self.has_errors:
            return self._action_open()
        try:
            context = json.loads(self.resume_context or '{}')
        except ValueError:
            context = {}
        # The PDF was rendered before the user corrected the data: render it again
        # so the signed document matches what is being declared.
        self.move_ids.action_ngsign_prepare()
        return self.move_ids.with_context(
            ngsign_skip_validation=True, **context
        ).action_ngsign_send()

    def action_close(self):
        return {'type': 'ir.actions.act_window_close'}


class NGSignValidationIssue(models.TransientModel):
    _name = 'ngsign.validation.issue'
    _description = 'NGSign e-Invoice Data Issue'
    _order = 'sequence, id'

    result_id = fields.Many2one('ngsign.validation.result', required=True, ondelete='cascade')
    sequence = fields.Integer(default=0)

    code = fields.Char(string='Rule')
    severity = fields.Selection([
        (ERROR, 'Critical'),
        (WARNING, 'Warning'),
        (INFO, 'Information'),
    ], string='Severity', required=True, default=ERROR)
    move_id = fields.Many2one('account.move', string='Invoice')
    context_label = fields.Char(string='Where')
    label = fields.Char(string='Field')
    message = fields.Char(string='Problem')
    hint = fields.Char(string='What to do')
    value = fields.Char(string='Current value')
    path = fields.Char(string='API field')

    res_model = fields.Char(string='Record Model')
    res_id = fields.Integer(string='Record ID')
    res_name = fields.Char(string='Record')

    def action_open_record(self):
        """Open the Odoo record holding the faulty value."""
        self.ensure_one()
        if not self.res_model or not self.res_id:
            return False
        return {
            'type': 'ir.actions.act_window',
            'res_model': self.res_model,
            'res_id': self.res_id,
            'view_mode': 'form',
            'views': [(False, 'form')],
            'target': 'current',
        }
