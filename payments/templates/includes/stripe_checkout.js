// Embedded Elements checkout using PaymentIntents + PaymentElement.
// Flow: create an (unconfirmed) PaymentIntent on the server -> mount the
// PaymentElement with its client_secret -> confirm client-side (handles 3DS)
// -> hand the confirmed PaymentIntent id back to the server to finalize.

var stripe = Stripe("{{ publishable_key }}");

var checkoutData = {
	data: JSON.stringify({{ frappe.form_dict|json }}),
	reference_doctype: {{ reference_doctype | tojson }},
	reference_docname: {{ reference_docname | tojson }},
	payment_gateway: {{ payment_gateway | tojson }}
};

var elements;
var paymentElement;
var clientSecret;

function showError(message) {
	var displayError = document.getElementById('card-errors');
	if (displayError) {
		displayError.textContent = message || '';
	}
}

function setSubmitting(isSubmitting) {
	if (isSubmitting) {
		$('#submit').prop('disabled', true).html(__('Processing...'));
	} else {
		$('#submit').prop('disabled', false).html(__('Pay') + ' {{ amount }}');
	}
}

function redirectAfter(result) {
	setTimeout(function () {
		if (result && result.redirect_to) {
			window.location.href = result.redirect_to;
		}
	}, 2000);
}

function mountPaymentElement() {
	frappe.call({
		method: "payments.templates.pages.stripe_checkout.create_payment_intent",
		freeze: true,
		headers: { "X-Requested-With": "XMLHttpRequest" },
		args: {
			data: checkoutData.data,
			reference_doctype: checkoutData.reference_doctype,
			reference_docname: checkoutData.reference_docname,
			payment_gateway: checkoutData.payment_gateway
		},
		callback: function (r) {
			if (!r.message || !r.message.client_secret) {
				showError(__('Could not initialise the payment. Please try again.'));
				return;
			}
			clientSecret = r.message.client_secret;
			elements = stripe.elements({ clientSecret: clientSecret });
			paymentElement = elements.create('payment', {
				defaultValues: {
					billingDetails: {
						name: {{ payer_name | tojson }},
						email: {{ payer_email | tojson }}
					}
				}
			});
			paymentElement.mount('#card-element');
		}
	});
}

function confirmPayment() {
	if (!elements || !clientSecret) {
		showError(__('Payment is still initialising. Please wait a moment.'));
		return;
	}
	setSubmitting(true);
	stripe.confirmPayment({
		elements: elements,
		redirect: 'if_required',
		confirmParams: {
			receipt_email: $('input[name=cardholder-email]').val()
		}
	}).then(function (result) {
		if (result.error) {
			showError(result.error.message);
			$('.error').show();
			setSubmitting(false);
			return;
		}

		var intent = result.paymentIntent;
		if (intent && (intent.status === 'succeeded' || intent.status === 'processing')) {
			frappe.call({
				method: "payments.templates.pages.stripe_checkout.make_payment",
				freeze: true,
				headers: { "X-Requested-With": "XMLHttpRequest" },
				args: {
					payment_intent: intent.id,
					data: checkoutData.data,
					reference_doctype: checkoutData.reference_doctype,
					reference_docname: checkoutData.reference_docname,
					payment_gateway: checkoutData.payment_gateway
				},
				callback: function (r) {
					var msg = r.message || {};
					$('#submit').hide();
					if (msg.status === "Completed") {
						$('.success').show();
					} else {
						$('.error').show();
					}
					redirectAfter(msg);
				}
			});
		} else {
			showError(__('The payment could not be completed.'));
			setSubmitting(false);
		}
	});
}

frappe.ready(function () {
	mountPaymentElement();
	$('#submit').off("click").on("click", function (e) {
		e.preventDefault();
		confirmPayment();
	});
});
