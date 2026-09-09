import frappe
import requests


class ChapaGateway:
	BASE_URL = "https://api.chapa.co/v1"

	def __init__(self):
		settings = frappe.get_single("Chapa Settings")

		self.secret = settings.get_password("secret_key")

		self.headers = {"Authorization": f"Bearer {self.secret}"}

	def initialize(self, data):

		payload = {
			"amount": str(data.amount),
			"currency": "ETB",
			"email": data.payer_email,
			"first_name": data.payer_name,
			"tx_ref": data.reference_doctype + "-" + data.reference_docname,
			"callback_url": data.callback_url,
			"return_url": data.return_url,
		}

		r = requests.post(self.BASE_URL + "/transaction/initialize", json=payload, headers=self.headers)

		return r.json()
