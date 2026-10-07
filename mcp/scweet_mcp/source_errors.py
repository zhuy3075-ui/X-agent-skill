"""Safe source diagnostics and durable account-wait signals; never include raw responses."""
from scweet_mcp.store import StoreError


class SourceFailure(StoreError):
    def __init__(self, message, *, details=None, retry_after=None):
        super().__init__(message)
        self.details = details or {}
        self.retry_after = retry_after


class AccountWait(SourceFailure):
    """No network attempt was made: wait for an eligible account without spending an attempt."""
