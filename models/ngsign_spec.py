"""Declarative specification of the NGSign / TEIF e-invoice payload.

This module is the SINGLE SOURCE OF TRUTH for the constraints enforced by the
NGSign e-invoice API (TEIFInvoice JSON schema) and by TTN.

It is transcribed from:
  * the ``TEIFInvoice`` JSON schema published by NGSign (maxLength / minLength /
    required / pattern),
  * ``docs/NGSign_WS_Elfatoora_v2.36_1.0.md`` (field semantics and enumerations).

Each rule describes ONE leaf of the payload built by
``account.move._prepare_ngsign_invoice_payload()`` and, when possible, where
that value comes from in Odoo, so the validator can tell the user *which record
and which field* to fix and open it directly.

Rule keys
---------
code            unique technical id of the rule (used for tests / overrides)
path            dotted path inside the payload. ``[]`` expands a list, e.g.
                ``invoiceTIEF.items[].name``
label           human readable name of the field, wrapped in the lazy translation
                helper ``_lt`` so it is extracted by ``--i18n-export`` and rendered
                in the language of the user reading it
record          which Odoo record the value originates from, see
                ``ngsign.validator._resolve_record``: move / partner / company /
                line / product / bank / payment_term
source_model    Odoo model holding the source value (used by the per-form checks)
source_field    field name on ``source_model`` (shown to the user)
required        the API rejects the payload when the value is empty
min_length      minimum string length
max_length      maximum string length
pattern         regular expression the value must FULLY match
value_type      'number' | 'integer' | 'string'
severity        'error' (the only level that blocks signing), 'warning' (sent but
                altered or suspicious) or 'info' (nothing to fix, just to check)
missing_label   optional short name used INSTEAD of ``label`` when the value is
                simply absent, so the list of issues can name the two cases
                differently ("Missing Tax ID" vs "Invalid Tunisian Tax ID")
missing_message optional wording replacing the generic "The value is missing.";
                may contain ``%(field)s``
hint            what the user should do about it
truncated       True when the payload builder already truncates the value; the
                check is then run against the *source* value and downgraded to a
                warning, because data is silently lost rather than rejected.

Known deviations between the module payload and the JSON schema
---------------------------------------------------------------
The JSON schema shipped with this repo (``TEIFInvoice``) and the v2.36 web
service documentation disagree on a few structures. The payload builder follows
v2.36, which is what the production API accepts today, so the rules below follow
v2.36 too:

  * ``items[].taxes[]`` — v2.36: ``{taxTypeName: {code, value}, taxDetails:
    {taxRate}}``; JSON schema: ``{code, taxRate, amount, amountBase}``.
  * ``paymentDetails[].pyt`` — v2.36: ``paiConditionCode``; JSON schema:
    ``paymentTearmsTypeCode``.
  * ``documentReferences[].date`` — v2.36: a date; JSON schema: a ``Dtm``
    object ``{dateCode, date}``.
  * ``paymentDetails[].pytFii`` — the JSON schema marks ``functionCode``
    (I-141..I-143) and ``institutionIdentification.nameCode`` as required; the
    builder does not send them.

These are payload-construction concerns, not user data problems, so they are
deliberately NOT reported to the end user.
"""

from odoo.tools.translate import LazyTranslate

# Labels and hints are declared here, outside of any request, so they use the
# lazy flavour of the translation helper: the lookup happens when the string is
# rendered, in the language of the user reading it.
_lt = LazyTranslate(__name__)

# Severity levels. ONLY ``ERROR`` blocks the signature; the other two are
# informative and never prevent sending.
#   ERROR    the API or TTN refuses the value: the invoice cannot be signed
#   WARNING  the invoice is sent, but the data is altered or looks wrong
#   INFO     nothing is wrong, the user simply has something to check
ERROR = 'error'
WARNING = 'warning'
INFO = 'info'

# Display order, worst first. Everything that sorts, counts, colours or groups
# issues MUST go through this, never through "is / is not an error".
SEVERITY_ORDER = (ERROR, WARNING, INFO)
SEVERITY_RANK = {severity: rank for rank, severity in enumerate(SEVERITY_ORDER)}

