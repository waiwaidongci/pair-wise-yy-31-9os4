"""Cross-cutting API error shared by HTTP layer, pricing policy and ledger."""
from __future__ import annotations


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message
