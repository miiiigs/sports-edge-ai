"""Storage helpers for local research persistence."""

from .paper_shadow_ledger import (
    OutcomeUpdate,
    PaperDecision,
    append_outcome_update,
    connect as connect_paper_shadow_ledger,
    fetch_ledger_rows,
    record_decision,
)