# Severities that interrupt a signature with the check dialog. Informational
# messages are shown on the record, they do not stop anybody.
BLOCKING_SEVERITIES = (ERROR,)
INTERRUPTING_SEVERITIES = (ERROR, WARNING)

# Codes accepted by account.tax.teif_code but NOT by the pattern published in
# the TEIFInvoice JSON schema ("I-(16[0-9]|160[1-3])"). They are reported as
# warnings so users are not blocked if the API turns out to be more permissive.
UNCERTAIN_TAX_CODES = ('I-1604', 'I-1605', 'I-1606')

# Same idea for payment condition codes: the schema pattern is "I-12[1-4]"
# while account.payment.term.teif_code also offers I-125 (Autre).
UNCERTAIN_PAYMENT_CONDITION_CODES = ('I-125',)

# Tax codes known to this module (used as the "hard" pattern).
TAX_CODE_PATTERN = r'I-(16[0-9]|160[1-6])'
PAYMENT_CONDITION_PATTERN = r'I-12[1-5]'
PAYMENT_MEANS_PATTERN = r'I-13[1-7]'
DOCUMENT_TYPE_PATTERN = r'I-1[1-6]'
REFERENCE_ID_PATTERN = r'I-(8[0-9]|81[1-7])'
# Declared by the JSON schema for documentIdentifier (maxLength 70, but the
# pattern caps the real length at 30 and forbids "/"). Kept for reference only:
# TTN accepts numbers that violate it, see the document_identifier rule below.
DOCUMENT_IDENTIFIER_PATTERN = r'[A-Za-z0-9][A-Za-z0-9._\-]{0,29}'


