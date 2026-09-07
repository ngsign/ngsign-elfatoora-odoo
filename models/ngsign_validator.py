"""Generic, spec-driven validator for the NGSign e-invoice payload.

The validator answers one question: *if we send this invoice to NGSign right
now, what will TTN complain about?* — and answers it in Odoo terms (which
record, which field, what to change) instead of JSON paths.

It runs in two modes:

* :meth:`validate_moves` builds the real payload (without the PDF) and checks
  it against :data:`ngsign_spec.TEIF_RULES`, plus a set of cross-field checks
  that a path/length rule cannot express (missing TEIF codes on taxes, HTML in
  notes, multi-line descriptions, ...).
* :meth:`validate_record` applies the subset of the same rules that concerns a
  single record (contact, product, company) so the problem can be shown on the
  record's own form, long before an invoice exists.

Both modes return a list of "issue" dictionaries with the same keys, consumed
by the validation wizard and by the form banners.
"""

import logging
import re

from odoo import models, api, _
from markupsafe import Markup

from .ngsign_spec import (
    TEIF_RULES,
    ERROR,
    WARNING,
    INFO,
    SEVERITY_ORDER,
    SEVERITY_RANK,
    BLOCKING_SEVERITIES,
    UNCERTAIN_TAX_CODES,
    UNCERTAIN_PAYMENT_CONDITION_CODES,
    rules_for_model,
)

_logger = logging.getLogger(__name__)

# The Matricule Fiscal is normally 7 digits + a control key, possibly followed
# by the VAT code, the category and the establishment number (1234567AAM000).
# That structure is NOT enforced by TTN: invoices whose customer VAT was
# "12345678910" or "12345678L" have been signed and returned a TTN reference.
# So only characters that cannot survive the transport are reported: the VAT is
# sent as-is, and separators (the "1234567/A/M/000" data entry habit), spaces or
# punctuation do break it.
MATRICULE_CHARS_RE = re.compile(r'^[A-Z0-9]+$')

# Plausible length of a Matricule Fiscal: 8 (7 digits + key letter) up to 13
# (+ VAT code, category and establishment number). The v2.36 documentation says
# "Length 8 or 9" for clientIdentifier; the range is kept wider because values
# of 9 and 11 characters have been accepted and signed by TTN. Anything outside
# it cannot be a Matricule, whatever the reading.
MATRICULE_MIN_LEN = 8
MATRICULE_MAX_LEN = 13

# Structure of a Matricule Fiscal: 7 digits + a key letter, then an optional
# suffix (VAT code, category, establishment). Only used for the *company*: its
# identity is not carried by the payload but TTN matches the signer against the
# Matricule registered for the certificate, so a malformed one is worth a word.
COMPANY_MATRICULE_RE = re.compile(r'^[0-9]{7}[A-Z][A-Z0-9]*$')

HTML_TAG_RE = re.compile(r'<[^>]+>')


