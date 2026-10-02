"""Native provider credits, separately from dollars, conservatively reserved in Mongo.

The measured window/capacity is explicit configuration, never an automatic refill.
Every attempt debits before I/O; failures keep the debit as uncertain. Other users
of the same provider account can reduce its real balance outside this service.
"""
from datetime import datetime, timezone
from uuid import uuid4

from pymongo.errors import DuplicateKeyError

from research.acquisition.base import BudgetRefusedError, parse_datetime


class ProviderCredits:
    def __init__(self, db, *, provider, account, capacity, window):
        self.accounts = db.research_provider_credits
        self.usage = db.research_provider_credit_usage
        self.provider, self.key = provider, f"{provider}:{account}:{window}"
        self.capacity = capacity
        self.expires_at = parse_datetime(window)

    async def reserve(self, credits):
        if (self.capacity is None or self.capacity <= 0 or credits <= 0
                or self.expires_at is not None and datetime.now(timezone.utc) >= self.expires_at):
            raise BudgetRefusedError("Saldo de creditos desconocido o agotado", provider=self.provider)
        try:
            await self.accounts.update_one({"_id": self.key},
                {"$setOnInsert": {"reserved_credits": 0, "unit": "credits"}}, upsert=True)
        except DuplicateKeyError:
            pass
        reservation_id = uuid4().hex
        accepted = await self.accounts.update_one(
            {"_id": self.key, "reserved_credits": {"$lte": self.capacity - credits}},
            {"$inc": {"reserved_credits": credits}})
        if not accepted.matched_count:
            raise BudgetRefusedError("Techo compartido de creditos agotado", provider=self.provider)
        # A crash here loses detail but retains the conservative account debit.
        await self.usage.insert_one({"_id": reservation_id, "account": self.key,
            "provider": self.provider, "unit": "credits", "reserved_credits": credits,
            "actual_credits": None, "state": "uncertain", "at": datetime.now(timezone.utc)})
        return reservation_id

    async def settle(self, reservation_id, actual=None):
        # No automatic refund: neither a successful result nor HTTP error proves
        # an invoice. Native usage is kept separately when the API reports it.
        await self.usage.update_one({"_id": reservation_id}, {"$set": {
            "actual_credits": actual if type(actual) is int and actual >= 0 else None,
            "state": "reported" if type(actual) is int and actual >= 0 else "uncertain"}})
