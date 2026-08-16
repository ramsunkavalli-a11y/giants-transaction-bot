"""Verified MLB StatsAPI access and transaction semantics.

This module is the authoritative domain layer for transaction classification,
player stats, and player transaction history. It deliberately avoids the
people-hydrate stats/transactions shapes that proved unreliable in real data.
"""

from urllib.parse import urlencode

import bot_core as infra

SPORT_LEVELS = [(11, "AAA"), (12, "AA"), (13, "A+"), (14, "A"), (16, "Rk")]
SPORT_LEVEL_BY_ID = {1: "MLB", **dict(SPORT_LEVELS)}


def _hay(tx: dict) -> str:
    return f"{tx.get('typeDesc','')} {tx.get('description','')}".lower()


def is_contract_selected_transaction(tx: dict) -> bool:
    """MLB StatsAPI uses SE / Selected for contract selections."""
    tc = (tx.get("typeCode") or "").strip().upper()
    hay = _hay(tx)
    return (
        "selected the contract" in hay
        or "contract selected" in hay
        or (tc == "SE" and "selected" in hay and "contract" in hay)
    )


def is_signing_transaction(tx: dict) -> bool:
    tc = (tx.get("typeCode") or "").strip().upper()
    hay = _hay(tx)

    if is_contract_selected_transaction(tx):
        return False

    negative = (
        "assigned", "designated", "released", "traded", "waiver",
        "optioned", "outright", "activated", "recalled",
    )
    if any(word in hay for word in negative):
        return False

    # SC is generic "Status Change" (IL/roster moves), not a signing.
    if tc in {"SFA", "SMC", "S", "FA"}:
        return True

    return any(
        phrase in hay
        for phrase in (
            "signed", "minor league contract", "major league contract",
            "free agent signing",
        )
    )


def is_optioned_transaction(tx: dict) -> bool:
    tc = (tx.get("typeCode") or "").strip().upper()
    hay = _hay(tx)
    return tc == "OPT" or "optioned" in hay


def is_recalled_transaction(tx: dict) -> bool:
    tc = (tx.get("typeCode") or "").strip().upper()
    hay = _hay(tx)
    # CU is the code observed on real 2026 recall transactions.
    return tc in {"CU", "RCL", "REC"} or "recalled" in hay


def is_dfa_transaction(tx: dict) -> bool:
    tc = (tx.get("typeCode") or "").strip().upper()
    hay = _hay(tx)
    # DES is the code observed on real 2026 DFA transactions.
    return tc in {"DES", "DFA"} or (
        "designated" in hay and "for assignment" in hay
    )


def classify_transaction(tx: dict, season_mode: bool) -> str | None:
    """Return the special-post category, or None for grouped transactions."""
    if is_signing_transaction(tx):
        return "signing"
    if not season_mode:
        return None
    if is_optioned_transaction(tx):
        return "optioned"
    if is_recalled_transaction(tx):
        return "recalled"
    if is_dfa_transaction(tx):
        return "dfa"
    if is_contract_selected_transaction(tx):
        return "contract_selected"
    return None


def _stats_group(pitcher: bool) -> str:
    return "pitching" if pitcher else "hitting"


def _appearance_volume(stat: dict, pitcher: bool) -> float:
    if pitcher:
        return infra._safe_float((stat or {}).get("inningsPitched")) or 0.0
    return float(
        infra._safe_int((stat or {}).get("plateAppearances"))
        or infra._safe_int((stat or {}).get("atBats"))
        or infra._safe_int((stat or {}).get("gamesPlayed"))
        or 0
    )


def _stats_url(person_id: int, pitcher: bool, sport_id: int, stat_type: str,
               season: int, start_date=None, end_date=None) -> str:
    params = {
        "stats": stat_type,
        "group": _stats_group(pitcher),
        "sportId": sport_id,
        "season": int(season),
    }
    if start_date:
        params["startDate"] = str(start_date)
    if end_date:
        params["endDate"] = str(end_date)
    return (
        f"https://statsapi.mlb.com/api/v1/people/{person_id}/stats?"
        + urlencode(params)
    )


