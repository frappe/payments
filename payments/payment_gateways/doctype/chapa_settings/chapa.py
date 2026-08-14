import frappe


class Chapa:
    def __init__(self):
        settings_name = frappe.db.get_value(
            "Chapa Settings",
            {"gateway_name": "Chapa"},
            "name"
        )

        if not settings_name:
            frappe.throw("Chapa Settings configuration not found")

        self.settings = frappe.get_cached_doc(
            "Chapa Settings",
            settings_name
        )

    def validate_transaction_currency(self, currency):
        self.settings.validate_transaction_currency(currency)

    def get_payment_url(self, **kwargs):
        return self.settings.get_payment_url(**kwargs)

    def create_request(self, data):
        return self.settings.create_request(data)