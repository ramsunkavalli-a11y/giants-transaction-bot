"""Selective projection/Statcast identity for incoming acquisitions.

These helpers intentionally return at most one compact nugget.  Transaction
posts already contain actual performance; this layer is for something that
helps explain what kind of player the Giants just acquired.
"""

from urllib.parse import urlencode

import bot_core as infra


def _stats_url(person_id: int, pitcher: bool, stat_type: str, season: int, **params):
    query = {
        "stats": stat_type,
        "group": "pitching" if pitcher else "hitting",
        "season": int(season),
    }
    query.update({k: v for k, v in params.items() if v is not None})
    return (
        f"https://statsapi.mlb.com/api/v1/people/{int(person_id)}/stats?"
        + urlencode(query)
    )


def _fetch_stats(person_id: int, pitcher: bool, stat_type: str, season: int,
                 cache=None, **params):
    cache = cache if cache is not None else {}
    key = (
        "acq_stat", int(person_id), bool(pitcher), str(stat_type), int(season),
        tuple(sorted((str(k), str(v)) for k, v in params.items())),
    )
    if key in cache:
        return cache[key]
    try:
        data = infra.request_json_with_retry(
            _stats_url(person_id, pitcher, stat_type, season, **params)
        )
        result = (data.get("stats") or [], True)
    except Exception:
        result = ([], False)
    cache[key] = result
    return result


def _first_stat(stats_blocks):
    for block in stats_blocks or []:
        for split in block.get("splits") or []:
            stat = split.get("stat") or {}
            if stat:
                return stat
    return None


def fetch_zips_ros(person_id: int, pitcher: bool, season: int, cache=None):
    blocks, ok = _fetch_stats(
        person_id, pitcher, "projected_ZipsRos", season, cache=cache
    )
    return _first_stat(blocks), ok


def fetch_oaa(person_id: int, season: int, cache=None):
    blocks, ok = _fetch_stats(
        person_id, False, "outsAboveAverage", season, cache=cache
    )
    return _first_stat(blocks), ok


def fetch_pitch_arsenal(person_id: int, season: int, cache=None):
    blocks, ok = _fetch_stats(
        person_id, True, "pitchArsenal", season, cache=cache
    )
    arsenal = []
    for block in blocks or []:
        for split in block.get("splits") or []:
            stat = split.get("stat") or {}
            pitch_type = stat.get("type") or {}
            code = infra._clean_text(pitch_type.get("code"))
            if not code:
                continue
            arsenal.append({
                "code": code,
                "description": infra._clean_text(pitch_type.get("description")),
                "percentage": infra._safe_float(stat.get("percentage")),
                "count": infra._safe_int(stat.get("count")),
                "totalPitches": infra._safe_int(stat.get("totalPitches")),
                "averageSpeed": infra._safe_float(stat.get("averageSpeed")),
            })
    return arsenal, ok


def _hitter_part(person_id: int, season: int, cache) -> str | None:
    oaa, _oaa_ok = fetch_oaa(person_id, season, cache=cache)
    attempts = infra._safe_int((oaa or {}).get("attempts")) or 0
    value = infra._safe_int((oaa or {}).get("totalOutsAboveAverage"))
    if value is not None and attempts >= 20 and abs(value) >= 3:
        sign = "+" if value > 0 else ""
        return f"2026 defense: {sign}{value} OAA"

    zips, _zips_ok = fetch_zips_ros(person_id, False, season, cache=cache)
    wrc_plus = infra._safe_float((zips or {}).get("wRcPlus"))
    if wrc_plus is not None:
        return f"ZiPS ROS: {int(round(wrc_plus))} wRC+"
    return None


def _pitcher_part(person_id: int, season: int, cache) -> str | None:
    arsenal, _arsenal_ok = fetch_pitch_arsenal(person_id, season, cache=cache)
    if arsenal:
        total = max(
            (infra._safe_int(item.get("totalPitches")) or 0 for item in arsenal),
            default=0,
        )
        usable = [
            item for item in arsenal
            if (item.get("percentage") is not None and item.get("averageSpeed") is not None)
        ]
        if total >= 100 and usable:
            usable.sort(key=lambda item: item.get("percentage") or 0.0, reverse=True)
            bits = []
            for item in usable[:2]:
                pct = int(round(100 * (item.get("percentage") or 0.0)))
                velo = int(round(item.get("averageSpeed") or 0.0))
                bits.append(f"{item['code']} {velo} ({pct}%)")
            if bits:
                return "Arsenal: " + " | ".join(bits)

    zips, _zips_ok = fetch_zips_ros(person_id, True, season, cache=cache)
    era = infra._safe_float((zips or {}).get("era"))
    fip = infra._safe_float((zips or {}).get("fip"))
    if era is not None and fip is not None:
        return f"ZiPS ROS: {era:.2f} ERA / {fip:.2f} FIP"
    if era is not None:
        return f"ZiPS ROS: {era:.2f} ERA"
    return None


def best_acquisition_part(person_id: int, pitcher: bool, season: int, cache=None):
    cache = cache if cache is not None else {}
    if not person_id:
        return None
    return (
        _pitcher_part(person_id, season, cache)
        if pitcher else _hitter_part(person_id, season, cache)
    )