TEIF_RULES = [

    # ------------------------------------------------------------------
    # Document header
    # ------------------------------------------------------------------
    {
        'code': 'document_identifier',
        'path': 'invoiceTIEF.documentIdentifier',
        'label': _lt("Invoice number"),
        'record': 'move',
        'source_model': 'account.move',
        'source_field': 'name',
        'required': True,
        'min_length': 1,
        'max_length': 70,
        # The JSON schema declares the pattern DOCUMENT_IDENTIFIER_PATTERN, which
        # forbids "/" and caps the length at 30. It is NOT enforced in practice:
        # invoices numbered "INV/2026/00001" have been signed by TTN and returned
        # a valid TTN reference. Checking it here would block every standard Odoo
        # sequence for nothing, so only the length is enforced.
        'severity': ERROR,
    },
    {
        'code': 'document_type',
        'path': 'invoiceTIEF.documentType',
        'label': _lt("Document type"),
        'record': 'move',
        'source_model': 'account.move',
        'source_field': 'move_type',
        'required': True,
        'pattern': DOCUMENT_TYPE_PATTERN,
        'severity': ERROR,
        'hint': _lt("Only customer invoices (I-11) and credit notes (I-12) can be sent to TTN."),
    },
    {
        'code': 'invoice_date',
        'path': 'invoiceTIEF.invoiceDate',
        'label': _lt("Invoice date"),
        'record': 'move',
        'source_model': 'account.move',
        'source_field': 'invoice_date',
        'required': True,
        'value_type': 'integer',
        'severity': ERROR,
    },
    {
        'code': 'currency',
        'path': 'invoiceTIEF.currencyIdentifier',
        'label': _lt("Currency"),
        'record': 'move',
        'source_model': 'account.move',
        'source_field': 'currency_id',
        'required': True,
        'max_length': 3,
        'severity': ERROR,
        'hint': _lt("The currency code must be an ISO 4217 code such as TND."),
    },
    {
        'code': 'total_in_letters',
        'path': 'invoiceTIEF.invoiceTotalinLetters',
        'label': _lt("Total amount in letters"),
        'record': 'move',
        'max_length': 500,
        'severity': ERROR,
    },
    {
        'code': 'comment',
        'path': 'invoiceTIEF.comments[]',
        'label': _lt("Invoice note (Terms & Conditions)"),
        'record': 'move',
        'source_model': 'account.move',
        'source_field': 'narration',
        'max_length': 500,
        'severity': WARNING,
        'hint': _lt("Shorten the note in the 'Terms and conditions' field of the invoice."),
    },

    # ------------------------------------------------------------------
    # Customer (clientDetails)
    # ------------------------------------------------------------------
    {
        'code': 'client_identifier',
        'path': 'invoiceTIEF.clientIdentifier',
        'label': _lt("Customer Tax ID (Matricule Fiscal)"),
        'missing_label': _lt("Missing Tax ID"),
        # The Tax ID is a commercial field: it belongs to the customer COMPANY,
        # and Odoo syncs it down to its contacts. Point the user at the company,
        # which is where the field can actually be corrected.
        'record': 'commercial_partner',
        'source_model': 'res.partner',
        'source_field': 'vat',
        'required': True,
        'max_length': 35,
        'severity': ERROR,
        'hint': _lt("Fill the 'Tax ID' field of the customer with its Matricule Fiscal "
                "(e.g. 1234567A or 1234567AAM000). The 'TN' prefix is removed automatically."),
    },
    {
        'code': 'client_partner_identifier',
        'path': 'invoiceTIEF.clientDetails.partnerIdentifier',
        'label': _lt("Customer Tax ID (Matricule Fiscal)"),
        'missing_label': _lt("Missing Tax ID"),
        # The Tax ID is a commercial field: it belongs to the customer COMPANY,
        # and Odoo syncs it down to its contacts. Point the user at the company,
        # which is where the field can actually be corrected.
        'record': 'commercial_partner',
        'source_model': 'res.partner',
        'source_field': 'vat',
        'required': True,
        'min_length': 1,
        'max_length': 35,
        'severity': ERROR,
    },
    {
        'code': 'client_name',
        'path': 'invoiceTIEF.clientDetails.partnerName',
        'label': _lt("Customer name"),
        # The builder sends the parent's name when the invoice is addressed to a
        # contact, so the issue must point at whichever record supplied it.
        'record': 'partner_name_holder',
        'source_model': 'res.partner',
        'source_field': 'name',
        'required': True,
        'min_length': 1,
        'max_length': 200,
        'severity': ERROR,
    },
    {
        'code': 'client_address_description',
        'path': 'invoiceTIEF.clientDetails.address.description',
        'label': _lt("Customer address"),
        'record': 'partner',
        'source_model': 'res.partner',
        'source_field': 'contact_address',
        'required': True,
        'max_length': 500,
        'severity': ERROR,
        'hint': _lt("The full address of the customer (street, zip, city, country) is required."),
    },
    {
        'code': 'client_street',
        'path': 'invoiceTIEF.clientDetails.address.street',
        'label': _lt("Customer street"),
        'record': 'partner',
        'source_model': 'res.partner',
        'source_field': 'street',
        'max_length': 35,
        'severity': ERROR,
        'hint': _lt("Shorten the street or move the end of it to the 'Street 2' field."),
    },
    {
        'code': 'client_city',
        'path': 'invoiceTIEF.clientDetails.address.cityName',
        'label': _lt("Customer city"),
        'record': 'partner',
        'source_model': 'res.partner',
        'source_field': 'city',
        'max_length': 35,
        'severity': ERROR,
    },
    {
        'code': 'client_zip',
        'path': 'invoiceTIEF.clientDetails.address.postalCode',
        'label': _lt("Customer ZIP"),
        'record': 'partner',
        'source_model': 'res.partner',
        'source_field': 'zip',
        'max_length': 17,
        'severity': ERROR,
    },
    {
        'code': 'client_country',
        'path': 'invoiceTIEF.clientDetails.address.country',
        'label': _lt("Customer country"),
        'record': 'partner',
        'source_model': 'res.partner',
        'source_field': 'country_id',
        'required': True,
        'min_length': 1,
        'max_length': 6,
        'severity': ERROR,
    },
    {
        'code': 'client_email',
        'path': 'clientEmail',
        'label': _lt("Customer email"),
        'record': 'partner',
        'source_model': 'res.partner',
        'source_field': 'email',
        'required': True,
        # Purely informative: a missing email costs the customer its copy of the
        # signed invoice, it never prevents the signature.
        'severity': INFO,
        'missing_message': _lt("The field \"%(field)s\" does not exist."),
        'hint': _lt("Without an email address NGSign cannot deliver the signed invoice "
                "to your customer."),
    },

    # ------------------------------------------------------------------
    # Bank details (header level)
    # ------------------------------------------------------------------
    {
        'code': 'account_number',
        'path': 'invoiceTIEF.accountNumber',
        'label': _lt("Bank account number"),
        'record': 'bank',
        'source_model': 'res.partner.bank',
        'source_field': 'acc_number',
        'min_length': 1,
        'max_length': 20,
        'severity': ERROR,
        'hint': _lt("TTN accepts 20 characters at most: use the RIB (20 digits) rather "
                "than the IBAN, and remove spaces."),
    },
    {
        'code': 'institution_name',
        'path': 'invoiceTIEF.institutionName',
        'label': _lt("Bank name"),
        'record': 'bank',
        'source_model': 'res.bank',
        'source_field': 'name',
        'max_length': 70,
        'severity': ERROR,
    },

    # ------------------------------------------------------------------
    # Invoice lines
    # ------------------------------------------------------------------
    {
        'code': 'items_present',
        'path': 'invoiceTIEF.items',
        'label': _lt("Invoice lines"),
        'record': 'move',
        'required': True,
        'severity': ERROR,
        'hint': _lt("The invoice must contain at least one product line "
                "(sections and notes are not sent)."),
    },
    {
        'code': 'item_name',
        'path': 'invoiceTIEF.items[].name',
        'label': _lt("Line description"),
        'record': 'line',
        'source_model': 'account.move.line',
        'source_field': 'name',
        'required': True,
        'min_length': 1,
        'max_length': 500,
        'severity': ERROR,
        'truncated': True,
        # The truncation advice lives in the dedicated check: this hint also has
        # to make sense when the description is simply missing.
        'hint': _lt("Fill in the description of the invoice line (500 characters max)."),
    },
    {
        'code': 'item_code',
        'path': 'invoiceTIEF.items[].code',
        'label': _lt("Product internal reference"),
        'record': 'product',
        'source_model': 'product.template',
        'source_field': 'default_code',
        'required': True,
        'min_length': 1,
        'max_length': 35,
        'severity': ERROR,
        # On the product itself this is only a recommendation: the payload
        # builder falls back to "N/A" when the reference is missing.
        'record_severity': WARNING,
        'hint': _lt("Set the 'Internal Reference' of the product (35 characters max)."),
    },
    {
        'code': 'item_unit',
        'path': 'invoiceTIEF.items[].unit',
        'label': _lt("Unit of measure"),
        'record': 'line',
        'source_model': 'uom.uom',
        'source_field': 'name',
        'min_length': 1,
        'max_length': 8,
        'severity': ERROR,
        'truncated': True,
        'hint': _lt("Rename the unit of measure to 8 characters at most (e.g. 'Units' -> 'U')."),
    },
    {
        'code': 'item_quantity',
        'path': 'invoiceTIEF.items[].quantity',
        'label': _lt("Quantity"),
        'record': 'line',
        'source_model': 'account.move.line',
        'source_field': 'quantity',
        'required': True,
        'value_type': 'number',
        'severity': ERROR,
    },
    {
        'code': 'item_unit_price',
        'path': 'invoiceTIEF.items[].unitPrice',
        'label': _lt("Unit price"),
        'record': 'line',
        'source_model': 'account.move.line',
        'source_field': 'price_unit',
        'required': True,
        'value_type': 'number',
        'severity': ERROR,
    },
    {
        'code': 'item_total_price',
        'path': 'invoiceTIEF.items[].totalPrice',
        'label': _lt("Line total (excl. tax)"),
        'record': 'line',
        'source_model': 'account.move.line',
        'source_field': 'price_subtotal',
        'required': True,
        'value_type': 'number',
        'severity': ERROR,
    },

    # Line taxes (v2.36 shape)
    {
        'code': 'item_tax_code',
        'path': 'invoiceTIEF.items[].taxes[].taxTypeName.code',
        'label': _lt("Line tax code"),
        'record': 'line',
        'source_model': 'account.tax',
        'source_field': 'teif_code',
        'required': True,
        'pattern': TAX_CODE_PATTERN,
        'severity': ERROR,
        'hint': _lt("Open the tax in Accounting > Configuration > Taxes and set its "
                "'TEIF Tax Code'."),
    },
    {
        'code': 'item_tax_rate',
        'path': 'invoiceTIEF.items[].taxes[].taxDetails.taxRate',
        'label': _lt("Line tax rate"),
        'record': 'line',
        'source_model': 'account.tax',
        'source_field': 'amount',
        'required': True,
        'min_length': 1,
        'max_length': 5,
        'severity': ERROR,
    },

    # ------------------------------------------------------------------
    # Global taxes
    # ------------------------------------------------------------------
    {
        'code': 'global_tax_code',
        'path': 'invoiceTIEF.taxes[].code',
        'label': _lt("Tax code"),
        'record': 'move',
        'source_model': 'account.tax',
        'source_field': 'teif_code',
        'required': True,
        'pattern': TAX_CODE_PATTERN,
        'severity': ERROR,
    },
    {
        'code': 'global_tax_rate',
        'path': 'invoiceTIEF.taxes[].taxRate',
        'label': _lt("Tax rate"),
        'record': 'move',
        'source_model': 'account.tax',
        'source_field': 'amount',
        'required': True,
        'min_length': 1,
        'max_length': 5,
        'severity': ERROR,
    },

    # ------------------------------------------------------------------
    # Payment details
    # ------------------------------------------------------------------
    {
        'code': 'payment_condition_code',
        'path': 'invoiceTIEF.paymentDetails[].pyt.paiConditionCode',
        'label': _lt("Payment condition code"),
        'record': 'payment_term',
        'source_model': 'account.payment.term',
        'source_field': 'teif_code',
        'required': True,
        'max_length': 6,
        'pattern': PAYMENT_CONDITION_PATTERN,
        'severity': ERROR,
        'hint': _lt("Open the payment term and set its 'TEIF Condition Code'."),
    },
    {
        'code': 'payment_terms_description',
        'path': 'invoiceTIEF.paymentDetails[].pyt.paymentTearmsDescription',
        'label': _lt("Payment terms"),
        'record': 'payment_term',
        'source_model': 'account.payment.term',
        'source_field': 'name',
        'max_length': 500,
        'severity': ERROR,
    },
    {
        'code': 'payment_means_code',
        'path': 'invoiceTIEF.paymentDetails[].pytPai.paiMeansCode',
        'label': _lt("Payment means code"),
        'record': 'move',
        'required': True,
        'max_length': 6,
        'pattern': PAYMENT_MEANS_PATTERN,
        'severity': ERROR,
    },
    {
        'code': 'payment_account_number',
        'path': 'invoiceTIEF.paymentDetails[].pytFii.accountHolder.accountNumber',
        'label': _lt("Bank account number (payment)"),
        'record': 'bank',
        'source_model': 'res.partner.bank',
        'source_field': 'acc_number',
        'required': True,
        'min_length': 1,
        'max_length': 20,
        'severity': ERROR,
        'hint': _lt("TTN accepts 20 characters at most: use the RIB (20 digits) rather "
                "than the IBAN, and remove spaces."),
    },
    {
        'code': 'payment_institution_name',
        'path': 'invoiceTIEF.paymentDetails[].pytFii.institutionIdentification.institutionName',
        'label': _lt("Bank name (payment)"),
        'record': 'bank',
        'source_model': 'res.bank',
        'source_field': 'name',
        'max_length': 70,
        'severity': ERROR,
    },

    # ------------------------------------------------------------------
    # Document references (credit notes)
    # ------------------------------------------------------------------
    {
        'code': 'reference_value',
        'path': 'invoiceTIEF.documentReferences[].value',
        'label': _lt("Related document reference"),
        'record': 'move',
        'required': True,
        'min_length': 1,
        'max_length': 200,
        'severity': ERROR,
    },
    {
        'code': 'reference_id',
        'path': 'invoiceTIEF.documentReferences[].refID',
        'label': _lt("Related document reference type"),
        'record': 'move',
        'required': True,
        'pattern': REFERENCE_ID_PATTERN,
        'severity': ERROR,
    },
]


def rules_for_model(model_name):
    """Return the rules whose source value lives on ``model_name``.

    Used by the per-form checks (contact, product, company) so the very same
    constraints are shown on the record itself, before any invoice is created.
    """
    return [r for r in TEIF_RULES if r.get('source_model') == model_name]


def rule_by_code(code):
    for rule in TEIF_RULES:
        if rule['code'] == code:
            return rule
    return None
