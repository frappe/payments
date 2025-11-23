import json
import frappe
import requests
from frappe import _
from urllib.parse import parse_qsl, urlencode
from frappe.utils import flt, get_url, getdate
from frappe.integrations.utils import create_request_log
from payments.payment_gateways.doctype.bankmuscat_settings.bankmuscat_settings import (
	BankMuscatSettings as BankMuscat,
	get_gateway_controller
)

# Check if any previously created Integration Request has status = "Completed";
# if yes, return the payment success page URL
def check_already_payment_processed(request, reference_doctype, reference_docname):
	status = frappe.db.get_value("Integration Request", request, "status")
	if status != "Completed":
		return None

	params = urlencode({
		"doctype": reference_doctype,
		"docname": reference_docname
	})
	redirect_url = f"payment-success?{params}"

	return {"payment_url": get_url(redirect_url)}	

# Fetch and return the Bank Muscat payment URL for a valid Integration Request.
@frappe.whitelist(allow_guest=True)
def get_payment_url(data=None):
	try:
		if isinstance(data, str):
			data = frappe.parse_json(data or "{}")

		data = frappe._dict(data or {})

		if not data.get("order_id"):
			frappe.throw(_("Order ID not found in request."), title="Invalid Request")

		integration_request = frappe.db.exists("Integration Request", data.order_id)
		if not integration_request:
			if not integration_request:
				frappe.throw(_("Invalid Order ID. No Integration Request found."))

		order_data = frappe.db.get_value("Integration Request", integration_request, "data")
		if not order_data:
			frappe.throw(
				_("Integration Request data is missing."),
				title=_("Payment Failed")
			)

		order_details = frappe._dict(frappe.parse_json(order_data))

		condition, msg, status = check_url_usage_status(data.order_id) 

		if condition:
			if status == "pending":
				return {"msg": msg, "url": get_url("/payment-failed")}
			if status == "completed":	
				return {"msg": msg, "url": get_url("/payment-success")}
		
		reference_doctype = order_details.get("reference_doctype")
	
		reference_docname = order_details.get("reference_docname")

		redirect_url = check_already_payment_processed(
			integration_request, reference_doctype, reference_docname
		)

		if redirect_url:
			return redirect_url

		if not (reference_doctype and reference_docname):
			frappe.throw(
				_("Reference document details are missing."),
				title=_("Invalid Request")
			)

		payment_gateway = frappe.db.get_value(reference_doctype, reference_docname, "payment_gateway")
		if not payment_gateway:
			frappe.throw(
				_("Payment Gateway not linked to this transaction."),
				title=_("Payment Failed")
			)

		gateway_controller = get_gateway_controller(reference_doctype, reference_docname, payment_gateway)
		if not gateway_controller:
			frappe.throw(
				_("Unable to identify payment gateway controller."),
				title=_("Payment Failed")
			)

		gateway_doc = frappe.get_doc("BankMuscat Settings", gateway_controller)

		payment_url = gateway_doc.get_payment_page_url(**order_details)
		if not payment_url:
			frappe.throw(
				_("Unable to generate payment URL. Please try again later."),
				title=_("Payment Failed")
			)

		return {"payment_url": payment_url}

	except Exception as e:
		frappe.log_error(
			title="BankMuscat: Payment URL Generation Failed",
			message=frappe.get_traceback(with_context=True),
		)
		frappe.throw(e.message)

