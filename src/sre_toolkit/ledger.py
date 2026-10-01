"""Per-run cost ledger.

Every billable call in this toolkit goes through `Ledger.charge()`. That gives
three things for free:
  * `--max-spend` can refuse a call *before* it happens (`check()`),
  * `--dry-run` can price a whole investigation without calling AWS,
  * every report ends with what it actually cost.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .errors import BudgetExceeded
from .pricing import cost_of, token_cost


@dataclass
class Entry:
    operation: str
    quantity: float
    unit: str
    usd: float
    detail: str = ""
    billed: bool = True  # False when served from cache or skipped by --dry-run


@dataclass
class Ledger:
    max_spend: float | None = None
    dry_run: bool = False
    entries: list[Entry] = field(default_factory=list)

    # -- totals ----------------------------------------------------------
    @property
    def total(self) -> float:
        return sum(e.usd for e in self.entries if e.billed)

    @property
    def avoided(self) -> float:
        """Money not spent thanks to the cache / dry-run."""
        return sum(e.usd for e in self.entries if not e.billed)

    def by_operation(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for e in self.entries:
            row = out.setdefault(e.operation, {"calls": 0, "usd": 0.0, "avoided_usd": 0.0})
            row["calls"] += 1
            if e.billed:
                row["usd"] += e.usd
            else:
                row["avoided_usd"] += e.usd
        return out

    # -- guard rails -----------------------------------------------------
    def check(self, operation: str, quantity: float, detail: str = "") -> float:
        """Price an operation and raise if it would breach --max-spend."""
        usd = cost_of(operation, quantity)
        if self.max_spend is not None and self.total + usd > self.max_spend:
            raise BudgetExceeded(
                f"{operation} would cost ~${usd:.4f}, taking the run to "
                f"${self.total + usd:.4f} over the --max-spend limit of "
                f"${self.max_spend:.4f}. Narrow the window, drop --deep, or raise the limit."
            )
        return usd

    def charge(
        self, operation: str, quantity: float = 1, detail: str = "", billed: bool = True
    ) -> float:
        usd = self.check(operation, quantity, detail) if billed else cost_of(operation, quantity)
        from .pricing import api_price

        self.entries.append(
            Entry(
                operation=operation,
                quantity=quantity,
                unit=api_price(operation).get("unit", "unknown"),
                usd=usd,
                detail=detail,
                billed=billed and not self.dry_run,
            )
        )
        return usd

    def charge_tokens(
        self, model_id: str, input_tokens: int, output_tokens: int, detail: str = ""
    ) -> float:
        usd = token_cost(model_id, input_tokens, output_tokens)
        if self.max_spend is not None and self.total + usd > self.max_spend:
            raise BudgetExceeded(
                f"Bedrock call on {model_id} (~{input_tokens} in / {output_tokens} out) "
                f"would cost ~${usd:.4f} and breach --max-spend ${self.max_spend:.4f}."
            )
        self.entries.append(
            Entry(
                operation=f"bedrock:{model_id}",
                quantity=input_tokens + output_tokens,
                unit="tokens",
                usd=usd,
                detail=detail or f"{input_tokens} in / {output_tokens} out",
                billed=not self.dry_run,
            )
        )
        return usd

    def to_dict(self) -> dict:
        return {
            "total_usd": round(self.total, 6),
            "avoided_usd": round(self.avoided, 6),
            "dry_run": self.dry_run,
            "max_spend_usd": self.max_spend,
            "by_operation": {
                k: {"calls": v["calls"], "usd": round(v["usd"], 6),
                    "avoided_usd": round(v["avoided_usd"], 6)}
                for k, v in self.by_operation().items()
            },
        }
