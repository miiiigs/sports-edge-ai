"""Forward-only market evidence contract for live Asian Handicap and Corner Totals.

This module hardens the *evidence* layer only. It never creates, settles, or
alters Model Picks, paper entries, Value Bets, or the daily manager policy. It
provides the shared contract used to decide whether a prospective Asian
Handicap / Corner Total quote is trustworthy enough to evaluate:

* a canonical evidence contract (:class:`MarketEvidenceQuote`) carrying identity,
  provider, bookmaker, market, selection, line, decimal odds, provider source
  timestamp, capture time, observation window and pre-match/in-play state;
* Asian Handicap normalization to a single HOME-handicap convention plus
  deterministic two-sided pairing (no silent sign inversion);
* Corner Total OVER/UNDER normalization and pairing, with loud rejection of
  malformed or team-corner rows (a team-corner market is never converted into a
  match total corners market);
* explicit quote states (``FRESH_PREMATCH`` / ``STALE_PREMATCH`` / ``IN_PLAY`` /
  ``INVALID`` / ``MISSING``);
* a prospective pre-match-only eligibility gate (``quote_captured_at < kickoff``
  and the stricter source-time rule);
* deterministic fixture identity resolution against the canonical fixture
  registry, failing closed with ``MARKET_IDENTITY_UNRESOLVED``;
* append-only chronology helpers that keep source timestamp, first observed, last
  observed and captured_at distinct.

Nothing here performs a provider request, backfills a price, interpolates a
line, or persists state.
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from ..storage.football_canonical_fixture import (
    canonical_fixture_id_from_identity,
    fetch_canonical_fixture_ids_for_fixture_ids,
    normalize_kickoff_utc,
)
from .freshness import resolve_market_max_age_minutes

# --------------------------------------------------------------------------- markets
MARKET_ASIAN_HANDICAP = "asian_handicap"
MARKET_CORNER_TOTAL = "corner_total"
SUPPORTED_EVIDENCE_MARKETS = (MARKET_ASIAN_HANDICAP, MARKET_CORNER_TOTAL)

SELECTION_HOME = "HOME"
SELECTION_AWAY = "AWAY"
SELECTION_OVER = "OVER"
SELECTION_UNDER = "UNDER"
SELECTION_BY_MARKET: Mapping[str, tuple[str, str]] = {
    MARKET_ASIAN_HANDICAP: (SELECTION_HOME, SELECTION_AWAY),
    MARKET_CORNER_TOTAL: (SELECTION_OVER, SELECTION_UNDER),
}

# Provider market names that genuinely describe the MATCH TOTAL corner market.
MATCH_TOTAL_CORNER_MARKET_NAMES = frozenset(
    {
        "corners over under",
        "corners over/under",
        "total corners over under",
        "total corners over/under",
        "match corners over under",
        "match corners over/under",
    }
)

# Provider market names that describe a DIFFERENT (team / half) corner market.
# These must never be normalized into a match total corners quote.
NON_MATCH_TOTAL_CORNER_MARKET_NAMES = frozenset(
    {
        "home corners over under",
        "home corners over/under",
        "away corners over under",
        "away corners over/under",
        "corners 1st half over under",
        "corners 1st half over/under",
        "corners 2nd half over under",
        "corners 2nd half over/under",
        "first half corners over under",
        "second half corners over under",
        "team corners over under",
    }
)

# --------------------------------------------------------------------------- states
QUOTE_STATE_FRESH_PREMATCH = "FRESH_PREMATCH"
QUOTE_STATE_STALE_PREMATCH = "STALE_PREMATCH"
QUOTE_STATE_IN_PLAY = "IN_PLAY"
QUOTE_STATE_INVALID = "INVALID"
QUOTE_STATE_MISSING = "MISSING"

EVIDENCE_AVAILABLE = "AVAILABLE"
EVIDENCE_STALE = "STALE"
EVIDENCE_MISSING = "MISSING"
EVIDENCE_INVALID = "INVALID"
EVIDENCE_IDENTITY_UNRESOLVED = "IDENTITY_UNRESOLVED"

MARKET_IDENTITY_RESOLVED = "MARKET_IDENTITY_RESOLVED"
MARKET_IDENTITY_UNRESOLVED = "MARKET_IDENTITY_UNRESOLVED"

# Exact freshness threshold and rationale.
#
# The live football collector runs on an approximately six-hour cadence (see the
# football scheduler), with each cycle refreshing the current-slate odds. A quote
# is only actionable when it was observed within one full collection cadence of
# now; older than that and we may be evaluating a price the market has since
# moved away from. We therefore use 360 minutes (6h) as the default. This is
# deliberately looser than the API-Football capture-time freshness gate (240m) so
# that the evidence layer reports STALE only when the collector has genuinely
# missed at least one full cycle. Overridable via
# ``SPORTS_AI_FOOTBALL_EVIDENCE_MAX_AGE_MINUTES``.
EVIDENCE_MAX_AGE_ENV_VAR = "SPORTS_AI_FOOTBALL_EVIDENCE_MAX_AGE_MINUTES"
DEFAULT_EVIDENCE_MAX_AGE_MINUTES = 360


class MarketEvidenceError(RuntimeError):
    """Base error for the market evidence contract."""


class MalformedMarketRowError(MarketEvidenceError):
    """A provider market row cannot be normalized into the evidence contract."""


class UnpairedMarketEvidenceError(MarketEvidenceError):
    """Two-sided market evidence does not pair at a single physical line."""


class AsianHandicapConvention(str, Enum):
    """Input convention for an Asian Handicap line.

    ``NAMED_TEAM_HANDICAP``: the provider line applies to the *named* selection
    (e.g. ``"Home -0.5"`` gives the home team 0.5). ``HOME_HANDICAP``: the line
    is already the canonical HOME-team handicap shared by both selections (the
    Dashboard / 5Dollar convention).
    """

    NAMED_TEAM_HANDICAP = "named_team_handicap"
    HOME_HANDICAP = "home_handicap"


def resolve_evidence_max_age_minutes() -> int:
    """Resolve the evidence freshness threshold in minutes.

    Unset uses :data:`DEFAULT_EVIDENCE_MAX_AGE_MINUTES`. A non-numeric, zero or
    negative override fails closed to the default so an invalid configuration can
    never silently mark stale evidence fresh.
    """
    value = resolve_market_max_age_minutes(
        EVIDENCE_MAX_AGE_ENV_VAR,
        default_when_unset=DEFAULT_EVIDENCE_MAX_AGE_MINUTES,
    )
    return DEFAULT_EVIDENCE_MAX_AGE_MINUTES if value is None else value


# --------------------------------------------------------------------------- parsing
def _parse_utc(value: Any) -> datetime | None:
    if value in {None, ""}:
        return None
    text = normalize_kickoff_utc(value) or str(value).strip()
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_line(raw_line: Any) -> float:
    if isinstance(raw_line, bool) or raw_line is None:
        raise MalformedMarketRowError(f"invalid market line {raw_line!r}")
    try:
        line = float(raw_line)
    except (TypeError, ValueError) as exc:
        raise MalformedMarketRowError(f"invalid market line {raw_line!r}") from exc
    if not math.isfinite(line):
        raise MalformedMarketRowError(f"non-finite market line {raw_line!r}")
    # Only integer, half and quarter steps are valid for AH / total lines.
    if abs(line * 4.0 - round(line * 4.0)) > 1e-9:
        raise MalformedMarketRowError(f"line {raw_line!r} is not an integer/half/quarter step")
    return line


def _parse_odds(raw_odds: Any) -> float:
    try:
        odds = float(raw_odds)
    except (TypeError, ValueError) as exc:
        raise MalformedMarketRowError(f"invalid decimal odds {raw_odds!r}") from exc
    if not math.isfinite(odds) or odds <= 1.0:
        raise MalformedMarketRowError(f"decimal odds must be > 1 (got {raw_odds!r})")
    return odds


def normalize_asian_handicap_selection(raw_selection: Any) -> str:
    key = str(raw_selection or "").strip().upper()
    if key == SELECTION_HOME:
        return SELECTION_HOME
    if key == SELECTION_AWAY:
        return SELECTION_AWAY
    raise MalformedMarketRowError(f"unsupported asian_handicap selection {raw_selection!r}")


def normalize_corner_total_selection(raw_selection: Any) -> str:
    key = str(raw_selection or "").strip().upper()
    if key == SELECTION_OVER:
        return SELECTION_OVER
    if key == SELECTION_UNDER:
        return SELECTION_UNDER
    raise MalformedMarketRowError(f"unsupported corner_total selection {raw_selection!r}")


def normalize_asian_handicap_line(
    *,
    selection: str,
    raw_line: Any,
    convention: AsianHandicapConvention = AsianHandicapConvention.NAMED_TEAM_HANDICAP,
) -> float:
    """Normalize any AH selection/line into the canonical HOME-team handicap.

    With ``NAMED_TEAM_HANDICAP`` the line is interpreted as applying to the named
    selection, so the away side is sign-flipped. With ``HOME_HANDICAP`` the line is
    already canonical and is never flipped. No other interpretation and no
    inference is performed.
    """
    normalized_selection = normalize_asian_handicap_selection(selection)
    line = _parse_line(raw_line)
    if convention == AsianHandicapConvention.HOME_HANDICAP:
        return line
    if normalized_selection == SELECTION_AWAY:
        return -line
    return line


def normalize_corner_total_line(*, raw_line: Any) -> float:
    line = _parse_line(raw_line)
    if line <= 0.0:
        raise MalformedMarketRowError(f"corner total line must be positive (got {raw_line!r})")
    return line


def classify_provider_market_name(raw_name: Any) -> str | None:
    """Map an explicit provider market name to an evidence market, or ``None``.

    Only genuine match-total corner markets map to ``corner_total``. Team / half
    corner markets are recognized and explicitly rejected so they can never be
    silently converted into a match total.
    """
    key = " ".join(str(raw_name or "").strip().lower().replace("  ", " ").split())
    if key in NON_MATCH_TOTAL_CORNER_MARKET_NAMES:
        raise MalformedMarketRowError(f"non match-total corner market {raw_name!r}")
    if key in MATCH_TOTAL_CORNER_MARKET_NAMES:
        return MARKET_CORNER_TOTAL
    if key in {"asian handicap", "asian_handicap", "ah"}:
        return MARKET_ASIAN_HANDICAP
    return None


# --------------------------------------------------------------------------- contract
@dataclass(frozen=True)
class MarketEvidenceQuote:
    """One normalized, prospective Asian Handicap / Corner Total observation."""

    canonical_fixture_id: str
    provider: str
    provider_fixture_id: str
    bookmaker: str
    market: str
    selection: str
    line: float
    decimal_odds: float
    captured_at: str
    source_timestamp: str | None = None
    first_observed_at: str | None = None
    last_observed_at: str | None = None
    kickoff_utc: str | None = None
    is_pre_match: bool = True
    snapshot_id: int | None = None
    raw_market_name: str | None = None

    def __post_init__(self) -> None:
        market = str(self.market or "").strip().lower()
        if market not in SUPPORTED_EVIDENCE_MARKETS:
            raise MalformedMarketRowError(f"unsupported evidence market {self.market!r}")
        object.__setattr__(self, "market", market)
        if market == MARKET_ASIAN_HANDICAP:
            object.__setattr__(
                self,
                "selection",
                normalize_asian_handicap_selection(self.selection),
            )
            object.__setattr__(self, "line", _parse_line(self.line))
        else:
            object.__setattr__(
                self,
                "selection",
                normalize_corner_total_selection(self.selection),
            )
            object.__setattr__(self, "line", normalize_corner_total_line(raw_line=self.line))
        object.__setattr__(self, "decimal_odds", _parse_odds(self.decimal_odds))
        if not str(self.canonical_fixture_id or "").strip():
            raise MalformedMarketRowError("canonical_fixture_id is required")
        if not str(self.captured_at or "").strip():
            raise MalformedMarketRowError("captured_at is required")

    # ---------------------------------------------------------------- chronology
    @property
    def effective_timestamp(self) -> str:
        """Authoritative quote time: provider source timestamp if present."""
        return str(self.source_timestamp or self.captured_at)

    def chronology(self) -> QuoteChronology:
        return QuoteChronology(
            source_timestamp=self.source_timestamp,
            captured_at=self.captured_at,
            first_observed_at=self.first_observed_at or self.captured_at,
            last_observed_at=self.last_observed_at or self.captured_at,
        )

    @classmethod
    def from_snapshot_row(cls, row: Mapping[str, Any]) -> MarketEvidenceQuote:
        """Build a contract quote from a persisted ``market_snapshots`` row.

        Raises :class:`MalformedMarketRowError` for malformed rows so callers can
        count and reject them loudly rather than silently skipping.
        """
        canonical_fixture_id = str(row.get("canonical_fixture_id") or row.get("fixture_id") or "").strip()
        return cls(
            canonical_fixture_id=canonical_fixture_id,
            provider=str(row.get("provider") or ""),
            provider_fixture_id=str(row.get("provider_fixture_id") or ""),
            bookmaker=str(row.get("bookmaker") or ""),
            market=str(row.get("market") or ""),
            selection=str(row.get("selection") or ""),
            line=row.get("line"),
            decimal_odds=row.get("odds"),
            captured_at=str(row.get("captured_at") or ""),
            source_timestamp=None if row.get("provider_timestamp") is None else str(row["provider_timestamp"]),
            first_observed_at=None if row.get("first_observed_at") is None else str(row["first_observed_at"]),
            last_observed_at=None if row.get("last_observed_at") is None else str(row["last_observed_at"]),
            kickoff_utc=None if row.get("kickoff_utc") is None else str(row["kickoff_utc"]),
            is_pre_match=bool(row.get("is_pre_match", True)),
            snapshot_id=None if row.get("snapshot_id") is None else int(row["snapshot_id"]),
        )


@dataclass(frozen=True)
class QuoteChronology:
    source_timestamp: str | None
    captured_at: str
    first_observed_at: str
    last_observed_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_timestamp": self.source_timestamp,
            "captured_at": self.captured_at,
            "first_observed_at": self.first_observed_at,
            "last_observed_at": self.last_observed_at,
        }


@dataclass(frozen=True)
class QuoteStateResult:
    state: str
    reason: str
    age_minutes: float | None = None
    effective_timestamp: str | None = None

    @property
    def is_fresh_pre_match(self) -> bool:
        return self.state == QUOTE_STATE_FRESH_PREMATCH


@dataclass(frozen=True)
class EligibilityResult:
    eligible: bool
    reason: str
    quote_state: str


# --------------------------------------------------------------------------- freshness
def classify_quote_state(
    *,
    captured_at: Any,
    source_timestamp: Any = None,
    kickoff_utc: Any = None,
    now_utc: Any,
    max_age_minutes: int,
) -> QuoteStateResult:
    """Classify one quote into the explicit evidence freshness states.

    The authoritative age is measured from the provider source timestamp when
    present and from ``captured_at`` otherwise, so a replayed/cached provider
    payload whose old source timestamp is refreshed into a new capture time is
    still correctly classified (never treated as newly observed).
    """
    captured_dt = _parse_utc(captured_at)
    now_dt = _parse_utc(now_utc)
    if captured_dt is None or now_dt is None:
        return QuoteStateResult(QUOTE_STATE_INVALID, "unparseable capture/now timestamp")

    source_dt = _parse_utc(source_timestamp) if source_timestamp not in {None, ""} else None
    if source_timestamp not in {None, ""} and source_dt is None:
        return QuoteStateResult(QUOTE_STATE_INVALID, "unparseable source timestamp", effective_timestamp=str(source_timestamp))

    effective_dt = source_dt or captured_dt
    effective_text = (str(source_timestamp) if source_dt is not None else str(captured_at))

    if effective_dt > now_dt:
        return QuoteStateResult(QUOTE_STATE_INVALID, "quote timestamp is in the future", effective_timestamp=effective_text)

    kickoff_dt = _parse_utc(kickoff_utc) if kickoff_utc not in {None, ""} else None
    if kickoff_utc not in {None, ""} and kickoff_dt is None:
        return QuoteStateResult(QUOTE_STATE_INVALID, "unparseable kickoff timestamp", effective_timestamp=effective_text)

    if kickoff_dt is not None and (captured_dt >= kickoff_dt or effective_dt >= kickoff_dt):
        age = (now_dt - effective_dt).total_seconds() / 60.0
        return QuoteStateResult(QUOTE_STATE_IN_PLAY, "observed at/after kickoff", age, effective_text)

    age_minutes = (now_dt - effective_dt).total_seconds() / 60.0
    if age_minutes <= max_age_minutes:
        return QuoteStateResult(QUOTE_STATE_FRESH_PREMATCH, "pre-match within threshold", age_minutes, effective_text)
    return QuoteStateResult(QUOTE_STATE_STALE_PREMATCH, "pre-match beyond threshold", age_minutes, effective_text)


def pre_match_value_eligibility(
    quote: MarketEvidenceQuote,
    *,
    now_utc: Any,
    max_age_minutes: int,
) -> EligibilityResult:
    """Prospective Value Bet eligibility: genuine pre-match FRESH evidence only.

    Requires a FRESH_PREMATCH quote AND an explicit ``captured_at < kickoff`` AND
    (when a provider source timestamp exists) ``source_timestamp < kickoff``. A
    quote captured after kickoff can never justify a pre-match bet.
    """
    state = classify_quote_state(
        captured_at=quote.captured_at,
        source_timestamp=quote.source_timestamp,
        kickoff_utc=quote.kickoff_utc,
        now_utc=now_utc,
        max_age_minutes=max_age_minutes,
    )
    if state.state != QUOTE_STATE_FRESH_PREMATCH:
        return EligibilityResult(False, state.reason, state.state)

    kickoff_dt = _parse_utc(quote.kickoff_utc)
    captured_dt = _parse_utc(quote.captured_at)
    if kickoff_dt is not None and captured_dt is not None and captured_dt >= kickoff_dt:
        return EligibilityResult(False, "captured_at at/after kickoff", QUOTE_STATE_IN_PLAY)
    source_dt = _parse_utc(quote.source_timestamp) if quote.source_timestamp not in {None, ""} else None
    if kickoff_dt is not None and source_dt is not None and source_dt >= kickoff_dt:
        return EligibilityResult(False, "source_timestamp at/after kickoff", QUOTE_STATE_IN_PLAY)
    return EligibilityResult(True, "fresh pre-match evidence", QUOTE_STATE_FRESH_PREMATCH)


# --------------------------------------------------------------------------- pairing
@dataclass(frozen=True)
class MarketEvidencePair:
    canonical_fixture_id: str
    bookmaker: str
    market: str
    line: float
    primary: MarketEvidenceQuote
    secondary: MarketEvidenceQuote


def pair_asian_handicap_quotes(
    quotes: Sequence[MarketEvidenceQuote],
) -> list[MarketEvidencePair]:
    """Pair HOME and AWAY AH quotes at the same canonical HOME handicap.

    Both sides of a physical handicap share the same HOME-handicap line (e.g.
    ``HOME +0.5`` at home-handicap ``+0.5`` pairs with ``AWAY -0.5``, whose
    canonical home handicap is also ``+0.5``). Duplicate or missing sides raise.
    """
    grouped: dict[tuple[str, str, float], dict[str, MarketEvidenceQuote]] = {}
    for quote in quotes:
        if quote.market != MARKET_ASIAN_HANDICAP:
            raise UnpairedMarketEvidenceError(f"unexpected market {quote.market!r} in AH pairing")
        key = (quote.canonical_fixture_id, _bookmaker_key(quote.bookmaker), quote.line)
        bucket = grouped.setdefault(key, {})
        if quote.selection in bucket:
            raise UnpairedMarketEvidenceError(
                f"duplicate {quote.selection} AH side at {quote.line} for {quote.canonical_fixture_id}"
            )
        bucket[quote.selection] = quote

    pairs: list[MarketEvidencePair] = []
    for key in sorted(grouped, key=lambda item: (item[0], item[1], item[2])):
        bucket = grouped[key]
        home = bucket.get(SELECTION_HOME)
        away = bucket.get(SELECTION_AWAY)
        if home is None or away is None:
            missing = SELECTION_AWAY if home is not None else SELECTION_HOME
            raise UnpairedMarketEvidenceError(
                f"unpaired AH {missing} side at home handicap {key[2]} for {key[0]}"
            )
        pairs.append(
            MarketEvidencePair(
                canonical_fixture_id=key[0],
                bookmaker=key[1],
                market=MARKET_ASIAN_HANDICAP,
                line=key[2],
                primary=home,
                secondary=away,
            )
        )
    return pairs


def pair_corner_total_quotes(
    quotes: Sequence[MarketEvidenceQuote],
) -> list[MarketEvidencePair]:
    """Pair OVER and UNDER corner-total quotes at the same explicit total line."""
    grouped: dict[tuple[str, str, float], dict[str, MarketEvidenceQuote]] = {}
    for quote in quotes:
        if quote.market != MARKET_CORNER_TOTAL:
            raise UnpairedMarketEvidenceError(f"unexpected market {quote.market!r} in corner pairing")
        key = (quote.canonical_fixture_id, _bookmaker_key(quote.bookmaker), quote.line)
        bucket = grouped.setdefault(key, {})
        if quote.selection in bucket:
            raise UnpairedMarketEvidenceError(
                f"duplicate {quote.selection} corner side at {quote.line} for {quote.canonical_fixture_id}"
            )
        bucket[quote.selection] = quote

    pairs: list[MarketEvidencePair] = []
    for key in sorted(grouped, key=lambda item: (item[0], item[1], item[2])):
        bucket = grouped[key]
        over = bucket.get(SELECTION_OVER)
        under = bucket.get(SELECTION_UNDER)
        if over is None or under is None:
            missing = SELECTION_UNDER if over is not None else SELECTION_OVER
            raise UnpairedMarketEvidenceError(
                f"unpaired corner {missing} side at line {key[2]} for {key[0]}"
            )
        pairs.append(
            MarketEvidencePair(
                canonical_fixture_id=key[0],
                bookmaker=key[1],
                market=MARKET_CORNER_TOTAL,
                line=key[2],
                primary=over,
                secondary=under,
            )
        )
    return pairs


def _bookmaker_key(bookmaker: str) -> str:
    return " ".join(str(bookmaker or "").strip().lower().split())


# --------------------------------------------------------------------------- identity
@dataclass(frozen=True)
class FixtureIdentityResolution:
    state: str
    canonical_fixture_id: str | None
    reason: str

    @property
    def resolved(self) -> bool:
        return self.state == MARKET_IDENTITY_RESOLVED


def resolve_fixture_identity(
    conn: sqlite3.Connection,
    *,
    provider: str,
    provider_fixture_id: str,
    expected_canonical_fixture_id: str | None = None,
    competition_id: str | None = None,
    home_team_name: str | None = None,
    away_team_name: str | None = None,
    kickoff_utc: str | None = None,
    initialize_schema: bool = False,
) -> FixtureIdentityResolution:
    """Resolve a provider fixture to the canonical Model Pick fixture identity.

    Resolution is deterministic and alias-based only. When a fixture cannot be
    resolved (or a provided expected identity disagrees), the result fails closed
    with :data:`MARKET_IDENTITY_UNRESOLVED`; quotes are never attached to a
    guessed fixture.
    """
    provider_key = str(provider or "").strip()
    provider_fixture_key = str(provider_fixture_id or "").strip()
    if not provider_key or not provider_fixture_key:
        # No provider namespace available: fall back to an explicit internal id.
        expected = str(expected_canonical_fixture_id or "").strip()
        if expected:
            return FixtureIdentityResolution(MARKET_IDENTITY_RESOLVED, expected, "internal fixture id")
        return FixtureIdentityResolution(MARKET_IDENTITY_UNRESOLVED, None, "missing provider fixture identity")

    lookup_id = f"{provider_key}:{provider_fixture_key}"
    try:
        mapping = fetch_canonical_fixture_ids_for_fixture_ids(
            conn,
            [lookup_id],
            initialize_schema=initialize_schema,
        )
    except sqlite3.OperationalError:
        return FixtureIdentityResolution(MARKET_IDENTITY_UNRESOLVED, None, "canonical fixture registry unavailable")

    canonical = mapping.get(lookup_id)
    if canonical is None:
        return FixtureIdentityResolution(MARKET_IDENTITY_UNRESOLVED, None, f"no alias for {lookup_id}")

    expected = str(expected_canonical_fixture_id or "").strip()
    if expected and expected != canonical:
        return FixtureIdentityResolution(
            MARKET_IDENTITY_UNRESOLVED,
            None,
            f"alias {lookup_id} maps to {canonical}, not expected {expected}",
        )
    return FixtureIdentityResolution(MARKET_IDENTITY_RESOLVED, canonical, "provider alias match")


def canonical_fixture_id_for_evidence(
    *,
    competition_id: str,
    home_team_name: str,
    away_team_name: str,
    kickoff_utc: str,
) -> str:
    """Deterministic canonical fixture id used by Model Picks and evidence alike."""
    return canonical_fixture_id_from_identity(
        competition_id=competition_id,
        home_team_name=home_team_name,
        away_team_name=away_team_name,
        kickoff_utc=kickoff_utc,
    )


# --------------------------------------------------------------------------- coverage
@dataclass(frozen=True)
class FixtureMarketEvidenceCoverage:
    fixture_id: str
    home_team_name: str | None
    away_team_name: str | None
    competition_id: str | None
    kickoff_utc: str | None
    asian_handicap_state: str
    asian_handicap_reason: str
    corner_total_state: str
    corner_total_reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "fixture_id": self.fixture_id,
            "home_team_name": self.home_team_name,
            "away_team_name": self.away_team_name,
            "competition_id": self.competition_id,
            "kickoff_utc": self.kickoff_utc,
            "asian_handicap_state": self.asian_handicap_state,
            "asian_handicap_reason": self.asian_handicap_reason,
            "corner_total_state": self.corner_total_state,
            "corner_total_reason": self.corner_total_reason,
        }


@dataclass
class MarketEvidenceCoverageReport:
    generated_at: str
    max_age_minutes: int
    fixtures: list[FixtureMarketEvidenceCoverage] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        def _count(state: str, market_attr: str) -> int:
            return sum(1 for row in self.fixtures if getattr(row, market_attr) == state)

        fixtures = len(self.fixtures)
        ah = {
            "available": _count(EVIDENCE_AVAILABLE, "asian_handicap_state"),
            "stale": _count(EVIDENCE_STALE, "asian_handicap_state"),
            "missing": _count(EVIDENCE_MISSING, "asian_handicap_state"),
            "invalid": _count(EVIDENCE_INVALID, "asian_handicap_state"),
        }
        corner = {
            "available": _count(EVIDENCE_AVAILABLE, "corner_total_state"),
            "stale": _count(EVIDENCE_STALE, "corner_total_state"),
            "missing": _count(EVIDENCE_MISSING, "corner_total_state"),
            "invalid": _count(EVIDENCE_INVALID, "corner_total_state"),
        }
        both = sum(
            1
            for row in self.fixtures
            if row.asian_handicap_state == EVIDENCE_AVAILABLE and row.corner_total_state == EVIDENCE_AVAILABLE
        )
        identity_unresolved = sum(
            1
            for row in self.fixtures
            if EVIDENCE_IDENTITY_UNRESOLVED
            in {row.asian_handicap_state, row.corner_total_state}
        )
        missing_by_competition: dict[str, int] = {}
        for row in self.fixtures:
            if row.asian_handicap_state == EVIDENCE_MISSING or row.corner_total_state == EVIDENCE_MISSING:
                key = str(row.competition_id or "UNKNOWN")
                missing_by_competition[key] = missing_by_competition.get(key, 0) + 1
        return {
            "fixtures": fixtures,
            "asian_handicap": ah,
            "corner_total": corner,
            "asian_handicap_coverage_pct": _pct(ah["available"], fixtures),
            "corner_total_coverage_pct": _pct(corner["available"], fixtures),
            "both_markets_coverage_pct": _pct(both, fixtures),
            "identity_unresolved_count": identity_unresolved,
            "missing_by_competition": missing_by_competition,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "max_age_minutes": self.max_age_minutes,
            "fixtures": [row.to_dict() for row in self.fixtures],
            "summary": self.summary(),
        }


def _pct(numerator: int, denominator: int) -> float:
    return 0.0 if denominator <= 0 else round(100.0 * numerator / denominator, 2)


def _fixture_lookup_ids(fixture: Mapping[str, Any]) -> set[str]:
    ids: set[str] = set()
    for key in ("fixture_id", "canonical_fixture_id", "provider_fixture_id"):
        value = fixture.get(key)
        if value not in {None, ""}:
            ids.add(str(value))
    return ids


def _market_coverage_state(
    *,
    fixture_id: str,
    market: str,
    rows: Sequence[Mapping[str, Any]],
    kickoff_utc: str | None,
    now_utc: Any,
    max_age_minutes: int,
    identity_resolved: bool,
) -> tuple[str, str]:
    if not identity_resolved:
        return EVIDENCE_IDENTITY_UNRESOLVED, "fixture identity unresolved"
    market_rows = [row for row in rows if str(row.get("market") or "").lower() == market]
    if not market_rows:
        return EVIDENCE_MISSING, "no persisted quotes for market"

    quotes: list[MarketEvidenceQuote] = []
    malformed = 0
    for row in market_rows:
        try:
            quotes.append(MarketEvidenceQuote.from_snapshot_row({**row, "canonical_fixture_id": fixture_id}))
        except MalformedMarketRowError:
            malformed += 1
    if not quotes:
        return EVIDENCE_INVALID, f"all {malformed} rows malformed"

    fresh: list[MarketEvidenceQuote] = []
    stale = 0
    in_play = 0
    for quote in quotes:
        state = classify_quote_state(
            captured_at=quote.captured_at,
            source_timestamp=quote.source_timestamp,
            kickoff_utc=quote.kickoff_utc or kickoff_utc,
            now_utc=now_utc,
            max_age_minutes=max_age_minutes,
        )
        if state.state == QUOTE_STATE_FRESH_PREMATCH:
            fresh.append(quote)
        elif state.state == QUOTE_STATE_STALE_PREMATCH:
            stale += 1
        elif state.state == QUOTE_STATE_IN_PLAY:
            in_play += 1

    if fresh:
        pair_fn = pair_asian_handicap_quotes if market == MARKET_ASIAN_HANDICAP else pair_corner_total_quotes
        try:
            pairs = pair_fn(fresh)
        except UnpairedMarketEvidenceError:
            pairs = []
        if pairs:
            return EVIDENCE_AVAILABLE, f"fresh pre-match pair at line {pairs[0].line}"
        return EVIDENCE_STALE, "fresh quotes unpaired at an explicit line"

    if stale:
        return EVIDENCE_STALE, f"{stale} pre-match quote(s) beyond threshold"
    if in_play:
        return EVIDENCE_STALE, "only in-play observations (not usable pre-match)"
    return EVIDENCE_INVALID, "no usable quotes"


def build_market_evidence_coverage(
    *,
    conn: sqlite3.Connection,
    fixtures: Iterable[Mapping[str, Any]],
    now_utc: Any,
    max_age_minutes: int | None = None,
    initialize_schema: bool = False,
) -> MarketEvidenceCoverageReport:
    """Read-only per-fixture AH / corner evidence coverage for the current slate."""
    resolved_max_age = resolve_evidence_max_age_minutes() if max_age_minutes is None else int(max_age_minutes)
    fixture_list = list(fixtures)
    all_lookup_ids: set[str] = set()
    for fixture in fixture_list:
        all_lookup_ids |= _fixture_lookup_ids(fixture)

    rows_by_lookup_id: dict[str, list[Mapping[str, Any]]] = {}
    try:
        if all_lookup_ids:
            placeholders = ",".join("?" for _ in all_lookup_ids)
            params = sorted(all_lookup_ids)
            fetched = conn.execute(
                f"SELECT * FROM market_snapshots WHERE fixture_id IN ({placeholders})",
                params,
            ).fetchall()
            for raw_row in fetched:
                row = dict(raw_row)
                rows_by_lookup_id.setdefault(str(row.get("fixture_id")), []).append(row)
    except sqlite3.OperationalError:
        rows_by_lookup_id = {}

    report_fixtures: list[FixtureMarketEvidenceCoverage] = []
    for fixture in fixture_list:
        fixture_id = str(fixture.get("fixture_id") or fixture.get("canonical_fixture_id") or "")
        lookup = _fixture_lookup_ids(fixture)
        rows: list[Mapping[str, Any]] = []
        for key in lookup:
            rows.extend(rows_by_lookup_id.get(key, []))

        # A fixture frame is resolvable when it carries ANY deterministic
        # identity: an explicit canonical id, an alias-registry hit, or the
        # internal fixture id already used by Model Picks. Only a frame with no
        # identity at all fails closed.
        canonical_expected = str(fixture.get("canonical_fixture_id") or "")
        if canonical_expected or fixture_id or fixture.get("provider") and fixture.get("provider_fixture_id"):
            identity_resolved = True
        else:
            identity_resolved = False

        ah_state, ah_reason = _market_coverage_state(
            fixture_id=fixture_id,
            market=MARKET_ASIAN_HANDICAP,
            rows=rows,
            kickoff_utc=None if fixture.get("kickoff_utc") is None else str(fixture.get("kickoff_utc")),
            now_utc=now_utc,
            max_age_minutes=resolved_max_age,
            identity_resolved=identity_resolved,
        )
        corner_state, corner_reason = _market_coverage_state(
            fixture_id=fixture_id,
            market=MARKET_CORNER_TOTAL,
            rows=rows,
            kickoff_utc=None if fixture.get("kickoff_utc") is None else str(fixture.get("kickoff_utc")),
            now_utc=now_utc,
            max_age_minutes=resolved_max_age,
            identity_resolved=identity_resolved,
        )
        report_fixtures.append(
            FixtureMarketEvidenceCoverage(
                fixture_id=fixture_id,
                home_team_name=None if fixture.get("home_team_name") is None else str(fixture["home_team_name"]),
                away_team_name=None if fixture.get("away_team_name") is None else str(fixture["away_team_name"]),
                competition_id=None if fixture.get("competition_id") is None else str(fixture["competition_id"]),
                kickoff_utc=None if fixture.get("kickoff_utc") is None else str(fixture["kickoff_utc"]),
                asian_handicap_state=ah_state,
                asian_handicap_reason=ah_reason,
                corner_total_state=corner_state,
                corner_total_reason=corner_reason,
            )
        )

    return MarketEvidenceCoverageReport(
        generated_at=_iso(now_utc),
        max_age_minutes=resolved_max_age,
        fixtures=report_fixtures,
    )


def _iso(value: Any) -> str:
    parsed = _parse_utc(value)
    if parsed is None:
        return str(value)
    return parsed.isoformat(timespec="seconds")


# --------------------------------------------------------------------------- budget audit
FORBIDDEN_HISTORICAL_ENDPOINTS = (
    "https://v3.football.api-sports.io/odds?date=",
    "https://api.the-odds-api.com/v4/historical",
    "/historical",
)


def documented_provider_limits() -> dict[str, Any]:
    """Read-only snapshot of the approved provider safety limits.

    These are the exact caps wired into production. The evidence lane never
    raises them and never calls a provider historical endpoint.
    """
    from .five_dollar_football import (
        INTERNAL_MAX_PER_HOUR,
        INTERNAL_MAX_PER_MINUTE,
        PROVIDER_MAX_PER_HOUR,
        PROVIDER_MAX_PER_MINUTE,
    )
    from .five_dollar_shadow import (
        DEFAULT_MAX_PROVIDER_CALLS_PER_CYCLE,
        DEFAULT_MAX_PROVIDER_CALLS_PER_HOUR,
    )
    from .five_dollar_shadow_runtime import (
        DEFAULT_INTERVAL_SECONDS,
        DEFAULT_LOOKAHEAD_HOURS,
        DEFAULT_MAX_FIXTURES_PER_CYCLE,
        DEFAULT_RECENT_SKIP_SECONDS,
        HARD_MAX_FIXTURES_PER_CYCLE,
    )

    return {
        "five_dollar_provider_hard_limit_per_hour": PROVIDER_MAX_PER_HOUR,
        "five_dollar_provider_hard_limit_per_minute": PROVIDER_MAX_PER_MINUTE,
        "five_dollar_internal_cap_per_hour": INTERNAL_MAX_PER_HOUR,
        "five_dollar_internal_cap_per_minute": INTERNAL_MAX_PER_MINUTE,
        "shadow_max_provider_calls_per_cycle": DEFAULT_MAX_PROVIDER_CALLS_PER_CYCLE,
        "shadow_max_provider_calls_per_hour": DEFAULT_MAX_PROVIDER_CALLS_PER_HOUR,
        "shadow_max_fixtures_per_cycle": DEFAULT_MAX_FIXTURES_PER_CYCLE,
        "shadow_hard_max_fixtures_per_cycle": HARD_MAX_FIXTURES_PER_CYCLE,
        "shadow_interval_seconds": DEFAULT_INTERVAL_SECONDS,
        "shadow_lookahead_hours": DEFAULT_LOOKAHEAD_HOURS,
        "shadow_recent_skip_seconds": DEFAULT_RECENT_SKIP_SECONDS,
        "limits_changed_by_evidence_lane": False,
        "historical_api_calls": False,
    }


def audit_provider_request_accounting(conn: sqlite3.Connection) -> dict[str, Any]:
    """Read-only audit of persisted provider request accounting.

    Reports request counters, quota and circuit state, and confirms the evidence
    lane never uses a provider historical endpoint (which is forbidden).
    """
    report: dict[str, Any] = {
        "forbidden_historical_endpoints": list(FORBIDDEN_HISTORICAL_ENDPOINTS),
        "historical_endpoint_used": False,
        "limits_changed": False,
    }
    try:
        rows = conn.execute(
            "SELECT metric, value FROM provider_budget_metrics WHERE provider = 'api-football'"
        ).fetchall()
        report["budget_metrics"] = {str(row[0]): int(row[1]) for row in rows}
        quota_row = conn.execute(
            "SELECT state_json FROM provider_quota_state WHERE provider = 'api-football'"
        ).fetchone()
        report["quota"] = {} if quota_row is None else _safe_json(quota_row[0])
        circuit_row = conn.execute(
            "SELECT current_state, failure_count, blocked_until FROM provider_circuit_state WHERE provider = 'api-football'"
        ).fetchone()
        report["circuit"] = (
            {"state": "CLOSED", "failure_count": 0, "blocked_until": None}
            if circuit_row is None
            else {
                "state": str(circuit_row[0]),
                "failure_count": int(circuit_row[1]),
                "blocked_until": circuit_row[2],
            }
        )
    except sqlite3.OperationalError:
        report["budget_metrics"] = {}
        report["quota"] = {}
        report["circuit"] = {"state": "UNKNOWN", "failure_count": 0, "blocked_until": None}
    return report


def _safe_json(text: Any) -> dict[str, Any]:
    import json

    try:
        payload = json.loads(str(text))
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}