class NGSignValidator(models.AbstractModel):
    _name = 'ngsign.validator'
    _description = 'NGSign e-Invoice Data Validator'

    # ==================================================================
    # Public API
    # ==================================================================

    def validate_moves(self, moves):
        """Validate every move and return a flat list of issues."""
        issues = []
        for move in moves:
            issues.extend(self.validate_move(move))
        # Issues about a shared record (the company) would otherwise be repeated
        # for every invoice of a batch.
        seen_once = set()
        result = []
        for issue in issues:
            if issue.get('once'):
                key = (issue['code'], issue['res_model'], issue['res_id'])
                if key in seen_once:
                    continue
                seen_once.add(key)
            result.append(issue)
        return result

    def validate_move(self, move):
        """Validate a single invoice. Never raises: a crash while building the
        payload is itself reported as an issue."""
        move.ensure_one()
        try:
            payload = move._prepare_ngsign_invoice_payload(include_pdf=False)
        except Exception as e:  # pragma: no cover - defensive
            _logger.exception("NGSign: could not build payload for %s", move.name)
            return [self._make_issue(
                move=move,
                severity=ERROR,
                code='payload_error',
                label=_("e-Invoice data"),
                message=_("The e-invoice data could not be generated: %s") % e,
                record=move,
            )]

        issues = self._validate_payload(move, payload)
        issues.extend(self._extra_checks(move, payload))
        return self._deduplicate(issues)

    @api.model
    def _deduplicate(self, issues):
        """Drop issues the user would read twice.

        A single Odoo field can feed several payload fields (the customer VAT is
        sent as both ``clientIdentifier`` and ``clientDetails.partnerIdentifier``),
        which would otherwise produce two identical rows.
        """
        seen = set()
        unique = []
        for issue in issues:
            key = (issue['severity'], issue['message'], issue['context_label'],
                   issue['res_model'], issue['res_id'], issue['move_id'])
            if key in seen:
                continue
            seen.add(key)
            unique.append(issue)
        return unique

    def validate_record(self, record):
        """Validate a single Odoo record (contact, product, ...) against the
        rules that concern it. Used by the on-form banners."""
        issues = []
        seen_fields = set()
        for rule in rules_for_model(record._name):
            field = rule.get('source_field')
            if field in seen_fields:
                # Several payload fields can share one Odoo field (e.g. the VAT
                # is sent twice); report it once.
                continue
            seen_fields.add(field)
            value = self._record_value(record, rule)
            issues.extend(self._check_value(
                value=value,
                rule=rule,
                move=None,
                record=record,
                context_label='',
            ))
        if record._name == 'res.partner':
            issues.extend(self._check_matricule(record.vat, move=None, record=record))
        elif record._name == 'res.company':
            issues.extend(self._check_company_vat(record))
        return self._deduplicate(issues)

    # ==================================================================
    # Payload validation
    # ==================================================================

    def _validate_payload(self, move, payload):
        issues = []
        for rule in TEIF_RULES:
            if self._skip_rule(move, rule):
                continue
            for value, indices, present in self._expand(payload, rule['path'].split('.'), (), True):
                record, context_label = self._resolve_record(move, rule.get('record'), indices)
                issues.extend(self._check_value(
                    value=value if present else None,
                    rule=rule,
                    move=move,
                    record=record,
                    context_label=context_label,
                ))
        return issues

    # Values Odoo only fills in when the invoice is posted. Reporting them on a
    # draft would be pure noise, and signing requires a posted invoice anyway.
    _DRAFT_EXEMPT_RULES = ('document_identifier', 'invoice_date')

    def _skip_rule(self, move, rule):
        if move.state == 'draft' and rule['code'] in self._DRAFT_EXEMPT_RULES:
            return True
        return False

    @api.model
    def _expand(self, node, segments, indices, present):
        """Yield ``(value, indices, present)`` for every expansion of a path.

        ``segments`` come from ``path.split('.')``; a segment ending with
        ``[]`` iterates over a list and appends its index to ``indices``.
        """
        if not segments:
            yield node, indices, present
            return

        segment = segments[0]
        rest = segments[1:]
        is_list = segment.endswith('[]')
        key = segment[:-2] if is_list else segment

        if present and isinstance(node, dict):
            child = node.get(key)
            child_present = key in node and child is not None
        else:
            child = None
            child_present = False

        if is_list:
            if not isinstance(child, (list, tuple)):
                # Absent or empty list: nothing to validate inside it. The
                # presence of the list itself is covered by its own rule.
                return
            for index, item in enumerate(child):
                yield from self._expand(item, rest, indices + (index,), True)
        else:
            yield from self._expand(child, rest, indices, child_present)

    # ==================================================================
    # Single value checks
    # ==================================================================

    def _check_value(self, value, rule, move, record, context_label):
        issues = []
        # str() resolves the lazy translation in the language of the current user.
        label = str(rule['label'])
        rule_hint = str(rule['hint']) if rule.get('hint') else ''
        severity = rule.get('severity', ERROR)
        # A missing value can be milder on the record itself than on an invoice,
        # because the payload builder has a fallback for it ("N/A" for the item
        # code). That leniency applies to the *absence* only: an over-long or
        # malformed value is refused by the API wherever it is reported.
        missing_severity = severity
        if move is None and rule.get('record_severity'):
            missing_severity = rule['record_severity']
        empty = value is None or value == '' or value == [] or value == {}

        def issue(message, sev=severity):
            return self._make_issue(
                move=move, severity=sev, code=rule['code'], label=label,
                message=message, hint=rule_hint, record=record,
                path=rule['path'], value=value, context_label=context_label,
            )

        # The messages below deliberately do NOT repeat the field name: it is
        # displayed next to them, in bold on the banners and in its own column
        # in the check dialog.
        if empty:
            if rule.get('required'):
                if rule.get('missing_message'):
                    text = str(rule['missing_message'])
                    if '%(field)s' in text:
                        text = text % {'field': label}
                else:
                    text = _("The value is missing.")
                missing = issue(text, sev=missing_severity)
                # A rule may name the "absent" case differently from the field
                # itself, so the list of issues reads as a list of problems.
                if rule.get('missing_label'):
                    missing['label'] = str(rule['missing_label'])
                issues.append(missing)
            return issues

        if isinstance(value, str):
            length = len(value)
            max_length = rule.get('max_length')
            # Values the payload builder already truncates are checked against
            # their source value by _extra_checks(), as a warning.
            if max_length and not rule.get('truncated') and length > max_length:
                issues.append(issue(_(
                    "Exceeds %(max)s characters (currently %(len)s)."
                ) % {'max': max_length, 'len': length}))
            min_length = rule.get('min_length')
            if min_length and length < min_length:
                issues.append(issue(_(
                    "Must contain at least %(min)s character(s)."
                ) % {'min': min_length}))
            pattern = rule.get('pattern')
            if pattern and not re.fullmatch(pattern, value):
                issues.append(issue(_(
                    "Invalid format: \"%(value)s\"."
                ) % {'value': value[:70]}))

        value_type = rule.get('value_type')
        if value_type == 'number':
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                issues.append(issue(_("Must be a number.")))
        elif value_type == 'integer':
            if isinstance(value, bool) or not isinstance(value, int):
                issues.append(issue(_("Must be a whole number.")))

        return issues

    # ==================================================================
    # Checks that a declarative rule cannot express
    # ==================================================================

    def _extra_checks(self, move, payload):
        issues = []
        issues.extend(self._check_matricule(
            move.partner_id.vat, move, move.partner_id.commercial_partner_id or move.partner_id))
        issues.extend(self._check_client_company(move))
        issues.extend(self._check_dates(move))
        issues.extend(self._check_lines(move))
        issues.extend(self._check_taxes(move))
        issues.extend(self._check_payment(move))
        issues.extend(self._check_narration(move))
        issues.extend(self._check_totals(move, payload))
        issues.extend(self._check_company_vat(move.company_id, move))
        return issues

    def _check_company_vat(self, company, move=None):
        """The company Tax ID is NOT part of the payload (NGSign derives the
        supplier from the account and the certificate), so nothing here can be
        blocking. It is still reported, because TTN rejects a signature whose
        signer does not match the Matricule registered for the certificate
        ("SERV09 - Signataire non autorise")."""
        if not company:
            return []
        note = _("It is not sent in the e-invoice, but TTN checks the signer against "
                 "the Matricule Fiscal registered for your certificate.")
        cleaned = self._clean_vat(company.vat)

        if not cleaned:
            message = _("The Tax ID (Matricule Fiscal) of the company \"%(company)s\" "
                        "is not set.") % {'company': company.name}
        elif not MATRICULE_CHARS_RE.match(cleaned):
            message = _("The Tax ID of the company \"%(company)s\" (\"%(value)s\") contains "
                        "characters other than letters and digits.") % {
                'company': company.name, 'value': cleaned}
        elif not self._is_tunisian_customer(company.partner_id):
            # Same reasoning as for the customer: only a Tunisian company has a
            # Matricule Fiscal to check the shape of.
            return []
        elif not COMPANY_MATRICULE_RE.match(cleaned):
            message = _("The Tax ID of the company \"%(company)s\" (\"%(value)s\") does not "
                        "look like a Matricule Fiscal: 7 digits followed by a key letter "
                        "are expected (e.g. 1234567A or 1234567AAM000).") % {
                'company': company.name, 'value': cleaned}
        else:
            return []

        return [self._make_issue(
            move=move, severity=WARNING, code='company_vat',
            label=_("Company Tax ID (Matricule Fiscal)"),
            message='%s %s' % (message, note),
            hint=_("Set the 'Tax ID' of the company in Settings > Companies, and check "
                   "it matches the Matricule declared to TTN for your certificate."),
            record=company, value=cleaned, once=True,
        )]

    def _check_client_company(self, move):
        """When the invoice is addressed to a contact, the e-invoice must carry
        the identity of the COMPANY the contact belongs to."""
        issues = []
        partner = move.partner_id
        company = partner.commercial_partner_id
        if not partner.parent_id or company == partner:
            return issues

        # The payload sends parent_id.name. For a contact filed under a
        # department (contact -> department -> company) that is the department,
        # not the company registered at TTN.
        if partner.parent_id != company:
            issues.append(self._make_issue(
                move=move, severity=WARNING, code='client_company_name',
                label=_("Customer name"),
                message=_(
                    "\"%(sent)s\" is sent as the customer name, but the company of "
                    "\"%(contact)s\" is \"%(company)s\"."
                ) % {'sent': partner.parent_id.name, 'contact': partner.name,
                     'company': company.name},
                hint=_("Invoice the company directly, or attach the contact to it, so "
                       "that the name sent matches the Matricule Fiscal."),
                record=partner,
            ))

        # vat is a commercial field, so Odoo normally keeps them in sync; a
        # divergence means the payload would carry the contact's own Tax ID.
        if self._clean_vat(partner.vat) != self._clean_vat(company.vat):
            issues.append(self._make_issue(
                move=move, severity=WARNING, code='client_company_vat',
                label=_("Customer Tax ID (Matricule Fiscal)"),
                message=_(
                    "The Tax ID of the contact \"%(contact)s\" (\"%(contact_vat)s\") differs "
                    "from the one of its company \"%(company)s\" (\"%(company_vat)s\"). "
                    "The contact's one is sent."
                ) % {'contact': partner.name, 'contact_vat': partner.vat or '',
                     'company': company.name, 'company_vat': company.vat or ''},
                hint=_("Set the Matricule Fiscal on the customer company: Odoo copies it "
                       "to its contacts."),
                record=company,
            ))
        return issues

    def _check_matricule(self, vat, move, record):
        """The Matricule Fiscal is sent as-is, so it must at least be plausible.

        Its internal structure is deliberately NOT enforced: values such as
        "12345678910" or "12345678L" have been signed by TTN. Only what cannot
        be a Matricule under any reading is reported - separators, and a length
        outside 8..13 characters.
        """
        cleaned = self._clean_vat(vat)
        if not cleaned:
            return []

        if not self._is_tunisian_customer(record):
            # A foreign customer has no Matricule Fiscal: checking it against the
            # Tunisian format would be meaningless. The value is still sent as
            # clientIdentifier, so the user is told to check it themselves.
            country = record.commercial_partner_id.country_id or record.country_id
            return [self._make_issue(
                move=move, severity=INFO, code='matricule_foreign',
                label=_("Foreign Tax ID"),
                message=_(
                    "\"%(name)s\" is not a Tunisian customer (%(country)s): its Tax ID "
                    "\"%(value)s\" is sent to TTN as it is, without any format check."
                ) % {'name': record.display_name, 'country': country.display_name or '-',
                     'value': cleaned},
                hint=_("Check yourself that this Tax ID is the identifier TTN expects "
                       "for a foreign customer."),
                record=record, value=cleaned,
            )]

        # Both checks below are blocking: the value is sent to TTN as-is, and
        # something that cannot be a Matricule Fiscal will be rejected there.
        # (The company Tax ID is only a warning, because it is NOT part of the
        # payload - see _check_company_vat.)
        if not MATRICULE_CHARS_RE.match(cleaned):
            return [self._make_issue(
                move=move, severity=ERROR, code='matricule_format',
                label=_("Invalid Tunisian Tax ID"),
                message=_(
                    "\"%(value)s\" contains characters other than letters and digits. "
                    "The Matricule Fiscal is sent to TTN as it is written here."
                ) % {'value': cleaned},
                hint=_("Write the Matricule Fiscal without separators or spaces "
                       "(1234567AAM000, not 1234567/A/A/M/000)."),
                record=record, value=cleaned,
            )]

        if not (MATRICULE_MIN_LEN <= len(cleaned) <= MATRICULE_MAX_LEN):
            return [self._make_issue(
                move=move, severity=ERROR, code='matricule_length',
                label=_("Invalid Tunisian Tax ID"),
                message=_(
                    "\"%(value)s\" is %(len)s characters long: a Tunisian Matricule Fiscal "
                    "has %(min)s characters (7 digits and a key letter), up to %(max)s with "
                    "the VAT code, the category and the establishment number."
                ) % {'value': cleaned, 'len': len(cleaned),
                     'min': MATRICULE_MIN_LEN, 'max': MATRICULE_MAX_LEN},
                hint=_("Check the 'Tax ID' of the contact (e.g. 1234567A or 1234567AAM000)."),
                record=record, value=cleaned,
            )]
        return []

    def _check_dates(self, move):
        # Posting fills the invoice date in: only complain once it is posted.
        if move.state != 'draft' and not move.invoice_date:
            return [self._make_issue(
                move=move, severity=ERROR, code='invoice_date_missing',
                label=_("Invoice date"),
                message=_("The invoice date is not set; today's date would be sent instead."),
                record=move,
            )]
        return []

    def _check_lines(self, move):
        issues = []
        lines = move._ngsign_payload_lines()
        for index, line in enumerate(lines):
            context_label = self._line_label(index, line)
            name = line.name or ''
            if len(name) > 500:
                issues.append(self._make_issue(
                    move=move, severity=WARNING, code='item_name_truncated',
                    label=_("Line description"),
                    message=_(
                        "The description is %(len)s characters long: only the first "
                        "500 will be sent to TTN."
                    ) % {'len': len(name)},
                    hint=_("Shorten the description of the line."),
                    record=move, context_label=context_label,
                ))
            if '\n' in name.strip():
                issues.append(self._make_issue(
                    move=move, severity=INFO, code='item_name_multiline',
                    label=_("Line description"),
                    message=_(
                        "The description spans several lines: only the first line "
                        "(\"%(first)s\") will be sent to TTN."
                    ) % {'first': name.split('\n')[0][:70]},
                    hint=_("Put the whole description on a single line if the rest matters."),
                    record=move, context_label=context_label,
                ))
            uom_name = line.product_uom_id.name or ''
            if len(uom_name) > 8:
                issues.append(self._make_issue(
                    move=move, severity=WARNING, code='item_unit_truncated',
                    label=_("Unit of measure"),
                    message=_(
                        "The unit of measure \"%(uom)s\" is longer than 8 characters "
                        "and will be shortened to \"%(sent)s\"."
                    ) % {'uom': uom_name, 'sent': uom_name[:7]},
                    hint=_("Rename the unit of measure or use a shorter one."),
                    record=line.product_uom_id, context_label=context_label,
                ))
            # Only actionable when there IS a product to set the reference on.
            # Manual lines (stamp duty, down payments) legitimately have none, and
            # TTN accepts the "N/A" fallback - invoices sent that way are signed.
            if line.product_id and not line.product_id.default_code:
                issues.append(self._make_issue(
                    move=move, severity=WARNING, code='item_code_missing',
                    label=_("Product internal reference"),
                    message=_("The product has no internal reference: \"N/A\" will be "
                              "sent as the item code."),
                    hint=_("Set the 'Internal Reference' of the product."),
                    record=line.product_id, context_label=context_label,
                ))
        return issues

    def _check_taxes(self, move):
        """Every tax used on the invoice must carry a TEIF code."""
        issues = []
        seen = set()
        for index, line in enumerate(move._ngsign_payload_lines()):
            context_label = self._line_label(index, line)
            vat_taxes = line.tax_ids.filtered(lambda t: t.teif_code == 'I-1602')
            if len(vat_taxes) > 1:
                issues.append(self._make_issue(
                    move=move, severity=WARNING, code='item_multiple_vat',
                    label=_("VAT"),
                    message=_(
                        "The line carries %(count)s VAT taxes; only \"%(kept)s\" will be "
                        "sent as the line VAT rate, the others as additional taxes."
                    ) % {'count': len(vat_taxes), 'kept': vat_taxes[0].name},
                    record=move, context_label=context_label,
                ))
            for tax in line.tax_ids:
                if tax.id in seen:
                    continue
                seen.add(tax.id)
                if not tax.teif_code:
                    issues.append(self._make_issue(
                        move=move, severity=ERROR, code='tax_code_missing',
                        label=_("TEIF Tax Code"),
                        message=_(
                            "The tax \"%(tax)s\" has no TEIF code; it would be sent as "
                            "\"I-1606\" (other tax)."
                        ) % {'tax': tax.name},
                        hint=_("Open the tax in Accounting > Configuration > Taxes and "
                               "set its 'TEIF Tax Code'."),
                        record=tax, context_label=context_label,
                    ))
                elif tax.teif_code in UNCERTAIN_TAX_CODES:
                    issues.append(self._make_issue(
                        move=move, severity=WARNING, code='tax_code_uncertain',
                        label=_("TEIF Tax Code"),
                        message=_(
                            "The tax \"%(tax)s\" uses the code %(code)s, which the current "
                            "NGSign schema does not list (only I-1601 to I-1603 and "
                            "I-160 to I-169 are documented). It may be rejected by TTN."
                        ) % {'tax': tax.name, 'code': tax.teif_code},
                        record=tax, context_label=context_label,
                    ))
        return issues

    def _check_payment(self, move):
        issues = []
        term = move.invoice_payment_term_id
        if term and not term.teif_code:
            issues.append(self._make_issue(
                move=move, severity=WARNING, code='payment_term_code_missing',
                label=_("TEIF Condition Code"),
                message=_(
                    "The payment term \"%(term)s\" has no TEIF condition code; "
                    "\"I-121\" (immediate payment) would be sent instead."
                ) % {'term': term.name},
                hint=_("Open the payment term and set its 'TEIF Condition Code'."),
                record=term,
            ))
        elif term and term.teif_code in UNCERTAIN_PAYMENT_CONDITION_CODES:
            issues.append(self._make_issue(
                move=move, severity=WARNING, code='payment_term_code_uncertain',
                label=_("TEIF Condition Code"),
                message=_(
                    "The payment term \"%(term)s\" uses the code %(code)s, which the "
                    "current NGSign schema does not list (I-121 to I-124). It may be "
                    "rejected by TTN."
                ) % {'term': term.name, 'code': term.teif_code},
                record=term,
            ))

        bank = move._ngsign_resolve_bank_account()
        if bank and not bank.bank_id:
            issues.append(self._make_issue(
                move=move, severity=WARNING, code='bank_missing',
                label=_("Bank"),
                message=_("The bank account \"%(acc)s\" is not linked to a bank; "
                          "\"Bank\" would be sent as the institution name.")
                % {'acc': bank.acc_number or ''},
                hint=_("Set the 'Bank' of the account on the contact."),
                record=bank,
            ))
        return issues

    def _check_narration(self, move):
        if move.narration and HTML_TAG_RE.search(str(move.narration)):
            return [self._make_issue(
                move=move, severity=WARNING, code='narration_html',
                label=_("Invoice note (Terms & Conditions)"),
                message=_(
                    "The invoice note contains HTML formatting; the tags are sent "
                    "as-is in the e-invoice comments."
                ),
                hint=_("Use plain text in the 'Terms and conditions' field."),
                record=move,
            )]
        return []

    def _check_totals(self, move, payload):
        """Guard against rounding drift between Odoo and the payload."""
        issues = []
        teif = payload.get('invoiceTIEF', {})
        items = teif.get('items') or []
        currency = move.currency_id
        lines_total = sum(item.get('totalPrice') or 0.0 for item in items)
        if currency and currency.compare_amounts(lines_total, move.amount_untaxed) != 0:
            issues.append(self._make_issue(
                move=move, severity=WARNING, code='totals_mismatch',
                label=_("Total excluding tax"),
                message=_(
                    "The sum of the e-invoice lines (%(lines)s) differs from the invoice "
                    "total excluding tax (%(total)s). TTN may reject the invoice."
                ) % {
                    'lines': currency.round(lines_total),
                    'total': move.amount_untaxed,
                },
                hint=_("This usually comes from global discounts or from lines excluded "
                       "from the e-invoice."),
                record=move,
            ))
        return issues

    # ==================================================================
    # Helpers
    # ==================================================================

    @api.model
    def _clean_vat(self, vat):
        return (vat or '').upper().replace('TN', '').strip()

    @api.model
    def _is_tunisian_customer(self, record):
        """Does the Tunisian Matricule Fiscal format apply to this partner?

        The country is read from the commercial partner first, because that is
        the record the module attributes the Tax ID to (``vat`` is a commercial
        field, synced from the customer company down to its contacts).

        A partner with NO country counts as Tunisian: the payload builder
        hardcodes ``country: 'TN'`` in that case, so the invoice really is
        declared as Tunisian whatever the UI would like to believe.
        """
        if not record or record._name != 'res.partner':
            return True
        country = record.commercial_partner_id.country_id or record.country_id
        return country.code in ('TN', False)

    def _record_value(self, record, rule):
        """Read the source value of a rule directly from an Odoo record."""
        field = rule.get('source_field')
        if not field or field not in record._fields:
            return None
        value = record[field]
        if field == 'vat':
            return self._clean_vat(value)
        field_obj = record._fields[field]
        if field_obj.type == 'many2one':
            if field == 'country_id':
                return value.code or None
            return value.display_name if value else None
        if field_obj.type in ('char', 'text', 'html'):
            return value or None
        return value

    def _resolve_record(self, move, key, indices):
        """Map a payload location to the Odoo record the user must fix.

        Returns ``(record, context_label)``. Invoice lines have no form view of
        their own, so they resolve to their invoice plus a "Line n" label.
        """
        if not move:
            return move, ''
        if key == 'partner':
            return move.partner_id, ''
        if key == 'commercial_partner':
            return move.partner_id.commercial_partner_id or move.partner_id, ''
        if key == 'partner_name_holder':
            return move.partner_id.parent_id or move.partner_id, ''
        if key == 'company':
            return move.company_id, ''
        if key == 'payment_term':
            return move.invoice_payment_term_id or move, ''
        if key == 'bank':
            return move._ngsign_resolve_bank_account() or move, ''
        if key in ('line', 'product'):
            lines = move._ngsign_payload_lines()
            if indices and indices[0] < len(lines):
                line = lines[indices[0]]
                label = self._line_label(indices[0], line)
                if key == 'product' and line.product_id:
                    return line.product_id, label
                return move, label
        return move, ''

    @api.model
    def _line_label(self, index, line):
        name = (line.name or line.product_id.display_name or '').split('\n')[0]
        return _("Line %(number)s (%(name)s)") % {'number': index + 1, 'name': name[:40]}

    def _make_issue(self, move, severity, code, label, message, record=None,
                    hint=None, path=None, value=None, context_label='', once=False):
        record = record if record is not None and record else move
        return {
            'once': once,
            'code': code,
            'severity': severity,
            'label': label,
            'message': message,
            'hint': hint or '',
            'path': path or '',
            'value': ('%s' % value)[:200] if value is not None else '',
            'context_label': context_label or '',
            'move_id': move.id if move else False,
            'move_name': move.display_name if move else '',
            'res_model': record._name if record else False,
            'res_id': record.id if record else False,
            'res_name': record.display_name if record else '',
        }

    # ==================================================================
    # Rendering
    # ==================================================================

    @api.model
    def has_errors(self, issues):
        """True when at least one issue prevents the signature."""
        return any(issue['severity'] in BLOCKING_SEVERITIES for issue in issues)

    @api.model
    def count_by_severity(self, issues):
        """Number of issues per severity, for every level (0 when absent)."""
        return {severity: sum(1 for i in issues if i['severity'] == severity)
                for severity in SEVERITY_ORDER}

    @api.model
    def sort_by_severity(self, issues):
        """Worst first, then by invoice and field. Unknown severities go last."""
        return sorted(issues, key=lambda i: (
            SEVERITY_RANK.get(i['severity'], len(SEVERITY_ORDER)),
            i['move_name'], i['label'],
        ))

    # Bootstrap contextual class of the surrounding alert, per severity.
    _SEVERITY_CSS = {ERROR: 'alert-danger', WARNING: 'alert-warning', INFO: 'alert-info'}
    # Colour of the pill shown in front of every line (Bootstrap 5 / Odoo tags).
    _SEVERITY_BADGE = {ERROR: 'text-bg-danger', WARNING: 'text-bg-warning', INFO: 'text-bg-info'}

    @api.model
    def severity_label(self, severity):
        """Short human label of a severity, for the coloured pills."""
        return {
            ERROR: _("Critical"),
            WARNING: _("Warning"),
            INFO: _("Information"),
        }.get(severity, severity)

    @api.model
    def severity_count_label(self, severity, count):
        """"3 warning(s)" - the counted form, which pluralises per language."""
        template = {
            ERROR: _("%(count)s critical"),
            WARNING: _("%(count)s warning(s)"),
            INFO: _("%(count)s information"),
        }.get(severity)
        if not template:
            return '%s %s' % (count, severity)
        return template % {'count': count}

    @api.model
    def _severity_badge(self, severity, text=None):
        return Markup('<span class="badge rounded-pill %s">%s</span>') % (
            self._SEVERITY_BADGE.get(severity, 'text-bg-secondary'),
            text if text is not None else self.severity_label(severity),
        )

    @api.model
    def issues_to_html(self, issues, limit=6):
        """Compact HTML summary used by the on-form banners.

        Every line carries a coloured pill with its severity, so the three
        levels can be told apart at a glance; the alert itself takes the colour
        of the worst severity present. The wrapper is part of the value rather
        than of the view, so a single field renders the three styles.
        """
        if not issues:
            return False
        counts = self.count_by_severity(issues)
        ordered = self.sort_by_severity(issues)
        worst = next(s for s in SEVERITY_ORDER if counts[s])

        # Header: one counter pill per severity present, worst first.
        counters = [
            self._severity_badge(severity, self.severity_count_label(severity, counts[severity]))
            for severity in SEVERITY_ORDER if counts[severity]
        ]

        parts = [
            Markup('<div class="alert %s mb-2" role="alert">') % self._SEVERITY_CSS[worst],
            Markup('<p class="mb-2"><strong>%s</strong> %s</p>') % (
                _("e-Invoice data check"),
                Markup(' ').join(counters),
            ),
            Markup('<ul class="list-unstyled mb-0">'),
        ]
        for issue in ordered[:limit]:
            # Name of the problem in bold, then where it is, then what is wrong.
            name = Markup('<strong>%s</strong>') % issue['label']
            context = Markup(' <em>%s</em>') % issue['context_label'] \
                if issue['context_label'] else ''
            hint = Markup(' <span class="text-muted">%s</span>') % issue['hint'] \
                if issue['hint'] else ''
            parts.append(Markup('<li class="mb-1">%s %s%s — %s%s</li>') % (
                self._severity_badge(issue['severity']), name, context,
                issue['message'], hint))
        if len(ordered) > limit:
            parts.append(Markup('<li class="mb-0 text-muted">%s</li>') % (
                _("and %(count)s more...") % {'count': len(ordered) - limit}))
        parts.append(Markup('</ul></div>'))
        return Markup('').join(parts)

    @api.model
    def issues_to_text(self, issues):
        """Plain text rendering, used for UserError fallbacks and logs."""
        lines = []
        markers = {ERROR: '[!]', WARNING: '[*]', INFO: '[i]'}
        for issue in self.sort_by_severity(issues):
            prefix = '%s ' % markers.get(issue['severity'], '•')
            if issue['move_name']:
                prefix += '[%s] ' % issue['move_name']
            if issue['context_label']:
                prefix += '%s — ' % issue['context_label']
            # The message no longer repeats the field name, so name it here.
            lines.append('%s%s: %s' % (prefix, issue['label'], issue['message']))
            if issue['hint']:
                lines.append('    %s' % issue['hint'])
        return '\n'.join(lines)
