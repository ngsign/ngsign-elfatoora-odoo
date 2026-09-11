import requests
import json
import base64
from odoo import _
from odoo.exceptions import UserError

class NGSignClient:
    def __init__(self, api_url, token):
        self.api_url = f"{api_url.rstrip('/')}/server"
        self.token = token

    def _get_headers(self):
        return {
            'Authorization': f'Bearer {self.token}',
            'Content-Type': 'application/json'
        }

    def create_transaction_seal(self, invoices_payload, passphrase, notify_owner=True, cc_email=None, use_v2=False):
        """
        Create and sign a transaction using SEAL certificate.
        """
        if use_v2:
            url = f"{self.api_url}/protected/invoice/v2/transaction/seal"
        else:
            url = f"{self.api_url}/protected/invoice/transaction/seal"
        headers = self._get_headers()
        
        # The payload structure for SEAL usually requires the invoices and the passphrase
        # Based on docs: Input is same as transaction endpoints.
        # And "When using SEAL, include the passphrase to decrypt the SEAL PIN."
        # So we wrap the invoices in an object that includes the passphrase?
        # Doc says: 
        # 7.3 NGAdvancedInvoiceUpload
        # - invoices: NGInvoiceUpload[]
        # - passphrase: string
        
        payload = {
            'invoices': invoices_payload,
            'passphrase': passphrase,
            'notifyOwner': notify_owner,
        }
        
        if cc_email:
            payload['ccEmail'] = cc_email
        
        response = requests.post(url, headers=headers, json=payload)
        response.raise_for_status()
        return response.json()

    def check_status(self, uuid):
        """
        Check the status of an invoice transaction.
        """
        url = f"{self.api_url}/protected/invoice/check/{uuid}"
        headers = self._get_headers()
        response = requests.post(url, headers=headers)
        response.raise_for_status()
        return response.json()

    def download_pdf(self, uuid):
        """
        Download the signed PDF.
        Response is JSON with base64 encoded PDF in 'object' field.
        """
        url = f"{self.api_url}/protected/invoice/pdf/{uuid}"
        headers = self._get_headers()
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        
        # Parse JSON response
        data = response.json()
        pdf_base64 = data.get('object')
        
        if not pdf_base64:
            raise UserError(_("PDF content not found in response"))
            
        return base64.b64decode(pdf_base64)

    def download_xml(self, uuid):
        """
        Download the signed XML.
        Response is JSON with base64 encoded XML in 'object' field.
        """
        url = f"{self.api_url}/protected/invoice/xml/{uuid}"
        headers = self._get_headers()
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        
        # Parse JSON response
        data = response.json()
        xml_base64 = data.get('object')
        
        if not xml_base64:
            raise UserError(_("XML content not found in response"))
            
        return base64.b64decode(xml_base64)

    def get_transaction_details(self, uuid):
        """
        Get transaction details.
        Endpoint: GET /any/invoice/{uuid}
        """
        url = f"{self.api_url}/any/invoice/{uuid}"
        headers = self._get_headers()
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        return response.json()

    def get_transaction_status_public(self, transaction_uuid):
        """
        Check the status of a transaction using the public endpoint.
        Endpoint: GET /any/invoice/{transaction_uuid}
        No Authorization header needed.
        """
        url = f"{self.api_url}/any/invoice/{transaction_uuid}"
        # No auth headers for public endpoint
        headers = {'Content-Type': 'application/json'}
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        return response.json()

    def create_transaction_advanced(self, invoices_payload, signer_email=None, cc_email=None):
        """
        Create an advanced transaction with optional delegated signer.
        This creates a transaction for DigiGO/SSCD that requires user interaction via PDS.
        
        Endpoint: POST /protected/invoice/v2/transaction/advanced
        
        :param invoices_payload: List of invoice objects (NGXMLInvoiceUpload)
        :param signer_email: Email of the delegated signer (optional)
        :param cc_email: Email to CC the final PDF (optional)
        :return: Response with transaction UUID and details
        """
        url = f"{self.api_url}/protected/invoice/v2/transaction/advanced"
        headers = self._get_headers()
        
        payload = {
            'invoices': invoices_payload
        }
        
        if signer_email:
            payload['signerEmail'] = signer_email
        
        if cc_email:
            payload['ccEmail'] = cc_email
        
        response = requests.post(url, headers=headers, json=payload)
        response.raise_for_status()
        return response.json()

    def cancel_transaction(self, transaction_uuid):
        """
        Cancel a transaction that has not been signed yet, with all its invoices.
        Endpoint: POST /protected/invoice/transaction/cancel/{uuid}

        :return: Response {message, errorCode}
        """
        url = f"{self.api_url}/protected/invoice/transaction/cancel/{transaction_uuid}"
        headers = self._get_headers()
        response = requests.post(url, headers=headers)
        response.raise_for_status()
        return response.json() if response.content else {}

    def generate_pds_url(self, transaction_uuid, base_url='https://sandbox.ng-sign.com/pds/#/teif/invoice/'):
        """
        Generate the Page de Signature (PDS) URL for a transaction.
        
        :param transaction_uuid: UUID of the transaction
        :param base_url: Base URL for the PDS (default: sandbox)
        :return: Complete PDS URL
        """
        return f"{base_url.rstrip('/')}/{transaction_uuid}"
