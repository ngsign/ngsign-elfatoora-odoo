{
    'name': 'NGSign e-invoice',
    "version": "18.0.1.0.0",
    'category': 'Accounting',
    'summary': 'Integration with NGSign for e-invoicing and SEAL signing',
    'description': """
        This module integrates Odoo with NGSign API to:
        - Generate TEIF compliant e-invoices
        - Sign invoices using NGSign SEAL (Electronic Seal)
        - Automatically enrich PDF with TTN QR code and reference
        - Manage TEIF Tax and Payment Term mappings
        - Debug JSON payload generation
    """,
    'author': 'NGSIGN',
    'depends': ['account'],
    'data': [
        'security/ir.model.access.csv',
        'views/res_config_settings_views.xml',
        'views/ngsign_sign_options_wizard_views.xml',
        'views/ngsign_pds_options_wizard_views.xml',
        'views/ngsign_validation_wizard_views.xml',
        'views/account_move_views.xml',
        'views/product_views.xml',
        'views/account_tax_views.xml',
        'views/account_payment_term_views.xml',
        'views/report_invoice.xml',
        'data/mail_template_data.xml',
        # 'data/ngsign_setup.xml',
        'views/res_partner_views.xml',
        'views/res_company_views.xml',
        'views/ngsign_developer_settings_views.xml',
        'views/ngsign_template_settings_views.xml',
        'views/ngsign_ttn_layout_settings_views.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'ngsign_einvoice_odoo/static/src/js/ngsign_action.js',
        ],
    },
    'installable': True,
    'application': False,
    'license': 'LGPL-3',
}
