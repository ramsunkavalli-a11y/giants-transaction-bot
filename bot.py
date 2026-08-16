"""Presentation/enrichment overrides for the Giants transaction bot.

The proven transaction polling, dedupe, state, and Bluesky plumbing lives in
bot_core.py unchanged. This module only replaces transaction classification and
post-enrichment/formatting behavior, then delegates execution to bot_core.main().
"""

import bot_core as core

SIGNING_MLB_MIN_PA = 30
SIGNING_MLB_MIN_IP = 10.0


def is_signing_transaction(tx: dict) -> bool:
    tc = (tx.get("typeCode") or "").strip().upper()
    td = (tx.get("typeDesc") or "").strip().lower()
    desc = (tx.get("description") or "").strip().lower()
    hay = f"{td} {desc}"

    negative = ("assigned", "designated", "released", "traded", "waiver", "optioned", "outright")
    if any(w in hay for w in negative):
        return False

    # Contract selections are roster moves, not new signings. The old bot
    # classified SC as a signing, which is why these sometimes received bio text.
    if tc in {"SC", "CS"} or "contract selected" in hay:
        return False

    if tc in {"SFA", "SMC", "S", "FA"}:
        return True

    positive = ("signed", "minor league contract", "major league contract", "free agent signing")
    return any(w in hay for w in positive)


def _selected_split_payload(split: dict):
    return {
        "seasonYear": core._safe_int(split.get("season")),
        "orgName": core._clean_text(
            core._get_in(split, "team", "name") or core._get_in(split, "organization", "name")
        ),
        "levelToken": core.level_token_from_split(split),
        "splitStats": split.get("stat") or {},
        "split": split,
    }


def select_filtered_level(stats_blocks, pitcher: bool, *, target_level=None, exclude_mlb=False, season=None):
    target_group = "pitching" if pitcher else "hitting"
    candidates = []
    for split in core._extract_group_splits(stats_blocks, target_group):
        if not core.has_appearances(split, pitcher):
            continue
        split_season = core._safe_int(split.get("season"))
        if season is not None and split_season != int(season):
            continue
        token = core.level_token_from_split(split)
        if not token:
            continue
        if target_level is not None and token != target_level:
            continue
        if exclude_mlb and token == "MLB":
            continue
        token_rank = core.LEVEL_RANK.get(token, 0)
        vol = core._appearance_volume(split, pitcher)
        team_name = core._clean_text(
            core._get_in(split, "team", "name") or core._get_in(split, "organization", "name")
        ) or ""
        team_id = core._safe_int(
            core._get_in(split, "team", "id") or core._get_in(split, "organization", "id")
        ) or 0
        candidates.append((split_season or 0, token_rank, vol, team_id, team_name, split))
    if not candidates:
        return None
    candidates.sort(key=lambda x: (x[0], x[1], x[2], x[3], x[4]), reverse=True)
    return _selected_split_payload(candidates[0][5])


def select_mlb_appeared(stats_blocks, pitcher: bool, season=None):
    return select_filtered_level(stats_blocks, pitcher, target_level="MLB", season=season)


def select_highest_milb_appeared(stats_blocks, pitcher: bool, season=None):
    return select_filtered_level(stats_blocks, pitcher, exclude_mlb=True, season=season)


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
    k_pct = _format_pct(stat.get("strikeoutPercentage"))
    bb_pct = _format_pct(stat.get("baseOnBallsPercentage"))
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
        parts = [p for p in parts if not p.endswith(" H") and not p.endswith(" BB")]
    else:
        parts = [p for p in parts if not (p.endswith("% K") or p.endswith("% BB"))]
    return ", ".join(parts).strip() or None


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


def _selected_stats_labels(selected: dict):
    if not selected:
        return None, None
    season = selected.get("seasonYear")
    level = core._clean_text(selected.get("levelToken"))
    # Keep signing labels simple. A player may have played for multiple clubs at
    # the same level in a season, so naming one team can be misleading.
    base = " ".join(str(x) for x in (season, level) if x) or "Stats"
    return base, base


