"""Canonical provider-independent fixture identity and paper reconciliation.

The same physical football match can be represented under several provider
namespaces (for example ``football-data.org:<id>`` and ``api-football:<id>``).
The historical paper ledger stored the provider-qualified ``fixture_id`` in its
business identity, so the same real match + market + selection + line could be
papered twice when the fixture provider changed.

This module introduces:

* a deterministic provider-independent canonical fixture identity derived from
  competition + normalized home team + normalized away team + kickoff;
* a provider-alias mapping that preserves every original provider fixture ID;
* an additive ``paper_position_reconciliations`` table that marks duplicate
  representations as excluded from aggregate performance while preserving all
  original paper rows, entry odds, and settlement trails;
* a deterministic, outcome-independent reconciliation planner/applier.

Reconciliation is strictly additive: no paper row, update, or provider ID is
ever deleted or rewritten.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from ..locking import canonical_hash

# --------------------------------------------------------------------------- config
DEFAULT_KICKOFF_TOLERANCE_SECONDS = 0
CANONICAL_RECONCILIATION_VERSION = "canonical_fixture_v1"
REASON_CROSS_PROVIDER_DUPLICATE = "CROSS_PROVIDER_DUPLICATE"

# Settlement states considered final for reconciliation consistency checks.
_FINAL_STATES = {"WON", "LOST", "PUSH", "HALF_WIN", "HALF_LOSS", "VOID"}

# Proven provider naming variants (canonical -> equivalent provider spellings).
# Keys/values use the same alphanumeric-normalized form produced by
# :func:`normalize_text_for_identity`. Each entry is derived from actual
# persisted provider fixtures in the supported leagues and is safe because the
# clubs are unambiguous within their competitions.
TEAM_VARIANTS: dict[str, str] = {
    "leeds": "leedsunited",
    "leedsunited": "leedsunited",
    "manchesterunited": "manchesterunited",
    "manunited": "manchesterunited",
    "manutd": "manchesterunited",
    "manchestercity": "manchestercity",
    "mancity": "manchestercity",
    "ipswich": "ipswichtown",
    "ipswichtown": "ipswichtown",
    "celta": "celtavigo",
    "celtavigo": "celtavigo",
    "athletic": "athleticclub",
    "athleticclub": "athleticclub",
    "athleticbilbao": "athleticclub",
    "sevilla": "sevilla",
    "sevillafc": "sevilla",
}

SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS canonical_fixtures (
        canonical_fixture_id TEXT PRIMARY KEY,
        competition_id TEXT NOT NULL,
        season TEXT,
        normalized_home_team TEXT NOT NULL,
        normalized_away_team TEXT NOT NULL,
        kickoff_utc TEXT NOT NULL,
        created_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS fixture_provider_aliases (
        provider TEXT NOT NULL,
        provider_fixture_id TEXT NOT NULL,
        canonical_fixture_id TEXT NOT NULL,
        first_seen_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        matching_method TEXT NOT NULL,
        PRIMARY KEY (provider, provider_fixture_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS paper_position_reconciliations (
        reconciliation_id INTEGER PRIMARY KEY AUTOINCREMENT,
        duplicate_paper_trade_id TEXT NOT NULL UNIQUE,
        canonical_paper_trade_id TEXT NOT NULL,
        reason TEXT NOT NULL,
        canonical_fixture_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        reconciliation_version TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_reconciliations_canonical_paper_trade_id
    ON paper_position_reconciliations(canonical_paper_trade_id)
    """,
)


class CanonicalFixtureError(RuntimeError):
    """Base error for canonical fixture resolution."""


