"""Metered, cached, read-only AWS client factory.

Three cost properties worth knowing:
  1. One boto3 Session is shared by every client (one credential resolution,
     one set of connection pools).
  2. Every call is routed through the ledger, so it is priced and budget-checked.
  3. Every call is cached on disk, so re-running an investigation is free.
"""

from __future__ import annotations

from typing import Any, Callable

from ..errors import BudgetExceeded, CollectorError, MissingDependency


class AwsClients:
    def __init__(self, ctx):
        self.ctx = ctx
        self._session = None
        self._clients: dict[str, Any] = {}
        self._account: str | None = None

    # -- plumbing --------------------------------------------------------
    @property
    def session(self):
        if self._session is None:
            try:
                import boto3
            except ImportError as exc:  # pragma: no cover - env dependent
                raise MissingDependency("boto3", "aws") from exc
            self._session = boto3.Session(
                profile_name=self.ctx.profile, region_name=self.ctx.region
            )
        return self._session

    def client(self, service: str):
        if service not in self._clients:
            from botocore.config import Config

            self._clients[service] = self.session.client(
                service,
                config=Config(
                    retries={"max_attempts": 3, "mode": "adaptive"},
                    connect_timeout=5,
                    read_timeout=30,
                    user_agent_extra="sre-toolkit/0.1.0",
                ),
            )
        return self._clients[service]

    @property
    def region(self) -> str:
        # Planning a run (--dry-run / `sre cost estimate`) must work on a machine
        # with no boto3 and no credentials — it is a costing tool, not a client.
        try:
            return self.ctx.region or self.session.region_name or "us-east-1"
        except MissingDependency:
            if self.ctx.dry_run:
                return self.ctx.region or "us-east-1"
            raise

    def account_id(self) -> str | None:
        if self._account is None:
            try:
                self._account = self.call(
                    "sts", "get_caller_identity", "sts:GetCallerIdentity"
                ).get("Account")
            except Exception:  # noqa: BLE001 - identity is a nicety, never fatal
                self._account = ""
        return self._account or None

    # -- the one entry point collectors use ------------------------------
    def call(
        self,
        service: str,
        operation: str,
        priced_as: str,
        quantity: float = 1,
        cacheable: bool = True,
        detail: str = "",
        **kwargs,
    ) -> dict:
        """Invoke a boto3 operation with caching, pricing and dry-run support."""
        params = {"region": self.region, "op": operation, **kwargs}
        if cacheable:
            hit = self.ctx.cache.get(priced_as, params)
            if hit is not None:
                self.ctx.ledger.charge(priced_as, quantity, detail=detail + " (cached)",
                                       billed=False)
                self.ctx.log(f"cache hit {priced_as} {detail}")
                return hit

        self.ctx.ledger.check(priced_as, quantity, detail)
        if self.ctx.dry_run:
            self.ctx.ledger.charge(priced_as, quantity, detail=detail + " (dry-run)",
                                   billed=False)
            return {}

        self.ctx.ledger.charge(priced_as, quantity, detail=detail)
        client = self.client(service)
        try:
            result = getattr(client, operation)(**kwargs)
        except Exception as exc:  # noqa: BLE001 - surfaced as a clean message
            raise CollectorError(f"{service}:{operation} failed: {_clean(exc)}") from exc
        result.pop("ResponseMetadata", None)
        if cacheable:
            self.ctx.cache.set(priced_as, params, result)
        return result

    def paginate(
        self,
        service: str,
        operation: str,
        priced_as: str,
        key: str,
        max_items: int = 200,
        **kwargs,
    ) -> list[dict]:
        """Bounded pagination — `max_items` exists so a wide query cannot run away."""
        params = {"region": self.region, "op": operation, "max": max_items, **kwargs}
        hit = self.ctx.cache.get(priced_as, params)
        if hit is not None:
            self.ctx.ledger.charge(priced_as, 1, detail=f"{operation} (cached)", billed=False)
            return hit
        if self.ctx.dry_run:
            self.ctx.ledger.charge(priced_as, 1, detail=f"{operation} (dry-run)", billed=False)
            return []

        client = self.client(service)
        items: list[dict] = []
        try:
            paginator = client.get_paginator(operation)
            for page in paginator.paginate(**kwargs):
                self.ctx.ledger.charge(priced_as, 1, detail=operation)
                items.extend(page.get(key, []))
                if len(items) >= max_items:
                    items = items[:max_items]
                    break
        except Exception as exc:  # noqa: BLE001
            raise CollectorError(f"{service}:{operation} failed: {_clean(exc)}") from exc
        self.ctx.cache.set(priced_as, params, items)
        return items


def safe(ctx, name: str, fn: Callable[[], Any], default: Any = None) -> Any:
    """Run a collector step; record failures instead of exploding.

    A partial investigation during an outage beats a traceback. Two exceptions are
    deliberately *not* swallowed: a budget breach must stop the run (otherwise
    --max-spend would silently degrade into "skip the expensive bits"), and a
    missing optional dependency needs to reach the user as an install hint.
    """
    try:
        return fn()
    except (BudgetExceeded, MissingDependency):
        raise
    except Exception as exc:  # noqa: BLE001
        ctx.log(f"{name}: {exc}")
        return default


def _clean(exc: Exception) -> str:
    msg = str(exc)
    return msg.split(":", 1)[-1].strip() if "ClientError" in type(exc).__name__ else msg
