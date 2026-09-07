from odoo import models, fields, api

class ResPartner(models.Model):
    _inherit = 'res.partner'

    ngsign_validation_message = fields.Html(
        string='e-Invoice Data Check',
        compute='_compute_ngsign_validation_message',
        help="Problems that would prevent this contact from being used on a "
             "Tunisian electronic invoice."
    )

    @api.depends('vat', 'name', 'street', 'city', 'zip', 'country_id', 'email',
                 'customer_rank', 'parent_id')
    def _compute_ngsign_validation_message(self):
        validator = self.env['ngsign.validator']
        for partner in self:
            partner.ngsign_validation_message = False
            # Only customers end up in an e-invoice: do not nag on other contacts.
            if not (partner.customer_rank or partner.parent_id.customer_rank):
                continue
            partner.ngsign_validation_message = validator.issues_to_html(
                validator.validate_record(partner)
            )

    ngsign_notify_owner = fields.Boolean(
        string='Notify Owner (NGSign)', 
        compute='_compute_ngsign_notify_owner', 
        inverse='_inverse_ngsign_notify_owner', 
        store=False,
        help="If checked, the owner will be notified when an invoice is signed via NGSign. (Stored via Tag 'NGSign Notify Owner')"
    )

    def _get_ngsign_tag(self):
        tag_name = 'NGSign Notify Owner'
        tag = self.env['res.partner.category'].search([('name', '=', tag_name)], limit=1)
        if not tag:
            tag = self.env['res.partner.category'].create({'name': tag_name})
        return tag

    @api.depends('category_id')
    def _compute_ngsign_notify_owner(self):
        tag_name = 'NGSign Notify Owner'
        for partner in self:
            partner.ngsign_notify_owner = any(t.name == tag_name for t in partner.category_id)

    def _inverse_ngsign_notify_owner(self):
        tag = self._get_ngsign_tag()
        for partner in self:
            if partner.ngsign_notify_owner:
                partner.category_id = [(4, tag.id)]
            else:
                partner.category_id = [(3, tag.id)]