def fetch_player_stat(person_id: int, pitcher: bool, sport_id: int, stat_type: str,
                      season: int, cache=None, start_date=None, end_date=None):
    """Fetch an exact player's stat line at one exact sport level.

    Returns ``(selected, request_succeeded)``. The dedicated player-scoped
    endpoint and singular ``sportId`` parameter were verified against real MLB
    and MiLB 2026 data. For season responses with multiple organizations, the
    aggregate ``team=None`` split is preferred.
    """
    cache = cache if cache is not None else {}
    key = (
        "domain_stat", int(person_id), bool(pitcher), int(sport_id), stat_type,
        int(season), str(start_date or ""), str(end_date or ""),
    )
    if key in cache:
        return cache[key]

    try:
        data = infra.request_json_with_retry(
            _stats_url(
                person_id, pitcher, sport_id, stat_type, season,
                start_date=start_date, end_date=end_date,
            )
        )
    except Exception:
        result = (None, False)
        cache[key] = result
        return result

    candidates = []
    for block in data.get("stats") or []:
        for split in block.get("splits") or []:
            if not isinstance(split, dict):
                continue
            if infra._safe_int(infra._get_in(split, "sport", "id")) != sport_id:
                continue
            stat = split.get("stat") or {}
            volume = _appearance_volume(stat, pitcher)
            if volume <= 0:
                continue
            aggregate_rank = 1 if not split.get("team") else 0
            candidates.append((aggregate_rank, volume, split))

    if not candidates:
        result = (None, True)
        cache[key] = result
        return result

    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    split = candidates[0][2]
    selected = {
        "seasonYear": infra._safe_int(split.get("season")) or int(season),
        "orgName": infra._clean_text(infra._get_in(split, "team", "name")),
        "levelToken": SPORT_LEVEL_BY_ID.get(sport_id),
        "splitStats": split.get("stat") or {},
        "split": split,
    }
    result = (selected, True)
    cache[key] = result
    return result


def fetch_highest_milb_stat(person_id: int, pitcher: bool, stat_type: str,
                            season: int, cache=None, start_date=None, end_date=None):
    """Return the highest MiLB level at which the player actually appeared."""
    any_success = False
    for sport_id, _level in SPORT_LEVELS:
        selected, ok = fetch_player_stat(
            person_id, pitcher, sport_id, stat_type, season,
            cache=cache, start_date=start_date, end_date=end_date,
        )
        any_success = any_success or ok
        if selected:
            return selected, True
    return None, any_success


def fetch_player_details(person_id: int, cache=None):
    """Fetch only bio fields needed for enrichment; no transaction/stat hydrate."""
    cache = cache if cache is not None else {}
    key = ("domain_details", int(person_id))
    if key in cache:
        return cache[key]
    hydrate = "currentTeam,education,draft"
    url = f"https://statsapi.mlb.com/api/v1/people/{person_id}?hydrate={hydrate}"
    try:
        data = infra.request_json_with_retry(url)
        result = (data.get("people") or [{}])[0] or {}
    except Exception:
        result = {}
    cache[key] = result
    return result


def _transactions_url(person_id: int, start_date, end_date) -> str:
    return "https://statsapi.mlb.com/api/v1/transactions?" + urlencode({
        "playerId": int(person_id),
        "startDate": str(start_date),
        "endDate": str(end_date),
    })


def fetch_player_transactions(person_id: int, start_date, end_date, cache=None):
    """Fetch direct player transaction history, avoiding unreliable hydrates."""
    cache = cache if cache is not None else {}
    key = (
        "domain_transactions", int(person_id), str(start_date), str(end_date),
    )
    if key in cache:
        return cache[key]
    try:
        data = infra.request_json_with_retry(
            _transactions_url(person_id, start_date, end_date)
        )
        result = (
            [tx for tx in (data.get("transactions") or []) if isinstance(tx, dict)],
            True,
        )
    except Exception:
        result = ([], False)
    cache[key] = result
    return result


def latest_prior_transaction_date(person_id: int, current_tx: dict, predicate,
                                  cache=None):
    """Find the latest qualifying transaction strictly before ``current_tx``.

    Transaction IDs break same-day ties so a later same-day event can never be
    treated as history for an earlier event.
    """
    current_date = infra.txn_date_obj(current_tx)
    if not current_date:
        return None
    current_id = infra.txn_id(current_tx)
    start_date = f"{current_date.year}-01-01"
    txs, ok = fetch_player_transactions(
        person_id, start_date, current_date, cache=cache
    )
    if not ok:
        return None

    candidates = []
    for hist in txs:
        hist_date = infra.txn_date_obj(hist)
        hist_id = infra.txn_id(hist)
        if not hist_date or hist_date > current_date:
            continue
        if hist_date == current_date:
            if current_id and hist_id and hist_id >= current_id:
                continue
            if current_id and hist_id == current_id:
                continue
        if predicate(hist):
            candidates.append((hist_date, hist_id))

    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][0]
