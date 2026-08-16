"""40-man roster occupancy and reconciliation helpers.

MLB's ``40Man`` roster endpoint includes players on the 60-day injured list,
who do not occupy 40-man spots.  The helpers below exclude D60 players and add
a narrow reconciliation path for a known StatsAPI failure mode: a recently
traded player who was on his previous club's 40-man can temporarily disappear
from the acquiring club's 40-man feed while on a minor-league IL/rehab
assignment.
"""

from datetime import date, timedelta
from urllib.parse import urlencode

import bot_core as infra
import mlb_domain as domain

D60_CODE = "D60"


def _as_date(value):
    if isinstance(value, date):
        return value
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _team_transactions_url(team_id: int, start_date, end_date) -> str:
    return "https://statsapi.mlb.com/api/v1/transactions?" + urlencode({
        "teamId": int(team_id),
        "startDate": str(start_date),
        "endDate": str(end_date),
    })


def fetch_team_transactions(team_id: int, start_date, end_date, cache=None):
    cache = cache if cache is not None else {}
    key = ("roster_team_tx", int(team_id), str(start_date), str(end_date))
    if key in cache:
        return cache[key]
    try:
        data = infra.request_json_with_retry(
            _team_transactions_url(team_id, start_date, end_date)
        )
        result = (
            [tx for tx in (data.get("transactions") or []) if isinstance(tx, dict)],
            True,
        )
    except Exception:
        result = ([], False)
    cache[key] = result
    return result


def fetch_40man_members(team_id: int, as_of_date=None, cache=None):
    """Return non-D60 40-man members keyed by player ID.

    ``as_of_date`` uses MLB's historical roster snapshot when supplied.
    """
    cache = cache if cache is not None else {}
    d = _as_date(as_of_date)
    key = ("roster_40man", int(team_id), str(d or "current"))
    if key in cache:
        return cache[key]

    url = f"https://statsapi.mlb.com/api/v1/teams/{int(team_id)}/roster/40Man"
    if d:
        url += "?" + urlencode({"date": d.isoformat()})
    try:
        data = infra.request_json_with_retry(url)
        members = {}
        for item in data.get("roster") or []:
            person = item.get("person") or {}
            status = item.get("status") or {}
            pid = infra._safe_int(person.get("id"))
            if not pid or (status.get("code") or "").upper() == D60_CODE:
                continue
            members[pid] = infra._clean_text(person.get("fullName")) or str(pid)
        result = (members, True)
    except Exception:
        result = ({}, False)
    cache[key] = result
    return result


def is_d60_create_transaction(tx: dict) -> bool:
    hay = f"{tx.get('typeDesc','')} {tx.get('description','')}".lower()
    if "60-day injured list" not in hay:
        return False
    return (
        "transferred" in hay
        or "placed" in hay
    ) and (
        " to the 60-day injured list" in hay
        or " on the 60-day injured list" in hay
    )


def is_d60_return_transaction(tx: dict) -> bool:
    hay = f"{tx.get('typeDesc','')} {tx.get('description','')}".lower()
    return (
        "60-day injured list" in hay
        and ("reinstated" in hay or "activated" in hay)
        and "from" in hay
    )


def _is_removal_from_40man(tx: dict, team_id: int, person_id: int) -> bool:
    if infra.extract_tx_player_id(tx) != person_id:
        return False
    if domain.is_dfa_transaction(tx):
        return True
    if domain.is_outrighted_transaction(tx):
        return True
    if domain.is_released_transaction(tx):
        return True
    if domain.is_declared_free_agency_transaction(tx):
        return True
    if is_d60_create_transaction(tx):
        return True

    code = (tx.get("typeCode") or "").strip().upper()
    from_id = infra._safe_int(infra._get_in(tx, "fromTeam", "id"))
    to_id = infra._safe_int(infra._get_in(tx, "toTeam", "id"))
    if domain.is_trade_transaction(tx) and from_id == int(team_id) and to_id != int(team_id):
        return True
    if code in {"CLW", "WA"} and from_id == int(team_id) and to_id != int(team_id):
        return True
    return False


