"""EXTERNAL — Account Service (dashed box, edge 1).

Supplies the account class. This subsystem consumes and re-validates it; it
never computes it. Swap this for the real identity client.
"""
from __future__ import annotations

from ..domain.models import AccountClass


class AccountService:
    def __init__(self, accounts: dict[str, AccountClass] | None = None):
        self._accounts: dict[str, AccountClass] = accounts or {
            "tenant-spot-a": AccountClass.SPOT,
            "tenant-spot-b": AccountClass.SPOT,
            "tenant-spot-c": AccountClass.SPOT,
            "tenant-dynamic": AccountClass.DYNAMIC,
            "tenant-static": AccountClass.STATIC,
        }

    async def get_account_class(self, tenant_id: str) -> AccountClass | None:
        """Edge 1: API Gateway -> Account Service."""
        return self._accounts.get(tenant_id)

    def set_account_class(self, tenant_id: str, cls: AccountClass) -> None:
        self._accounts[tenant_id] = cls
