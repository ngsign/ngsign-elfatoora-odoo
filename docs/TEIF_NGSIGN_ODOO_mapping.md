This table provides a comprehensive, double-checked mapping for integrating Odoo 18 with the NGSign/Fatoora API.

It aligns the **NGSign JSON** payload (PDF 1), the official **TEIF XML** structure (PDF 2), and the required **Odoo 18** data points.

### ⚠️ Critical Odoo Prerequisites
1.  **Tax Mapping:** You must add a field (e.g., `teif_code`) to `account.tax` to map Odoo taxes to TEIF codes (I-16).
2.  **Payment Mapping:** You must add a field to `account.payment.term` or use a dictionary to map Odoo terms to TEIF Condition codes (I-12).
3.  **Fiscal ID Validation:** Odoo's `vat` field must be cleaned (remove 'TN' prefix) before sending.
    ⚠️ The 7-digit+key regex is **not** enforced by TTN in practice — invoices whose customer
    VAT was `12345678910` or `12345678L` have been signed and returned a TTN reference. The
    module therefore only rejects values that cannot be a Matricule at all (separators, or a
    length outside 8–13), and only for Tunisian customers. See `docs/data_validation.md`.

---

### 1. Transaction Wrapper (`NGInvoiceUpload`)

| NGSign JSON Field | TEIF XML Element | Odoo 18 Equivalent | Logic / Constraint |
| :--- | :--- | :--- | :--- |
| `invoiceFileB64` | N/A (Transport) | `base64.b64encode(pdf_content)` | Render the report `account.report_invoice`. |
| `type` | `Bgm/DocumentType` | `account.move.move_type` | **Map to Code I-1:**<br>• `out_invoice` $\rightarrow$ `"I-11"`<br>• `out_refund` $\rightarrow$ `"I-12"` |
| `clientEmail` | N/A (Notification) | `res.partner.email` | Used by NGSign to email the signed PDF. |
| `configuration` | N/A (Visual) | `ngsign.template.settings` | **Dynamic Settings:**<br>`qrPositionX`, `qrPositionY`, `qrPositionP`<br>`qrRatio`, `textPositionX`, `textPositionY`, `textPage`<br>`labelPositionX`, `labelPositionY`, `labelPositionP`<br>`allPages` |
| `invoiceTIEF` | `TEIF` (Root) | `account.move` | *The root of the data structure below.* |

---

### 2. Header Data (`TEIFInvoice`)

| NGSign JSON Field | TEIF XML Element | Odoo 18 Equivalent | Logic / Constraint |
| :--- | :--- | :--- | :--- |
| `documentIdentifier` | `Bgm/DocumentIdentifier` | `account.move.name` | Max 70 chars. The JSON schema also declares a pattern forbidding `/` and capping at 30 chars — **not enforced by TTN**: `INV/2026/00001` is signed. |
| `invoiceDate` | `Dtm/DateText` (I-31) | `account.move.invoice_date` | **Unix Timestamp (Milliseconds)**<br>Example: `1754348400000`. |
| `documentType` | `Bgm/DocumentType` | `account.move.move_type` | Same mapping as wrapper `type`. |
| `clientIdentifier` | `Nad/PartnerIdentifier` | `res.partner.vat` | **Strict Regex:** `[0-9]{7}[A-Z]...`<br>Strip country code (e.g., remove 'TN'). |
| `currencyIdentifier` | `InvoiceMoa/.../@currency` | `account.move.currency_id.name` | ISO 4217 (e.g., "TND"). |
| `comments` | `Ftx/FreeTexts` | `account.move.narration` | Invoice terms/notes. |
| `accountNumber` | `PytFii/AccountHolder` | `partner_bank_id.acc_number` | **Recommended:** The Supplier's bank account (Company). |
| `institutionName` | `PytFii/InstitutionName` | `partner_bank_id.bank_id.name` | |

---

### 3. Partner Details (`PartnerDetails` Object)

| NGSign JSON Field | TEIF XML Element | Odoo 18 Equivalent | Logic / Constraint |
| :--- | :--- | :--- | :--- |
| `partnerIdentifier` | `Nad/PartnerIdentifier` | `res.partner.vat` | **Required.** Max 35 chars. |
| `partnerName` | `Nad/PartnerNom` | `res.partner.name` | Max 200 chars. |
| `address.description` | `Nad/.../AdressDescription` | *Computed* | Combine street, zip, city. |
| `address.street` | `Nad/.../Street` | `res.partner.street` | |
| `address.cityName` | `Nad/.../CityName` | `res.partner.city` | |
| `address.postalCode` | `Nad/.../PostalCode` | `res.partner.zip` | |
| `address.country` | `Nad/.../Country` | `res.partner.country_id.code` | ISO 3166-1 (e.g., "TN"). |

---

### 4. Financial Totals (`TEIFInvoice` continued)