def _generic_40man_state_change(tx: dict):
    """Return True/False when a transaction clearly establishes 40-man state."""
    if domain.is_contract_selected_transaction(tx):
        return True
    if domain.is_claimed_off_waivers_transaction(tx):
        return True
    if is_d60_return_transaction(tx):
        return True

    if domain.is_dfa_transaction(tx):
        return False
    if domain.is_outrighted_transaction(tx):
        return False
    if domain.is_released_transaction(tx):
        return False
    if domain.is_declared_free_agency_transaction(tx):
        return False
    if is_d60_create_transaction(tx):
        return False

    # A clearly identified MLB contract creates a 40-man spot.  Generic/minor
    # free-agent signings intentionally remain unknown.
    if domain.is_signing_transaction(tx):
        hay = f"{tx.get('typeDesc','')} {tx.get('description','')}".lower()
        if "major league contract" in hay:
            return True
    return None


def recent_player_40man_state(person_id: int, before_date, cache=None,
                              lookback_days: int = 1825):
    """Infer 40-man state immediately before a date from transaction history.

    This is independent confirmation for roster-feed reconciliation.  Trades,
    options, recalls and minor-league assignments preserve the last known state.
    """
    cache = cache if cache is not None else {}
    before = _as_date(before_date)
    if not before:
        return None, False
    end = before - timedelta(days=1)
    start = end - timedelta(days=max(1, int(lookback_days)))
    history, ok = domain.fetch_player_transactions(
        person_id, start, end, cache=cache
    )
    if not ok:
        return None, False
    state = None
    for hist in sorted(
        history,
        key=lambda item: (infra.txn_date_obj(item) or date.min, infra.txn_id(item)),
    ):
        change = _generic_40man_state_change(hist)
        if change is not None:
            state = change
    return state, True


def build_trade_exception_windows(team_id: int, start_date, end_date, cache=None):
    """Find incoming trades with independently verified 40-man membership.

    StatsAPI's historical ``40Man`` feed can itself include false positives, so
    appearance on the previous club's roster is not enough.  We require both:
    (1) the player appears on that prior-club snapshot, and (2) direct player
    transaction history says his last known 40-man state was ON.  Once verified,
    the state carries through the trade until a later explicit removal.
    """
    cache = cache if cache is not None else {}
    start = _as_date(start_date)
    end = _as_date(end_date)
    if not start or not end or end < start:
        return [], False

    key = ("roster_trade_windows", int(team_id), str(start), str(end))
    if key in cache:
        return cache[key]

    txs, ok = fetch_team_transactions(team_id, start, end, cache=cache)
    if not ok:
        result = ([], False)
        cache[key] = result
        return result

    incoming = {}
    for tx in txs:
        if not domain.is_trade_transaction(tx):
            continue
        to_id = infra._safe_int(infra._get_in(tx, "toTeam", "id"))
        from_id = infra._safe_int(infra._get_in(tx, "fromTeam", "id"))
        pid = infra.extract_tx_player_id(tx)
        tx_date = infra.txn_date_obj(tx)
        if to_id != int(team_id) or not from_id or not pid or not tx_date:
            continue
        incoming[(pid, tx_date)] = (tx, from_id)

    windows = []
    for (pid, trade_date), (tx, from_id) in incoming.items():
        previous_members, prior_ok = fetch_40man_members(
            from_id, trade_date - timedelta(days=1), cache=cache
        )
        inferred, infer_ok = recent_player_40man_state(
            pid, trade_date, cache=cache
        )
        if not (
            prior_ok
            and pid in previous_members
            and infer_ok
            and inferred is True
        ):
            continue

        player_txs, hist_ok = domain.fetch_player_transactions(
            pid, trade_date, end, cache=cache
        )
        if not hist_ok:
            continue
        removal = None
        current_id = infra.txn_id(tx)
        for hist in sorted(
            player_txs,
            key=lambda item: (infra.txn_date_obj(item) or date.min, infra.txn_id(item)),
        ):
            hist_date = infra.txn_date_obj(hist)
            hist_id = infra.txn_id(hist)
            if not hist_date or hist_date < trade_date:
                continue
            if hist_date == trade_date and current_id and hist_id <= current_id:
                continue
            if _is_removal_from_40man(hist, team_id, pid):
                removal = hist_date
                break

        name = (
            previous_members.get(pid)
            or infra._clean_text(infra._get_in(tx, "person", "fullName"))
            or str(pid)
        )
        windows.append({
            "person_id": pid,
            "name": name,
            "start": trade_date,
            "end_exclusive": removal,
            "from_team_id": from_id,
        })

    result = (windows, True)
    cache[key] = result
    return result


