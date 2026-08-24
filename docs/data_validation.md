# e-Invoice data validation

How the module checks an invoice before sending it to NGSign, and how to change what it
checks. User-facing behaviour is summarised in the README; this document is for whoever
maintains the rules.

## Why it exists

The NGSign API and TTN reject a payload as a whole, with a message that rarely says which
Odoo field is at fault. The validation layer answers the same question locally — *what
would be refused, and where do I fix it?* — before anything is sent, and before any PDF is
rendered.

## The three files

| File | Role |
| :--- | :--- |
| `models/ngsign_spec.py` | Declarative table of the API constraints. One entry per payload leaf. |
| `models/ngsign_validator.py` | `ngsign.validator` — walks the payload against the table, plus the checks a rule cannot express. |
| `models/ngsign_validation_wizard.py` | The result dialog (`ngsign.validation.result` / `.issue`). |

Everything produces the same *issue* dictionary — `severity`, `code`, `label`, `message`,
`hint`, `res_model`/`res_id` (the record to open), `context_label` (e.g. "Line 3") — which
the dialog and the on-form banners both render.

## Severity

Defined in `ngsign_spec.py`. Only `ERROR` blocks a signature; `INTERRUPTING_SEVERITIES`
decides which levels open the dialog.

- `ERROR` — the API or TTN would refuse the value.
- `WARNING` — sent, but altered, truncated or suspicious.
- `INFO` — nothing to fix; never interrupts a signature.

Always go through `SEVERITY_ORDER` / `SEVERITY_RANK` / `count_by_severity()`. Code that
splits issues into "errors" and "everything else" silently mis-handles the third level.

## Adding or changing a rule

For anything expressible as *this payload field must look like this*, add an entry to
`TEIF_RULES` — no code change:

```python
{
    'code': 'client_city',                              # unique id
    'path': 'invoiceTIEF.clientDetails.address.cityName',  # [] expands a list
    'label': _lt("Customer city"),                      # shown to the user
    'record': 'partner',                                # which record to open
    'source_model': 'res.partner',                      # used by the on-form banners
    'source_field': 'city',
    'max_length': 35,
    'severity': ERROR,
    'hint': _lt("Shorten the city name."),
},
```

Optional keys worth knowing: `missing_label` and `missing_message` let the *absent* case be
named and worded differently from the *invalid* case; `record_severity` softens a rule when
it is evaluated on a record rather than on an invoice (the item code falls back to `"N/A"`,
so a missing product reference is only a warning on the product form); `truncated` marks a
value the payload builder already cuts, so the check runs against the source value and is
reported as a warning.

Anything needing several fields at once — cross-checks, totals, taxes, the Matricule
Fiscal — goes in `_extra_checks()` in the validator instead.

## Rules are calibrated against reality, not against the schema

Several constraints published in `TEIFInvoice_schema.json` are **not** enforced in
practice. This was established by looking at invoices TTN actually signed and returned a
reference for:

| Schema says | Reality | What the module does |
| :--- | :--- | :--- |
| `documentIdentifier` must match `^[A-Za-z0-9][A-Za-z0-9._-]{0,29}$` | `INV/2026/00001` is signed | Only the 70-char length is enforced |
| Matricule Fiscal is 7 digits + key letter | `12345678910` and `12345678L` are signed | Only the character set and a length of 8–13 |
| Tax code must match `I-(16[0-9]\|160[1-3])` | Unverified | `I-1604`/`I-1605`/`I-1606` are a warning, not an error |

**Before making a rule blocking, run it against `ngsign_status = 'TTN Signed'` invoices.**
A rule that fires on an invoice TTN already accepted is wrong, and blocking it would stop
work that used to succeed.

The mirror of that principle: **do not validate what is not sent.** The company Tax ID
never appears in the payload — NGSign derives the supplier from the account and the
certificate — so it can only ever be a warning, however malformed it is.

## Tunisian vs foreign customers

`_is_tunisian_customer()` reads the country from the **commercial partner** (the customer
company), falling back to the invoice partner. A partner with no country counts as
Tunisian, because `_prepare_ngsign_invoice_payload()` hardcodes `country: 'TN'` in that
case — the invoice really is declared Tunisian, whatever the form shows.

Foreign customers get a single informational message instead of the Matricule checks: the
value is still sent as `clientIdentifier`, so the user is asked to verify it themselves.

## When the check runs

`action_ngsign_check_before_send()` runs from the JS client action *before* the PDFs are
rendered, so a batch that must be corrected costs nothing and leaves no attachments
behind. The same gate is repeated inside `action_ngsign_send()` for callers that bypass
the client action. `ngsign_skip_validation=True` in the context is the only way past it,
and is used solely by the wizard's *Sign Anyway* button.

## Enumerating what is checked

The current list is derived from the code rather than maintained by hand:

```python
from odoo.addons.ngsign_einvoice_odoo.models import ngsign_spec as S
for r in S.TEIF_RULES:
    print(r['code'], r['path'], r.get('severity'), r.get('max_length'), r.get('pattern'))
```

Hand-written checks are the `_make_issue(... code='...')` calls in
`ngsign_validator.py::_extra_checks` and the helpers it calls.
