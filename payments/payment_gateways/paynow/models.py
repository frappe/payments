import re
from dataclasses import dataclass, field
from typing import List, Optional, Dict

from .enums import PaymentStatus

@dataclass
class CartItem:
    title: str
    amount: float

@dataclass
class PaymentResponse:
    """
        Represent the request response
    """
    upstream_data: dict
    success: bool = False
    status: PaymentStatus = PaymentStatus.UNKNOWN
    
    message: Optional[str] = None
    hash: Optional[str] = None
    token: Optional[str] = None
    merchant_reference: Optional[str] = None
    redirect_url: Optional[str] = None
    poll_url: Optional[str] = None
    gateway_reference: Optional[str] = None
    gateway_guid: Optional[str] = None
    instructions: Optional[str] = None
    amount: Optional[float] = None

    @property
    def is_paid(self) -> bool:
        return self.status == PaymentStatus.PAID
    
    @classmethod
    def from_raw(cls, data: Dict[str, str]):
        """
        Unified parser for any Paynow response (API init or Status callback).
        """
        status = PaymentStatus.parse(data.get('status', ''))
        
        obj = cls(
            upstream_data=data,
            status=status,
            hash=data.get("hash"),
            merchant_reference=data.get("reference"),
            poll_url=data.get("pollurl"),
            redirect_url=data.get("browserurl"),
            instructions=data.get("instructions"),
            token=data.get("token"),
        )

        obj.success = status not in [PaymentStatus.ERROR, PaymentStatus.CANCELLED, PaymentStatus.UNKNOWN]

        if data.get("amount"):
            try:
                obj.amount = float(data.get("amount"))
            except ValueError:
                pass

        if obj.poll_url:
            guid_match = re.search(r'guid=([a-f0-9\-]+)', obj.poll_url, re.I)
            if guid_match:
                obj.gateway_guid = guid_match.group(1)

        if data.get("paynowreference"):
            obj.gateway_reference = data.get("paynowreference")
        elif obj.redirect_url:
            # Fallback: Extract numeric ID from the redirect URL
            ref_match = re.search(r'\d+', obj.redirect_url)
            if ref_match:
                obj.gateway_reference = ref_match.group(0)

        if status == PaymentStatus.ERROR:
            obj.message = data.get("error", "Transaction failed upstream")
        elif obj.instructions:
            obj.message = obj.instructions
        else:
            obj.message = f"Payment Status: {status.value}"

        return obj
    
@dataclass
class Payment:
    """
        Represents the payment payload.
    """
    merchant_reference: str
    currency: str = "USD"
    
    # Customer / Buyer Details (Optional for Web, but customer_email is required for Express)
    customer_email: Optional[str] = None
    customer_phone: Optional[str] = None
    customer_name: Optional[str] = None 
    
    # Advanced / Express Fields
    tokenize: bool = False
    merchant_trace: Optional[str] = None
    
    items: List[CartItem] = field(default_factory=list)

    def add(self, item: CartItem):
        self.items.append(item)
        return self

    @property
    def total(self) -> float:
        return sum(item.amount for item in self.items)

    @property
    def info_string(self) -> str:
        return ", ".join(item.title for item in self.items)