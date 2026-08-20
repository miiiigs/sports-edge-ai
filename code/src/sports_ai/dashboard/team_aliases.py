"""Deterministic provider-team alias rules for the operational football dashboard."""

from __future__ import annotations

from dataclasses import dataclass
from difflib import get_close_matches
from typing import Iterable

from .models import normalize_text_key


@dataclass(frozen=True)
class ProviderTeamAlias:
    canonical_team_name: str
    diagnostic_category: str
    notes: str


_FOOTBALL_DATA_TEAM_ID_ALIASES: dict[str, dict[str, ProviderTeamAlias]] = {
    "ENG1": {
        "397": ProviderTeamAlias("Brighton", "D", "football-data.org shortName differs from historical canonical name"),
        "1076": ProviderTeamAlias(
            "Coventry",
            "E",
            "provider shortName is deterministic, but current top-flight model history may still be missing",
        ),
        "322": ProviderTeamAlias(
            "Hull",
            "E",
            "provider shortName is deterministic, but current top-flight model history may still be missing",
        ),
        "349": ProviderTeamAlias("Ipswich", "D", "provider shortName differs from historical canonical name"),
        "341": ProviderTeamAlias("Leeds", "D", "provider shortName differs from historical canonical name"),
        "351": ProviderTeamAlias("Nott'm Forest", "D", "provider shortName differs from historical canonical name"),
    },
    "ENG2": {
        "60": ProviderTeamAlias(
            "Bolton",
            "E",
            "club identity is deterministic, but current Championship model history may still be missing",
        ),
        "342": ProviderTeamAlias("Derby", "D", "provider shortName differs from historical canonical name"),
        "1126": ProviderTeamAlias(
            "Lincoln City",
            "E",
            "club identity is deterministic, but current Championship model history may still be missing",
        ),
        "1081": ProviderTeamAlias("Preston", "D", "provider shortName differs from historical canonical name"),
        "563": ProviderTeamAlias(
            "West Ham",
            "E",
            "club identity is deterministic, but current Championship model history may still be missing",
        ),
        "76": ProviderTeamAlias(
            "Wolves",
            "E",
            "provider shortName differs from historical canonical name and current Championship history may be missing",
        ),
    },
    "ESP1": {
        "78": ProviderTeamAlias("Ath Madrid", "D", "provider shortName differs from historical canonical name"),
        "81": ProviderTeamAlias("Barcelona", "B", "accented shortName differs from historical canonical name"),
        "84": ProviderTeamAlias(
            "Malaga",
            "E",
            "provider shortName identifies the club, but current La Liga model history may still be missing",
        ),
        "5335": ProviderTeamAlias(
            "Racing Santander",
            "E",
            "provider shortName identifies the club, but current La Liga model history may still be missing",
        ),
    },
}

_FOOTBALL_DATA_TEAM_NAME_ALIASES: dict[str, dict[str, ProviderTeamAlias]] = {
    "ENG1": {
        normalize_text_key("Brighton Hove"): _FOOTBALL_DATA_TEAM_ID_ALIASES["ENG1"]["397"],
        normalize_text_key("Coventry City"): _FOOTBALL_DATA_TEAM_ID_ALIASES["ENG1"]["1076"],
        normalize_text_key("Hull City"): _FOOTBALL_DATA_TEAM_ID_ALIASES["ENG1"]["322"],
        normalize_text_key("Ipswich Town"): _FOOTBALL_DATA_TEAM_ID_ALIASES["ENG1"]["349"],
        normalize_text_key("Leeds United"): _FOOTBALL_DATA_TEAM_ID_ALIASES["ENG1"]["341"],
        normalize_text_key("Nottingham"): _FOOTBALL_DATA_TEAM_ID_ALIASES["ENG1"]["351"],
    },
    "ENG2": {
        normalize_text_key("Bolton"): _FOOTBALL_DATA_TEAM_ID_ALIASES["ENG2"]["60"],
        normalize_text_key("Derby County"): _FOOTBALL_DATA_TEAM_ID_ALIASES["ENG2"]["342"],
        normalize_text_key("Lincoln City"): _FOOTBALL_DATA_TEAM_ID_ALIASES["ENG2"]["1126"],
        normalize_text_key("Preston NE"): _FOOTBALL_DATA_TEAM_ID_ALIASES["ENG2"]["1081"],
        normalize_text_key("West Ham"): _FOOTBALL_DATA_TEAM_ID_ALIASES["ENG2"]["563"],
        normalize_text_key("Wolverhampton"): _FOOTBALL_DATA_TEAM_ID_ALIASES["ENG2"]["76"],
    },
    "ESP1": {
        normalize_text_key("Atleti"): _FOOTBALL_DATA_TEAM_ID_ALIASES["ESP1"]["78"],
        normalize_text_key("Barça"): _FOOTBALL_DATA_TEAM_ID_ALIASES["ESP1"]["81"],
        normalize_text_key("Malaga"): _FOOTBALL_DATA_TEAM_ID_ALIASES["ESP1"]["84"],
        normalize_text_key("Santander"): _FOOTBALL_DATA_TEAM_ID_ALIASES["ESP1"]["5335"],
    },
}


