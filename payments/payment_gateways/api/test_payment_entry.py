import frappe
from frappe.tests.utils import FrappeTestCase
from payments.payment_gateways.api.payment_entry import (
    get_outstanding_invoices,
    get_unallocated_payments,
    process_pos_payment,
    get_available_pos_profiles,
    set_paid_amount_and_received_amount,
)

class TestPaymentFunctions(FrappeTestCase):
    def test_get_outstanding_invoices(self):
        company = "Test Company Maniac"
        currency = "KES"
        customer = "Test Customer"
        pos_profile_name = "Test POS Profile"

        invoices = get_outstanding_invoices(company, currency, customer, pos_profile_name)

        # Assert the result
        self.assertTrue(isinstance(invoices, list))

    def test_get_unallocated_payments(self):
        customer = "Test Customer"
        company = "Test Company Maniac"
        currency = "KES"
        mode_of_payment = "Cash"

        # Call the function
        unallocated_payments = get_unallocated_payments(customer, company, currency, mode_of_payment)

        # Assert the result
        self.assertTrue(isinstance(unallocated_payments, list))

    def test_process_pos_payment(self):
        payload = {
            "company": "Test Company Maniac",
            "currency": "KES",
            "customer": "Test Customer",
            "pos_opening_shift_name": "Test Opening Shift",
 "pos_profile": {
        "name": "Test POS Profile",
        "custom_allow_make_new_payments": 1,
        "custom_allow_make_new_invoices": 1,
        "custom_use_pos_payments": 1
    },            "pos_profile_name": "Test POS Profile",
            
            "selected_invoices": [],
            "selected_payments": [],
            "selected_mpesa_payments": [],
            "total_selected_invoices": 0,
            "total_selected_payments": 0,
            "total_selected_mpesa_payments": 0,
            "payment_methods": [
                {"mode_of_payment": "Cash", "amount": 50.00}
            ],
            "total_payment_methods": 50.00,
        }

        result = process_pos_payment(payload)

        self.assertTrue(isinstance(result, dict))

    def test_get_available_pos_profiles(self):
        company = "Test Company Maniac"
        currency = "KES"

        pos_profiles = get_available_pos_profiles(company, currency)

        self.assertTrue(isinstance(pos_profiles, list))

    def test_set_paid_amount_and_received_amount(self):
        party_account_currency = "KES"
        bank = {"account_currency": "KES", "bank_currency": "KES", "conversion_rate": 1.0}
        outstanding_amount = 100.00
        payment_type = "Receive"
        bank_amount = None
        conversion_rate = 1.0

        paid_amount, received_amount = set_paid_amount_and_received_amount(
            party_account_currency, bank, outstanding_amount, payment_type, bank_amount, conversion_rate
        )

        self.assertEqual(paid_amount, 100.00)
        self.assertEqual(received_amount, 100.00)
