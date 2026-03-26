from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class ResponseFeedBack:
	message: str | None
	data: Any = None
	status_code: int = None
	exception_error: str = None
