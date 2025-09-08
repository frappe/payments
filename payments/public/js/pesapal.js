// Copyright (c) 2024, Frappe Technologies Pvt. Ltd. and Contributors
// License: MIT. See LICENSE

// Pesapal Payment Gateway Integration for ERPNext

frappe.provide("frappe.integration_service");

frappe.integration_service.pesapal_gateway = class PesapalGateway {
	constructor(options) {
		this.gateway = "Pesapal";
		this.options = options || {};
	}

	process(options, success, error) {
		let args = {
			"payment_gateway": this.gateway,
			"order_id": options.order_id || frappe.utils.get_random(10),
			"amount": options.amount,
			"currency": options.currency,
			"title": options.title || "Payment",
			"description": options.description || "Payment via Pesapal",
			"payer_email": options.payer_email,
			"payer_name": options.payer_name,
			"payer_phone": options.payer_phone,
			"reference_doctype": options.reference_doctype,
			"reference_docname": options.reference_docname,
			"redirect_to": options.redirect_to || window.location.href
		};

		// Validate required fields
		if (!args.amount || !args.currency || !args.payer_email) {
			if (error) {
				error("Missing required payment information");
			}
			return;
		}

		// Create payment request
		frappe.call({
			method: "payments.utils.create_payment_gateway",
			args: args,
			callback: function(r) {
				if (r.message && r.message.redirect_to) {
					// Redirect to payment page
					window.location.href = r.message.redirect_to;
				} else {
					if (error) {
						error("Failed to create payment request");
					}
				}
			},
			error: function() {
				if (error) {
					error("Payment request failed");
				}
			}
		});
	}

	// Method for POS integration
	make_payment(payment_request, success_callback, error_callback) {
		let me = this;
		
		frappe.call({
			method: "payments.payment_gateways.doctype.pesapal_settings.pesapal_settings.get_payment_url",
			args: payment_request,
			callback: function(r) {
				if (r.message) {
					// Open payment in new window for POS
					let payment_window = window.open(r.message, 'pesapal_payment', 'width=800,height=600');
					
					// Monitor payment window
					let check_window = setInterval(function() {
						if (payment_window.closed) {
							clearInterval(check_window);
							// Check payment status
							me.check_payment_status(payment_request, success_callback, error_callback);
						}
					}, 1000);
				} else {
					if (error_callback) {
						error_callback("Failed to get payment URL");
					}
				}
			},
			error: function() {
				if (error_callback) {
					error_callback("Payment request failed");
				}
			}
		});
	}

	check_payment_status(payment_request, success_callback, error_callback) {
		frappe.call({
			method: "payments.payment_gateways.doctype.pesapal_settings.pesapal_settings.get_transaction_status",
			args: {
				order_tracking_id: payment_request.order_tracking_id
			},
			callback: function(r) {
				if (r.message) {
					let status = r.message.payment_status_description;
					if (status === "COMPLETED") {
						if (success_callback) {
							success_callback(r.message);
						}
					} else if (status === "FAILED" || status === "REVERSED") {
						if (error_callback) {
							error_callback("Payment " + status.toLowerCase());
						}
					} else {
						// Payment still pending, check again after delay
						setTimeout(() => {
							this.check_payment_status(payment_request, success_callback, error_callback);
						}, 3000);
					}
				} else {
					if (error_callback) {
						error_callback("Failed to check payment status");
					}
				}
			},
			error: function() {
				if (error_callback) {
					error_callback("Failed to check payment status");
				}
			}
		});
	}
};

// Register with frappe
frappe.integration_service.pesapal = frappe.integration_service.pesapal_gateway;

// POS Integration
if (typeof erpnext !== 'undefined' && erpnext.PointOfSale) {
	// Add Pesapal to POS payment methods
	frappe.provide("erpnext.PointOfSale.Payment");
	
	erpnext.PointOfSale.Payment.prototype.setup_payment_methods = function() {
		// Call original method
		this._super();
		
		// Add Pesapal if configured
		frappe.call({
			method: "frappe.client.get_value",
			args: {
				doctype: "Payment Gateway Account",
				filters: {"payment_gateway": "Pesapal"},
				fieldname: "name"
			},
			callback: (r) => {
				if (r.message && r.message.name) {
					this.payment_methods.push({
						name: "Pesapal",
						type: "Card",
						gateway: "Pesapal"
					});
				}
			}
		});
	};
}

// Webshop Integration
$(document).ready(function() {
	// Handle Pesapal payment button in webshop
	$(document).on('click', '.btn-pesapal-payment', function(e) {
		e.preventDefault();
		
		let payment_data = $(this).data();
		let pesapal = new frappe.integration_service.pesapal_gateway();
		
		pesapal.process(payment_data, 
			function(response) {
				// Success callback
				console.log("Payment successful", response);
			},
			function(error) {
				// Error callback
				frappe.msgprint("Payment failed: " + error);
			}
		);
	});
});