| NGSign JSON Field | TEIF XML Element | Odoo 18 Equivalent | Logic / Constraint |
| :--- | :--- | :--- | :--- |
| `invoiceTotalWithoutTax`| `InvoiceMoa/Amount` (I-172) | `account.move.amount_untaxed` | Total HT. |
| `invoiceTotalWithTax` | `InvoiceMoa/Amount` (I-180) | `account.move.amount_total` | Total TTC. |
| `invoiceTotalTax` | `InvoiceMoa/Amount` (I-181) | `account.move.amount_tax` | Total Tax Amount. |
| `taxes` | `InvoiceTax` (List) | `account.move.tax_totals` | **Global Tax List:**<br>List of all taxes applied.<br>Fields: `code`, `taxRate`, `amount`, `amountBase`. |
| `stampTax` | `InvoiceTax` (I-1601) | *Calculated* | Filter `line_ids` for tax name "Timbre".<br>Usually `0.600` or `1.000`. |
| `invoiceTotalinLetters` | `InvoiceMoa/AmountDesc` | `account.move.amount_total_words` | Use Odoo's built-in num2words function. |
| `totalDiscount` | `InvoiceAlc` | *Calculated* | Odoo does not store a global discount total by default. Sum line discounts if needed. |
| `amountServicesIncludingTax` | N/A (Validation Logic) | *Calculated* | Sum `price_total` of lines where `product.type == 'service'`. |

---

### 5. Invoice Lines (`items` List of `InvoiceItem`)

Iterate over `account.move.invoice_line_ids` (exclude sections/notes).

| NGSign JSON Field | TEIF XML Element | Odoo 18 Equivalent | Logic / Constraint |
| :--- | :--- | :--- | :--- |
| `name` | `LinImd/ItemDescription` | `account.move.line.name` | Max 500 chars. |
| `code` | `LinImd/ItemCode` | `product.product.default_code` | |
| `quantity` | `LinQty/Quantity` | `account.move.line.quantity` | |
| `unit` | `LinQty/.../@measurementUnit`| `product_uom_id.name` | Default: "UNIT". |
| `unitPrice` | `LinMoa/Amount` (I-183) | `account.move.line.price_unit` | Unit price (HT). |
| `totalPrice` | `LinMoa/Amount` (I-171) | `account.move.line.price_subtotal`| Total HT for the line. |
| `tvaRate` | `LinTax/.../TaxRate` | `account.move.line.tax_ids` | **Filter:** Find tax where TEIF Code = **I-1602** (VAT). Pass value (e.g., 19.0). |
| `service` | N/A (Internal) | `product.product.type` | `True` if type is 'service'. |
| **`discountPercentage`** | `LinAlc/Pcd/Percentage` | `account.move.line.discount` | Pass the percentage (e.g., `10.0`). |
| **`discount`** | `LinAlc/Moa/Amount` | *Calculated* | `(qty * price_unit * discount) / 100` |
| **`itemAlc`** | `LinAlc` | *Object* | **Only if discount > 0.** See structure below. |
| **`taxes`** | `LinTax` (List) | `account.move.line.tax_ids` | **Loop:** For all taxes where TEIF Code $\neq$ I-1602. |

#### 5.1 `itemAlc` Structure (If Discount Exists)

| NGSign JSON Field | TEIF XML Element | Odoo 18 Logic |
| :--- | :--- | :--- |
| `itemAlc.alc.allowanceCode` | `Alc/AllowanceCode` | Fixed: `"I-151"` (Réduction). |
| `itemAlc.pcd.percentage` | `Pcd/Percentage` | `str(line.discount)` |
| `itemAlc.pcd.percentageBasis` | `Pcd/PercentageBasis` | `str(line.quantity * line.price_unit)` |

#### 5.2 `taxes` Structure (Other Taxes - FODEC, DC)

| NGSign JSON Field | TEIF XML Element | Odoo 18 Logic |
| :--- | :--- | :--- |
| `taxes[].taxTypeName.code` | `TaxTypeName/@code` | **Map from Odoo Tax:**<br>FODEC $\rightarrow$ `I-162`<br>DC $\rightarrow$ `I-161`<br>Retenue $\rightarrow$ `I-1604` |
| `taxes[].taxTypeName.value` | `TaxTypeName` | `account.tax.name` |
| `taxes[].taxDetails.taxRate` | `TaxDetails/TaxRate` | `str(account.tax.amount)` (e.g., "1.0"). |

---

### 6. Payment Details (`paymentDetails` List)

| NGSign JSON Field | TEIF XML Element | Odoo 18 Equivalent | Logic / Constraint |
| :--- | :--- | :--- | :--- |
| `pyt.paiConditionCode` | `Pyt/PaymentTearmsTypeCode`| `invoice_payment_term_id` | **Mapping Required:**<br>Immediate $\rightarrow$ `I-121`<br>Bank Specific $\rightarrow$ `I-122`<br>Any Bank $\rightarrow$ `I-123` |
| `pyt.paymentTearmsDescription`| `Pyt/PaymentTearmsDescription`| `invoice_payment_term_id.name` | |
| `pytPai.paiMeansCode` | `PytPai/PaiMeansCode` | *Custom Selection Field* | **Mapping Required (Ref I-13):**<br>Espèce $\rightarrow$ `I-131`<br>Chèque $\rightarrow$ `I-132`<br>Virement $\rightarrow$ `I-135` |
| `pytFii` | `PytFii` | `partner_bank_id` | **Only if** a bank account is selected on the invoice. |
| `pytFii.accountHolder.accountNumber`| `AccountHolder/AccountNumber`| `partner_bank_id.acc_number` | RIB / IBAN. |
| `pytFii.institutionIdentification.institutionName`| `InstitutionName` | `partner_bank_id.bank_id.name` | |