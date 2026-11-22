import frappe
import requests
import uuid


CHAPA_URL = "https://api.chapa.co/v1/transaction/initialize"


@frappe.whitelist(allow_guest=True)
def chapa_checkout(
	amount,
	title,
	description,
	reference_doctype=None,
	reference_docname=None,
	payer_email=None,
	payer_name=None,
	currency="ETB",
	redirect_to="/",
):
	"""
	Create Chapa payment session and return checkout URL
	"""

	# 🔐 Get API key from settings (create doctype if needed)
	settings = frappe.get_single("Chapa Settings")
	secret_key = settings.secret_key

	# 🔑 unique transaction reference
    tx_ref = f"{self.data.reference_docname}-{uuid.uuid4().hex[:10]}"

	payload = {
		"amount": float(amount),
		"currency": currency,
		"email": payer_email or "mehariwamlake@gmail.com",
		"first_name": payer_name or "Guest",
		"last_name": "User",
		"tx_ref": tx_ref,
		"callback_url": frappe.utils.get_url(
			f"/api/method/payments.payment_gateways.doctype.chapa_settings.chapa_settings.chapa_callback"
		),
		"return_url": frappe.utils.get_url(redirect_to),
		"customization": {"title": title, "description": description},
	}

	headers = {"Authorization": f"Bearer {secret_key}", "Content-Type": "application/json"}

	response = requests.post(CHAPA_URL, json=payload, headers=headers)
	data = response.json()

	if response.status_code != 200 or data.get("status") != "success":
		frappe.throw(f"Chapa Error: {data}")

	# 🔥 Redirect user to checkout page
	checkout_url = data["data"]["checkout_url"]

	return {"checkout_url": checkout_url, "tx_ref": tx_ref}
