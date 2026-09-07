from odoo import models, fields, api


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    ngsign_validation_message = fields.Html(
        string='e-Invoice Data Check',
        compute='_compute_ngsign_validation_message',
        help="Problems that would show up when this product is invoiced "
             "on a Tunisian electronic invoice."
    )

    @api.depends('default_code')
    def _compute_ngsign_validation_message(self):
        validator = self.env['ngsign.validator']
        for product in self:
            product.ngsign_validation_message = validator.issues_to_html(
                validator.validate_record(product)
            )
