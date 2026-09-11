var stripe = Stripe("{{ publishable_key }}");

var elements = stripe.elements();

var style = {
	base: {
		color: '#32325d',
		lineHeight: '18px',
		fontFamily: '"Helvetica Neue", Helvetica, sans-serif',
		fontSmoothing: 'antialiased',
		fontSize: '16px',
		'::placeholder': {
			color: '#aab7c4'
		}
	},
	invalid: {
		color: '#fa755a',
		iconColor: '#fa755a'
	}
};

var card = elements.create('card', {
	hidePostalCode: true,
	style: style
});

card.mount('#card-element');

// Subscriptions keep the card-token flow: the token is what sets up the recurring
// mandate. Every other payment uses a PaymentIntent, which is what 3-D Secure
// works with.
var payment_intent_flow = {{ "false" if is_subscription else "true" }};
var submit_label = "{{ _('Pay') }} {{ amount }}";

function show_error(message) {
	$('#submit').prop('disabled', false).html(submit_label);
	$('.error').html(message || __('An error occured during the payment process. Please contact us.')).show();
}

function succeeded(redirect_to) {
	$('#submit').hide();
	$('.success').show();
	if (redirect_to) {
		setTimeout(function() {
			window.location.href = redirect_to;
		}, 2000);
	}
}

function pay_with_payment_intent() {
	frappe.call({
		type: "POST",
		method: "payments.payment_gateways.stripe_api.create_payment_intent_for_reference",
		args: {
			reference_doctype: "{{ reference_doctype }}",
			reference_docname: "{{ reference_docname }}"
		},
		callback: function(r) {
			if (!r.message || !r.message.client_secret) {
				show_error();
				return;
			}

			// Stripe shows the 3-D Secure challenge itself when the card needs it.
			stripe.confirmCardPayment(r.message.client_secret, {
				payment_method: {
					card: card,
					billing_details: {
						name: $('#cardholder-name').val(),
						email: $('#cardholder-email').val()
					}
				}
			}).then(function(result) {
				if (result.error) {
					show_error(result.error.message);
					return;
				}

				frappe.call({
					type: "POST",
					method: "payments.payment_gateways.stripe_api.confirm_payment",
					args: {
						payment_intent_id: result.paymentIntent.id,
						payment_request: r.message.payment_request
					},
					callback: function(res) {
						if (res.message && res.message.redirect_to) {
							succeeded(res.message.redirect_to);
							return;
						}
						show_error();
					},
					error: function() {
						show_error();
					}
				});
			});
		},
		error: function() {
			show_error();
		}
	});
}

function pay_with_token(token) {
	frappe.call({
		method:"payments.templates.pages.stripe_checkout.make_payment",
		freeze:true,
		headers: {"X-Requested-With": "XMLHttpRequest"},
		args: {
			"stripe_token_id": token.id,
			"data": JSON.stringify({{ frappe.form_dict|json }}),
			"reference_doctype": "{{ reference_doctype }}",
			"reference_docname": "{{ reference_docname }}",
			"payment_gateway": "{{ payment_gateway }}"
		},
		callback: function(r) {
			if (r.message.status == "Completed") {
				succeeded(r.message.redirect_to);
				return;
			}

			// Keep the legacy outcome for subscriptions.
			$('#submit').hide();
			$('.error').show();
			if (r.message.redirect_to) {
				setTimeout(function() {
					window.location.href = r.message.redirect_to;
				}, 2000);
			}
		}
	});
}

card.on('change', function(event) {
	var displayError = document.getElementById('card-errors');
	if (event.error) {
		displayError.textContent = event.error.message;
	} else {
		displayError.textContent = '';
	}
});

frappe.ready(function() {
	$('#submit').off("click").on("click", function(e) {
		e.preventDefault();
		$('#submit').prop('disabled', true).html(__('Processing...'));

		if (payment_intent_flow) {
			pay_with_payment_intent();
			return;
		}

		var extraDetails = {
			name: $('input[name=cardholder-name]').val(),
			email: $('input[name=cardholder-email]').val()
		}
		stripe.createToken(card, extraDetails).then(function(result) {
			if (result.error) {
				show_error(result.error.message);
				return;
			}
			pay_with_token(result.token);
		});
	})
});