class CanonicalFixtureAmbiguityError(CanonicalFixtureError):
    """A fixture identity matched more than one existing canonical fixture."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_text_for_identity(value: Any) -> str:
    """Lowercase alphanumeric normalization (case/punctuation-insensitive)."""
    return "".join(ch for ch in str(value or "").strip().lower() if ch.isalnum())


def canonical_team_name(value: Any) -> str:
    """Canonical team key: normalized name mapped through proven variants."""
    key = normalize_text_for_identity(value)
    return TEAM_VARIANTS.get(key, key)


def normalize_kickoff_utc(value: Any) -> str | None:
    """Canonical UTC ISO kickoff (``Z`` → ``+00:00``)."""
    if value in {None, ""}:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return text


def _kickoff_close(left: str, right: str, tolerance_seconds: int) -> bool:
    if tolerance_seconds <= 0:
        return left == right
    try:
        return abs((datetime.fromisoformat(left) - datetime.fromisoformat(right)).total_seconds()) <= tolerance_seconds
    except ValueError:
        return left == right


def canonical_fixture_id_from_identity(
    *,
    competition_id: str,
    home_team_name: str,
    away_team_name: str,
    kickoff_utc: str,
    season: str | None = None,
) -> str:
    """Deterministic provider-independent canonical fixture identity.

    The identity hash is based on competition + canonical home + canonical
    away + kickoff ONLY. Season is deliberately excluded from the hash so a
    fixture represented with season metadata by one provider and without it by
    another still resolves to the SAME canonical fixture. Season is retained as
    metadata on the ``canonical_fixtures`` row.
    """
    normalized_kickoff = normalize_kickoff_utc(kickoff_utc) or ""
    payload = {
        "competition_id": str(competition_id or "").upper(),
        "home": canonical_team_name(home_team_name),
        "away": canonical_team_name(away_team_name),
        "kickoff_utc": normalized_kickoff,
    }
    return canonical_hash(payload)


def _normalize_business_key_line(line: float | None) -> float | None:
    if line is None:
        return None
    normalized = float(line)
    return 0.0 if normalized == 0.0 else normalized


def canonical_paper_business_key_signature(
    *,
    canonical_fixture_id: str,
    market: str,
    selection: str,
    line: float | None,
) -> str:
    """Canonical paper business identity across providers."""
    return canonical_hash(
        {
            "canonical_fixture_id": canonical_fixture_id,
            "market": market.lower(),
            "selection": selection.upper(),
            "line": _normalize_business_key_line(line),
        }
    )


def connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    # NOTE: statements run without an explicit commit so that callers inside a
    # transaction (e.g. atomic APPLY) control the commit/rollback boundary.
    for statement in SCHEMA_STATEMENTS:
        conn.execute(statement)
    has_paper_table = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='paper_trade_entries'"
    ).fetchone()[0] > 0
    if not has_paper_table:
        return
    # Defensively ensure the derived canonical-key column exists (it is normally
    # added by football_paper_trading.ensure_schema).
    existing_columns = {
        str(row["name"]) for row in conn.execute("PRAGMA table_info(paper_trade_entries)").fetchall()
    }
    if "canonical_business_key_signature" not in existing_columns:
        conn.execute("ALTER TABLE paper_trade_entries ADD COLUMN canonical_business_key_signature TEXT")
    # Enforce the canonical business-key invariant: a COUNTED position may have
    # at most one canonical business key. Duplicate/superseded representations
    # keep NULL canonical keys so they never compete for the unique value.
    # This fails loudly if non-NULL duplicates already exist.
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_paper_trade_entries_canonical_business_key_signature
        ON paper_trade_entries(canonical_business_key_signature)
        WHERE canonical_business_key_signature IS NOT NULL
        """
    )


def split_provider_fixture_id(fixture_id: str) -> tuple[str, str]:
    provider, _, rest = str(fixture_id).partition(":")
    return provider or "", rest


