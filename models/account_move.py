import base64
import re
import json
import logging
from datetime import datetime, timedelta
from odoo import models, fields, api, _

_logger = logging.getLogger(__name__)
from odoo.exceptions import UserError
from .ngsign_client import NGSignClient
from .ngsign_spec import ERROR as NGSIGN_ERROR, INTERRUPTING_SEVERITIES as NGSIGN_INTERRUPTING

class AccountMove(models.Model):
    _inherit = 'account.move'

    ngsign_transaction_uuid = fields.Char(string='NGSign Transaction UUID', copy=False)
    ngsign_invoice_uuid = fields.Char(string='NGSign Invoice UUID', copy=False, help="Unique UUID for the specific invoice within the transaction")
    ngsign_ttn_reference = fields.Char(string='TTN eInvoice ID', copy=False, help="Unique reference returned by TTN after signing")
    ngsign_ttn_qr_code = fields.Binary(string='TTN QR Code', copy=False, attachment=True)
    ngsign_pds_url = fields.Char(string='NGSign PDS URL', copy=False, help="URL to the Page de Signature for DigiGO/SSCD certificates")
    ngsign_status = fields.Selection([
        ('draft', 'Draft'),
        ('pending_signature', 'Pending Signature'),
        ('pending', 'Pending'),
        ('TTN Signed', 'TTN Signed'),
        ('CANCELLED', 'Cancelled'),
        ('TTN_REJECTED', 'TTN Rejected'),
        ('signed_ngsign', 'Signed'),
        ('error', 'Error')
    ], string='NGSign Status', default='draft', copy=False)
    
    ngsign_notify_owner = fields.Boolean(string='Notify Owner', default=lambda self: self.env['ir.config_parameter'].sudo().get_param('ngsign.notify_owner_default', 'True') == 'True', copy=False)
    ngsign_last_check = fields.Datetime(string='Last NGSign Check', copy=False)
    ngsign_ttn_mode = fields.Selection([
        ('test', 'TEST'),
        ('prod', 'PROD')
    ], string='TTN Mode', copy=False, help="Mode used when signing this invoice")

    ngsign_certificate_type = fields.Selection([
        ('seal', 'SEAL'),
        ('digigo', 'DigiGO'),
        ('sscd', 'SSCD')
    ], string='Certificate Type', compute='_compute_ngsign_certificate_type')

    def _compute_ngsign_certificate_type(self):
        cert_type = self.env['ir.config_parameter'].sudo().get_param('ngsign.certificate_type', 'seal')
        for move in self:
            move.ngsign_certificate_type = cert_type

    def action_delete_test_transaction(self):
        """
        Delete NGSign transaction details and attachments if in TEST mode.
        """
        self.ensure_one()
        if self.ngsign_ttn_mode == 'prod':
             raise UserError(_("You can only delete transactions signed in TEST mode."))
        
        # Clear NGSign fields
        self.write({
            'ngsign_transaction_uuid': False,
            'ngsign_invoice_uuid': False,
            'ngsign_ttn_reference': False,
            'ngsign_ttn_qr_code': False,
            'ngsign_pds_url': False,
            'ngsign_status': 'draft',
            'ngsign_last_check': False,
            'ngsign_ttn_mode': False,
        })
        
        # Delete related attachments
        attachment_names = [
            f"{self.name}_signed.pdf",
            f"{self.name}_signed.xml",
            f"{self.name}_ngsign_prepare.pdf"
        ]
        
        attachments = self.env['ir.attachment'].search([
            ('res_model', '=', 'account.move'),
            ('res_id', '=', self.id),
            ('name', 'in', attachment_names)
        ])
        
        if attachments:
            attachments.unlink()
            
        return True

    def read(self, fields=None, load='_classic_read'):
        """
        Override read to auto-check NGSign status when opening the form view.
        We use a debounce of 60 seconds to avoid excessive API calls.
        """
        # Only check if reading a single record (likely form view)
        if len(self) == 1:
            try:
                # Check eligibility: Not draft, cancelled, or fully signed (TTN Signed)
                # We want to check for pending, signed (NGSign only), error, TTN_REJECTED
                if self.ngsign_status not in ('draft', 'CANCELLED', 'TTN Signed'):
                    should_check = False
                    if not self.ngsign_last_check:
                        should_check = True
                    else:
                        # Check if last check was more than 60 seconds ago.
                        # NB: `fields` is the read() argument here, it shadows the
                        # odoo.fields module - use datetime directly.
                        diff = datetime.now() - self.ngsign_last_check
                        if diff > timedelta(seconds=60):
                            should_check = True
                    
                    if should_check:
                        # Run status check
                        # We use a new cursor or try/except to ensure read doesn't fail if API fails
                        # But action_check_ngsign_status already handles errors gracefully (logs them)
                        self.action_check_ngsign_status()
            except Exception as e:
                _logger.warning(f"Auto-check NGSign status failed in read(): {e}")

        return super(AccountMove, self).read(fields, load)

    @api.onchange('partner_id')
    def _onchange_partner_id_ngsign(self):
        if self.partner_id:
            self.ngsign_notify_owner = self.partner_id.ngsign_notify_owner

    ngsign_show_debug_json_button = fields.Boolean(compute='_compute_ngsign_developer_settings')
    ngsign_show_transaction_uuid = fields.Boolean(compute='_compute_ngsign_developer_settings')
    ngsign_show_invoice_uuid = fields.Boolean(compute='_compute_ngsign_developer_settings')

    def _compute_ngsign_developer_settings(self):
        params = self.env['ir.config_parameter'].sudo()
        show_debug = params.get_param('ngsign.show_debug_json_button', 'False') == 'True'
        show_trans_uuid = params.get_param('ngsign.show_transaction_uuid', 'False') == 'True'
        show_inv_uuid = params.get_param('ngsign.show_invoice_uuid', 'False') == 'True'
        
        for move in self:
            move.ngsign_show_debug_json_button = show_debug
            move.ngsign_show_transaction_uuid = show_trans_uuid
            move.ngsign_show_invoice_uuid = show_inv_uuid

    def _ngsign_payload_lines(self):
        """Invoice lines actually sent to NGSign (sections and notes excluded).

        Single source of truth so that the payload builder and the validator
        always talk about the same lines (and the same line numbers).
        """
        self.ensure_one()
        return self.invoice_line_ids.filtered(
            lambda l: l.display_type not in ('line_section', 'line_note')
        )

    def _ngsign_resolve_bank_account(self):
        """Bank account sent in the e-invoice.

        Priority: 1. invoice bank account, 2. journal bank account,
                  3. first bank account of the company.
        """
        self.ensure_one()
        # sudo() so the details can be read whatever the user's permissions are
        bank_account = self.partner_bank_id.sudo()

        if not bank_account and self.journal_id.bank_account_id:
            bank_account = self.journal_id.bank_account_id.sudo()

        if not bank_account:
            company_banks = self.company_id.partner_id.bank_ids.sudo()
            if company_banks:
                bank_account = company_banks[0]

        return bank_account

    def _get_ngsign_client(self):
        params = self.env['ir.config_parameter'].sudo()
        api_url = params.get_param('ngsign.api_einvoice_url')
        token = params.get_param('ngsign.bearer_token')
        
        _logger.info(f"NGSign Config Debug: api_url='{api_url}', token_len={len(token) if token else 0}, token_set={bool(token)}")

        missing_params = []
        if not api_url:
            missing_params.append("API URL")
        if not token:
            missing_params.append("Bearer Token")
            
        if missing_params:
            raise UserError(_("NGSign configuration is missing: %s. Please check Settings.") % ", ".join(missing_params))
            
        return NGSignClient(api_url, token)

    def get_ngsign_print_config(self):
        """Get print configuration for NGSign overlay"""
        self.ensure_one()
        config = self.env['ir.config_parameter'].sudo()
        
        # Allow context override for preview wizard
        preview_config = self.env.context.get('ngsign_preview_config')
        if preview_config:
             return preview_config

        company = self.company_id
        use_v2 = self.env['ir.config_parameter'].sudo().get_param('ngsign.use_v2_endpoint', 'False') == 'True'
        
        return {
            'use_v2': use_v2,
            'qr_position_type': company.ngsign_qr_position_type,
            'qr_x': company.ngsign_qr_position_x,
            'qr_y': company.ngsign_qr_position_y,
            'qr_size': company.ngsign_qr_size,
            'pdf_margin_offset': company.ngsign_pdf_margin_offset,
            'label_x': company.ngsign_label_position_x,
            'label_y': company.ngsign_label_position_y,
            'label_width': company.ngsign_label_width,
            'label_text': company.ngsign_label_text,
            'label_font_size': company.ngsign_label_font_size,
            'show_debug_info': self.env['ir.config_parameter'].sudo().get_param('ngsign.show_report_debug_info', 'False') == 'True',
        }
    
    def get_ttn_qr_code_base64(self):
        """
        Returns the TTN QR code.
        Priority:
        1. The QR code image returned by the API (stored in ngsign_ttn_qr_code).
        2. Fallback: Generate one on-the-fly using the reference (only if API image is missing).
        """
        self.ensure_one()
        
        # 1. Try to use the API provided QR code (stored as binary)
        if self.ngsign_ttn_qr_code:
            try:
                # ngsign_ttn_qr_code is a binary field, so it's already base64 encoded bytes in Odoo generally,
                # but sometimes it reads as bytes. We need to ensure we return a string for the report.
                if isinstance(self.ngsign_ttn_qr_code, bytes):
                    return self.ngsign_ttn_qr_code.decode('utf-8')
                return self.ngsign_ttn_qr_code
            except Exception as e:
                import logging
                _logger = logging.getLogger(__name__)
                _logger.warning(f"Failed to decode stored QR code: {e}")
        
        # 2. Fallback: Generate on-the-fly
        if not self.ngsign_ttn_reference:
            return False
        
        try:
            import qrcode
            import io
            import base64
            
            qr = qrcode.QRCode(version=1, box_size=10, border=4)
            qr.add_data(self.ngsign_ttn_reference)
            qr.make(fit=True)
            
            img = qr.make_image(fill_color="black", back_color="white")
            buffer = io.BytesIO()
            img.save(buffer, format='PNG')
            buffer.seek(0)
            
            return base64.b64encode(buffer.getvalue()).decode('utf-8')
        except Exception as e:
            import logging
            _logger = logging.getLogger(__name__)
            _logger.error(f"Error generating QR code: {e}")
            return False

    def _prepare_ngsign_invoice_payload(self, include_pdf=True):
        """
        Prepare the payload for NGSign API.
        This maps Odoo invoice data to TEIF format.
        :param include_pdf: Whether to generate/include the PDF content.
        """
        self.ensure_one()
        
        # Check if V2 endpoint is enabled
        use_v2 = self.env['ir.config_parameter'].sudo().get_param('ngsign.use_v2_endpoint', 'False') == 'True'
        
        # If V2 is enabled, we force include_pdf to False for the payload (no PDF upload)
        # But for debug purposes, we might still want to see it if include_pdf is True?
        # The requirement says: "if the option 'Use V2 Seal endpoint' is selected : we will not send the line 'invoiceFileB64': pdf_base64"
        # So we should omit it from the payload.
        
        # Generate PDF report
        pdf_base64 = ""
        # Only generate PDF if include_pdf is True AND V2 is False
        if include_pdf and not use_v2:
            try:
                # Check if we already have a generated PDF attachment (from prepare step)
                attachment_name = f"{self.name}_ngsign_prepare.pdf"
                attachment = self.env['ir.attachment'].search([
                    ('res_model', '=', 'account.move'),
                    ('res_id', '=', self.id),
                    ('name', '=', attachment_name)
                ], limit=1)

                if attachment:
                    pdf_base64 = attachment.datas.decode('utf-8')
                else:
                    # Find report action using search to be safe
                    report_action = self.env['ir.actions.report'].sudo().search([
                        ('model', '=', 'account.move'),
                        ('report_name', 'in', ['account.report_invoice_with_payments', 'account.report_invoice'])
                    ], limit=1)

                    if report_action:
                        # Correctly call _render_qweb_pdf on the model, passing the report record and IDs
                        # Force language to partner's lang or French
                        lang = self.partner_id.lang or 'fr_FR'
                        pdf_content, _ = self.env['ir.actions.report'].with_context(lang=lang).sudo()._render_qweb_pdf(report_action, self.ids)
                        pdf_base64 = base64.b64encode(pdf_content).decode('utf-8')
                    else:
                        pdf_base64 = "REPORT_ACTION_NOT_FOUND"
                    
            except Exception as e:
                # If PDF generation fails, we still want to see the JSON data mapping
                pdf_base64 = f"PDF_GENERATION_FAILED: {str(e)}"
        elif use_v2:
             pdf_base64 = None # Will be omitted in final dict construction if None or we handle it there
        else:
            pdf_base64 = "PDF_CONTENT_OMITTED_BY_DEVELOPER_SETTING"

        # 1. Header Data
        # Map move_type to TEIF codes
        teif_doc_type = 'I-11' # Default Facture
        if self.move_type == 'out_refund':
            teif_doc_type = 'I-12'
        
        # Clean VAT (remove non-numeric characters for check, but keep logic simple as per req)
        # Req: Strip country code (e.g. 'TN') and ensure 7 digits + key.
        partner_vat = self.partner_id.vat or ''
        partner_vat = partner_vat.upper().replace('TN', '').strip()
        # Basic regex validation could be added here or just pass it. 
        # The API might reject it if invalid.
        
        # 2. Items Mapping
        items = []
        # In Odoo 16+, display_type can be 'product'. We want to exclude sections and notes.
        for line in self._ngsign_payload_lines():
            # VAT Rate and Taxes Logic
            # 1. Identify Primary VAT (I-1602)
            # 2. All other taxes (including secondary VATs or non-VATs) go to 'taxes' list
            
            all_taxes = line.tax_ids
            vat_taxes = all_taxes.filtered(lambda t: t.teif_code == 'I-1602')
            taxes_with_codes = all_taxes.filtered(lambda t: t.teif_code)
            
            primary_vat_tax = None
            if vat_taxes:
                primary_vat_tax = vat_taxes[0]
            elif not taxes_with_codes and all_taxes:
                # Fallback: if no codes configured at all, assume first tax is VAT
                primary_vat_tax = all_taxes[0]
            
            vat_rate = primary_vat_tax.amount if primary_vat_tax else 0.0

            # Other taxes (FODEC, DC, etc. OR secondary VATs)
            other_taxes = []
            for tax in all_taxes:
                if tax == primary_vat_tax:
                    continue
                
                # Exclude Stamp Tax (I-1601) as it is handled in stampTax field
                if tax.teif_code == 'I-1601':
                    continue
                
                # Calculate tax amount for this line item
                # This is an approximation. Odoo stores tax amount on the line only if it's price_include=True or we compute it.
                # line.price_total - line.price_subtotal gives total tax. But we need per tax.
                # We can use the tax object to compute it.
                tax_values = tax.compute_all(line.price_unit, currency=line.currency_id, quantity=line.quantity, product=line.product_id, partner=line.partner_id)
                # tax_values['taxes'] is a list of dicts. Find the one matching our tax.id
                current_tax_amount = 0.0
                for t in tax_values['taxes']:
                    if t['id'] == tax.id:
                        current_tax_amount = t['amount']
                        break

                other_taxes.append({
                    'taxTypeName': {
                        'code': tax.teif_code or 'I-1606', # Default to Autre
                        'value': str(round(current_tax_amount, 5)) # Value should be the amount
                    },
                    'taxDetails': {
                        'taxRate': str(tax.amount)
                    }
                })

            # Use the first line of the invoice line name to preserve translations/custom names
            # while stripping the multiline item description from the API payload.
            item_name = line.name or ''
            items.append({
                'name': item_name.split('\n')[0][:500],
                'code': line.product_id.default_code or 'N/A',
                'quantity': line.quantity,
                'unit': (line.product_uom_id.name or 'UNIT')[:7],
                'unitPrice': round(line.price_unit, 5),
                'totalPrice': round(line.price_subtotal, 5),
                'tvaRate': round(vat_rate, 5),
                'currencyIdentifier': self.currency_id.name,
                'taxes': other_taxes,
                'discountPercentage': round(line.discount, 5),
                'discount': round((line.quantity * line.price_unit * line.discount) / 100, 5),
                'service': line.product_id.type == 'service'
            })

        # 3. Totals and Financials
        # Stamp Tax (Timbre)
        stamp_tax_amount = 0.0
        
        # Better approach for Stamp Tax: Look at tax_totals or line_ids if it's a specific line.
        # Often Timbre is a tax.
        # Let's try to find it in the tax lines (account.tax objects don't store the computed amount on the invoice directly easily without inspecting tax_lines)
        # We can iterate over line.tax_ids and if we find I-1601, we calculate the amount.
        # Or use `amount_by_group` if available.
        # For this implementation, let's leave stampTax as 0.600 if we find a tax named 'Timbre' or code I-1601.
        
        stamp_tax = 0.0
        # This is a rough approximation. In real Odoo, check `tax_totals` json or `line_ids` tax amounts.
        # We will assume if any tax has code I-1601, we sum its amount.
        # Since accessing tax amounts per tax type is complex in Odoo 14+ (it's in tax_totals), 
        # we will iterate invoice lines and compute it manually or check tax lines.
        # Let's check `line_ids` (journal items) which are tax lines.
        for tax_line in self.line_ids:
            if tax_line.tax_line_id and tax_line.tax_line_id.teif_code == 'I-1601':
                # tax_line.amount_currency is in invoice currency.
                stamp_tax += abs(tax_line.amount_currency)

        # 4. Payment Details
        # Resolve Bank Account (Unconditionally)
        # Priority: 1. Invoice specific bank (partner_bank_id)
        #           2. Journal's bank account (if set)
        #           3. Company's first bank account
        
        bank_account = self._ngsign_resolve_bank_account()

        _logger.info(f"NGSign Debug: Final Resolved Bank={bank_account}")

        payment_details = []
        if self.invoice_payment_term_id:
            pyt_code = self.invoice_payment_term_id.teif_code or 'I-121' # Default Immediate
            
            # Payment Means (Espèce, Chèque, Virement)
            pai_means_code = 'I-135' # Virement
            
            pyt_fii = None
            if bank_account:
                pyt_fii = {
                    'accountHolder': {
                        'accountNumber': bank_account.acc_number,
                    },
                    'institutionIdentification': {
                        'institutionName': bank_account.bank_id.name or 'Bank',
                    }
                }

            payment_details.append({
                'pyt': {
                    'paiConditionCode': pyt_code,
                    'paymentTearmsDescription': self.invoice_payment_term_id.name
                },
                'pytPai': {
                    'paiConditionCode': pyt_code,
                    'paiMeansCode': pai_means_code
                },
                'pytFii': pyt_fii
            })

        # Clean Company VAT (Issuer)
        company_vat = self.company_id.vat or ''
        company_vat = company_vat.upper().replace('TN', '').strip()

        # Document References
        document_references = []
             
        # 1. Previous Invoice (I-88) for Credit Notes
        ref_value = None
        ref_date = None

        if self.move_type == 'out_refund' and self.reversed_entry_id:
            # We need the TTN reference of the original invoice
            original_ttn_ref = self.reversed_entry_id.ngsign_ttn_reference
            if original_ttn_ref:
                document_references.append({
                    'refID': 'I-88', # Référence TTN
                    'value': original_ttn_ref,
                    'date': self.reversed_entry_id.invoice_date.isoformat() if self.reversed_entry_id.invoice_date else None
                })
            
            # Use original invoice details for I-89
            ref_value = self.reversed_entry_id.name
            ref_date = self.reversed_entry_id.invoice_date

        # 2. Reference Invoice (I-89)
        if ref_value:
            document_references.append({
                'refID': 'I-89', # Référence interne
                'value': ref_value,
                'date': ref_date.isoformat() if ref_date else None
            })

        # Construct TEIF Invoice object
        # Convert date to Unix timestamp (seconds)
        invoice_date_ts = 0
        if self.invoice_date:
            # Convert date to datetime at midnight
            dt = datetime.combine(self.invoice_date, datetime.min.time())
            invoice_date_ts = int(dt.timestamp() * 1000)
        else:
            invoice_date_ts = int(datetime.now().timestamp() * 1000)

        # 5. Global Taxes Aggregation
        # We need to aggregate taxes by code and rate for the global 'taxes' list
        # And calculate global tvaTax and tvaRate
        
        global_taxes_map = {} # Key: (code, rate), Value: {amount, base}
        total_vat_amount = 0.0
        
        # Iterate over invoice lines to aggregate taxes
        for line in self._ngsign_payload_lines():
            price_subtotal = line.price_subtotal
            
            for tax in line.tax_ids:
                # Calculate tax amount for this line
                # This is an approximation if multiple taxes are applied. 
                # Ideally use tax_totals but mapping back to codes is hard.
                # We'll use the tax rate to estimate line tax amount.
                tax_amount = (price_subtotal * tax.amount) / 100
                
                if tax.teif_code == 'I-1602':
                    total_vat_amount += tax_amount
                elif tax.teif_code == 'I-1601':
                    # Skip Stamp Tax in global taxes list as it has its own field
                    continue
                else:
                    key = (tax.teif_code or 'I-1606', str(tax.amount))
                    if key not in global_taxes_map:
                        global_taxes_map[key] = {'amount': 0.0, 'base': 0.0, 'name': tax.name}
                    
                    global_taxes_map[key]['amount'] += tax_amount
                    global_taxes_map[key]['base'] += price_subtotal

        global_taxes_list = []
        for (code, rate), data in global_taxes_map.items():
            global_taxes_list.append({
                'code': code,
                'taxRate': rate,
                'amount': f"{data['amount']:.5f}",
                'amountBase': f"{data['base']:.5f}"
            })

        # Determine global VAT rate (if uniform)
        # If multiple VAT rates exist, what to put? Example says 0.0.
        # Let's check if we have a single VAT rate.
        vat_rates = self.invoice_line_ids.mapped('tax_ids').filtered(lambda t: t.teif_code == 'I-1602').mapped('amount')
        global_tva_rate = vat_rates[0] if len(set(vat_rates)) == 1 else 0.0

        teif_invoice = {
            'documentIdentifier': self.name,
            'invoiceDate': invoice_date_ts,
            'documentType': teif_doc_type,
            'documentReferences': document_references,
            'clientIdentifier': partner_vat, # Customer Tax ID
            'currencyIdentifier': self.currency_id.name,
            'comments': [self.narration] if self.narration else [],
            'accountNumber': bank_account.acc_number if bank_account else None,
            'institutionName': bank_account.bank_id.name if bank_account and bank_account.bank_id else None,
            
            'clientDetails': {
                'partnerIdentifier': partner_vat, # Customer Tax ID
                'partnerName': self.partner_id.parent_id.name if self.partner_id.parent_id else self.partner_id.name,
                'address': {
                    'description': self.partner_id.contact_address.replace('\n', ' ') if self.partner_id.contact_address else '',
                    'street': self.partner_id.street,
                    'cityName': self.partner_id.city,
                    'postalCode': self.partner_id.zip,
                    'country': self.partner_id.country_id.code if self.partner_id.country_id else 'TN'
                }
            },
            
            'items': items,
            'taxes': global_taxes_list,
            
            'invoiceTotalWithoutTax': round(self.amount_untaxed, 5),
            'invoiceTotalWithTax': round(self.amount_total, 5),
            'invoiceTotalTax': round(self.amount_tax, 5),
            'stampTax': round(stamp_tax, 5),
            'tvaRate': round(global_tva_rate, 5),
            'tvaTax': round(total_vat_amount, 5),
            'invoiceTotalinLetters': self.currency_id.with_context(lang='fr_FR').amount_to_text(self.amount_total),
            
            'paymentDetails': payment_details
        }

        # Construct NGInvoiceUpload object
        # Fetch configuration from settings
        params = self.env['ir.config_parameter'].sudo()
        
        invoice_upload = {
            'clientEmail': self.partner_id.email or '',
            'invoiceTIEF': teif_invoice,
            'configuration': {
                'qrPositionX': int(params.get_param('ngsign.qr_position_x', 10)),
                'qrPositionY': int(params.get_param('ngsign.qr_position_y', 10)),
                'qrPositionP': int(params.get_param('ngsign.qr_position_p', 0)),
                'qrRatio': float(params.get_param('ngsign.qr_ratio', 0.5)),
                'textPositionX': int(params.get_param('ngsign.text_position_x', 40)),
                'textPositionY': int(params.get_param('ngsign.text_position_y', 40)),
                'textPage': int(params.get_param('ngsign.text_page', 0)),
                'labelPositionX': int(params.get_param('ngsign.label_position_x', 150)),
                'labelPositionY': int(params.get_param('ngsign.label_position_y', 10)),
                'labelPositionP': int(params.get_param('ngsign.label_position_p', 0)),
                'allPages': params.get_param('ngsign.all_pages', 'False') == 'True'
            }
        }
        
        if pdf_base64 is not None:
            invoice_upload['invoiceFileB64'] = pdf_base64
        
        return invoice_upload

    # ------------------------------------------------------------------
    # e-Invoice data validation
    # ------------------------------------------------------------------

    ngsign_validation_message = fields.Html(
        string='e-Invoice Data Check',
        compute='_compute_ngsign_validation_message',
        help="Problems detected in the data that would be sent to NGSign."
    )
    ngsign_validation_has_errors = fields.Boolean(compute='_compute_ngsign_validation_message')

    @api.depends('partner_id', 'invoice_line_ids', 'invoice_date', 'name', 'state',
                 'invoice_payment_term_id', 'partner_bank_id', 'narration', 'currency_id',
                 'ngsign_status')
    def _compute_ngsign_validation_message(self):
        for move in self:
            move.ngsign_validation_message = False
            move.ngsign_validation_has_errors = False
            if move.move_type not in ('out_invoice', 'out_refund'):
                continue
            # Once sent, the data is frozen on NGSign side: nothing to fix here.
            if move.ngsign_status in ('pending', 'pending_signature', 'signed_ngsign',
                                      'TTN Signed', 'CANCELLED'):
                continue
            if not move.id or not move.partner_id:
                continue
            try:
                issues = self.env['ngsign.validator'].validate_move(move)
            except Exception as e:  # pragma: no cover - the banner must never break the form
                _logger.warning("NGSign: validation banner failed for %s: %s", move.name, e)
                continue
            move.ngsign_validation_message = self.env['ngsign.validator'].issues_to_html(issues)
            move.ngsign_validation_has_errors = self.env['ngsign.validator'].has_errors(issues)

    def action_ngsign_check_data(self):
        """Open the detailed e-invoice data check for the selected invoices."""
        if not self:
            return False
        return self.env['ngsign.validation.result'].open_for_moves(self)

    def action_ngsign_check_before_send(self):
        """Pre-flight check called by the client action BEFORE rendering the PDFs.

        Returns the check wizard when the data must be looked at, otherwise
        False, meaning "go ahead". Checking first avoids rendering PDFs (and
        leaving them behind as attachments) for invoices that will not be sent.
        """
        return self._ngsign_validation_action()

    def _ngsign_validation_action(self, resume_signature=True):
        """Return the wizard action if the data must be reviewed, else False.

        Purely informational issues do not interrupt a signature: they are shown
        on the invoice, on the contact and through the "Check e-Invoice Data"
        button, but they are not a reason to stop the user.
        """
        if self.env.context.get('ngsign_skip_validation'):
            return False
        issues = self.env['ngsign.validator'].validate_moves(self)
        if not any(issue['severity'] in NGSIGN_INTERRUPTING for issue in issues):
            return False
        return self.env['ngsign.validation.result'].open_for_moves(
            self, issues=issues, resume_signature=resume_signature
        )

    def _validate_partner_data_for_ngsign(self):
        """Deprecated: kept for backward compatibility.

        The full check now lives in the ``ngsign.validator`` model; this wrapper
        only raises on blocking problems, as it used to.
        """
        self.ensure_one()
        validator = self.env['ngsign.validator']
        issues = [i for i in validator.validate_move(self) if i['severity'] == NGSIGN_ERROR]
        if issues:
            raise UserError(_(
                "Cannot generate the e-invoice for %(invoice)s.\n\n"
                "The following data is missing or invalid:\n\n%(issues)s"
            ) % {'invoice': self.name, 'issues': validator.issues_to_text(issues)})

    def _ngsign_cleanup_prepared_pdf(self):
        """Remove the PDFs rendered by the prepare step for these invoices."""
        for move in self:
            self.env['ir.attachment'].search([
                ('res_model', '=', 'account.move'),
                ('res_id', '=', move.id),
                ('name', '=', f"{move.name}_ngsign_prepare.pdf"),
            ]).unlink()

    def action_ngsign_prepare(self):
        """
        Step 1 of signing process: Generate PDFs and store them as attachments.
        This is called by the JS client action before sending.
        """
        for move in self:
            try:
                # Find report action
                report_action = self.env['ir.actions.report'].sudo().search([
                    ('model', '=', 'account.move'),
                    ('report_name', 'in', ['account.report_invoice_with_payments', 'account.report_invoice'])
                ], limit=1)

                if report_action:
                    lang = move.partner_id.lang or 'fr_FR'
                    pdf_content, _format = self.env['ir.actions.report'].with_context(lang=lang).sudo()._render_qweb_pdf(report_action, move.ids)
                    
                    # Save as temporary attachment
                    attachment_name = f"{move.name}_ngsign_prepare.pdf"
                    
                    # Remove old attachment if exists
                    old_attachment = self.env['ir.attachment'].search([
                        ('res_model', '=', 'account.move'),
                        ('res_id', '=', move.id),
                        ('name', '=', attachment_name)
                    ])
                    old_attachment.unlink()
                    
                    self.env['ir.attachment'].create({
                        'name': attachment_name,
                        'type': 'binary',
                        'datas': base64.b64encode(pdf_content),
                        'res_model': 'account.move',
                        'res_id': move.id,
                        'mimetype': 'application/pdf'
                    })
            except Exception as e:
                _logger.error(f"Failed to prepare PDF for invoice {move.name}: {e}")
                raise UserError(_("Failed to prepare PDF for invoice %s: %s") % (move.name, str(e)))
        return True

    def action_ngsign_send(self):
        """
        Step 2 of signing process: Send invoices to NGSign.
        This is called by the JS client action after preparation.
        Supports both SEAL (automatic) and DigiGO/SSCD (manual) certificate types.
        """
        # Support bulk signing
        if not self:
            return
        
        # Guard against a batch selected in a generic journal entry list: the
        # bulk action is bound to account.move as a whole.
        not_invoices = self.filtered(lambda m: m.move_type not in ('out_invoice', 'out_refund'))
        if not_invoices:
            raise UserError(_(
                "Only customer invoices and credit notes can be sent to NGSign.\n"
                "The following documents are not: %s"
            ) % ', '.join(not_invoices.mapped('display_name')))

        # Check the data that is about to be sent. When something is wrong the
        # user gets the detailed wizard instead of an API rejection; warnings
        # can be acknowledged from there ("Sign Anyway").
        validation_action = self._ngsign_validation_action()
        if validation_action:
            # Nothing is sent: drop the PDFs prepared for this attempt rather
            # than leaving them attached to the invoices.
            self._ngsign_cleanup_prepared_pdf()
            return validation_action

        client = self._get_ngsign_client()
        params = self.env['ir.config_parameter'].sudo()
        
        # Get certificate type
        cert_type = params.get_param('ngsign.certificate_type', 'seal')
        
        # Validate passphrase for SEAL certificates
        if cert_type == 'seal':
            passphrase = params.get_param('ngsign.passphrase')
            if not passphrase:
                raise UserError(_("SEAL Passphrase is missing in Settings."))
        
        # Get TTN Mode
        ttn_mode = params.get_param('ngsign.ttn_mode', 'test')

        try:
            invoices_payload = []
            cc_email = None
            notify_owner = False
            
            # Prepare payload for each invoice
            for move in self:
                # This will now use the attachment created in prepare step
                invoice_payload = move._prepare_ngsign_invoice_payload()
                invoices_payload.append(invoice_payload)
                
                # Logic for CC Email and Notify Owner for the batch
                if not cc_email:
                    invoice_contact = move.partner_id.child_ids.filtered(lambda p: p.type == 'invoice')
                    if invoice_contact:
                        cc_email = invoice_contact[0].email
                
                if move.ngsign_notify_owner:
                    notify_owner = True

            # Route to appropriate endpoint based on certificate type
            if cert_type == 'seal':
                # SEAL Certificate - Automatic signing
                use_v2 = params.get_param('ngsign.use_v2_endpoint', 'False') == 'True'
                
                response = client.create_transaction_seal(
                    invoices_payload, 
                    passphrase, 
                    notify_owner=notify_owner,
                    cc_email=cc_email,
                    use_v2=use_v2
                )
                
                # Update status to pending (automatic signing in progress)
                target_status = 'pending'
                pds_url = None
                
            else:
                # DigiGO or SSCD Certificate - Manual signing via PDS
                action_type = self.env.context.get('ngsign_action_type', 'sign_now')
                if action_type == 'send':
                    send_to_user_id = self.env.context.get('ngsign_send_to_user_id')
                    user = self.env['res.users'].browse(send_to_user_id)
                    signer_email = user.email
                else:
                    signer_email = self.env.user.email

                # Always use advanced endpoint (v2)
                response = client.create_transaction_advanced(
                    invoices_payload,
                    signer_email=signer_email,
                    cc_email=cc_email
                )
                
                # Generate PDS URL for user to complete signing
                _logger.info(f"NGSign Debug: Response type={type(response)}, content={response}")
                if not isinstance(response, dict):
                    raise UserError(_("Unexpected response from NGSign API: %s") % str(response))

                response_data = response.get('object', {})
                if not isinstance(response_data, dict):
                    msg = response.get('message', str(response_data))
                    raise UserError(_("NGSign API Error: %s") % msg)

                transaction_uuid = response_data.get('uuid')
                pds_base_url = params.get_param('ngsign.pds_base_url', 'https://sandbox.ng-sign.com/pdsv2/#/invoice/')
                pds_url = client.generate_pds_url(transaction_uuid, pds_base_url)
                
                # Update status to pending_signature (awaiting user action)
                target_status = 'pending_signature'
            
            # The API returns a wrapper { "object": { ... }, "errorCode": ... }
            if not isinstance(response, dict):
                 # This check handles the SEAL case or if the previous check was missed
                 raise UserError(_("Unexpected response from NGSign API (Global Check): %s - Type: %s") % (str(response), type(response)))
            
            response_data = response.get('object', {})
            if not isinstance(response_data, dict):
                 msg = response.get('message', str(response_data))
                 raise UserError(_("NGSign API Error: %s") % msg)

            transaction_uuid = response_data.get('uuid')
            invoices_data = response_data.get('invoices', [])
            
            # Update moves individually with their specific Invoice UUID and the common Transaction UUID
            for move in self:
                matched_inv = None
                # Try to match by invoiceNumber
                for inv in invoices_data:
                    if inv.get('invoiceNumber') == move.name:
                        matched_inv = inv
                        break
                
                # Fallback: if only 1 invoice and 1 move, assume match
                if not matched_inv and len(self) == 1 and len(invoices_data) == 1:
                    matched_inv = invoices_data[0]
                
                if matched_inv:
                    write_vals = {
                        'ngsign_transaction_uuid': transaction_uuid,
                        'ngsign_invoice_uuid': matched_inv.get('uuid'),
                        'ngsign_status': target_status,
                        'ngsign_ttn_mode': ttn_mode
                    }
                    
                    if pds_url:
                        write_vals['ngsign_pds_url'] = pds_url
                    
                    move.write(write_vals)
                else:
                    _logger.warning(f"NGSign: Could not find UUID for invoice {move.name}")
                    move.write({'ngsign_status': 'error'})
            
            # Cleanup temporary attachments
            self._ngsign_cleanup_prepared_pdf()
            
            # For DigiGO/SSCD, return action to open PDS URL or send email
            _logger.info(f"NGSign: Checking redirection - cert_type={cert_type}, pds_url={pds_url}")
            if cert_type in ('digigo', 'sscd') and pds_url:
                action_type = self.env.context.get('ngsign_action_type')
                
                if action_type == 'send':
                    # Send email with PDS URL
                    send_to_user_id = self.env.context.get('ngsign_send_to_user_id')
                    try:
                        user = self.env['res.users'].browse(send_to_user_id)
                        template_id_str = params.get_param('ngsign.email_template_id')
                        if template_id_str:
                            template = self.env['mail.template'].browse(int(template_id_str))
                            if template.exists():
                                for move in self:
                                    template.sudo().with_context(
                                        ngsign_pds_url=move.ngsign_pds_url,
                                        ngsign_send_to_user_name=user.name
                                    ).send_mail(move.id, force_send=True, email_values={'email_to': user.email})
                                _logger.info(f"NGSign: Sent signature request email to {user.email}")
                            else:
                                _logger.warning("NGSign: Configured email template does not exist.")
                        else:
                            _logger.warning("NGSign: No email template configured for signature requests.")
                    except Exception as e:
                        _logger.error(f"NGSign: Failed to send signature email: {e}")
                    
                    return True # Do not open URL locally
                else:
                    return {
                        'type': 'ir.actions.act_url',
                        'url': pds_url,
                        'target': 'new',
                    }
                
        except Exception as e:
            self.write({'ngsign_status': 'error'})
            
            error_msg = str(e)
            
            # Check if debug is enabled
            enable_debug = self.env['ir.config_parameter'].sudo().get_param('ngsign.enable_debug_button')
            if enable_debug and hasattr(e, 'response') and e.response is not None:
                try:
                    # Extract response details
                    response_body = e.response.text
                    request_url = e.response.url
                    status_code = e.response.status_code
                    
                    # Extract request details
                    request_body = "N/A"
                    if hasattr(e, 'request') and e.request is not None and e.request.body:
                        request_body = e.request.body
                        # Try to pretty print if it's JSON
                        try:
                            if isinstance(request_body, bytes):
                                request_body = request_body.decode('utf-8')
                            request_json = json.loads(request_body)
                            request_body = json.dumps(request_json, indent=4, ensure_ascii=False)
                        except:
                            pass
                    
                    debug_info = f"\n\n--- NGSign Debug Info ---\nURL: {request_url}\nStatus: {status_code}\nRequest Payload:\n{request_body}\n\nResponse:\n{response_body}"
                    error_msg += debug_info
                except Exception as debug_e:
                    error_msg += f"\n(Failed to extract debug info: {debug_e})"
            
            raise UserError(_("Failed to sign invoice(s): %s") % error_msg)

    def action_sign_ngsign(self):
        # Deprecated: Kept for backward compatibility if needed, but UI now calls JS action
        # We can just redirect to send, but prepare step is skipped if called directly.
        # Ideally this should not be called anymore from UI.
        self.action_ngsign_prepare()
        return self.action_ngsign_send()

    def action_open_pds(self):
        """
        Open the Page de Signature (PDS) URL in a new browser window.
        Used for DigiGO and SSCD certificates.
        """
        self.ensure_one()
        
        if not self.ngsign_pds_url:
            raise UserError(_("No PDS URL found. Please sign the invoice first."))
        
        return {
            'type': 'ir.actions.act_url',
            'url': self.ngsign_pds_url,
            'target': 'new',
        }

    def action_check_ngsign_status(self):
        self.ensure_one()
        
        # Update last check time
        self.ngsign_last_check = fields.Datetime.now()
        
        # We need at least one UUID. Preferably transaction UUID for public check.
        if not self.ngsign_transaction_uuid:
            return

        client = self._get_ngsign_client()
        try:
            # Try public endpoint first (transaction level)
            invoice_data = None
            try:
                response = client.get_transaction_status_public(self.ngsign_transaction_uuid)
                transaction_data = response.get('object', {})
                invoices = transaction_data.get('invoices', [])
                
                # Find the invoice matching this record
                
                # 1. Try matching by Invoice UUID if we have it
                if self.ngsign_invoice_uuid:
                    for inv in invoices:
                        if inv.get('uuid') == self.ngsign_invoice_uuid:
                            invoice_data = inv
                            break
                
                # 2. If not found or no Invoice UUID, try matching by invoiceNumber (name)
                if not invoice_data:
                    for inv in invoices:
                        if inv.get('invoiceNumber') == self.name:
                            invoice_data = inv
                            break
                
                # 3. Fallback: if only 1 invoice in transaction, assume it's this one
                if not invoice_data and len(invoices) == 1:
                    invoice_data = invoices[0]

            except Exception as e:
                _logger.warning(f"NGSign: Public status check failed: {e}")
                # Fallback will be handled below

            # Fallback: Try protected single invoice check if we have invoice UUID and no data yet
            if not invoice_data and self.ngsign_invoice_uuid:
                 try:
                    _logger.info(f"NGSign: Trying protected check for invoice {self.ngsign_invoice_uuid}")
                    response = client.check_status(self.ngsign_invoice_uuid)
                    invoice_data = response.get('object', {})
                 except Exception as e:
                    _logger.warning(f"NGSign: Protected status check failed: {e}")

            if not invoice_data:
                _logger.warning(f"NGSign: Could not find invoice {self.name} (UUID: {self.ngsign_invoice_uuid}) in transaction {self.ngsign_transaction_uuid}")
                return

            status = invoice_data.get('status')
            invoice_uuid = invoice_data.get('uuid')
            ttn_error = invoice_data.get('ttnErrorMessage')

            if status == 'TTN_SIGNED':
                self.ngsign_status = 'TTN Signed'
                self.ngsign_ttn_reference = invoice_data.get('ttnReference')
                
                # Save TTN QR Code
                qr_code_data = invoice_data.get('twoDocImage') or invoice_data.get('qrCode')
                if qr_code_data:
                    try:
                        if isinstance(qr_code_data, list):
                            self.ngsign_ttn_qr_code = base64.b64encode(bytes(qr_code_data))
                        else:
                            self.ngsign_ttn_qr_code = qr_code_data
                    except Exception as e:
                        _logger.warning(f"Failed to process TTN QR Code: {e}")
                
                # Download signed files
                try:
                    # PDF
                    pdf_content = client.download_pdf(invoice_uuid)
                    self.env['ir.attachment'].create({
                        'name': f"{self.name}_signed.pdf",
                        'type': 'binary',
                        'datas': base64.b64encode(pdf_content),
                        'res_model': 'account.move',
                        'res_id': self.id,
                        'mimetype': 'application/pdf'
                    })
                    
                    # XML
                    xml_content = client.download_xml(invoice_uuid)
                    self.env['ir.attachment'].create({
                        'name': f"{self.name}_signed.xml",
                        'type': 'binary',
                        'datas': base64.b64encode(xml_content),
                        'res_model': 'account.move',
                        'res_id': self.id,
                        'mimetype': 'application/xml'
                    })
                except Exception as e:
                    _logger.error(f"Failed to download signed files: {e}")
                    self.message_post(body=_("Invoice signed but failed to download files: %s") % str(e))
                
            elif status == 'TTN_REJECTED':
                self.ngsign_status = 'TTN_REJECTED'
                msg = _("NGSign signing failed/rejected. Status: %s") % status
                if ttn_error:
                    msg += f"\nTTN Error: {ttn_error}"
                self.message_post(body=msg)
            elif status == 'CANCELLED':
                self.ngsign_status = 'CANCELLED'
                msg = _("NGSign transaction cancelled. Status: %s") % status
                if ttn_error:
                    msg += f"\nTTN Error: {ttn_error}"
                self.message_post(body=msg)
            elif status == 'SIGNED':
                 # Signed by NGSign but not yet by TTN
                 self.ngsign_status = 'signed_ngsign'
                 if ttn_error:
                     self.message_post(body=_("NGSign Signed but TTN Error: %s") % ttn_error)
            else:
                # Still pending or processing
                pass
                
        except Exception as e:
            raise UserError(_("Failed to check status: %s") % str(e))

    def action_generate_debug_json(self):
        """
        Generate a debug JSON payload for the current invoice(s).
        """
        if not self:
            return

        cert_type = self.env['ir.config_parameter'].sudo().get_param('ngsign.certificate_type', 'seal')
        if cert_type in ('digigo', 'sscd') and not self.env.context.get('ngsign_action_type'):
            return {
                'type': 'ir.actions.act_window',
                'name': _('Debug JSON Signature Options'),
                'res_model': 'ngsign.sign.options.wizard',
                'view_mode': 'form',
                'target': 'new',
                'context': dict(self.env.context, ngsign_is_debug=True, active_id=self[0].id, active_ids=self.ids)
            }

        try:
            invoices_payload = []
            cc_email = None
            notify_owner = False
            
            # Check if we should include PDF content
            include_pdf = self.env['ir.config_parameter'].sudo().get_param('ngsign.debug_include_pdf', 'False') == 'True'
            
            for move in self:
                invoice_payload = move._prepare_ngsign_invoice_payload(include_pdf=include_pdf)
                invoices_payload.append(invoice_payload)
                
                if not cc_email:
                    invoice_contact = move.partner_id.child_ids.filtered(lambda p: p.type == 'invoice')
                    if invoice_contact:
                        cc_email = invoice_contact[0].email
                
                if move.ngsign_notify_owner:
                    notify_owner = True

            # Construct the full transaction payload as it would be sent to the API
            passphrase = self.env['ir.config_parameter'].sudo().get_param('ngsign.passphrase') or "DUMMY_PASSPHRASE"
            
            full_payload = {
                'invoices': invoices_payload,
                'passphrase': passphrase,
                'notifyOwner': notify_owner,
            }

            # Not part of the API payload: the result of the local data check,
            # so the JSON is self-explanatory when sent to support.
            validator = self.env['ngsign.validator']
            full_payload['_validation'] = [
                {
                    'severity': issue['severity'],
                    'invoice': issue['move_name'],
                    'where': issue['context_label'],
                    'field': issue['label'],
                    'apiField': issue['path'],
                    'problem': issue['message'],
                    'record': issue['res_name'],
                }
                for issue in validator.validate_moves(self)
            ]
            if cc_email:
                full_payload['ccEmail'] = cc_email

            if cert_type in ('digigo', 'sscd'):
                action_type = self.env.context.get('ngsign_action_type', 'sign_now')
                if action_type == 'send':
                    send_to_user_id = self.env.context.get('ngsign_send_to_user_id')
                    if send_to_user_id:
                        signer_email = self.env['res.users'].browse(send_to_user_id).email
                    else:
                        signer_email = self.env.user.email
                else:
                    signer_email = self.env.user.email
                full_payload['signerEmail'] = signer_email
            
            json_data = json.dumps(full_payload, indent=4, default=str, ensure_ascii=False)
            
            # Create attachment
            name_suffix = self[0].name if len(self) == 1 else f"bulk_{len(self)}"
            attachment = self.env['ir.attachment'].create({
                'name': f'ngsign_debug_{name_suffix}.json',
                'type': 'binary',
                'datas': base64.b64encode(json_data.encode('utf-8')),
                'mimetype': 'application/json',
                # 'res_model': 'account.move', # Don't attach to the record to avoid clutter
                # 'res_id': self[0].id,
            })
            
            return {
                'type': 'ir.actions.act_url',
                'url': f'/web/content/{attachment.id}?download=true',
                'target': 'self',
            }
        except Exception as e:
            raise UserError(_("Failed to generate debug JSON: %s") % str(e))
