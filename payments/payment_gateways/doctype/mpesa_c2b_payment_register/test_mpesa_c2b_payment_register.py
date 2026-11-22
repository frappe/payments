# Copyright (c) 2024, Frappe Technologies and Contributors
# See license.txt

import frappe
from frappe.tests.utils import FrappeTestCase
from .mpesa_c2b_payment_register import MpesaC2BPaymentRegister



def create_payment_registers():
    # First payment record
    payment_register1 = frappe.new_doc("Mpesa C2B Payment Register")
    payment_register1.firstname = "John"
    payment_register1.middlename = "Doe"
    payment_register1.lastname = "Smith"
    payment_register1.full_name = "John Doe Smith"
    payment_register1.businessshortcode = "123456"
    payment_register1.transactiontype = "Paybill"
    payment_register1.posting_date = "2024-05-01"
    payment_register1.posting_time = "12:00:00"
    payment_register1.transid = "1234567890"
    payment_register1.invoicenumber = "123456"
    payment_register1.msisdn = "254712345678"
    payment_register1.transamount = 100
    payment_register1.billrefnumber = "123456"
    payment_register1.thirdpartytransid = "123456"
    payment_register1.mode_of_payment="Cash"
    payment_register1.default_currency="USD"
    payment_register1.customer = "Test Customer"
    payment_register1.company = "Test Company Maniac" 
    payment_register1.save()

    # Second payment record
    payment_register2 = frappe.new_doc("Mpesa C2B Payment Register")
    payment_register2.firstname = "Jane"
    payment_register2.middlename = "Doe"
    payment_register2.lastname = "Johnson"
    payment_register1.full_name = "Jane Doe Johnson"
    payment_register2.businessshortcode = "654321"
    payment_register2.transactiontype = "Paybill"
    payment_register2.posting_date = "2024-05-02"
    payment_register2.posting_time = "13:00:00"
    payment_register2.transid = "9876543210"
    payment_register2.invoicenumber = "654321"
    payment_register2.msisdn = "254712345679"
    payment_register2.transamount = 200
    payment_register2.billrefnumber = "654321"
    payment_register2.thirdpartytransid = "654321"
    payment_register2.mode_of_payment="M-pesa Kituru"
    payment_register2.default_currency="USD" 
    payment_register2.customer = "Test Customer"
    payment_register2.company = "Test Company Maniac" 

    payment_register2.save()

    # Third payment record
    payment_register3 = frappe.new_doc("Mpesa C2B Payment Register")
    payment_register3.firstname = "Alice"
    payment_register3.middlename = "Wonder"
    payment_register3.lastname = "Land"
    payment_register1.full_name = "Alice Wonder Land"
    payment_register3.businessshortcode = "987654"
    payment_register3.transactiontype = "Paybill"
    payment_register3.posting_date = "2024-05-03"
    payment_register3.posting_time = "14:00:00"
    payment_register3.transid = "1239874560"
    payment_register3.invoicenumber = "987654"
    payment_register3.msisdn = "254712345670"
    payment_register3.transamount = 300
    payment_register3.billrefnumber = "987654"
    payment_register3.thirdpartytransid = "987654"
    payment_register3.mode_of_payment="M-pesa Kituru"
    payment_register3.default_currency="USD"
    payment_register3.customer = "Test Customer"
    payment_register3.company = "Test Company Maniac" 

    payment_register3.save()
    
def create_customer():
    customer = frappe.new_doc("Customer")
    customer.customer_name = "Test Customer"
    customer.customer_type = "Individual"
    customer.default_currency = "USD"
    customer.customer_group = "All Customer Groups"
    customer.save()
class TestMpesaC2BPaymentRegister(FrappeTestCase):
    def setUp(self):
        create_customer()
        create_payment_registers()
        
    def tearDown(self):
        payment_register = frappe.get_list(
            "Mpesa C2B Payment Register",
            filters={"firstname": "John", "middlename": "Doe", "lastname": "Smith"},
            fields=["name"],
            limit=1,
        )
        
        # Delete the fetched document if found
        if payment_register:
            frappe.delete_doc("Mpesa C2B Payment Register", payment_register[0].name)


    def test_missing_values_set(self):
        """Tests if missing values are set correctly"""
        payment_register = frappe.get_doc(
            "Mpesa C2B Payment Register",
            {"transid": "1234567890"},
        )
        self.assertEqual(payment_register.currency, "KES")
        self.assertEqual(payment_register.full_name, "John Doe Smith")
        self.assertIsNotNone(payment_register.company)
        self.assertIsNotNone(payment_register.mode_of_payment)

    def test_before_submit_validation(self):
        """Tests validation before submission"""
        # Case 1: transamount not set
        payment_register = frappe.get_doc(
            "Mpesa C2B Payment Register",
            {"transid": "1234567890"},
        )
        payment_register.transamount = None
        with self.assertRaises(frappe.ValidationError):
            payment_register.before_submit()

        # Case 2: company not set
        payment_register.transamount = 100  # Reset transamount
        payment_register.company = None
        with self.assertRaises(frappe.ValidationError):
            payment_register.before_submit()

        # Case 3: customer not set
        payment_register.company = "Test Company"
        payment_register.customer = None
        with self.assertRaises(frappe.ValidationError):
            payment_register.before_submit()

        # Case 4: mode_of_payment not set
        payment_register.customer = "Test Customer"
        payment_register.mode_of_payment = None
        with self.assertRaises(frappe.ValidationError):
            payment_register.before_submit()

    def test_create_payment_entry(self):
        """Tests if payment entry is created"""
        payment_register = frappe.get_doc(
            "Mpesa C2B Payment Register",
            {"transid": "1234567890"},
        )
    
        payment_entry_name = payment_register.create_payment_entry()
        self.assertIsNotNone(payment_entry_name)
        self.assertEqual(
            frappe.get_value("Payment Entry", payment_entry_name, "company"),
            payment_register.company,
        )
        print(str(payment_entry_name))
       
