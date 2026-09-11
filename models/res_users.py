from odoo import models, fields


class ResUsers(models.Model):
    _inherit = 'res.users'

    ngsign_last_signer_id = fields.Many2one(
        'res.users',
        string='Last NGSign Signer',
        copy=False,
        help="Signer selected the last time this user launched a DigiGO / SSCD "
             "signature. Proposed by default in the signature wizards.",
    )
