from frappe.exceptions import ValidationError


class FailedToInitiateFlowError(Exception):
	def __init__(self, message, data):
		self.message = message
		self.data = data


class PayloadIntegrityError(ValidationError):
	pass


class PaymentControllerProcessingError(Exception):
	def __init__(self, message, psltype):
		self.message = message
		self.psltype = psltype


class RefDocHookProcessingError(Exception):
	def __init__(self, message, psltype):
		self.message = message
		self.psltype = psltype