# Route the UI page based on the response
def handle_payment_response(data_dict, reference_doctype, reference_docname):
	data = frappe._dict(data_dict)

	order_no = data.get("order_id") or data.get("order_no")
	doc_name = frappe.get_value("Payment Request", {"custom_name": order_no})

	if not doc_name:
		frappe.throw(f"No Payment Request found for order_no: {order_no}")

	payment_request = frappe.get_doc("Payment Request", doc_name)

	# Save tracking ID if not already saved
	if payment_request.status == "Initiated" and not payment_request.custom_payment_reference_no:
		payment_request.db_set({
			"transaction_date": getdate(data.get("trans_date")),
			"custom_payment_reference_no": data.get("tracking_id"),
			"bank_reference_no": data.get("bank_ref_no")
		})

	order_status = data.get("order_status", "").lower()

	try:
		if order_status == "success":
			msg = _(
				"The customer has successfully completed the payment for the requested order. "
				"Details: Order ID: {order_id}, Amount: {amount}, Payment Reference: {payment_ref_no}, "
				"Bank Reference: {bank_ref_no}, Date: {payment_date}. "
				"Please update your records accordingly."
			).format(
				order_id=data.get("order_id"),
				amount=data.get("amount"),
				payment_ref_no=data.get("tracking_id"),
				bank_ref_no=data.get("bank_ref_no"),
				payment_date=data.get("trans_date")
			)

			frappe.db.set_value(
				reference_doctype,
				reference_docname,
				{
					"status": "Paid",
					"transaction_status": "The payment has been completed",
					"response_command": msg,
				}
			)
			# payment_entry = payment_request.set_as_paid()
			# payment_request.db_set("transaction_status", "The payment has been completed")

			# frappe.db.set_value(
			# 	"Payment Entry",
			# 	payment_entry.name,
			# 	{"reference_no": data.get("bank_ref_no"), "reference_date": getdate(data.get("trans_date"))},
			# )

			frappe.db.set_value(
				"Integration Request", doc_name, {"status": "Completed", "output": json.dumps(data, indent=4)}
			)

			token = frappe.generate_hash(length=32)

			frappe.cache().set_value(
				f"payment_success:{token}",
				{"doctype": reference_doctype, "docname": reference_docname},
				expires_in_sec=300
			)

			return redirect_response(f"payment-success?token={token}")

			# return redirect_response("payment-success")

		elif order_status in ("failure", "invalid", "timeout"):
			payment_request.db_set("status", "Failed")
			payment_request.db_set("transaction_status", "Payment Not Completed")

			frappe.db.set_value(
				"Integration Request", doc_name, {"status": "Failed", "error": json.dumps(data, indent=4)}
			)

			return redirect_response("payment-failed")

		elif order_status == "aborted":
			try:
				payment_request.set_as_cancelled()
				payment_request.db_set("transaction_status", "Payment Cancelled")

				frappe.db.set_value(
					"Integration Request",
					doc_name,
					{"status": "Cancelled", "error": json.dumps(data, indent=4)},
				)

				return redirect_response("payment-cancel")

			except Exception:
				frappe.log_error("Error during cancel_payment()", frappe.get_traceback())
		else:
			payment_request.db_set("transaction_status", "Waiting for Payment Response")
			return redirect_response("payment-processing", reference_doctype, reference_docname)

	except Exception:
		frappe.log_error("Error while processing payment response", frappe.get_traceback())

# Update the Payment Request and Payment Entry based on the response
def handle_payment_page_response(
	payment_request, gateway_controller, data, payment_gateway_account, kwargs=None, integration_request=False
):
	if gateway_controller:
		gateway_doc = frappe.get_doc("BankMuscat Settings", gateway_controller)
		decrypted_data = gateway_doc.decrypt(data.get("encResp"), gateway_doc.get_password("working_key"))

		data_dict = dict(pair.split("=") for pair in decrypted_data.split("&"))

		frappe.log_error("Payment Success Response: ", data_dict)

		if data_dict.get("order_status") == "Success":
			try:
				if not integration_request:
					create_request_log(kwargs, service_name="BankMuscat", name=kwargs.get("order_id", ""))
				frappe.db.set_value(
					"Integration Request",
					data_dict.get("order_id"),
					{"status": "Completed", "output": json.dumps(data_dict, indent=4)},
				)
			except Exception:
				frappe.log_error("Integration request failed", frappe.get_traceback())
			try:
				gateway_account = frappe.get_doc("Payment Gateway Account", payment_gateway_account)
				frappe.db.set_value(
					"Payment Request",
					data_dict.get("order_id"),
					{
						"payment_gateway_account": gateway_account.name,
						"payment_gateway": gateway_account.payment_gateway,
						"payment_account": gateway_account.payment_account,
						"payment_channel": gateway_account.payment_channel,
						"transaction_status": "The payment has been completed",
					},
				)
				payment_request.reload()
				payment_entry = payment_request.set_as_paid()

				if payment_entry:
					frappe.db.set_value(
						"Payment Entry",
						payment_entry.name,
						{
							"reference_no": data_dict.get("bank_ref_no"),
							"reference_date": getdate(data_dict.get("trans_date")),
						},
					)
			except Exception:
				frappe.log_error("Error while mark as paid..", frappe.get_traceback())

			redirect_url = "payment-success"
			redirect_url += "?" + urlencode({"doctype": "Payment Request"})
			redirect_url += "&" + urlencode({"docname": payment_request.name})

			frappe.local.response["type"] = "redirect"
			frappe.local.response["location"] = get_url(redirect_url)

		else:
			try:
				frappe.db.set_value(
					"Payment Request",
					data_dict.get("order_id"),
					{"payment_gateway_account": payment_gateway_account},
				)
				payment_request = frappe.get_doc("Payment Request", data_dict.get("order_id"))
				payment_request.set_as_failed()
			except Exception:
				frappe.log_error("Error while mark as failed..", frappe.get_traceback())

			try:
				if not integration_request:
					create_request_log(
						kwargs,
						service_name="BankMuscat",
						name=kwargs.get("order_id", ""),
						error=json.dumps(data_dict, indent=4),
					)
				frappe.db.set_value("Integration Request", data_dict.get("order_id"), {"status": "Failed"})
			except Exception:
				frappe.log_error("Integration request failed", frappe.get_traceback())

			redirect_url = "payment-failed"
			frappe.local.response["type"] = "redirect"
			frappe.local.response["location"] = get_url(redirect_url)

