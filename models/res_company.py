from odoo import models, fields, api

class ResCompany(models.Model):
    _inherit = 'res.company'

    ngsign_validation_message = fields.Html(
        string='e-Invoice Data Check',
        compute='_compute_ngsign_validation_message',
        help="Problems with the identity of the company for Tunisian electronic invoicing."
    )

    @api.depends('vat', 'name')
    def _compute_ngsign_validation_message(self):
        validator = self.env['ngsign.validator']
        for company in self:
            company.ngsign_validation_message = validator.issues_to_html(
                validator.validate_record(company)
            )

    ngsign_qr_position_type = fields.Selection([
        ('custom', 'Custom Coordinates'),
        ('builtin', 'Builtin Position')
    ], string='QR Code Position Type', compute='_compute_qr_position_type', inverse='_inverse_qr_position_type', store=False)

    def _compute_qr_position_type(self):
        """Read position type from system parameters"""
        IrConfigParameter = self.env['ir.config_parameter'].sudo()
        for record in self:
            param_key = f'ngsign.qr_position_type.company_{record.id}'
            value = IrConfigParameter.get_param(param_key, 'custom')
            record.ngsign_qr_position_type = value

    def _inverse_qr_position_type(self):
        """Write position type to system parameters"""
        IrConfigParameter = self.env['ir.config_parameter'].sudo()
        for record in self:
            param_key = f'ngsign.qr_position_type.company_{record.id}'
            IrConfigParameter.set_param(param_key, record.ngsign_qr_position_type or 'custom')

    ngsign_qr_position_x = fields.Integer(string='TTN QR Position X (mm)', default=10)
    ngsign_qr_position_y = fields.Integer(string='TTN QR Position Y (mm)', default=10)
    ngsign_qr_size = fields.Integer(string='TTN QR Size (mm)', default=30)
    
    # PDF margin offset to compensate for wkhtmltopdf margins (stored in ir.config_parameter)
    ngsign_pdf_margin_offset = fields.Integer(
        string='PDF Top Margin Offset (mm)',
        compute='_compute_pdf_margin_offset',
        inverse='_inverse_pdf_margin_offset',
        store=False,
        help='Offset to compensate for PDF page margins. Increase if QR appears too low, decrease if too high.'
    )
    
    def _compute_pdf_margin_offset(self):
        """Read PDF margin offset from system parameters"""
        IrConfigParameter = self.env['ir.config_parameter'].sudo()
        for record in self:
            param_key = f'ngsign.pdf_margin_offset.company_{record.id}'
            value = IrConfigParameter.get_param(param_key, '40')
            try:
                record.ngsign_pdf_margin_offset = int(value)
            except (ValueError, TypeError):
                record.ngsign_pdf_margin_offset = 40
    
    def _inverse_pdf_margin_offset(self):
        """Write PDF margin offset to system parameters"""
        IrConfigParameter = self.env['ir.config_parameter'].sudo()
        for record in self:
            param_key = f'ngsign.pdf_margin_offset.company_{record.id}'
            IrConfigParameter.set_param(param_key, str(record.ngsign_pdf_margin_offset or 40))
    
    ngsign_label_position_x = fields.Integer(string='TTN Label Position X (mm)', default=150)
    ngsign_label_position_y = fields.Integer(string='TTN Label Position Y (mm)', default=10)
    ngsign_label_width = fields.Integer(string='TTN Label Width (mm)', default=50)
    ngsign_label_text = fields.Char(string='TTN Label Text', default='')
    ngsign_label_font_size = fields.Integer(string='TTN Label Font Size (pt)', default=10)
