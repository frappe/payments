from dataclasses import dataclass
from typing import Optional

@dataclass(frozen=True)
class PaynowConfig:
    """
        Configuration for a specific currency.
    """
    integration_id: str
    integration_key: str
    currency: str = "USD"

    redirect_callback_url: Optional[str] = None
    status_callback_url: Optional[str] = None