# Validate response data
@frappe.whitelist(allow_guest=True)
def verify_payment_status():
	data = frappe.form_dict
	order_id = frappe.db.get_value("Payment Request", {"custom_name": data.get("orderNo")})
	if order_id and frappe.db.exists("Integration Request", order_id):
		order_details = frappe.parse_json(frappe.db.get_value("Integration Request", order_id, "data"))
		reference_doctype = order_details.get("reference_doctype")
		reference_docname = order_details.get("reference_docname")
		if ("reference_doctype" in order_details) and ("reference_docname" in order_details):
			payment_gateway = frappe.db.get_value(reference_doctype, reference_docname, "payment_gateway")
			if payment_gateway:
				gateway_controller = get_gateway_controller(
					reference_doctype, reference_docname, payment_gateway
				)
				if gateway_controller:
					gateway_doc = frappe.get_doc("BankMuscat Settings", gateway_controller)
					decrypted_data = gateway_doc.decrypt(
						data.get("encResp"), gateway_doc.get_password("working_key")
					)
					decrypted_str = decrypted_data.decode("utf-8")
					data_dict = dict(pair.split("=") for pair in decrypted_str.split("&"))
					return handle_payment_response(data_dict, reference_doctype, reference_docname)
			else:
				payment_request = frappe.get_doc("Payment Request", order_id)
				payment_gateway, payment_gateway_account = frappe.db.get_value(
					"Default Gateway Account",
					{
						"parenttype": "Payment Request",
						"parent": payment_request.name,
						"company": frappe.db.get_value(reference_doctype, reference_docname, "company"),
						"gateway_settings": ["like", "%BankMuscat%"],
					},
					["payment_gateway", "payment_gateway_account"],
				)

				gateway_controller = get_gateway_controller(
					reference_doctype, reference_docname, payment_gateway
				)

				return handle_payment_page_response(
					payment_request,
					gateway_controller,
					data,
					payment_gateway_account,
					kwargs=order_details,
					integration_request=True,
				)

	elif order_id:
		payment_request = frappe.get_doc("Payment Request", order_id)
		request_data = frappe.db.get_value(
			payment_request.reference_doctype,
			payment_request.reference_name,
			["company", "customer_name"],
			as_dict=1,
		)
		request_data.update({"company": frappe.defaults.get_defaults().company})

		kwargs = {
			"amount": flt(payment_request.grand_total, payment_request.precision("grand_total")),
			"title": request_data.company,
			"description": payment_request.subject,
			"reference_doctype": "Payment Request",
			"reference_docname": payment_request.name,
			"payer_email": payment_request.email_to or frappe.session.user,
			"payer_name": request_data.customer_name,
			"order_id": payment_request.name,
			"currency": payment_request.currency,
		}

		payment_gateway, payment_gateway_account = frappe.db.get_value(
			"Default Gateway Account",
			{
				"parenttype": "Payment Request",
				"parent": payment_request.name,
				"company": frappe.db.get_value(payment_request.doctype, payment_request.name, "company"),
				"gateway_settings": ["like", "%BankMuscat%"],
			},
			["payment_gateway", "payment_gateway_account"],
		)

		gateway_controller = get_gateway_controller(
			payment_request.doctype, payment_request.name, payment_gateway
		)

		return handle_payment_page_response(
			payment_request,
			gateway_controller,
			data,
			payment_gateway_account,
			kwargs=kwargs,
			integration_request=False,
		)

	else:
		frappe.respond_as_web_page("Payment Failed", "Order ID not found", http_status_code=404)


@frappe.whitelist(allow_guest=True)
def cancel_payment():
	data = frappe.form_dict
	doc_name = frappe.get_value("Payment Request", {"custom_name": data.get("orderNo")})

	if frappe.db.exists("Integration Request", doc_name):
		frappe.db.set_value("Integration Request", doc_name, "status", "Cancelled")
		try:
			payment_request = frappe.get_doc("Payment Request", doc_name)
			payment_request.set_as_cancelled()
		except Exception:
			frappe.log_error("Error while mark as Cancelled..", frappe.get_traceback())

	return redirect_response("payment-cancel")


def redirect_response(page, doctype=None, docname=None):
	user = frappe.session.user

	if user != "Guest":
		return True

	query_params = {}

	if doctype:
		query_params["doctype"] = doctype
	if docname:
		query_params["docname"] = docname

	url = f"{page}"
	if query_params:
		url += "?" + urlencode(query_params)

	frappe.local.response["type"] = "redirect"
	frappe.local.response["location"] = get_url(url)

