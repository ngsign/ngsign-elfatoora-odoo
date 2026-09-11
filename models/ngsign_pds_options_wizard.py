from odoo import models, fields, api, _
from odoo.exceptions import UserError
import logging

_logger = logging.getLogger(__name__)

class NgsignPdsOptionsWizard(models.TransientModel):
    _name = 'ngsign.pds.options.wizard'
    _description = 'NGSign Signing Page Options'

    move_id = fields.Many2one('account.move', string='Invoice', required=True, default=lambda self: self.env.context.get('active_id'))
    action_type = fields.Selection([
        ('open_pds', 'Open Signing Page'),
        ('send_email', 'Send via Email'),
        ('restart', 'Restart with another signer'),
    ], string='Action', default='open_pds', required=True)

    # Signer the existing transaction was created for. Empty on transactions
    # created before the signer selection existed.
    current_signer_id = fields.Many2one(related='move_id.ngsign_signer_id', string='Transaction Signer')
    transaction_move_count = fields.Integer(compute='_compute_transaction_move_count')

    authorized_user_id = fields.Many2one(
        'res.users', 
        string='Send To', 
        default=lambda self: self.env['account.move']._ngsign_default_signer(),
        domain="[('id', 'in', authorized_user_ids)]",
        help="Signer who receives the signing page link. The last signer you selected is proposed by default.",
    )
    # New signer when the transaction is cancelled and created again.
    signer_id = fields.Many2one(
        'res.users',
        string='New Signer',
        default=lambda self: self.env['account.move']._ngsign_default_signer(),
        domain="[('id', 'in', authorized_user_ids)]",
        help="The pending transaction is cancelled on NGSign and created again for this signer.",
    )
    restart_delivery = fields.Selection([
        ('open', 'Open the signing page'),
        ('email', 'Send the link by email to the signer'),
    ], string='Then', default='open', required=True)
    authorized_user_ids = fields.Many2many('res.users', compute='_compute_authorized_user_ids')

    @api.depends('action_type')
    def _compute_authorized_user_ids(self):
        authorized = self.env['account.move']._ngsign_authorized_users()
        if not authorized:
            # No restriction configured: any internal user may sign.
            authorized = self.env['res.users'].search([('share', '=', False)])
        for wiz in self:
            wiz.authorized_user_ids = [(6, 0, authorized.ids)]

    @api.depends('move_id')
    def _compute_transaction_move_count(self):
        for wiz in self:
            wiz.transaction_move_count = len(wiz.move_id._ngsign_transaction_moves()) if wiz.move_id else 0

    def _check_signer(self, signer):
        if not signer:
            raise UserError(_("Please select a signer."))
        if signer not in self.authorized_user_ids:
            raise UserError(_("%s is not an authorized signer. Please configure authorized signers in Settings.") % signer.name)
        if not signer.email:
            raise UserError(_("The signer %s has no email address.") % signer.name)
        self.env['account.move']._ngsign_remember_signer(signer)

    def action_confirm(self):
        self.ensure_one()
        if self.action_type == 'restart':
            return self._action_restart()

        if not self.move_id.ngsign_pds_url:
            raise UserError(_("No Signing Page URL found for this invoice."))

        if self.action_type == 'open_pds':
            return {
                'type': 'ir.actions.act_url',
                'url': self.move_id.ngsign_pds_url,
                'target': 'new',
                'close': True,  # close this dialog once the page is opened
            }
        elif self.action_type == 'send_email':
            self._check_signer(self.authorized_user_id)
            
            params = self.env['ir.config_parameter'].sudo()
            template_id_str = params.get_param('ngsign.email_template_id')
            if template_id_str:
                template = self.env['mail.template'].browse(int(template_id_str))
                if template.exists():
                    try:
                        template.sudo().with_context(
                            ngsign_pds_url=self.move_id.ngsign_pds_url,
                            ngsign_send_to_user_name=self.authorized_user_id.name
                        ).send_mail(self.move_id.id, force_send=True, email_values={'email_to': self.authorized_user_id.email})
                        _logger.info(f"NGSign: Sent signature request email to {self.authorized_user_id.email}")
                        
                        # Add notification logic
                        return {
                            'type': 'ir.actions.client',
                            'tag': 'display_notification',
                            'params': {
                                'title': _('Email Sent'),
                                'message': _('Signature request email was successfully sent to %s.') % self.authorized_user_id.name,
                                'type': 'success',
                                'sticky': False,
                                'next': {'type': 'ir.actions.act_window_close'},
                            }
                        }
                    except Exception as e:
                        _logger.error(f"NGSign: Failed to send signature email: {e}")
                        raise UserError(_("Failed to send signature email: %s") % str(e))
                else:
                    raise UserError(_("Configured email template does not exist. Please check Settings."))
            else:
                raise UserError(_("No email template configured. Please set one in Settings."))

    def _action_restart(self):
        """Cancel the pending transaction and launch the signature again for
        the new signer, on every invoice the transaction contained."""
        self._check_signer(self.signer_id)
        moves = self.move_id.action_ngsign_cancel_transaction()
        return {
            'type': 'ir.actions.client',
            'tag': 'ngsign_einvoice_odoo.action_sign_ngsign_js',
            'context': {
                'ngsign_action_type': 'send' if self.restart_delivery == 'email' else 'sign_now',
                'ngsign_signer_id': self.signer_id.id,
                'ngsign_send_to_user_name': self.signer_id.name,
                'active_ids': moves.ids,
            },
        }