def _age_years(birth_date_str: str):
    if not birth_date_str:
        return None
    try:
        b = core.datetime.strptime(str(birth_date_str)[:10], "%Y-%m-%d").date()
    except Exception:
        return None
    t = core.today_pacific_date()
    age = t.year - b.year - ((t.month, t.day) < (b.month, b.day))
    return age if age >= 0 else None


def build_signing_enrichment(person: dict, stats_blocks=None):
    pitcher = core.is_pitcher(person)
    blocks = stats_blocks if stats_blocks is not None else (person.get("stats") or [])
    current_year = core.today_pacific_date().year

    # Relevance order for a signing:
    # 1) meaningful MLB work this season
    # 2) this season's highest MiLB level (with tiny MLB exposure disclosed)
    # 3) if there is no current-season MiLB work, fall back to the latest
    #    meaningful MLB sample, then the latest MiLB/MLB sample.
    current_mlb = select_mlb_appeared(blocks, pitcher=pitcher, season=current_year)
    current_milb = select_highest_milb_appeared(blocks, pitcher=pitcher, season=current_year)
    latest_mlb = select_mlb_appeared(blocks, pitcher=pitcher)
    latest_milb = select_highest_milb_appeared(blocks, pitcher=pitcher)

    primary = None
    secondary = None
    if current_mlb and _meaningful_mlb_sample(current_mlb.get("splitStats") or {}, pitcher):
        primary = current_mlb
    elif current_milb:
        primary = current_milb
        if current_mlb:
            secondary = _sample_only_clause(current_mlb.get("splitStats") or {}, pitcher)
    elif current_mlb:
        primary = current_mlb
    elif latest_mlb and _meaningful_mlb_sample(latest_mlb.get("splitStats") or {}, pitcher):
        primary = latest_mlb
    elif latest_milb:
        primary = latest_milb
        if latest_mlb:
            secondary = _sample_only_clause(latest_mlb.get("splitStats") or {}, pitcher)
    elif latest_mlb:
        primary = latest_mlb

    primary_stats = format_stat_clause((primary or {}).get("splitStats") or {}, pitcher) if primary else None
    label_full, label_short = _selected_stats_labels(primary)

    # Draft/school are fallback context only when there is no useful pro line.
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
        "primary_label_full": label_full,
        "primary_label_short": label_short,
        "secondary": secondary,
        "fallback": fallback,
    }


def build_signing_post(base_text: str, player_link: str, enrichment: dict, max_len=core.MAX_POST_LEN):
    pitcher = bool(enrichment.get("pitcher"))
    age = enrichment.get("age")
    primary_stats = enrichment.get("primary_stats")
    label_full = enrichment.get("primary_label_full")
    label_short = enrichment.get("primary_label_short")
    secondary = enrichment.get("secondary")
    fallback = list(enrichment.get("fallback") or [])

    def render(label, stats, *, include_secondary=True, include_age=True, fallback_lines=None):
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
        render(label_full, primary_stats, fallback_lines=fallback),
        render(label_short or label_full, primary_stats, fallback_lines=fallback),
        render(label_short or label_full, primary_stats, include_secondary=False, fallback_lines=fallback),
    ]
    short_stats = shorten_stat_clause(primary_stats, pitcher) if primary_stats else None
    if short_stats and short_stats != primary_stats:
        attempts.append(
            render(label_short or label_full, short_stats, include_secondary=False, fallback_lines=fallback)
        )
    attempts.extend(
        [
            render(label_short or label_full, short_stats or primary_stats, include_secondary=False, fallback_lines=[]),
            render(
                label_short or label_full,
                short_stats or primary_stats,
                include_secondary=False,
                include_age=False,
                fallback_lines=[],
            ),
        ]
    )
    for post in attempts:
        if len(post) <= max_len:
            return post

    min_post = f"{base_text}\n{player_link}"
    return min_post if len(min_post) <= max_len else min_post[: max_len - 1] + "…"


