from typing import Any

import requests
from frappe.utils.password import get_decrypted_password
from requests import HTTPError, JSONDecodeError, RequestException

from .paymob_urls import PaymobUrls
from .response_codes import (
	HTTP_EXCEPTION,
	HTTP_EXCEPTION_MESSAGE,
	JSON_DECODE_EXCEPTION,
	JSON_DECODE_EXCEPTION_MESSAGE,
	REQUEST_EXCEPTION,
	REQUEST_EXCEPTION_MESSAGE,
	SUCCESS,
	SUCCESS_MESSAGE,
	UNHANDLED_EXCEPTION,
	UNHANDLED_EXCEPTION_MESSAGE,
)
from .response_feedback_dataclass import ResponseFeedBack


class AcceptConnection:
	def __init__(self) -> None:
		"""Initializing the Following:
		1- Requests Session
		2- Auth Token
		3- Set Headers
		4- Paymob Urls
		"""
		self.session = requests.Session()
		self.paymob_urls = PaymobUrls()
		self.auth_token = self._get_auth_token()
		self.session.headers.update(self._get_headers())

	def _get_headers(self) -> dict[str, Any]:
		"""Initialize Header for Requests

		Returns:
		        Dict[str, Any]: Initialized Header Dict
		"""
		return {
			"Content-Type": "application/json",
			"Authorization": f"{self.auth_token}",
		}

	def _get_auth_token(self) -> str | None:
		"""Retrieve an Auth Token

		Returns:
		        Union[str, None]: Auth Token
		"""
		api_key = get_decrypted_password("Paymob Settings", "Paymob Settings", "api_key")
		request_body = {"api_key": api_key}

		code, feedback = self.post(
			url=self.paymob_urls.get_url("auth"),
			json=request_body,
		)

		token = None
		if code == SUCCESS:
			token = feedback.data.get("token")
		return token

	def _process_request(self, call, *args, **kwargs) -> tuple[str, dict[str, Any], ResponseFeedBack]:
		"""Process the Request

		Args:
		        call (Session.get/Session.post): Session.get/Session.post
		        *args, **kwargs: Same Args of requests.post/requests.get methods

		Returns:
		        Tuple[str, Dict[str, Any], ResponseFeedBack]: Tuple containes the Following (Code, Data, Success/Error Message)
		"""

		reponse_data = None
		try:
			response = call(*args, timeout=90, **kwargs)
			reponse_data = response.json()
			response.raise_for_status()
		except JSONDecodeError as error:
			reponse_feedback = ResponseFeedBack(
				message=JSON_DECODE_EXCEPTION_MESSAGE,
				status_code=response.status_code,
				exception_error=error,
			)
			return JSON_DECODE_EXCEPTION, reponse_feedback
		except HTTPError as error:
			reponse_feedback = ResponseFeedBack(
				message=HTTP_EXCEPTION_MESSAGE,
				data=reponse_data,
				status_code=response.status_code,
				exception_error=error,
			)
			return HTTP_EXCEPTION, reponse_feedback
		except RequestException as error:
			reponse_feedback = ResponseFeedBack(
				message=REQUEST_EXCEPTION_MESSAGE,
				exception_error=error,
			)
			return REQUEST_EXCEPTION, reponse_feedback
		except Exception as error:
			reponse_feedback = ResponseFeedBack(
				message=UNHANDLED_EXCEPTION_MESSAGE,
				exception_error=error,
			)
			return UNHANDLED_EXCEPTION, reponse_feedback

		reponse_feedback = ResponseFeedBack(
			message=SUCCESS_MESSAGE,
			data=reponse_data,
			status_code=response.status_code,
		)
		return SUCCESS, reponse_feedback

	def get(self, *args, **kwargs) -> tuple[str, dict[str, Any], ResponseFeedBack]:
		"""Wrapper for requests.get method

		Args:
		        Same Args of requests.post/requests.get methods

		Returns:
		        Tuple[str, Dict[str, Any], ResponseFeedBack]: Tuple containes the Following (Code, Data, Success/Error Message)
		"""
		return self._process_request(*args, call=self.session.get, **kwargs)

	def post(self, *args, **kwargs) -> tuple[str, dict[str, Any], ResponseFeedBack]:
		"""Wrapper for requests.get method

		Args:
		        Same Args of requests.post/requests.get methods

		Returns:
		        Tuple[str, Dict[str, Any], ResponseFeedBack]: Tuple containes the Following (Code, Data, Success/Error Message)
		"""
		return self._process_request(*args, call=self.session.post, **kwargs)