def adjusted_40man_members(team_id: int, as_of_date=None, cache=None,
                           trade_windows=None, lookback_days: int = 60):
    cache = cache if cache is not None else {}
    d = _as_date(as_of_date) or infra._la_now().date()
    members, ok = fetch_40man_members(team_id, d, cache=cache)
    if not ok:
        return {}, [], False

    if trade_windows is None:
        windows, windows_ok = build_trade_exception_windows(
            team_id, d - timedelta(days=max(1, int(lookback_days))), d, cache=cache
        )
        if not windows_ok:
            windows = []
    else:
        windows = list(trade_windows)

    adjusted = dict(members)
    additions = []
    for window in windows:
        start = _as_date(window.get("start"))
        end_exclusive = _as_date(window.get("end_exclusive"))
        pid = infra._safe_int(window.get("person_id"))
        if not start or not pid or d < start:
            continue
        if end_exclusive and d >= end_exclusive:
            continue
        if pid not in adjusted:
            adjusted[pid] = window.get("name") or str(pid)
            additions.append({
                "person_id": pid,
                "name": adjusted[pid],
                "reason": "recent incoming trade 40-man reconciliation",
            })

    return adjusted, additions, True


def adjusted_40man_count(team_id: int, as_of_date=None, cache=None,
                         trade_windows=None, lookback_days: int = 60):
    members, additions, ok = adjusted_40man_members(
        team_id,
        as_of_date=as_of_date,
        cache=cache,
        trade_windows=trade_windows,
        lookback_days=lookback_days,
    )
    # >40 historical snapshots are a StatsAPI sequencing artifact.  They are
    # useful for diagnostics but can never be a valid published roster count.
    return (min(40, len(members)) if ok else None), additions, ok


def daily_40man_counts(team_id: int, start_date, end_date, cache=None):
    """Return adjusted end-of-day occupancy for every calendar day in range."""
    cache = cache if cache is not None else {}
    start = _as_date(start_date)
    end = _as_date(end_date)
    if not start or not end or end < start:
        return {}, {}, False

    windows, windows_ok = build_trade_exception_windows(
        team_id, start, end, cache=cache
    )
    if not windows_ok:
        windows = []

    counts = {}
    reconciliations = {}
    d = start
    while d <= end:
        count, additions, ok = adjusted_40man_count(
            team_id,
            as_of_date=d,
            cache=cache,
            trade_windows=windows,
        )
        if not ok or count is None:
            return {}, {}, False
        counts[d] = count
        if additions:
            reconciliations[d] = additions
        d += timedelta(days=1)
    return counts, reconciliations, True


def regular_season_start(team_id: int, season: int, cache=None):
    """Return the club's first regular-season game date for the season."""
    cache = cache if cache is not None else {}
    key = ("roster_season_start", int(team_id), int(season))
    if key in cache:
        return cache[key]
    url = "https://statsapi.mlb.com/api/v1/schedule?" + urlencode({
        "sportId": 1,
        "teamId": int(team_id),
        "season": int(season),
        "gameType": "R",
    })
    try:
        data = infra.request_json_with_retry(url)
        dates = []
        for block in data.get("dates") or []:
            d = _as_date(block.get("date"))
            if d:
                dates.append(d)
        result = min(dates) if dates else None
    except Exception:
        result = None
    cache[key] = result
    return result
