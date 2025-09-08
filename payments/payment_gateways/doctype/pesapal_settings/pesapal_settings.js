// Copyright (c) 2024, Frappe Technologies and contributors
// For license information, please see license.txt

frappe.ui.form.on('Pesapal Settings', {
	refresh: function(frm) {
		// Add custom buttons and functionality
		if (!frm.doc.ipn_id) {
			frm.add_custom_button(__('Register IPN URL'), function() {
				frm.call('register_ipn_url').then(r => {
					if (r.message) {
						frappe.msgprint(__('IPN URL registered successfully'));
						frm.reload_doc();
					}
				});
			});
		}

		if (frm.doc.consumer_key && frm.doc.consumer_secret) {
			frm.add_custom_button(__('Test Connection'), function() {
				frm.call('test_connection').then(r => {
					if (r.message && r.message.success) {
						frappe.msgprint({
							title: __('Connection Successful'),
							message: __('Successfully connected to Pesapal API'),
							indicator: 'green'
						});
					} else {
						frappe.msgprint({
							title: __('Connection Failed'),
							message: r.message ? r.message.error : __('Failed to connect to Pesapal API'),
							indicator: 'red'
						});
					}
				});
			});
		}

		// Set IPN URL automatically
		if (!frm.doc.ipn_url) {
			let site_url = frappe.urllib.get_base_url();
			frm.set_value('ipn_url', site_url + '/api/method/payments.payment_gateways.doctype.pesapal_settings.pesapal_settings.handle_ipn');
		}
	},

	is_sandbox: function(frm) {
		// Clear IPN settings when switching between sandbox and production
		if (frm.doc.ipn_id) {
			frappe.msgprint(__('Please re-register IPN URL after changing sandbox mode'));
			frm.set_value('ipn_id', '');
		}
	}
});
