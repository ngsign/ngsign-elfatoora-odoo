from odoo import models, fields, api, _
from odoo.exceptions import UserError

class NgsignSignOptionsWizard(models.TransientModel):
    _name = 'ngsign.sign.options.wizard'
    _description = 'NGSign Signature Options'

    move_id = fields.Many2one('account.move', string='Invoice', required=True, default=lambda self: self.env.context.get('active_id'))
    action_type = fields.Selection([
        ('sign_now', 'Sign Now'),
        ('send', 'Send for Signature')
    ], string='Action', default='sign_now', required=True)

    # The person who signs the transaction on NGSign. Not necessarily the
    # connected user: an assistant may launch the signature for a manager.
    signer_id = fields.Many2one(
        'res.users',
        string='Signer',
        required=True,
        default=lambda self: self.env['account.move']._ngsign_default_signer(),
        domain="[('id', 'in', authorized_user_ids)]",
        help="Signer of the invoice(s) on NGSign. The last signer you selected is proposed by default.",
    )
    authorized_user_ids = fields.Many2many('res.users', compute='_compute_authorized_user_ids')

    @api.depends('action_type')
    def _compute_authorized_user_ids(self):
        authorized = self.env['account.move']._ngsign_authorized_users()
        if not authorized:
            # No restriction configured: any internal user may sign.
            authorized = self.env['res.users'].search([('share', '=', False)])
        for wiz in self:
            wiz.authorized_user_ids = [(6, 0, authorized.ids)]

    def action_confirm(self):
        self.ensure_one()
        if not self.signer_id:
            raise UserError(_("Please select a signer."))
        if self.signer_id not in self.authorized_user_ids:
            raise UserError(_("%s is not an authorized signer. Please configure authorized signers in Settings.") % self.signer_id.name)
        if not self.signer_id.email:
            raise UserError(_("The signer %s has no email address.") % self.signer_id.name)

        # Proposed by default next time, for this user
        self.env['account.move']._ngsign_remember_signer(self.signer_id)

        context_data = {
            'ngsign_action_type': self.action_type,
            'ngsign_signer_id': self.signer_id.id,
            'ngsign_send_to_user_name': self.signer_id.name,
        }

        active_ids = self.env.context.get('active_ids') or [self.move_id.id]
        
        if self.env.context.get('ngsign_is_debug'):
            moves = self.env['account.move'].browse(active_ids)
            return moves.with_context(**context_data).action_generate_debug_json()
        
        context_data['active_ids'] = active_ids
        return {
            'type': 'ir.actions.client',
            'tag': 'ngsign_einvoice_odoo.action_sign_ngsign_js',
            'context': context_data,
        }
