"""Small read-only CCXT-shaped adapter. There is no runtime CCXT dependency."""

from .client import DebykoCCXT
from .transport import AuthenticationError, BadResponse, BadSymbol, DataUnavailable, DebykoError

__all__ = [
    "DebykoCCXT",
    "DebykoError",
    "AuthenticationError",
    "BadResponse",
    "BadSymbol",
    "DataUnavailable",
]
