"""Presentation/enrichment overrides for the Giants transaction bot.

The proven transaction polling, dedupe, state, and Bluesky plumbing lives in
bot_core.py unchanged. This module replaces only transaction classification and
enrichment/formatting behavior, then delegates execution to bot_core.main().
"""

from datetime import datetime
from urllib.parse import urlencode

import bot_core as core

SIGNING_MLB_MIN_PA = 30
SIGNING_MLB_MIN_IP = 10.0
SPORT_LEVELS = [(11, "AAA"), (12, "AA"), (13, "A+"), (14, "A"), (16, "Rk")]
SPORT_LEVEL_BY_ID = {1: "MLB", **dict(SPORT_LEVELS)}


def is_contract_selected_transaction(tx: dict) -> bool:
    """StatsAPI currently reports contract selections as typeCode SE / Selected."""
    tc = (tx.get("typeCode") or "").strip().upper()
    hay = f"{tx.get('typeDesc','')} {tx.get('description','')}".lower()
    return "selected the contract" in hay or "contract selected" in hay or (
        tc == "SE" and "selected" in hay and "contract" in hay
    )


def is_signing_transaction(tx: dict) -> bool:
    tc = (tx.get("typeCode") or "").strip().upper()
    td = (tx.get("typeDesc") or "").strip().lower()
    desc = (tx.get("description") or "").strip().lower()
    hay = f"{td} {desc}"

    if is_contract_selected_transaction(tx):
        return False

    negative = (
        "assigned", "designated", "released", "traded", "waiver",
        "optioned", "outright", "activated", "recalled",
    )
    if any(w in hay for w in negative):
        return False

    # SC is a generic StatsAPI "Status Change" code (IL moves, roster status,
    # etc.), not a signing code. SE is "Selected" and is handled above.
    if tc in {"SFA", "SMC", "S", "FA"}:
        return True

    positive = (
        "signed", "minor league contract", "major league contract",
        "free agent signing",
    )
    return any(w in hay for w in positive)


def _format_pct(v):
    n = core._safe_float(v)
    if n is None:
        return None
    if abs(n) <= 1:
        n *= 100
    return f"{int(round(n))}%"


def _format_slash_value(v):
    n = core._safe_float(v)
    if n is None:
        return None
    txt = f"{n:.3f}"
    if txt.startswith("0."):
        return txt[1:]
    if txt.startswith("-0."):
        return "-" + txt[2:]
    return txt


def _format_ip(v):
    n = core._safe_float(v)
    return f"{n:.1f}" if n is not None else None


def format_stat_clause(stat: dict, pitcher: bool):
    if not stat:
        return None

    parts = []
    if pitcher:
        ip = _format_ip(stat.get("inningsPitched"))
        hits = core._safe_int(stat.get("hits"))
        era = core._safe_float(stat.get("era"))
        so = core._safe_int(stat.get("strikeOuts"))
        bb = core._safe_int(stat.get("baseOnBalls"))
        if ip is not None:
            parts.append(f"{ip} IP")
        if hits is not None:
            parts.append(f"{hits} H")
        if era is not None:
            parts.append(f"{era:.2f} ERA")
        if so is not None:
            parts.append(f"{so} K")
        if bb is not None:
            parts.append(f"{bb} BB")
        return ", ".join(parts) if parts else None

    pa = core._safe_int(stat.get("plateAppearances"))
    avg = _format_slash_value(stat.get("avg"))
    obp = _format_slash_value(stat.get("obp"))
    slg = _format_slash_value(stat.get("slg"))
    hr = core._safe_int(stat.get("homeRuns"))
    so = core._safe_int(stat.get("strikeOuts"))
    bb = core._safe_int(stat.get("baseOnBalls"))

    k_pct = _format_pct(stat.get("strikeoutPercentage"))
    bb_pct = _format_pct(stat.get("baseOnBallsPercentage"))
    # The player-scoped StatsAPI endpoint does not consistently populate the
    # percentage fields, but it does return PA/K/BB. Compute standard K%/BB%.
    if k_pct is None and pa and so is not None:
        k_pct = f"{int(round(100 * so / pa))}%"
    if bb_pct is None and pa and bb is not None:
        bb_pct = f"{int(round(100 * bb / pa))}%"

    if pa is not None:
        parts.append(f"{pa} PA")
    if avg and obp and slg:
        parts.append(f"{avg}/{obp}/{slg}")
    if hr is not None:
        parts.append(f"{hr} HR")
    if k_pct:
        parts.append(f"{k_pct} K")
    if bb_pct:
        parts.append(f"{bb_pct} BB")
    return ", ".join(parts) if parts else None