# Check payment status daily and update the status
def check_payment_status():
	payment_requests = frappe.get_all(
		"Payment Request",
		filters={"status": "Initiated"},
		fields=["name", "custom_name", "custom_payment_reference_no", "payment_gateway"],
	)

	filtered_requests = [
		pr
		for pr in payment_requests
		if pr.custom_payment_reference_no
		and pr.payment_gateway
		and pr.payment_gateway.startswith("BankMuscat-")
	]

	# Call get_payment_status for each filtered payment request
	for pr in filtered_requests:
		try:
			get_payment_status(pr.name)
		except Exception:
			frappe.log_error(
				title="check_payment_status",
				message=f"Error calling get_payment_status for {pr.name}: {frappe.get_traceback()}",
			)

# Check payment status in Payment Request
@frappe.whitelist()
def get_payment_status(payment_request):
	try:
		doc = frappe.get_doc("Payment Request", payment_request)
		reference_no = doc.custom_payment_reference_no

		if not reference_no or not doc.name:
			frappe.throw("Missing order number or reference number")

		# Prepare payload
		request_payload = {
			"order_id": doc.name,
			"reference_no": reference_no,
		}
		json_data = json.dumps(request_payload)

		# Get Gateway name from account format e.g., "bankmuscat-1234"
		gateway_name = doc.payment_gateway.split("-")[1]
		bankmuscat_settings = frappe.get_doc("BankMuscat Settings", gateway_name)

		encrypted_data = bankmuscat_settings.encrypt(
			json_data, bankmuscat_settings.get_password("working_key")
		)

		access_code = bankmuscat_settings.get_password("access_code")
		
		payload = {
			"enc_request": encrypted_data,
			"access_code": access_code,
			"request_type": "JSON",
			"response_type": "JSON",
			"command": "orderStatusTracker",
			"version": "1.2",
		}

		SMARTPAY_URL = f"{bankmuscat_settings.base_url}/apis/servlet/DoWebTrans?"
		
		# Make request to SmartPay
		response = requests.post(SMARTPAY_URL, data=payload)
		parsed_response = dict(parse_qsl(response.text))

		if parsed_response.get("status") != "0":
			frappe.throw("API request failed: " + parsed_response.get("enc_response", ""))

		# Decrypt and parse final response
		decrypted_data = bankmuscat_settings.decrypt(
			parsed_response["enc_response"], bankmuscat_settings.get_password("working_key")
		)
		decrypted_json = json.loads(decrypted_data)
		# Payment status handling
		return handle_payment_response(decrypted_json, "Payment Request", payment_request)

	except Exception:
		frappe.log_error(
			title="get_payment_status", message="BankMuscat Payment Status API Error: frappe.get_traceback()"
		)
		return {"status": "error", "message": frappe.get_traceback()}

def check_url_usage_status(id):
	try:
		from frappe.utils import now_datetime
		integration_request = frappe.db.get_value(
			"Integration Request",
			id,
			["status", "url_access_time"],
			as_dict=True
		)

		status = integration_request.status
		last_access_time = integration_request.url_access_time

		if status != "Completed" and not last_access_time:
			frappe.db.set_value(
				"Integration Request",
				id,
				"url_access_time",
				now_datetime()
			)
			return False, None, None

		elif status != "Completed" and last_access_time:

			from datetime import datetime
			if isinstance(last_access_time, str):
				last_access_time = datetime.fromisoformat(last_access_time)

			current_time = now_datetime()
			diff_minutes = (current_time - last_access_time).total_seconds() / 60

			if diff_minutes < 45:
				remaining = 45 - int(diff_minutes)

				message = _(
					"This payment link will be available again after {0} minutes. Please try again later."
				).format(remaining)

				return True, message, "pending"

			return False, None, None

		elif status == "Completed":
			message = (
				"This payment has already been completed successfully. No further action is required."
			)
			return True, None, "completed"

	except Exception as e:
		frappe.log_error(
			title="BankMuscat URL Access Check Failed",
			message=frappe.get_traceback()
		)
		return True, "Oops! Something didn’t work as expected. Please contact our Al Farsi service team for help.", "pending"

@frappe.whitelist()
def set_payment_entry(doc_name):

	exist_doc = frappe.db.get_value(
		"Payment Entry",
		{ "reference_no": doc_name },
		"name"
	)

	if exist_doc:
		frappe.db.set_value(
			"Payment Request",
			doc_name,
			"payment_entry",
			exist_doc
		)

	return True

@frappe.whitelist()
def check_roles():
	roles = frappe.get_all(
		"DocPerm",
		filters={
			"parent": "Payment Entry",
			"permlevel": 0,
			"create": 1
		},
		fields=["role"]
	)
	
	user_roles = frappe.get_roles(frappe.session.user)
	
	has_permission = any(r["role"] in user_roles for r in roles)

	return has_permission

