"""Pitcher-only rate-stat access for MLB transaction enrichment.

StatsAPI sabermetric payloads do not contain innings/PA, so they should not
flow through ``mlb_domain.fetch_player_stat``'s counting-stat selector. This
module keeps that distinction explicit and selects the requested MLB team when
multiple rate-stat splits are present.
"""

from urllib.parse import urlencode

import bot_core as infra


def _sabermetrics_url(person_id: int, season: int, start_date, end_date) -> str:
    params = {
        "stats": "sabermetrics",
        "group": "pitching",
        "sportId": 1,
        "season": int(season),
        "startDate": str(start_date),
        "endDate": str(end_date),
    }
    return (
        f"https://statsapi.mlb.com/api/v1/people/{int(person_id)}/stats?"
        + urlencode(params)
    )


def fetch_pitcher_sabermetrics(person_id: int, season: int, start_date,
                                end_date, cache=None, team_id=None):
    """Return date-bounded MLB pitcher sabermetrics as ``(stat, ok)``.

    The option-post caller passes the Giants team ID so a traded player's
    previous-team split can never leak into the current stint. If no exact team
    split exists, an API-provided aggregate is acceptable; otherwise ambiguous
    multi-team responses are intentionally ignored.
    """
    cache = cache if cache is not None else {}
    key = (
        "pitcher_sabermetrics", int(person_id), int(season),
        str(start_date), str(end_date),
        infra._safe_int(team_id) if team_id is not None else None,
    )
    if key in cache:
        return cache[key]

    try:
        data = infra.request_json_with_retry(
            _sabermetrics_url(person_id, season, start_date, end_date)
        )
    except Exception:
        result = (None, False)
        cache[key] = result
        return result

    splits = []
    for block in data.get("stats") or []:
        for split in block.get("splits") or []:
            if not isinstance(split, dict):
                continue
            if infra._safe_int(infra._get_in(split, "sport", "id")) != 1:
                continue
            stat = split.get("stat") or {}
            if not stat:
                continue
            splits.append(split)

    if not splits:
        result = (None, True)
        cache[key] = result
        return result

    selected = None
    if team_id is not None:
        target = int(team_id)
        matches = [
            split for split in splits
            if infra._safe_int(infra._get_in(split, "team", "id")) == target
        ]
        if matches:
            selected = matches[0]

    if selected is None:
        aggregates = [split for split in splits if not split.get("team")]
        if aggregates:
            selected = aggregates[0]
        elif len(splits) == 1:
            selected = splits[0]
        else:
            # FIP/xFIP cannot be safely combined from already-computed rates.
            result = (None, True)
            cache[key] = result
            return result

    result = (selected.get("stat") or None, True)
    cache[key] = result
    return result