def resolve_or_create_canonical_fixture(
    conn: sqlite3.Connection,
    *,
    provider: str,
    provider_fixture_id: str,
    competition_id: str,
    home_team_name: str,
    away_team_name: str,
    kickoff_utc: str,
    season: str | None = None,
    kickoff_tolerance_seconds: int = DEFAULT_KICKOFF_TOLERANCE_SECONDS,
) -> tuple[str, str]:
    """Resolve a provider fixture to its canonical fixture, registering aliases.

    Returns ``(canonical_fixture_id, status)`` where status is one of
    ``created``, ``matched``, or ``aliased``. Raises
    :class:`CanonicalFixtureAmbiguityError` when the identity matches more than
    one existing canonical fixture (fail closed; never auto-merge).
    """
    ensure_schema(conn)
    now_iso = _utc_now()
    normalized_home = canonical_team_name(home_team_name)
    normalized_away = canonical_team_name(away_team_name)
    normalized_kickoff = normalize_kickoff_utc(kickoff_utc) or ""

    existing_alias = conn.execute(
        "SELECT canonical_fixture_id FROM fixture_provider_aliases WHERE provider = ? AND provider_fixture_id = ?",
        (provider, provider_fixture_id),
    ).fetchone()
    if existing_alias is not None:
        canonical_fixture_id = str(existing_alias["canonical_fixture_id"])
        conn.execute(
            "UPDATE fixture_provider_aliases SET last_seen_at = ? WHERE provider = ? AND provider_fixture_id = ?",
            (now_iso, provider, provider_fixture_id),
        )
        conn.execute(
            "UPDATE canonical_fixtures SET last_seen_at = ? WHERE canonical_fixture_id = ?",
            (now_iso, canonical_fixture_id),
        )
        return canonical_fixture_id, "aliased"

    canonical_fixture_id = canonical_fixture_id_from_identity(
        competition_id=competition_id,
        home_team_name=home_team_name,
        away_team_name=away_team_name,
        kickoff_utc=kickoff_utc,
        season=season,
    )

    candidates = conn.execute(
        """
        SELECT canonical_fixture_id, kickoff_utc
        FROM canonical_fixtures
        WHERE competition_id = ? AND normalized_home_team = ? AND normalized_away_team = ?
        """,
        (str(competition_id or "").upper(), normalized_home, normalized_away),
    ).fetchall()
    matches = [
        row
        for row in candidates
        if _kickoff_close(str(row["kickoff_utc"]), normalized_kickoff, kickoff_tolerance_seconds)
    ]
    if len(matches) > 1:
        raise CanonicalFixtureAmbiguityError(
            f"CANONICAL_FIXTURE_AMBIGUOUS: {competition_id} {home_team_name} vs {away_team_name} "
            f"({kickoff_utc}) matches multiple canonical fixtures"
        )
    if len(matches) == 1:
        canonical_fixture_id = str(matches[0]["canonical_fixture_id"])
        status = "matched"
    else:
        status = "created"
        conn.execute(
            """
            INSERT INTO canonical_fixtures (
                canonical_fixture_id, competition_id, season, normalized_home_team,
                normalized_away_team, kickoff_utc, created_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                canonical_fixture_id,
                str(competition_id or "").upper(),
                season,
                normalized_home,
                normalized_away,
                normalized_kickoff,
                now_iso,
                now_iso,
            ),
        )
    conn.execute(
        """
        INSERT INTO fixture_provider_aliases (
            provider, provider_fixture_id, canonical_fixture_id, first_seen_at, last_seen_at, matching_method
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            provider,
            provider_fixture_id,
            canonical_fixture_id,
            now_iso,
            now_iso,
            "identity_match",
        ),
    )
    return canonical_fixture_id, status


def fetch_excluded_paper_trade_ids(conn: sqlite3.Connection, *, initialize_schema: bool = True) -> set[str]:
    """Paper trade IDs marked as duplicate representations (excluded from counts)."""
    if initialize_schema:
        ensure_schema(conn)
    rows = conn.execute("SELECT duplicate_paper_trade_id FROM paper_position_reconciliations").fetchall()
    return {str(row["duplicate_paper_trade_id"]) for row in rows}


def fetch_reconciliation_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    ensure_schema(conn)
    rows = conn.execute(
        """
        SELECT reconciliation_id, duplicate_paper_trade_id, canonical_paper_trade_id, reason,
               canonical_fixture_id, created_at, reconciliation_version
        FROM paper_position_reconciliations
        ORDER BY reconciliation_id
        """
    ).fetchall()
    return [dict(row) for row in rows]


def conn_row_count(conn: sqlite3.Connection, table_name: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0])


