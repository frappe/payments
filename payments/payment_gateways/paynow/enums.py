from enum import Enum, unique

@unique
class PaymentMethod(str, Enum):
    # Mobile Wallets
    ECOCASH = "ecocash"
    ONEMONEY = "onemoney"
    INNBUCKS = "innbucks"
    OMARI = "omari"
    
    # Card / Switch (Express)
    VMC = "vmc"               # Visa / Mastercard
    ZIMSWITCH = "zimswitch"

@unique
class TestMode(str, Enum):
    NONE = "none"
    SUCCESS = "success"             
    DELAYED_SUCCESS = "delayed"     
    USER_CANCELLED = "cancelled"    
    INSUFFICIENT_FUNDS = "funds"

@unique
class PaymentStatus(str, Enum):
    '''
        Ref: https://developers.paynow.co.zw/docs/status_update.html#statuses
    '''
    PAID = "Paid"
    AWAITING_DELIVERY = "Awaiting Delivery"
    DELIVERED = "Delivered"
    CREATED = "Created"
    SENT = "Sent"
    CANCELLED = "Cancelled"
    DISPUTED = "Disputed"
    REFUNDED = "Refunded"
    ERROR = "Error"
    OK = "Ok"

    # ambigous / new unmapped status
    UNKNOWN = "Unknown"

    @classmethod
    def parse(cls, status: str) -> 'PaymentStatus':
        """
            Safely maps a string to a PaymentStatus Enum.
            Returns PaymentStatus.ERROR if the string is unrecognized.
        """
        if not status:
            return cls.ERROR
            
        try:
            return cls(status)
        except ValueError:
            return cls.UNKNOWN