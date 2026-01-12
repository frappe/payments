import logging
import requests
import hashlib
from typing import Union, List, Dict, Optional
from urllib.parse import parse_qs

from .config import PaynowConfig
from .test_internals import resolve_test_credential
from .models import Payment, PaymentResponse
from .enums import PaymentMethod, PaymentStatus, TestMode

logger = logging.getLogger("paynow_sdk")
logger.setLevel(logging.DEBUG)

formatter = logging.Formatter(
    "%(asctime)s [%(levelname)s] %(message)s"
)

console_handler = logging.StreamHandler()
console_handler.setFormatter(formatter)

if not logger.handlers:
    logger.addHandler(console_handler)

class Paynow:
    _URL_WEB_INIT = "https://www.paynow.co.zw/interface/initiatetransaction"
    _URL_EXPRESS_INIT = "https://www.paynow.co.zw/interface/remotetransaction"
    _URL_TRACE = "https://www.paynow.co.zw/interface/trace"

    def __init__(self, 
                 config: Union[PaynowConfig, List[PaynowConfig]], 
                 redirect_callback_url: str = "http://localhost", 
                 status_callback_url: str = "http://localhost"):
        
        self._configs: Dict[str, PaynowConfig] = {}
        self.global_return_url = redirect_callback_url
        self.global_result_url = status_callback_url

        if isinstance(config, PaynowConfig):
            self._configs[config.currency.upper()] = config
        elif isinstance(config, list):
            for conf in config:
                self._configs[conf.currency.upper()] = conf
        else:
            raise TypeError("Config must be a PaynowConfig or list of PaynowConfig")

    # ------------------------------------------------------------------
    # 1. Standard Web Redirect Transaction
    # ------------------------------------------------------------------
    def initiate_web(self, payment: Payment) -> PaymentResponse:
        """
        Initiates a standard browser-based transaction.
        'authemail' is optional here.
        """
        conf = self._get_config(payment.currency)

        logger.debug(f"Initiating Web Transaction for request ref: {payment.merchant_reference} in {payment.currency}")
        
        # Build base payload
        payload = self._build_common_payload(payment, conf)
        
        # Add hash and send
        payload['hash'] = self._generate_hash(payload, conf.integration_key)
        
        response = requests.post(self._URL_WEB_INIT, data=payload)

        response.raise_for_status()

        logger.debug(f"Received response: {response.text}")

        result = self._parse_response(response.text, conf.integration_key)
        
        if result.merchant_reference is None:
            result.merchant_reference = payment.merchant_reference

        result.amount = payment.total
        result.instructions = f"Please proceed to: {result.redirect_url} to complete the payment."
        return result

    def poll_url(self, url: str) -> PaymentResponse:
        """
            Check the status transaction of a transaction with the given poll url
        """
        response = requests.post(url, data={})
        response.raise_for_status()
        return self._parse_response(response.text)

    # ------------------------------------------------------------------
    # 2. Express Checkout (Mobile/Remote/Token)
    # ------------------------------------------------------------------
    def initiate_express(self, 
                         payment: Payment, 
                         method: PaymentMethod, 
                         phone: Optional[str] = None,
                         token: Optional[str] = None,
                         test_mode: TestMode = TestMode.NONE) -> PaymentResponse:
        """
        Initiates an Express (Server-to-Server) transaction.
        
        Validations:
            - 'authemail' is REQUIRED.
            - Mobile Methods (EcoCash/OneMoney/Omari): Require 'phone'.
            - VMC/ZimSwitch: Require 'merchanttrace' (auto-filled from ref if missing).
                             'token' is optional (used for recurring).
        """
        # 1. Validate Express-Specific Requirements
        if not payment.customer_email:
            raise ValueError("Express Checkout requires 'customer_email' to be set on the Payment object.")

        conf = self._get_config(payment.currency)

        logger.debug(f"Initiating Express Transaction for request ref: {payment.merchant_reference} in {payment.currency} using {method.value}")

        payload = self._build_common_payload(payment, conf)

        phone = phone or payment.customer_phone
        
        # 2. Set Method
        payload["method"] = method.value

        # 3. Handle Identifier (Phone vs Token) + Test Mode
        # If Test Mode is active, it overrides the inputs with the magic numbers
        if test_mode != TestMode.NONE:    
            # If it's a mobile method, the magic credential goes to 'phone'
            if method in [PaymentMethod.ECOCASH, PaymentMethod.ONEMONEY, PaymentMethod.INNBUCKS, PaymentMethod.OMARI]:
                magic_credential = resolve_test_credential(method, test_mode, phone or "")
                payload["phone"] = magic_credential
            # If it's a card method, the magic credential goes to 'token' (if simulating tokenized recur)
            elif method in [PaymentMethod.VMC, PaymentMethod.ZIMSWITCH]:
                magic_credential = resolve_test_credential(method, test_mode, token or "")
                payload["token"] = magic_credential
                
        else:
            # LIVE MODE MAPPING
            if method in [PaymentMethod.ECOCASH, PaymentMethod.ONEMONEY, PaymentMethod.INNBUCKS, PaymentMethod.OMARI]:
                if not phone:
                    raise ValueError(f"{method.value} requires a 'phone' number.")
                payload["phone"] = phone
            
            elif method in [PaymentMethod.VMC, PaymentMethod.ZIMSWITCH]:
                # Token is optional (only for recurring)
                if token:
                    payload["token"] = token
                
                # Merchant Trace is REQUIRED for VMC/ZimSwitch
                # We use the explicit trace if set, otherwise fallback to reference
                trace_value = payment.merchant_trace or payment.merchant_reference
                payload["merchanttrace"] = trace_value

        # 4. Hash and Send
        payload['hash'] = self._generate_hash(payload, conf.integration_key)

        response = requests.post(self._URL_EXPRESS_INIT, data=payload)

        response.raise_for_status()

        logger.debug(f"Received response: {response.text}")

        result = self._parse_response(response.text, conf.integration_key)
    
        result.amount = payment.total
        if result.merchant_reference is None:
            result.merchant_reference = payment.merchant_reference
        return result

    def trace_transaction(self, merchant_trace: str, currency: str = "USD") -> PaymentResponse:
        """
        Recover a transaction status using the 'merchanttrace' if the Poll URL was lost 
        (e.g., due to a timeout during initiation).
        
        Args:
            merchant_trace: The unique merchanttrace string sent during initiation - max 32 chars.
            currency: The currency used (needed to select the correct Integration ID/Key).
        """
        conf = self._get_config(currency)
        
        payload = {
            "id": conf.integration_id,
            "merchanttrace": merchant_trace,
            "status": "Message"
        }
        
        payload['hash'] = self._generate_hash(payload, conf.integration_key)
        
        response = requests.post(self._URL_TRACE, data=payload)
        response.raise_for_status()
            
        result = self._parse_response(response.text, conf.integration_key)

        return result    
    
    # ------------------------------------------------------------------
    # Internal Logic
    # ------------------------------------------------------------------
    def _get_config(self, currency: str) -> PaynowConfig:
        key = currency.upper()
        if key not in self._configs:
            raise ValueError(f"No Integration Key found for currency: {key}")
        return self._configs[key]

    def _build_common_payload(self, payment: Payment, conf: PaynowConfig) -> dict:
        """
        Constructs the dictionary with all common fields, 
        filtering out None values to keep the payload clean.
        """
        data = {
            "resulturl": conf.status_callback_url or self.global_result_url,
            "returnurl": conf.redirect_callback_url or self.global_return_url,
            "reference": payment.merchant_reference,
            "amount": f"{payment.total:.2f}",
            "id": conf.integration_id,
            "additionalinfo": payment.info_string,
            "status": "Message"
        }

        # Optional fields - only add if they exist
        if payment.customer_email:
            data["authemail"] = payment.customer_email
        if payment.customer_phone:
            data["authphone"] = payment.customer_phone
        if payment.customer_name:
            data["authname"] = payment.customer_name
        
        if payment.tokenize:
            data["tokenize"] = True

        if payment.merchant_trace:
            data["merchanttrace"] = payment.merchant_trace

        return data

    def _generate_hash(self, values: dict, key: str) -> str:
        s = ""
        # Paynow hash order relies on values being concatenated 
        # (excluding 'hash' key) + IntegrationKey
        for k, v in values.items():
            if k != 'hash': 
                s += str(v)
        s += key
        return hashlib.sha512(s.encode('utf-8')).hexdigest().upper()

    def _parse_response(self, text: str, key: str=None) -> PaymentResponse:
        parsed = parse_qs(text)
        data = {k: v[0] for k, v in parsed.items()}
        
        response = PaymentResponse.from_raw(data)
        
        if key:
            calculated_hash = self._generate_hash(data, key)
            if response.hash != calculated_hash:
                return PaymentResponse(
                    upstream_data=data,
                    success=False,
                    status=PaymentStatus.ERROR,
                    message="Security Mismatch: Hashes do not match"
                )
             
        return response