# NGSign e-invoice Odoo Module

This module integrates Odoo with the **NGSign** platform to enable electronic invoicing in compliance with Tunisian regulations (TEIF/TTN).

## Features

- **TEIF Compliance**: Generates e-invoices in the Tunisian Electronic Invoice Format (TEIF).
- **Multiple Certificate Types**: Supports three types of digital signatures:
  - **SEAL (Automatic)**: Server-side electronic seal for automated signing
  - **DigiGO**: Personal digital certificate requiring user authentication
  - **SSCD**: Secure Signature Creation Device (USB token)
- **TTN Integration**: Submits signed invoices to the Tunisie TradeNet (TTN) platform.
- **Automatic PDF Enrichment**: Updates the invoice PDF with the TTN unique reference and QR code (2D-Doc) upon successful signing.
- **Status Tracking**: Tracks the status of invoices (Pending, Signed, Rejected) directly within Odoo.
- **Data Validation**: Checks the invoice data against the NGSign/TEIF constraints *before* sending it, and tells the user which record and which field to correct, at three levels (critical / warning / information).

## Installation

1.  Clone this repository into your Odoo addons path.
2.  Restart Odoo.
3.  Update your Odoo app list.
4.  Install the **NGSign e-invoice** module.

## Configuration

### 1. General Settings
1.  Go to **Accounting > Configuration > Settings**.
2.  Scroll down to the **NGSign e-invoice** section.
3.  Enter your NGSign credentials:
    -   **NGSign API URL**: The base URL for the NGSign API (e.g., `https://api.ng-sign.com`).
    -   **Bearer Token**: Your NGSign API Bearer Token.
    -   **Certificate Type**: Select the type of certificate to use:
        -   **SEAL (Automatic Signing)**: For automated server-side signing with passphrase
        -   **DigiGO (User Signature)**: For personal digital certificate requiring user authentication
        -   **SSCD (USB Token)**: For hardware security token
    -   **SEAL Passphrase** (SEAL only): The passphrase for your SEAL certificate.
    -   **PDS Base URL** (DigiGO/SSCD only): Base URL for the Page de Signature (default: sandbox URL).
    -   **Signer Email** (Optional): Email of the delegated signer.
    -   **QRCode and TTN UID PDF layout** (SEAL only): Set the QRCode and TTN UID coordinate under **NGSIGN (Seal V1) PDF Generation layout**.
    -   **BETA:Builtin (Seal V2) PDF layout** (SEAL only): Seal V2 must be activated under developer settings to enable this feature which generates the PDF in Odoo.

### 2. Tax Configuration
1.  Go to **Accounting > Configuration > Taxes**.
2.  Open each tax used in invoices (e.g., VAT 19%, FODEC).
3.  Set the **TEIF Tax Code** field to the corresponding TEIF code (e.g., `I-1602` for VAT).

### 3. Payment Terms Configuration
1.  Go to **Accounting > Configuration > Payment Terms**.
2.  Open your payment terms (e.g., Immediate Payment).
3.  Set the **TEIF Condition Code** field (e.g., `I-121` for Immediate).

## Usage

### Signing Invoices

#### SEAL Certificate (Automatic)
1.  **Create an Invoice**: Create and post a Customer Invoice as usual.
2.  **Sign**: Click the **"Sign with NGSign"** button in the invoice header. The status will change to "Pending".
3.  **Check Status**: Click the **"Check NGSign Status"** button to poll for updates.
    -   Once processed by TTN, the status will change to **"Signed"**.
    -   The signed PDF (containing the TTN QR code) will be automatically downloaded and attached to the invoice.

#### DigiGO / SSCD Certificate (Manual)
1.  **Create an Invoice**: Create and post a Customer Invoice as usual.
2.  **Initiate Signing**: Click the **"Sign with NGSign"** button in the invoice header.
    -   A new browser window will open with the Page de Signature (PDS).
    -   The invoice status will change to "Pending Signature".
3.  **Complete Signing**: On the PDS page:
    -   Review the invoice details
    -   Sign using your DigiGO credentials or SSCD USB token
    -   Complete the authentication process
4.  **Return to Odoo**: After signing on the PDS:
    -   Return to the Odoo invoice
    -   Click the **"Check NGSign Status"** button to update the status
    -   Once processed by TTN, the status will change to **"Signed"**
    -   The signed PDF will be automatically downloaded and attached

**Note**: You can also click the **"Open Signing Page"** button at any time to reopen the PDS if needed.

### Checking the data before signing

The data sent to NGSign must respect the TEIF constraints (mandatory fields, maximum
lengths, code formats). The module checks them locally so problems are fixed in Odoo
instead of coming back as an API rejection.

#### Three levels

| Level | Blocks signing | Shown when |
| :--- | :--- | :--- |
| **Critical** | Yes | The value would be refused by the API or by TTN — missing Tax ID, text over the maximum length, tax without a TEIF code. |
| **Warning** | No | The invoice can be sent, but the data leaves altered or incomplete — a description that will be truncated, a payment term with no TEIF code. |
| **Information** | No | Nothing to fix, only worth knowing — a foreign customer, a description spanning several lines. |

Each issue names the record to correct and opens it in one click (contact, product, tax,
payment term, bank account).

#### Where the check appears

-   **When signing**: **"Sign with NGSign"** checks the data *before* rendering any PDF,
    for a single invoice or for a whole selection. If anything critical or warning-level
    is found, the **e-Invoice Data Check** dialog opens instead of sending.
    -   Critical problems must be corrected — fix them in Odoo, then **Check Again**.
    -   Warnings can be acknowledged with **Sign Anyway**.
    -   Information alone never interrupts: the invoice is sent.
-   **On demand**: the **"Check e-Invoice Data"** button in the invoice header, on one
    invoice or on a list selection.
-   **Banners**: invoices, customer contacts, products and the company display their own
    problems directly on the form, each line tagged with its level.
-   **Debug JSON**: the generated JSON carries a `_validation` section listing everything
    detected (this key is not part of the API payload).

#### Tunisian and foreign customers

The Matricule Fiscal checks (character set, plausible length) only apply when the customer
is Tunisian — the country is read from the customer company, and a contact with no country
counts as Tunisian, because that is what the invoice declares. A foreign customer gets a
single *information* message instead, asking you to check its identifier yourself.

Your own company's Tax ID is also reported, but only as a warning: it is never sent in the
payload, TTN checks the signer against the certificate.

All the constraints live in `models/ngsign_spec.py` (one entry per API field, with its
maximum length / pattern and the Odoo field it comes from), so updating the module for a
new API version means editing that single file. Note that some constraints published in
the API schema are deliberately **not** enforced, because invoices violating them are
signed by TTN in practice — see `CLAUDE.md` for the details and the evidence.

### Debugging
1.  **Enable Debug Mode**: Go to **Accounting > Configuration > Settings > NGSign e-invoice** and check **Enable Debug Button**.
2.  **Generate JSON**: Open any Customer Invoice. Click the **"Generate Debug JSON"** button in the header to download the payload.

## Author

**NGSIGN**