def resolve_provider_team_alias(
    *,
    provider: str,
    competition_id: str,
    provider_team_id: str | None,
    provider_team_name: str,
) -> dict[str, str] | None:
    if provider != "football-data.org":
        return None

    if provider_team_id:
        by_id = _FOOTBALL_DATA_TEAM_ID_ALIASES.get(competition_id, {})
        alias = by_id.get(str(provider_team_id))
        if alias is not None:
            return {
                "canonical_team_name": alias.canonical_team_name,
                "resolution_kind": "provider_team_id_alias",
                "diagnostic_category": alias.diagnostic_category,
                "reason": alias.notes,
            }

    by_name = _FOOTBALL_DATA_TEAM_NAME_ALIASES.get(competition_id, {})
    alias = by_name.get(normalize_text_key(provider_team_name))
    if alias is None:
        return None
    return {
        "canonical_team_name": alias.canonical_team_name,
        "resolution_kind": "provider_team_name_alias",
        "diagnostic_category": alias.diagnostic_category,
        "reason": alias.notes,
    }


def suggest_canonical_candidates(
    candidate_names: Iterable[str],
    provider_team_name: str,
    *,
    limit: int = 3,
) -> tuple[str, ...]:
    unique_candidates = sorted({str(name) for name in candidate_names if str(name).strip()})
    if not unique_candidates:
        return ()
    normalized_lookup = {normalize_text_key(name): name for name in unique_candidates}
    normalized_provider_name = normalize_text_key(provider_team_name)
    direct = [name for key, name in normalized_lookup.items() if normalized_provider_name in key or key in normalized_provider_name]
    if direct:
        return tuple(sorted(set(direct))[:limit])
    close = get_close_matches(
        normalized_provider_name,
        list(normalized_lookup.keys()),
        n=limit,
        cutoff=0.62,
    )
    return tuple(normalized_lookup[key] for key in close)


def classify_unresolved_team(
    *,
    provider_team_name: str,
    canonical_candidates: tuple[str, ...],
) -> tuple[str, str]:
    normalized_name = normalize_text_key(provider_team_name)
    if len(canonical_candidates) > 1:
        return ("G", "multiple deterministic candidates remain after normalization")

    suffix_tokens = ("fc", "afc", "cf", "cd", "ud", "ac", "as", "ssc")
    lowered = provider_team_name.lower()
    if any(token in lowered.split() for token in suffix_tokens):
        return ("C", "provider naming uses a suffix variant that is not yet explicitly mapped")

    if any(char in provider_team_name for char in ("'", "-", ".", "&")):
        return ("B", "punctuation or formatting difference prevented a deterministic match")

    if canonical_candidates:
        return ("A", "an explicit alias is still required before auto-resolution is safe")

    if normalized_name:
        return ("F", "club identity may be known, but no approved current model history mapping exists yet")

    return ("H", "provider team payload is insufficient for deterministic resolution")
