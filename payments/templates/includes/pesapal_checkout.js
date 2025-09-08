// Copyright (c) 2024, Frappe Technologies Pvt. Ltd. and Contributors
// License: MIT. See LICENSE

$(document).ready(function() {
	// Initialize Pesapal payment flow
	initializePesapalPayment();
});

function initializePesapalPayment() {
	// Show loading message
	$('.pesapal-loading').show();

	// Handle any errors or timeouts
	setTimeout(function() {
		if ($('.pesapal-loading').is(':visible')) {
			$('.pesapal-loading').text('Taking longer than expected. Please wait...');
		}
	}, 5000);

	// Handle timeout after 30 seconds
	setTimeout(function() {
		if ($('.pesapal-loading').is(':visible')) {
			showError('Payment request timed out. Please try again.');
		}
	}, 30000);

	// Handle page visibility change (when user comes back from payment)
	document.addEventListener('visibilitychange', function() {
		if (!document.hidden) {
			// User came back to the page, check payment status
			checkPaymentStatus();
		}
	});

	// Handle beforeunload event
	window.addEventListener('beforeunload', function(e) {
		// Don't show confirmation if redirecting to payment
		if (window.location.href.includes('pesapal')) {
			return;
		}
	});
}

function checkPaymentStatus() {
	// Check if we have payment callback parameters
	const urlParams = new URLSearchParams(window.location.search);
	const orderTrackingId = urlParams.get('OrderTrackingId');
	const merchantReference = urlParams.get('OrderMerchantReference');
	const notificationType = urlParams.get('OrderNotificationType');

	if (orderTrackingId && merchantReference && notificationType === 'CALLBACKURL') {
		// Process payment callback
		processPaymentCallback(orderTrackingId, merchantReference);
	}
}

function processPaymentCallback(orderTrackingId, merchantReference) {
	$('.pesapal-loading').text('Processing payment...');
	$('.pesapal-confirming').removeClass('hidden');

	// Get token from URL or session
	const urlParams = new URLSearchParams(window.location.search);
	const token = urlParams.get('token') || sessionStorage.getItem('pesapal_token');

	if (!token) {
		showError('Invalid payment session. Please try again.');
		return;
	}

	// Call payment completion endpoint
	frappe.call({
		method: 'payments.templates.pages.pesapal_checkout.make_payment',
		args: {
			order_tracking_id: orderTrackingId,
			merchant_reference: merchantReference,
			token: token
		},
		callback: function(r) {
			if (r.message && r.message.redirect_to) {
				window.location.href = '/' + r.message.redirect_to;
			} else {
				showError('Payment processing failed. Please contact support.');
			}
		},
		error: function() {
			showError('Payment processing failed. Please contact support.');
		}
	});
}

function showError(message) {
	$('.pesapal-loading').hide();
	$('.pesapal-confirming').hide();
	$('.spinner-border').hide();

	$('.lead').html('<span class="text-danger">' + message + '</span>');

	// Add retry button
	setTimeout(function() {
		$('.lead').append('<br><br><button class="btn btn-primary" onclick="retryPayment()">Try Again</button>');
	}, 2000);
}

function retryPayment() {
	// Reload the page to retry payment
	window.location.reload();
}

// Store token in session for callback processing
if (window.location.search.includes('token=')) {
	const urlParams = new URLSearchParams(window.location.search);
	const token = urlParams.get('token');
	if (token) {
		sessionStorage.setItem('pesapal_token', token);
	}
}