def shorten_stat_clause(stat_clause: str, pitcher: bool):
    if not stat_clause:
        return None
    parts = [p.strip() for p in stat_clause.split(",") if p.strip()]
    if pitcher:
        # Keep IP / ERA / K first when character pressure requires trimming.
        parts = [p for p in parts if not p.endswith(" H") and not p.endswith(" BB")]
    else:
        parts = [p for p in parts if not (p.endswith("% K") or p.endswith("% BB"))]
    return ", ".join(parts).strip() or None


def _stats_group(pitcher: bool):
    return "pitching" if pitcher else "hitting"


def _appearance_volume(stat: dict, pitcher: bool):
    if pitcher:
        return core._safe_float((stat or {}).get("inningsPitched")) or 0.0
    return float(
        core._safe_int((stat or {}).get("plateAppearances"))
        or core._safe_int((stat or {}).get("atBats"))
        or core._safe_int((stat or {}).get("gamesPlayed"))
        or 0
    )


def _direct_stats_url(person_id: int, pitcher: bool, sport_id: int, stat_type: str,
                      season: int, start_date=None, end_date=None):
    params = {
        "stats": stat_type,
        "group": _stats_group(pitcher),
        "sportId": sport_id,
        "season": season,
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
    """Fetch one player's stats from an exact MLB/MiLB sport level.

    Returns (selected_split_payload, request_succeeded). For season stats, an
    aggregate team=None split is preferred when a player appeared for multiple
    clubs at the same level.
    """
    cache = cache if cache is not None else {}
    key = (
        "_direct_stats", person_id, pitcher, sport_id, stat_type, int(season),
        str(start_date or ""), str(end_date or ""),
    )
    if key in cache:
        return cache[key]

    try:
        data = core.request_json_with_retry(
            _direct_stats_url(
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
            if core._safe_int(core._get_in(split, "sport", "id")) != sport_id:
                continue
            stat = split.get("stat") or {}
            if _appearance_volume(stat, pitcher) <= 0:
                continue
            team = split.get("team")
            aggregate_rank = 1 if not team else 0
            vol = _appearance_volume(stat, pitcher)
            candidates.append((aggregate_rank, vol, split))

    if not candidates:
        result = (None, True)
        cache[key] = result
        return result

    candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
    split = candidates[0][2]
    selected = {
        "seasonYear": core._safe_int(split.get("season")) or int(season),
        "orgName": core._clean_text(core._get_in(split, "team", "name")),
        "levelToken": SPORT_LEVEL_BY_ID.get(sport_id),
        "splitStats": split.get("stat") or {},
        "split": split,
    }
    result = (selected, True)
    cache[key] = result
    return result


def fetch_highest_milb_stat(person_id: int, pitcher: bool, stat_type: str,
                            season: int, cache=None, start_date=None, end_date=None):
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


def _transactions_url(person_id: int, start_date, end_date):
    params = {
        "playerId": person_id,
        "startDate": str(start_date),
        "endDate": str(end_date),
    }
    return "https://statsapi.mlb.com/api/v1/transactions?" + urlencode(params)


def fetch_player_transactions(person_id: int, season_year: int, end_date, cache=None):
    cache = cache if cache is not None else {}
    start_date = f"{season_year}-01-01"
    key = ("_player_transactions", person_id, start_date, str(end_date))
    if key in cache:
        return cache[key]
    try:
        data = core.request_json_with_retry(_transactions_url(person_id, start_date, end_date))
        result = ([t for t in (data.get("transactions") or []) if isinstance(t, dict)], True)
    except Exception:
        result = ([], False)
    cache[key] = result
    return result


def latest_prior_transaction_date(person_id: int, current_tx: dict, predicate, cache=None):
    current_date = core.txn_date_obj(current_tx)
    if not current_date:
        return None
    txs, ok = fetch_player_transactions(person_id, current_date.year, current_date, cache=cache)
    if not ok:
        return None

    current_id = core.txn_id(current_tx)
    candidates = []
    for hist in txs:
        if current_id and core.txn_id(hist) == current_id:
            continue
        hist_date = core.txn_date_obj(hist)
        if not hist_date or hist_date > current_date:
            continue
        if predicate(hist):
            candidates.append((hist_date, core.txn_id(hist)))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][0]


def _meaningful_mlb_sample(stat: dict, pitcher: bool):
    if pitcher:
        return (core._safe_float((stat or {}).get("inningsPitched")) or 0.0) >= SIGNING_MLB_MIN_IP
    return (core._safe_int((stat or {}).get("plateAppearances")) or 0) >= SIGNING_MLB_MIN_PA


def _sample_only_clause(stat: dict, pitcher: bool):
    if pitcher:
        ip = _format_ip((stat or {}).get("inningsPitched"))
        return f"MLB: {ip} IP" if ip is not None else None
    pa = core._safe_int((stat or {}).get("plateAppearances"))
    return f"MLB: {pa} PA" if pa is not None else None


def _age_years(birth_date_str: str):
    if not birth_date_str:
        return None
    try:
        b = datetime.strptime(str(birth_date_str)[:10], "%Y-%m-%d").date()
    except Exception:
        return None
    t = core.today_pacific_date()
    age = t.year - b.year - ((t.month, t.day) < (b.month, b.day))
    return age if age >= 0 else None


def _signing_season_context(person_id: int, pitcher: bool, season: int):
    mlb, mlb_ok = fetch_player_stat(person_id, pitcher, 1, "season", season)
    milb, milb_ok = fetch_highest_milb_stat(person_id, pitcher, "season", season)
    return mlb, milb, (mlb_ok or milb_ok)


def build_signing_enrichment(person: dict, stats_blocks=None):
    """Choose signing context by relevance, not by whatever bio fields exist."""
    pitcher = core.is_pitcher(person)
    person_id = core._safe_int(person.get("id"))
    current_year = core.today_pacific_date().year

    primary = None
    secondary = None
    if person_id:
        current_mlb, current_milb, _ok = _signing_season_context(person_id, pitcher, current_year)

        if current_mlb and _meaningful_mlb_sample(current_mlb.get("splitStats") or {}, pitcher):
            primary = current_mlb
        elif current_milb:
            primary = current_milb
            if current_mlb:
                secondary = _sample_only_clause(current_mlb.get("splitStats") or {}, pitcher)
        elif current_mlb:
            primary = current_mlb
        else:
            # Useful for offseason signings before the new season has data.
            prev_mlb, prev_milb, _ok = _signing_season_context(person_id, pitcher, current_year - 1)
            if prev_mlb and _meaningful_mlb_sample(prev_mlb.get("splitStats") or {}, pitcher):
                primary = prev_mlb
            elif prev_milb:
                primary = prev_milb
                if prev_mlb:
                    secondary = _sample_only_clause(prev_mlb.get("splitStats") or {}, pitcher)
            elif prev_mlb:
                primary = prev_mlb

    primary_stats = format_stat_clause((primary or {}).get("splitStats") or {}, pitcher) if primary else None
    season = (primary or {}).get("seasonYear")
    level = core._clean_text((primary or {}).get("levelToken"))
    label = " ".join(str(x) for x in (season, level) if x) or None

    # Draft/school are fallback-only for players with no useful pro stat line.
    fallback = []
    if not primary_stats:
        draft = core.draft_clause(person)
        school = core.school_clause(person)
        if draft:
            fallback.append(draft)
        if school:
            fallback.append(school)

    return {
        "pitcher": pitcher,
        "age": _age_years(person.get("birthDate")),
        "primary_stats": primary_stats,
        "primary_label_full": label,
        "primary_label_short": label,
        "secondary": secondary,
        "fallback": fallback,
    }


def build_signing_post(base_text: str, player_link: str, enrichment: dict, max_len=core.MAX_POST_LEN):
    pitcher = bool(enrichment.get("pitcher"))
    age = enrichment.get("age")
    primary_stats = enrichment.get("primary_stats")
    label = enrichment.get("primary_label_short") or enrichment.get("primary_label_full")
    secondary = enrichment.get("secondary")
    fallback = list(enrichment.get("fallback") or [])

    def render(stats, *, include_secondary=True, include_age=True, fallback_lines=None):
        lines = [base_text]
        if stats and label:
            lines.append(f"{label}: {stats}")
        context_bits = []
        if include_secondary and secondary:
            context_bits.append(secondary)
        if include_age and age is not None:
            context_bits.append(f"Age {age}")
        if context_bits:
            lines.append(" | ".join(context_bits))
        lines.extend(fallback_lines or [])
        lines.append(player_link)
        return "\n".join(lines)

    attempts = [
        render(primary_stats, fallback_lines=fallback),
        render(primary_stats, include_secondary=False, fallback_lines=fallback),
    ]
    short_stats = shorten_stat_clause(primary_stats, pitcher) if primary_stats else None
    if short_stats and short_stats != primary_stats:
        attempts.append(render(short_stats, include_secondary=False, fallback_lines=fallback))
    attempts.extend([
        render(short_stats or primary_stats, include_secondary=False, fallback_lines=[]),
        render(short_stats or primary_stats, include_secondary=False, include_age=False, fallback_lines=[]),
    ])

    for post in attempts:
        if len(post) <= max_len:
            return post

    min_post = f"{base_text}\n{player_link}"
    return min_post if len(min_post) <= max_len else min_post[: max_len - 1] + "…"


def build_post_with_optional_stats(base_text: str, player_link: str, stats_line: str,
                                   pitcher: bool, max_len=core.MAX_POST_LEN):
    if stats_line:
        post = f"{base_text}\n{stats_line}\n{player_link}"
        if len(post) <= max_len:
            return post
        short_stats = shorten_stat_clause(stats_line, pitcher)
        if short_stats:
            post = f"{base_text}\n{short_stats}\n{player_link}"
            if len(post) <= max_len:
                return post

    min_post = f"{base_text}\n{player_link}"
    if len(min_post) <= max_len:
        return min_post
    allowed_link = max(0, max_len - len(base_text) - 1)
    return f"{base_text}\n{player_link[:allowed_link]}"


def _short_date_label(d):
    return f"{d.strftime('%b')} {d.day}" if d else None


def _labeled_stats(selected: dict, prefix: str, pitcher: bool):
    if not selected:
        return None
    stat_text = format_stat_clause(selected.get("splitStats") or {}, pitcher)
    return f"{prefix}: {stat_text}" if stat_text else None


def build_special_transaction_post(tx: dict, category: str, player_cache: dict, now_la):
    base_text = core.build_base_tx_text(tx)
    pid = core.extract_tx_player_id(tx)
    if not pid:
        return base_text

    link = core.player_url(pid)
    details = core.get_player_details(pid, player_cache)
    pitcher = core.is_pitcher(details)
    season_year = now_la.year
    tx_date = core.txn_date_obj(tx) or now_la.date()
    stats_line = None

    if category == "optioned":
        start = latest_prior_transaction_date(
            pid,
            tx,
            lambda hist: core.is_recalled_transaction(hist) or is_contract_selected_transaction(hist),
            cache=player_cache,
        )
        if start:
            sel, ok = fetch_player_stat(
                pid, pitcher, 1, "byDateRange", season_year,
                cache=player_cache, start_date=start, end_date=tx_date,
            )
            stats_line = _labeled_stats(sel, f"MLB since {_short_date_label(start)}", pitcher)
            if not stats_line and ok:
                stats_line = f"MLB since {_short_date_label(start)}: did not appear"
        if not stats_line:
            sel, _ok = fetch_player_stat(
                pid, pitcher, 1, "season", season_year, cache=player_cache,
            )
            stats_line = _labeled_stats(sel, f"{season_year} MLB", pitcher)

    elif category == "recalled":
        start = latest_prior_transaction_date(
            pid, tx, core.is_optioned_transaction, cache=player_cache,
        )
        if start:
            sel, _ok = fetch_highest_milb_stat(
                pid, pitcher, "byDateRange", season_year,
                cache=player_cache, start_date=start, end_date=tx_date,
            )
            if sel:
                level = core._clean_text(sel.get("levelToken")) or "MiLB"
                stats_line = _labeled_stats(sel, f"{level} since {_short_date_label(start)}", pitcher)
        if not stats_line:
            sel, _ok = fetch_highest_milb_stat(
                pid, pitcher, "season", season_year, cache=player_cache,
            )
            if sel:
                level = core._clean_text(sel.get("levelToken")) or "MiLB"
                stats_line = _labeled_stats(sel, f"{season_year} {level}", pitcher)

    elif category == "dfa":
        sel, _ok = fetch_player_stat(
            pid, pitcher, 1, "season", season_year, cache=player_cache,
        )
        stats_line = _labeled_stats(sel, f"{season_year} MLB", pitcher)

    elif category == "contract_selected":
        # Date-range season-to-date avoids future leakage when replaying old
        # transactions and still represents the player's performance at call-up.
        sel, _ok = fetch_highest_milb_stat(
            pid, pitcher, "byDateRange", season_year,
            cache=player_cache,
            start_date=f"{season_year}-01-01",
            end_date=tx_date,
        )
        if sel:
            level = core._clean_text(sel.get("levelToken")) or "MiLB"
            stats_line = _labeled_stats(sel, f"{season_year} {level}", pitcher)

    return build_post_with_optional_stats(
        base_text, link, stats_line, pitcher, max_len=core.MAX_POST_LEN,
    )


def apply_overrides():
    core.is_signing_transaction = is_signing_transaction
    core.is_contract_selected_transaction = is_contract_selected_transaction
    core.format_stat_clause = format_stat_clause
    core.shorten_stat_clause = shorten_stat_clause
    core.build_signing_enrichment = build_signing_enrichment
    core.build_signing_post = build_signing_post
    core.build_post_with_optional_stats = build_post_with_optional_stats
    core.build_special_transaction_post = build_special_transaction_post


apply_overrides()

if __name__ == "__main__":
    core.main()
