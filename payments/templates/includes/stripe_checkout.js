var stripe = Stripe({{ publishable_key|tojson }});
{% if client_secret %}
// One-off payments: Stripe's Payment Element confirms a PaymentIntent, with 3-D Secure when the
// payer's bank asks for it, then the site checks the payment with Stripe and records it.
var elements = stripe.elements({ clientSecret: {{ client_secret|tojson }} });
var payment = elements.create("payment", { defaultValues: { billingDetails: { name: {{ payer_name|tojson }}, email: {{ payer_email|tojson }} } } });
payment.mount("#card-element");

function showOutcome(message, failed) {
	$(".outcome .error, .outcome .success").hide();
	$(failed ? ".outcome .error" : ".outcome .success").text(message).show();
}

function recordPayment(paymentIntent) {
	return frappe.call({
		method: "payments.templates.pages.stripe_checkout.confirm_payment",
		freeze: true,
		args: {
			payment_intent: paymentIntent,
			reference_doctype: {{ reference_doctype|tojson }},
			reference_docname: {{ reference_docname|tojson }},
			payment_gateway: {{ payment_gateway|tojson }},
		},
	}).then(function (r) {
		var result = r.message || {};
		if (result.status === "Completed") {
			$("#submit").hide();
			showOutcome({{ _("Your payment has been successfully registered.") | json }});
			window.location.href = result.redirect_to;
		} else if (result.redirect_to) {
			$("#submit").hide();
			window.location.href = result.redirect_to;
		} else {
			showOutcome(result.message, result.status !== "Processing");
			$("#submit").prop("disabled", false);
		}
	});
}

frappe.ready(function () {
	// back from the bank's authentication page
	var returned = new URLSearchParams(window.location.search).get("payment_intent");
	if (returned) recordPayment(returned);

	$("#submit").off("click").on("click", function (e) {
		e.preventDefault();
		$("#submit").prop("disabled", true);
		stripe
			.confirmPayment({
				elements: elements,
				confirmParams: { return_url: window.location.href.split("#")[0] },
				redirect: "if_required",
			})
			.then(function (result) {
				if (result.error) {
					showOutcome(result.error.message, true);
					$("#submit").prop("disabled", false);
				} else {
					recordPayment(result.paymentIntent.id);
				}
			});
	});
});
{% else %}
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

function setOutcome(result) {

	if (result.token) {
		$('#submit').prop('disabled', true)
		$('#submit').html(__('Processing...'))
		frappe.call({
			method:"payments.templates.pages.stripe_checkout.make_payment",
			freeze:true,
			headers: {"X-Requested-With": "XMLHttpRequest"},
			args: {
				"stripe_token_id": result.token.id,
				"data": JSON.stringify({{ frappe.form_dict|tojson }}),
				"reference_doctype": {{ reference_doctype|tojson }},
				"reference_docname": {{ reference_docname|tojson }},
				"payment_gateway": {{ payment_gateway|tojson }}
			},
			callback: function(r) {
				if (r.message.status == "Completed") {
					$('#submit').hide()
					$('.success').show()
					setTimeout(function() {
						window.location.href = r.message.redirect_to
					}, 2000);
				} else {
					$('#submit').hide()
					$('.error').show()
					setTimeout(function() {
						window.location.href = r.message.redirect_to
					}, 2000);
				}
			}
		});

	} else if (result.error) {
		$('.error').html(result.error.message);
		$('.error').show()
	}
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
		var extraDetails = {
			name: $('input[name=cardholder-name]').val(),
			email: $('input[name=cardholder-email]').val()
		}
		stripe.createToken(card, extraDetails).then(setOutcome);
	})
});
{% endif %}
