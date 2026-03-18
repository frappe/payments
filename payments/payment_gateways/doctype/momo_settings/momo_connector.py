"""
MomoConnector — MTN Mobile Money Collection API client.

Mirrors the MpesaConnector pattern from:
  payments/payment_gateways/doctype/mpesa_settings/mpesa_connector.py

MTN MoMo API reference:
  https://momodeveloper.mtn.com/docs/services/collection
"""

import base64
import uuid

import requests


class MomoConnector:
    """
    Wraps the MTN MoMo Collection API.

    Usage::

        conn = MomoConnector(
            env="sandbox",
            api_user_id="",
            api_key="",
            subscription_key="",
            target_environment="sandbox",
        )

        result = conn.request_to_pay(
            amount=5000,
            currency="UGX",
            payer_msisdn="256700000000",
            external_id="INV-0001",
            callback_url="https://your-site.com/api/method/...verify_transaction",
        )

        # result = {"referenceId": "", "status": "PENDING"}
    """

    SANDBOX_URL = "https://sandbox.momodeveloper.mtn.com"
    PRODUCTION_URL = "https://proxy.momoapi.mtn.com"

    def __init__(
        self,
        env="sandbox",
        api_user_id=None,
        api_key=None,
        subscription_key=None,
        target_environment="sandbox",
    ):
        self.env = env
        self.api_user_id = api_user_id
        self.api_key = api_key
        self.subscription_key = subscription_key
        self.target_environment = target_environment
        self.base_url = (
            self.SANDBOX_URL if env == "sandbox" else self.PRODUCTION_URL
        )
        self.access_token = None
        self.authenticate()

    # ── Authentication ──────────────────────────────────────────────

    def authenticate(self):
        """
        Obtain a Bearer token from the MTN MoMo token endpoint.

        POST /collection/token/

        Authorization: Basic base64(api_user_id:api_key)
        Ocp-Apim-Subscription-Key:

        Returns the access_token string and stores it in self.access_token.
        """

        url = f"{self.base_url}/collection/token/"

        credentials = base64.b64encode(
            f"{self.api_user_id}:{self.api_key}".encode()
        ).decode()

        headers = {
            "Authorization": f"Basic {credentials}",
            "Ocp-Apim-Subscription-Key": self.subscription_key,
        }

        response = requests.post(url, headers=headers)
        response.raise_for_status()

        self.access_token = response.json()["access_token"]

        return self.access_token

    # ── Collection endpoints ─────────────────────────────────────────

    def request_to_pay(
        self,
        amount,
        currency,
        payer_msisdn,
        external_id,
        callback_url,
        payer_message="Payment",
        payee_note="Thank you",
    ):
        """
        Initiate a Request-to-Pay (debit) from the payer's mobile wallet.

        POST /collection/v1_0/requesttopay

        Returns {"referenceId": "", "status": "PENDING"} on 202 Accepted.

        :param amount: Numeric amount (int or Decimal)
        :param currency: ISO 4217 currency code, e.g. "UGX"
        :param payer_msisdn: Customer's mobile number in international format, e.g. "256700123456"
        :param external_id: Your internal reference (invoice number, order ID, etc.)
        :param callback_url: HTTPS URL MTN will POST the result to
        :param payer_message: Text shown to payer in the USSD prompt
        :param payee_note: Internal note stored on the MTN side
        """

        reference_id = str(uuid.uuid4())

        url = f"{self.base_url}/collection/v1_0/requesttopay"

        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "X-Reference-Id": reference_id,
            "X-Target-Environment": self.target_environment,
            "Ocp-Apim-Subscription-Key": self.subscription_key,
            "X-Callback-Url": callback_url,
            "Content-Type": "application/json",
        }

        payload = {
            "amount": str(amount),  # MTN API requires string
            "currency": currency,
            "externalId": external_id or reference_id,
            "payer": {
                "partyIdType": "MSISDN",
                "partyId": payer_msisdn,
            },
            "payerMessage": payer_message,
            "payeeNote": payee_note,
        }

        response = requests.post(url, headers=headers, json=payload)

        if response.status_code == 202:
            return {"referenceId": reference_id, "status": "PENDING"}

        # For any other status code, raise so callers can handle it
        response.raise_for_status()

    def get_transaction_status(self, reference_id):
        """
        Poll the status of a previously initiated Request-to-Pay.

        GET /collection/v1_0/requesttopay/{referenceId}

        Returns the full MTN status object, e.g.:

        {
            "financialTransactionId": "...",
            "externalId": "...",
            "amount": "5000",
            "currency": "UGX",
            "payer": {"partyIdType": "MSISDN", "partyId": "256700..."},
            "status": "SUCCESSFUL"   # or "FAILED" / "PENDING"
        }
        """

        url = f"{self.base_url}/collection/v1_0/requesttopay/{reference_id}"

        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "X-Target-Environment": self.target_environment,
            "Ocp-Apim-Subscription-Key": self.subscription_key,
        }

        response = requests.get(url, headers=headers)
        response.raise_for_status()

        return response.json()
