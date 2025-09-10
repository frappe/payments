import frappe

from payments.utils.utils import make_custom_fields


def execute():
	make_custom_fields()
