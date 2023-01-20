frappe.ready(() => {
	var stripe = Stripe("{{ publishable_key }}");


	initialize();
	checkStatus();
	document.querySelector("#payment-form").addEventListener("submit", handleSubmit);
	var emailAddress = {{frappe.form_dict}}.payer_email;

	async function initialize() {

		const response = await frappe.call({
			method:"payments.templates.pages.stripe_checkout.create_payment",
			freeze:true,
			headers: {
				"X-Requested-With": "XMLHttpRequest",
				"X-Frappe-CSRF-Token": frappe.csrf_token
			},
			args: {
				"data": JSON.stringify({{ frappe.form_dict|json }}),
				"reference_doctype": "{{ reference_doctype }}",
				"reference_docname": "{{ reference_docname }}",
			}
		});
		const clientSecret = await response.message;
		const validate = await frappe.call({
			method:"payments.templates.pages.stripe_checkout.make_payment",
			freeze:"true",
			headers: {
				"X-Requested-With": "XMLHttpRequest",
				"X-Frappe-CSRF-Token": frappe.csrf_token
			},
			args: {
				"data": JSON.stringify({{ frappe.form_dict|json }}),
				"reference_doctype": "{{ reference_doctype }}",
				"reference_docname": "{{ reference_docname }}",
			},
			callback: function(r) {
				if (r.message.status == "Completed") {
					window.location.href = r.message.redirect_to;
				}
			}

		});

		const appearance = {
			theme: 'stripe',
		};
		elements = stripe.elements({ appearance, clientSecret });

		const linkAuthenticationElement = elements.create("linkAuthentication", {
			defaultValues:
				{ email: emailAddress }
		});
		linkAuthenticationElement.mount("#link-authentication-element");

		linkAuthenticationElement.on('change', (event) => {
			emailAddress = event.value.email;
		});

		const paymentElementOptions = {
			layout: "tabs",
		};

		const paymentElement = elements.create("payment", paymentElementOptions);
		paymentElement.mount("#payment-element");

	};
	async function handleSubmit(e) {
		e.preventDefault();
		setLoading(true);

  		const { error } = await stripe.confirmPayment({
			elements,
			confirmParams: {
			// Make sure to change this to your payment completion page
				return_url: document.URL,
				receipt_email: emailAddress,
				},
			});

			// This point will only be reached if there is an immediate error when
			// confirming the payment. Otherwise, your customer will be redirected to
			// your `return_url`. For some payment methods like iDEAL, your customer will
			// be redirected to an intermediate site first to authorize the payment, then
			// redirected to the `return_url`.
			if (error.type === "card_error" || error.type === "validation_error") {
				showMessage(error.message);
			} else {
				showMessage("An unexpected error occurred.");
			}
			setLoading(false);
		}

	// Fetches the payment intent status after payment submission
	async function checkStatus() {
		const payment_id = new URLSearchParams(window.location.search).get(
			"payment_intent_client_secret"
		);

		if (!payment_id) {
			return;
		}

		const { paymentIntent } = await stripe.retrievePaymentIntent(payment_id);

		switch (paymentIntent.status) {
			case "succeeded":
				frappe.msgprint({
					title: __('Success'),
					indicator: 'green',
					message: __("Payment succeeded.")
				});
				break;
			case "processing":
				frappe.msgprint({
					title: __('Processing'),
					indicator: 'yellow',
					message: __("Your payment is processing.")
				});
				break;
			case "requires_payment_method":
				frappe.msgprint({
					title: __('Try Again'),
					indicator: 'yellow',
					message: __("Your payment was not successful, please try again.")
				});
				break;
			default:
				frappe.throw("Something went wrong.");
				break;
  		}
	}
	function showMessage(messageText) {
  		const messageContainer = document.querySelector("#payment-message");

  		messageContainer.classList.remove("hidden");
  		messageContainer.textContent = messageText;

  		setTimeout(function () {
    			messageContainer.classList.add("hidden");
    			messageText.textContent = "";
  			}, 4000);
	}
	// Show a spinner on payment submission
	function setLoading(isLoading) {
	  	if (isLoading) {
	    	// Disable the button and show a spinner
			document.querySelector("#submit").disabled = true;
			document.querySelector("#spinner").classList.remove("hidden");
			document.querySelector("#button-text").classList.add("hidden");
	  	} else {
			document.querySelector("#submit").disabled = false;
			document.querySelector("#spinner").classList.add("hidden");
			document.querySelector("#button-text").classList.remove("hidden");
	  	}
	}
})
