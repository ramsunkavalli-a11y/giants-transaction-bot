"""Optional MLB usage and Statcast context for transaction posts.

The core transaction post must never depend on this module succeeding. StatsAPI
remains the source of record for transaction/stat lines; these helpers add
playing-time context and date-bounded MLB expected results when available.
"""

import csv
import io
from urllib.parse import urlencode

import requests

import bot_core as infra

SAVANT_CSV_URL = "https://baseballsavant.mlb.com/statcast_search/csv"
OPTIONAL_TIMEOUT = 8


def _player_stats_url(person_id: int, group: str, stat_type: str, season: int,
                      start_date, end_date) -> str:
    return (
        f"https://statsapi.mlb.com/api/v1/people/{int(person_id)}/stats?"
        + urlencode({
            "group": group,
            "sportId": 1,
            "season": int(season),
            "startDate": str(start_date),
            "endDate": str(end_date),
            "stats": stat_type,
        })
    )


def _game_log(person_id: int, group: str, season: int, start_date, end_date,
              cache=None):
    cache = cache if cache is not None else {}
    key = (
        "mlb_game_log", int(person_id), group, int(season),
        str(start_date), str(end_date),
    )
    if key in cache:
        return cache[key]
    try:
        data = infra.request_json_with_retry(
            _player_stats_url(
                person_id, group, "gameLog", season, start_date, end_date
            )
        )
        splits = []
        for block in data.get("stats") or []:
            splits.extend(
                split for split in (block.get("splits") or [])
                if isinstance(split, dict)
            )
        result = (splits, True)
    except Exception:
        result = ([], False)
    cache[key] = result
    return result


def fetch_mlb_usage(person_id: int, pitcher: bool, season: int, start_date,
                    end_date, cache=None):
    """Return MLB games and starts in an exact stint.

    Hitter appearances come from the hitting game log while starts come from
    the fielding game log (which includes DH starts). Pitcher game logs already
    expose both appearances and starts.
    """
    cache = cache if cache is not None else {}
    key = (
        "mlb_usage", int(person_id), bool(pitcher), int(season),
        str(start_date), str(end_date),
    )
    if key in cache:
        return cache[key]

    primary_group = "pitching" if pitcher else "hitting"
    primary, primary_ok = _game_log(
        person_id, primary_group, season, start_date, end_date, cache=cache
    )
    game_ids = {
        infra._safe_int(infra._get_in(split, "game", "gamePk"))
        for split in primary
        if infra._safe_int(infra._get_in(split, "game", "gamePk"))
    }

    if pitcher:
        starts = sum(
            infra._safe_int((split.get("stat") or {}).get("gamesStarted")) or 0
            for split in primary
        )
        result = ({"games": len(game_ids), "starts": starts}, primary_ok)
        cache[key] = result
        return result

    fielding, fielding_ok = _game_log(
        person_id, "fielding", season, start_date, end_date, cache=cache
    )
    starts = sum(
        infra._safe_int((split.get("stat") or {}).get("gamesStarted")) or 0
        for split in fielding
    )
    result = (
        {"games": len(game_ids), "starts": starts},
        primary_ok or fielding_ok,
    )
    cache[key] = result
    return result


def _savant_params(person_id: int, pitcher: bool, season: int, start_date,
                   end_date) -> dict:
    role = "pitcher" if pitcher else "batter"
    lookup = "pitchers_lookup[]" if pitcher else "batters_lookup[]"
    return {
        "all": "true",
        "type": role,
        "player_type": role,
        "hfSea": f"{int(season)}|",
        "game_date_gt": str(start_date),
        "game_date_lt": str(end_date),
        lookup: str(int(person_id)),
    }


def fetch_date_bounded_xwoba(person_id: int, pitcher: bool, season: int,
                             start_date, end_date, cache=None):
    """Reconstruct MLB xwOBA over an exact date range from Savant pitch CSV.

    Returns ``(xwoba, actual_woba, denominator, request_succeeded)``. Savant's
    expected-wOBA value is recorded on the terminal pitch of each wOBA-eligible
    plate appearance. Actual wOBA is returned as a validation/debugging aid.
    Any network/shape failure is swallowed so enrichment can never block a post.
    """
    cache = cache if cache is not None else {}
    key = (
        "savant_xwoba", int(person_id), bool(pitcher), int(season),
        str(start_date), str(end_date),
    )
    if key in cache:
        return cache[key]

    try:
        response = requests.get(
            SAVANT_CSV_URL,
            params=_savant_params(
                person_id, pitcher, season, start_date, end_date
            ),
            timeout=OPTIONAL_TIMEOUT,
        )
        response.raise_for_status()
        rows = list(csv.DictReader(io.StringIO(response.text.lstrip("\ufeff"))))
        id_field = "pitcher" if pitcher else "batter"
        rows = [
            row for row in rows
            if str(row.get(id_field) or "") == str(int(person_id))
        ]
        pa_rows = [
            row for row in rows
            if row.get("woba_denom") not in (None, "", "0")
        ]
        denominator = sum(
            float(row.get("woba_denom") or 0) for row in pa_rows
        )
        if denominator <= 0:
            result = (None, None, 0, True)
        else:
            actual = sum(
                float(row.get("woba_value") or 0) for row in pa_rows
            ) / denominator
            expected = sum(
                float(row.get("estimated_woba_using_speedangle") or 0)
                for row in pa_rows
            ) / denominator
            result = (expected, actual, int(denominator), True)
    except Exception:
        result = (None, None, 0, False)

    cache[key] = result
    return result