def build_post_with_optional_stats(base_text: str, player_link: str, stats_line: str, pitcher: bool, max_len=core.MAX_POST_LEN):
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


def _person_transactions(person: dict):
    txs = person.get("transactions") or []
    return [t for t in txs if isinstance(t, dict)] if isinstance(txs, list) else []


def latest_prior_transaction_date(person: dict, current_tx: dict, predicate):
    current_date = core.txn_date_obj(current_tx)
    if not current_date:
        return None
    current_id = core.txn_id(current_tx)
    candidates = []
    for hist in _person_transactions(person):
        hist_date = core.txn_date_obj(hist)
        if not hist_date or hist_date > current_date:
            continue
        hist_id = core.txn_id(hist)
        if hist_date == current_date and (not hist_id or not current_id or hist_id >= current_id):
            continue
        if predicate(hist):
            candidates.append((hist_date, hist_id))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][0]


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
            details,
            tx,
            lambda hist: core.is_recalled_transaction(hist) or core.is_contract_selected_transaction(hist),
        )
        if start:
            payload = core.get_player_by_date_range(pid, season_year, start.isoformat(), tx_date.isoformat(), player_cache)
            sel = select_mlb_appeared(payload.get("stats") or [], pitcher)
            stats_line = _labeled_stats(sel, f"MLB since {_short_date_label(start)}", pitcher)
            # A successful hydrated response with an explicit empty stats list means
            # the player was on the MLB roster for the stint but never appeared.
            if not stats_line and payload and "stats" in payload:
                stats_line = f"MLB since {_short_date_label(start)}: did not appear"
        if not stats_line:
            payload = core.get_player_season_stats(pid, season_year, player_cache)
            sel = select_mlb_appeared(payload.get("stats") or [], pitcher, season=season_year)
            stats_line = _labeled_stats(sel, f"{season_year} MLB", pitcher)

    elif category == "recalled":
        start = latest_prior_transaction_date(details, tx, core.is_optioned_transaction)
        if start:
            payload = core.get_player_by_date_range(pid, season_year, start.isoformat(), tx_date.isoformat(), player_cache)
            sel = select_highest_milb_appeared(payload.get("stats") or [], pitcher)
            if sel:
                level = core._clean_text(sel.get("levelToken")) or "MiLB"
                stats_line = _labeled_stats(sel, f"{level} since {_short_date_label(start)}", pitcher)
        if not stats_line:
            payload = core.get_player_season_stats(pid, season_year, player_cache)
            sel = select_highest_milb_appeared(payload.get("stats") or [], pitcher, season=season_year)
            if sel:
                level = core._clean_text(sel.get("levelToken")) or "MiLB"
                stats_line = _labeled_stats(sel, f"{season_year} {level}", pitcher)

    elif category == "dfa":
        payload = core.get_player_season_stats(pid, season_year, player_cache)
        sel = select_mlb_appeared(payload.get("stats") or [], pitcher, season=season_year)
        stats_line = _labeled_stats(sel, f"{season_year} MLB", pitcher)

    elif category == "contract_selected":
        payload = core.get_player_season_stats(pid, season_year, player_cache)
        sel = select_highest_milb_appeared(payload.get("stats") or [], pitcher, season=season_year)
        if sel:
            level = core._clean_text(sel.get("levelToken")) or "MiLB"
            stats_line = _labeled_stats(sel, f"{season_year} {level}", pitcher)

    return build_post_with_optional_stats(base_text, link, stats_line, pitcher, max_len=core.MAX_POST_LEN)


def apply_overrides():
    core.is_signing_transaction = is_signing_transaction
    core.format_stat_clause = format_stat_clause
    core.shorten_stat_clause = shorten_stat_clause
    core.build_signing_enrichment = build_signing_enrichment
    core.build_signing_post = build_signing_post
    core.build_post_with_optional_stats = build_post_with_optional_stats
    core.build_special_transaction_post = build_special_transaction_post


apply_overrides()

if __name__ == "__main__":
    core.main()