def record_reconciliation(
    conn: sqlite3.Connection,
    *,
    duplicate_paper_trade_id: str,
    canonical_paper_trade_id: str,
    canonical_fixture_id: str,
    reason: str = REASON_CROSS_PROVIDER_DUPLICATE,
    version: str = CANONICAL_RECONCILIATION_VERSION,
) -> None:
    """Additively mark a duplicate representation.

    Idempotency is explicit:

    * same ``duplicate_paper_trade_id`` already reconciled to the SAME
      canonical row  -> safe no-op;
    * same ``duplicate_paper_trade_id`` mapped to a DIFFERENT canonical row
      -> integrity failure (never silently ignored).

    One canonical position may have any number of duplicate representations.
    """
    ensure_schema(conn)
    existing = conn.execute(
        "SELECT canonical_paper_trade_id FROM paper_position_reconciliations WHERE duplicate_paper_trade_id = ?",
        (duplicate_paper_trade_id,),
    ).fetchone()
    if existing is not None:
        if str(existing["canonical_paper_trade_id"]) != canonical_paper_trade_id:
            raise sqlite3.IntegrityError(
                "DUPLICATE_ALREADY_RECONCILED_TO_DIFFERENT_CANONICAL: "
                f"{duplicate_paper_trade_id} already maps to {existing['canonical_paper_trade_id']}, "
                f"cannot remap to {canonical_paper_trade_id}"
            )
        return
    conn.execute(
        """
        INSERT INTO paper_position_reconciliations (
            duplicate_paper_trade_id, canonical_paper_trade_id, reason,
            canonical_fixture_id, created_at, reconciliation_version
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (duplicate_paper_trade_id, canonical_paper_trade_id, reason, canonical_fixture_id, _utc_now(), version),
    )


def find_counted_paper_trade_by_canonical_key(
    conn: sqlite3.Connection,
    canonical_business_key_signature: str,
) -> dict[str, Any] | None:
    """Find an existing COUNTED paper position for a canonical business key.

    Excluded (reconciled duplicate) positions never block a new capture.
    """
    ensure_schema(conn)
    row = conn.execute(
        """
        SELECT paper_trade_id
        FROM paper_trade_entries
        WHERE canonical_business_key_signature = ?
          AND paper_trade_id NOT IN (
              SELECT duplicate_paper_trade_id FROM paper_position_reconciliations
          )
        ORDER BY created_at, paper_trade_id
        LIMIT 1
        """,
        (canonical_business_key_signature,),
    ).fetchone()
    return None if row is None else dict(row)


# ------------------------------------------------------------------ planning model
@dataclass(frozen=True)
class ReconciliationGroup:
    canonical_fixture_id: str
    canonical_business_key: str
    market: str
    selection: str
    line: float | None
    rows: tuple[dict[str, Any], ...]
    canonical_row: dict[str, Any]
    duplicate_rows: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class ReconciliationPlan:
    total_rows: int
    duplicate_groups: tuple[ReconciliationGroup, ...] = ()
    ambiguous_fixture_ids: tuple[str, ...] = ()
    unresolved_fixture_ids: tuple[str, ...] = ()
    conflicts: tuple[dict[str, Any], ...] = ()
    inconsistencies: tuple[dict[str, Any], ...] = ()
    canonical_fixture_count: int = 0
    cross_provider_fixture_groups: int = 0
    canonical_fixture_by_fixture_id: dict[str, str] = field(default_factory=dict)

    @property
    def duplicate_representation_count(self) -> int:
        return sum(len(group.duplicate_rows) for group in self.duplicate_groups)

    @property
    def projected_excluded_ids(self) -> set[str]:
        return {row["paper_trade_id"] for group in self.duplicate_groups for row in group.duplicate_rows}

    def counted_rows(self, paper_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        excluded = self.projected_excluded_ids
        return [row for row in paper_rows if str(row.get("paper_trade_id") or "") not in excluded]


def _canonical_selection(group_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Deterministic, outcome-independent canonical row selection.

    Preference: earliest created entry, then stable paper_trade_id tie-break.
    Neither settlement outcome nor P/L nor odds are consulted.
    """
    return min(group_rows, key=lambda row: (str(row.get("created_at") or ""), str(row.get("paper_trade_id") or "")))


def build_reconciliation_plan(
    paper_rows: list[dict[str, Any]],
    fixture_identity: Mapping[str, Mapping[str, Any]],
    *,
    kickoff_tolerance_seconds: int = DEFAULT_KICKOFF_TOLERANCE_SECONDS,
    persist_conn: sqlite3.Connection | None = None,
) -> ReconciliationPlan:
    """Build the deterministic cross-provider reconciliation plan.

    ``paper_rows`` is a list of merged paper-trade rows. ``fixture_identity``
    maps each provider-qualified fixture_id to a dict with ``competition_id``,
    ``home_team_name``, ``away_team_name``, ``kickoff_utc`` and optional
    ``season``.

    When ``persist_conn`` is provided the canonical fixtures + provider aliases
    are persisted into that connection as each fixture is resolved (APPLY path);
    otherwise resolution runs against an in-memory store so the dry run is
    non-mutating.
    """
    resolution_store = persist_conn if persist_conn is not None else connect(":memory:")
    ensure_schema(resolution_store)
    canonical_by_fixture: dict[str, str] = {}
    ambiguous: list[str] = []
    unresolved: list[str] = []

    distinct_fixture_ids = sorted({str(row.get("fixture_id") or "") for row in paper_rows})
    for fixture_id in distinct_fixture_ids:
        ident = fixture_identity.get(fixture_id) or {}
        competition_id = ident.get("competition_id")
        home = ident.get("home_team_name")
        away = ident.get("away_team_name")
        kickoff = ident.get("kickoff_utc")
        if not (competition_id and home and away and kickoff):
            canonical_by_fixture[fixture_id] = f"unresolved:{fixture_id}"
            unresolved.append(fixture_id)
            continue
        provider, provider_fixture_id = split_provider_fixture_id(fixture_id)
        try:
            canonical_fixture_id, _ = resolve_or_create_canonical_fixture(
                resolution_store,
                provider=provider,
                provider_fixture_id=provider_fixture_id,
                competition_id=str(competition_id),
                home_team_name=str(home),
                away_team_name=str(away),
                kickoff_utc=str(kickoff),
                season=ident.get("season"),
                kickoff_tolerance_seconds=kickoff_tolerance_seconds,
            )
            canonical_by_fixture[fixture_id] = canonical_fixture_id
        except CanonicalFixtureAmbiguityError:
            canonical_by_fixture[fixture_id] = f"ambiguous:{fixture_id}"
            ambiguous.append(fixture_id)

    groups: dict[str, list[dict[str, Any]]] = {}
    for row in paper_rows:
        fixture_id = str(row.get("fixture_id") or "")
        canonical_fixture_id = canonical_by_fixture.get(fixture_id) or f"unresolved:{fixture_id}"
        business_key = canonical_paper_business_key_signature(
            canonical_fixture_id=canonical_fixture_id,
            market=str(row.get("market") or ""),
            selection=str(row.get("selection") or ""),
            line=None if row.get("line") is None else float(row["line"]),
        )
        groups.setdefault(business_key, []).append(row)

    duplicate_groups: list[ReconciliationGroup] = []
    conflicts: list[dict[str, Any]] = []
    inconsistencies: list[dict[str, Any]] = []
    for business_key, group_rows in groups.items():
        if len(group_rows) < 2:
            continue
        canonical_row = _canonical_selection(group_rows)
        duplicate_rows = [row for row in group_rows if row["paper_trade_id"] != canonical_row["paper_trade_id"]]
        canonical_fixture_id = canonical_by_fixture.get(str(canonical_row.get("fixture_id") or "")) or f"unresolved:{str(canonical_row.get('fixture_id') or '')}"
        final_states = {str(row.get("trade_state") or "") for row in group_rows if str(row.get("trade_state") or "") in _FINAL_STATES}
        if len(final_states) > 1:
            conflicts.append(
                {
                    "canonical_fixture_id": canonical_fixture_id,
                    "market": str(canonical_row.get("market")),
                    "selection": str(canonical_row.get("selection")),
                    "line": canonical_row.get("line"),
                    "states": sorted(final_states),
                    "paper_trade_ids": [str(row.get("paper_trade_id")) for row in group_rows],
                }
            )
        if final_states and str(canonical_row.get("trade_state") or "") not in final_states:
            inconsistencies.append(
                {
                    "canonical_fixture_id": canonical_fixture_id,
                    "market": str(canonical_row.get("market")),
                    "selection": str(canonical_row.get("selection")),
                    "line": canonical_row.get("line"),
                    "canonical_state": str(canonical_row.get("trade_state")),
                    "group_states": sorted({str(row.get("trade_state") or "") for row in group_rows}),
                }
            )
        duplicate_groups.append(
            ReconciliationGroup(
                canonical_fixture_id=canonical_fixture_id,
                canonical_business_key=business_key,
                market=str(canonical_row.get("market") or ""),
                selection=str(canonical_row.get("selection") or ""),
                line=None if canonical_row.get("line") is None else float(canonical_row["line"]),
                rows=tuple(group_rows),
                canonical_row=canonical_row,
                duplicate_rows=tuple(duplicate_rows),
            )
        )

    by_canonical: dict[str, set[str]] = {}
    for fixture_id in distinct_fixture_ids:
        cfid = canonical_by_fixture.get(fixture_id) or f"unresolved:{fixture_id}"
        by_canonical.setdefault(cfid, set()).add(fixture_id)
    cross_provider_groups = sum(1 for fids in by_canonical.values() if len(fids) > 1)

    return ReconciliationPlan(
        total_rows=len(paper_rows),
        duplicate_groups=tuple(duplicate_groups),
        ambiguous_fixture_ids=tuple(sorted(set(ambiguous))),
        unresolved_fixture_ids=tuple(sorted(set(unresolved))),
        conflicts=tuple(conflicts),
        inconsistencies=tuple(inconsistencies),
        canonical_fixture_count=len(set(canonical_by_fixture.values())),
        cross_provider_fixture_groups=cross_provider_groups,
        canonical_fixture_by_fixture_id=dict(canonical_by_fixture),
    )


def summarize_performance(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Compact deterministic performance summary over a set of paper rows."""
    settled = [row for row in rows if str(row.get("trade_state") or "") in _FINAL_STATES]
    strike_rows = [row for row in rows if str(row.get("trade_state") or "") in {"WON", "LOST", "HALF_WIN", "HALF_LOSS"}]
    weighted_wins = sum(
        1.0 if row.get("trade_state") == "WON" else 0.5 if row.get("trade_state") == "HALF_WIN" else 0.0
        for row in strike_rows
    )
    net = sum(float(row.get("net_pnl") or 0.0) for row in settled)
    stake = float(len(settled))
    return {
        "total": len(rows),
        "open": len([row for row in rows if str(row.get("trade_state") or "") in {"PENDING", "OPEN", "UNSETTLED"}]),
        "settled": len(settled),
        "won": sum(1 for row in settled if row.get("trade_state") == "WON"),
        "lost": sum(1 for row in settled if row.get("trade_state") == "LOST"),
        "push": sum(1 for row in settled if row.get("trade_state") == "PUSH"),
        "half_win": sum(1 for row in settled if row.get("trade_state") == "HALF_WIN"),
        "half_loss": sum(1 for row in settled if row.get("trade_state") == "HALF_LOSS"),
        "void": sum(1 for row in settled if row.get("trade_state") == "VOID"),
        "pl": round(net, 4),
        "roi": round(net / stake, 6) if stake else None,
        "strike_rate": round(weighted_wins / len(strike_rows), 6) if strike_rows else None,
    }


def validate_reconciliation_safety(conn: sqlite3.Connection, plan: ReconciliationPlan, paper_rows: list[dict[str, Any]]) -> list[str]:
    """Return a list of invariant violations (empty means safe to apply)."""
    violations: list[str] = []
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    if str(integrity).lower() != "ok":
        violations.append(f"PAPER_DB_INTEGRITY: {integrity}")
    total = conn.execute("SELECT COUNT(*) FROM paper_trade_entries").fetchone()[0]
    distinct = conn.execute("SELECT COUNT(DISTINCT paper_trade_id) FROM paper_trade_entries").fetchone()[0]
    if int(total) != int(distinct):
        violations.append("PAPER_ID_COLLISION")
    orphan = conn.execute(
        """
        SELECT COUNT(*) FROM paper_trade_updates u
        LEFT JOIN paper_trade_entries e ON e.paper_trade_id = u.paper_trade_id
        WHERE e.paper_trade_id IS NULL
        """
    ).fetchone()[0]
    if int(orphan) > 0:
        violations.append(f"ORPHAN_PAPER_UPDATES: {orphan}")
    if plan.ambiguous_fixture_ids:
        violations.append("CANONICAL_FIXTURE_AMBIGUOUS")
    if plan.conflicts:
        violations.append("CROSS_PROVIDER_SETTLEMENT_CONFLICT")
    if plan.inconsistencies:
        violations.append("CROSS_PROVIDER_SETTLEMENT_INCONSISTENT")

    existing_excluded = fetch_excluded_paper_trade_ids(conn)
    canonical_ids: set[str] = set()
    for group in plan.duplicate_groups:
        canonical_id = str(group.canonical_row.get("paper_trade_id") or "")
        if canonical_id in existing_excluded:
            violations.append("CANONICAL_POSITION_ALREADY_EXCLUDED")
        if canonical_id in canonical_ids:
            violations.append("CANONICAL_POSITION_COUNTED_TWICE")
        canonical_ids.add(canonical_id)
        for duplicate in group.duplicate_rows:
            duplicate_id = str(duplicate.get("paper_trade_id") or "")
            if duplicate_id == canonical_id:
                violations.append("CANONICAL_SELF_DUPLICATE")
            if duplicate_id in canonical_ids:
                violations.append("RECONCILIATION_CYCLE")
    return violations


# --------------------------------------------------------------------- apply / dry run
@dataclass(frozen=True)
class ReconciliationResult:
    plan: ReconciliationPlan
    applied: bool
    records_written: int = 0
    violations: tuple[str, ...] = ()
    existing_excluded: int = 0
    canonical_keys_populated: int = 0
    aliases_persisted: int = 0


class _ReconciliationBlocked(RuntimeError):
    def __init__(self, violations: list[str]) -> None:
        super().__init__("; ".join(violations))
        self.violations = tuple(violations)


def build_plan_from_conn(
    paper_conn: sqlite3.Connection,
    fixture_identity: Mapping[str, Mapping[str, Any]],
    *,
    kickoff_tolerance_seconds: int = DEFAULT_KICKOFF_TOLERANCE_SECONDS,
) -> ReconciliationPlan:
    """Build the reconciliation plan using rows read from the paper DB."""
    from . import football_paper_trading

    ensure_schema(paper_conn)
    rows = football_paper_trading.fetch_paper_trade_rows(paper_conn)
    return build_reconciliation_plan(rows, fixture_identity, kickoff_tolerance_seconds=kickoff_tolerance_seconds)


def _populate_counted_canonical_keys(
    conn: sqlite3.Connection,
    paper_rows: list[dict[str, Any]],
    plan: ReconciliationPlan,
) -> int:
    """Populate canonical business keys for COUNTED historical positions.

    Only positions that will remain counted receive a canonical key; duplicate /
    superseded representations keep ``canonical_business_key_signature = NULL``
    so they never compete for the partial unique index.
    """
    excluded = plan.projected_excluded_ids
    populated = 0
    for row in paper_rows:
        paper_trade_id = str(row.get("paper_trade_id") or "")
        if paper_trade_id in excluded:
            continue
        fixture_id = str(row.get("fixture_id") or "")
        canonical_fixture_id = plan.canonical_fixture_by_fixture_id.get(fixture_id)
        if not canonical_fixture_id or canonical_fixture_id.startswith(("unresolved:", "ambiguous:")):
            continue
        signature = canonical_paper_business_key_signature(
            canonical_fixture_id=canonical_fixture_id,
            market=str(row.get("market") or ""),
            selection=str(row.get("selection") or ""),
            line=None if row.get("line") is None else float(row["line"]),
        )
        conn.execute(
            "UPDATE paper_trade_entries SET canonical_business_key_signature = ? WHERE paper_trade_id = ?",
            (signature, paper_trade_id),
        )
        populated += 1
    return populated


def apply_reconciliation(
    paper_conn: sqlite3.Connection,
    fixture_identity: Mapping[str, Mapping[str, Any]],
    *,
    kickoff_tolerance_seconds: int = DEFAULT_KICKOFF_TOLERANCE_SECONDS,
) -> ReconciliationResult:
    """Atomically validate and apply the additive reconciliation.

    All canonical-fixture persistence, provider-alias persistence, reconciliation
    exclusions, and counted canonical-key population run inside ONE SQLite
    transaction. Any failure rolls back everything; there is no partially
    applied reconciliation.
    """
    from . import football_paper_trading

    ensure_schema(paper_conn)
    rows = football_paper_trading.fetch_paper_trade_rows(paper_conn)

    integrity = paper_conn.execute("PRAGMA integrity_check").fetchone()[0]
    if str(integrity).lower() != "ok":
        return ReconciliationResult(
            plan=ReconciliationPlan(total_rows=len(rows)),
            applied=False,
            violations=(f"PAPER_DB_INTEGRITY: {integrity}",),
        )

    try:
        with paper_conn:
            # Build the plan against the real connection so canonical fixtures
            # and provider aliases are persisted as part of the same transaction.
            plan = build_reconciliation_plan(
                rows,
                fixture_identity,
                kickoff_tolerance_seconds=kickoff_tolerance_seconds,
                persist_conn=paper_conn,
            )
            violations = validate_reconciliation_safety(paper_conn, plan, rows)
            if violations:
                raise _ReconciliationBlocked(list(violations))

            existing = fetch_excluded_paper_trade_ids(paper_conn)
            records_written = 0
            for group in plan.duplicate_groups:
                for duplicate in group.duplicate_rows:
                    duplicate_id = str(duplicate.get("paper_trade_id") or "")
                    # record_reconciliation is idempotent for an identical
                    # existing mapping and raises on a conflicting one, so the
                    # same duplicate can never be silently remapped.
                    record_reconciliation(
                        paper_conn,
                        duplicate_paper_trade_id=duplicate_id,
                        canonical_paper_trade_id=str(group.canonical_row.get("paper_trade_id") or ""),
                        canonical_fixture_id=group.canonical_fixture_id,
                    )
                    if duplicate_id not in existing:
                        records_written += 1

            # Duplicates already carry reconciliation exclusions above; now mark
            # counted positions with their canonical business keys. The partial
            # unique index enforces the invariant inside this transaction.
            keys_populated = _populate_counted_canonical_keys(paper_conn, rows, plan)

        aliases = paper_conn.execute("SELECT COUNT(*) FROM fixture_provider_aliases").fetchone()[0]
        return ReconciliationResult(
            plan=plan,
            applied=True,
            records_written=records_written,
            existing_excluded=len(existing),
            canonical_keys_populated=keys_populated,
            aliases_persisted=int(aliases),
        )
    except _ReconciliationBlocked as exc:
        return ReconciliationResult(
            plan=plan if "plan" in locals() else ReconciliationPlan(total_rows=len(rows)),
            applied=False,
            violations=tuple(exc.violations),
            existing_excluded=len(fetch_excluded_paper_trade_ids(paper_conn)),
        )
    except sqlite3.IntegrityError as exc:
        return ReconciliationResult(
            plan=plan if "plan" in locals() else ReconciliationPlan(total_rows=len(rows)),
            applied=False,
            violations=(f"INTEGRITY_ERROR: {exc}",),
        )
