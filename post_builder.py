"""Transaction post construction on top of the verified MLB domain layer."""

from datetime import datetime

import bot_core as infra
import mlb_domain as domain
import pitching_domain as pitching
import statcast_domain as statcast

SIGNING_MLB_MIN_PA = 30
SIGNING_MLB_MIN_IP = 10.0
OPTION_XWOBA_MIN_PA = 40
OPTION_PITCHER_MIN_OUTS = 30  # 10.0 IP


def _format_pct(value):
    number = infra._safe_float(value)
    if number is None:
        return None
    if abs(number) <= 1:
        number *= 100
    return f"{int(round(number))}%"


def _format_pct_number(value):
    number = infra._safe_float(value)
    if number is None:
        return None
    if abs(number) <= 1:
        number *= 100
    return int(round(number))


def _format_slash_value(value):
    number = infra._safe_float(value)
    if number is None:
        return None
    text = f"{number:.3f}"
    if text.startswith("0."):
        return text[1:]
    if text.startswith("-0."):
        return "-" + text[2:]
    return text


def _format_ip(value):
    number = infra._safe_float(value)
    return f"{number:.1f}" if number is not None else None


def format_stat_clause(stat: dict, pitcher: bool):
    if not stat:
        return None

    parts = []
    if pitcher:
        ip = _format_ip(stat.get("inningsPitched"))
        hits = infra._safe_int(stat.get("hits"))
        era = infra._safe_float(stat.get("era"))
        strikeouts = infra._safe_int(stat.get("strikeOuts"))
        walks = infra._safe_int(stat.get("baseOnBalls"))
        if ip is not None:
            parts.append(f"{ip} IP")
        if hits is not None:
            parts.append(f"{hits} H")
        if era is not None:
            parts.append(f"{era:.2f} ERA")
        if strikeouts is not None:
            parts.append(f"{strikeouts} K")
        if walks is not None:
            parts.append(f"{walks} BB")
        return ", ".join(parts) if parts else None

    pa = infra._safe_int(stat.get("plateAppearances"))
    avg = _format_slash_value(stat.get("avg"))
    obp = _format_slash_value(stat.get("obp"))
    slg = _format_slash_value(stat.get("slg"))
    hr = infra._safe_int(stat.get("homeRuns"))
    strikeouts = infra._safe_int(stat.get("strikeOuts"))
    walks = infra._safe_int(stat.get("baseOnBalls"))

    k_pct = _format_pct(stat.get("strikeoutPercentage"))
    bb_pct = _format_pct(stat.get("baseOnBallsPercentage"))
    # Player-scoped StatsAPI does not consistently provide rate fields.
    if k_pct is None and pa and strikeouts is not None:
        k_pct = f"{int(round(100 * strikeouts / pa))}%"
    if bb_pct is None and pa and walks is not None:
        bb_pct = f"{int(round(100 * walks / pa))}%"

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
    parts = [part.strip() for part in stat_clause.split(",") if part.strip()]
    if pitcher:
        parts = [part for part in parts if not part.endswith(" H") and not part.endswith(" BB")]
    else:
        parts = [
            part for part in parts
            if not (part.endswith("% K") or part.endswith("% BB"))
        ]
    return ", ".join(parts).strip() or None


def _meaningful_mlb_sample(stat: dict, pitcher: bool):
    if pitcher:
        return (
            infra._safe_float((stat or {}).get("inningsPitched")) or 0.0
        ) >= SIGNING_MLB_MIN_IP
    return (
        infra._safe_int((stat or {}).get("plateAppearances")) or 0
    ) >= SIGNING_MLB_MIN_PA


def _sample_only_clause(stat: dict, pitcher: bool):
    if pitcher:
        ip = _format_ip((stat or {}).get("inningsPitched"))
        return f"MLB: {ip} IP" if ip is not None else None
    pa = infra._safe_int((stat or {}).get("plateAppearances"))
    return f"MLB: {pa} PA" if pa is not None else None


def _age_years(birth_date_str: str, as_of_date):
    if not birth_date_str or not as_of_date:
        return None
    try:
        birth_date = datetime.strptime(str(birth_date_str)[:10], "%Y-%m-%d").date()
    except Exception:
        return None
    age = as_of_date.year - birth_date.year - (
        (as_of_date.month, as_of_date.day) < (birth_date.month, birth_date.day)
    )
    return age if age >= 0 else None


def _signing_context(person_id: int, pitcher: bool, tx_date, cache):
    """Return MLB/MiLB context available as of the signing date only."""
    season = tx_date.year
    season_start = f"{season}-01-01"
    mlb, mlb_ok = domain.fetch_player_stat(
        person_id, pitcher, 1, "byDateRange", season,
        cache=cache, start_date=season_start, end_date=tx_date,
    )
    milb, milb_ok = domain.fetch_highest_milb_stat(
        person_id, pitcher, "byDateRange", season,
        cache=cache, start_date=season_start, end_date=tx_date,
    )
    if mlb or milb:
        return mlb, milb, (mlb_ok or milb_ok)

    # Early-offseason signings often have no stats in the new calendar year.
    prior = season - 1
    prior_mlb, prior_mlb_ok = domain.fetch_player_stat(
        person_id, pitcher, 1, "season", prior, cache=cache,
    )
    prior_milb, prior_milb_ok = domain.fetch_highest_milb_stat(
        person_id, pitcher, "season", prior, cache=cache,
    )
    return prior_mlb, prior_milb, (prior_mlb_ok or prior_milb_ok)


def build_signing_enrichment(person: dict, tx_date, cache=None):
    """Choose relevant professional performance before bio fallback."""
    cache = cache if cache is not None else {}
    pitcher = infra.is_pitcher(person)
    person_id = infra._safe_int(person.get("id"))

    primary = None
    secondary = None
    if person_id and tx_date:
        mlb, milb, _ok = _signing_context(person_id, pitcher, tx_date, cache)
        if mlb and _meaningful_mlb_sample(mlb.get("splitStats") or {}, pitcher):
            primary = mlb
        elif milb:
            primary = milb
            if mlb:
                secondary = _sample_only_clause(mlb.get("splitStats") or {}, pitcher)
        elif mlb:
            primary = mlb

    primary_stats = (
        format_stat_clause((primary or {}).get("splitStats") or {}, pitcher)
        if primary else None
    )
    season = (primary or {}).get("seasonYear")
    level = infra._clean_text((primary or {}).get("levelToken"))
    label = " ".join(str(value) for value in (season, level) if value) or None

    fallback = []
    if not primary_stats:
        draft = infra.draft_clause(person)
        school = infra.school_clause(person)
        if draft:
            fallback.append(draft)
        if school:
            fallback.append(school)

    return {
        "pitcher": pitcher,
        "age": _age_years(person.get("birthDate"), tx_date),
        "primary_stats": primary_stats,
        "primary_label": label,
        "secondary": secondary,
        "fallback": fallback,
    }


def build_signing_post(base_text: str, player_link: str, enrichment: dict,
                       max_len=infra.MAX_POST_LEN):
    pitcher = bool(enrichment.get("pitcher"))
    age = enrichment.get("age")
    primary_stats = enrichment.get("primary_stats")
    label = enrichment.get("primary_label")
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
        attempts.append(
            render(short_stats, include_secondary=False, fallback_lines=fallback)
        )
    attempts.extend([
        render(short_stats or primary_stats, include_secondary=False, fallback_lines=[]),
        render(
            short_stats or primary_stats,
            include_secondary=False,
            include_age=False,
            fallback_lines=[],
        ),
    ])

    for post in attempts:
        if len(post) <= max_len:
            return post

    minimum = f"{base_text}\n{player_link}"
    return minimum if len(minimum) <= max_len else minimum[: max_len - 1] + "…"


def build_post_with_optional_stats(base_text: str, player_link: str, stats_line: str,
                                   pitcher: bool, max_len=infra.MAX_POST_LEN):
    if stats_line:
        post = f"{base_text}\n{stats_line}\n{player_link}"
        if len(post) <= max_len:
            return post
        short_stats = shorten_stat_clause(stats_line, pitcher)
        if short_stats:
            post = f"{base_text}\n{short_stats}\n{player_link}"
            if len(post) <= max_len:
                return post

    minimum = f"{base_text}\n{player_link}"
    if len(minimum) <= max_len:
        return minimum
    allowed_link = max(0, max_len - len(base_text) - 1)
    return f"{base_text}\n{player_link[:allowed_link]}"


def _short_date_label(value):
    return f"{value.strftime('%b')} {value.day}" if value else None


def _labeled_stats(selected: dict, prefix: str, pitcher: bool):
    if not selected:
        return None
    stat_text = format_stat_clause(selected.get("splitStats") or {}, pitcher)
    return f"{prefix}: {stat_text}" if stat_text else None


def _option_usage_text(usage: dict | None):
    if not usage:
        return None
    games = infra._safe_int(usage.get("games"))
    starts = infra._safe_int(usage.get("starts"))
    if games is None or games <= 0:
        return None
    if starts is None:
        return f"{games} G"
    return f"{games} G ({starts} GS)"


def _option_pitcher_usage_text(usage: dict | None):
    if not usage:
        return None
    games = infra._safe_int(usage.get("games"))
    starts = infra._safe_int(usage.get("starts"))
    if games is None or games <= 0:
        return None
    if starts is not None and starts > 0:
        return f"{games} G ({starts} GS)"
    return f"{games} G"


def _option_hitter_primary(prefix: str, stat: dict, usage=None):
    pa = infra._safe_int((stat or {}).get("plateAppearances"))
    avg = _format_slash_value((stat or {}).get("avg"))
    obp = _format_slash_value((stat or {}).get("obp"))
    slg = _format_slash_value((stat or {}).get("slg"))
    obp_num = infra._safe_float((stat or {}).get("obp"))
    slg_num = infra._safe_float((stat or {}).get("slg"))
    ops = _format_slash_value(obp_num + slg_num) if obp_num is not None and slg_num is not None else None

    parts = []
    usage_text = _option_usage_text(usage)
    if usage_text:
        parts.append(usage_text)
    if pa is not None:
        parts.append(f"{pa} PA")
    if avg and obp and slg:
        slash = f"{avg}/{obp}/{slg}"
        if ops:
            slash += f" ({ops} OPS)"
        parts.append(slash)

    if not parts:
        fallback = format_stat_clause(stat, pitcher=False)
        return f"{prefix}: {fallback}" if fallback else None
    return f"{prefix}: {', '.join(parts)}"


def _option_hitter_secondary(stat: dict, woba=None, xwoba=None):
    pa = infra._safe_int((stat or {}).get("plateAppearances")) or 0
    if pa < OPTION_XWOBA_MIN_PA:
        return None

    parts = []
    if xwoba is not None:
        x_text = _format_slash_value(xwoba)
        w_text = _format_slash_value(woba) if woba is not None else None
        if x_text and w_text:
            parts.append(f"{w_text} wOBA / {x_text} xwOBA")
        elif x_text:
            parts.append(f"{x_text} xwOBA")

    strikeouts = infra._safe_int((stat or {}).get("strikeOuts"))
    walks = infra._safe_int((stat or {}).get("baseOnBalls"))
    k_pct = _format_pct_number((stat or {}).get("strikeoutPercentage"))
    bb_pct = _format_pct_number((stat or {}).get("baseOnBallsPercentage"))
    if k_pct is None and pa and strikeouts is not None:
        k_pct = int(round(100 * strikeouts / pa))
    if bb_pct is None and pa and walks is not None:
        bb_pct = int(round(100 * walks / pa))
    if k_pct is not None:
        parts.append(f"{k_pct} K%")
    if bb_pct is not None:
        parts.append(f"{bb_pct} BB%")
    return " | ".join(parts) if parts else None


def _option_pitcher_has_advanced_sample(stat: dict) -> bool:
    outs = infra._safe_int((stat or {}).get("outs"))
    if outs is not None:
        return outs >= OPTION_PITCHER_MIN_OUTS
    ip = infra._safe_float((stat or {}).get("inningsPitched")) or 0.0
    return ip >= 10.0


def _option_pitcher_primary(prefix: str, stat: dict, usage=None):
    ip = _format_ip((stat or {}).get("inningsPitched"))
    era = infra._safe_float((stat or {}).get("era"))
    parts = []
    usage_text = _option_pitcher_usage_text(usage)
    if usage_text:
        parts.append(usage_text)
    if ip is not None:
        parts.append(f"{ip} IP")
    if era is not None:
        parts.append(f"{era:.2f} ERA")
    if not parts:
        fallback = format_stat_clause(stat, pitcher=True)
        return f"{prefix}: {fallback}" if fallback else None
    return f"{prefix}: {', '.join(parts)}"


def _option_pitcher_secondary(stat: dict, saber=None):
    if not _option_pitcher_has_advanced_sample(stat):
        return None

    parts = []
    saber = saber or {}
    fip = infra._safe_float(saber.get("fip"))
    xfip = infra._safe_float(saber.get("xfip"))
    if fip is not None and xfip is not None:
        parts.append(f"{fip:.2f} FIP / {xfip:.2f} xFIP")
    elif fip is not None:
        parts.append(f"{fip:.2f} FIP")
    elif xfip is not None:
        parts.append(f"{xfip:.2f} xFIP")

    batters_faced = infra._safe_int((stat or {}).get("battersFaced")) or 0
    strikeouts = infra._safe_int((stat or {}).get("strikeOuts"))
    walks = infra._safe_int((stat or {}).get("baseOnBalls"))
    if batters_faced and strikeouts is not None:
        parts.append(f"{int(round(100 * strikeouts / batters_faced))} K%")
    if batters_faced and walks is not None:
        parts.append(f"{int(round(100 * walks / batters_faced))} BB%")
    return " | ".join(parts) if parts else None


def build_option_hitter_post(base_text: str, player_link: str, prefix: str,
                             selected: dict, usage=None, woba=None, xwoba=None,
                             max_len=infra.MAX_POST_LEN):
    """Render a demotion as opportunity + familiar results + expected/process."""
    stat = (selected or {}).get("splitStats") or {}
    full_primary = _option_hitter_primary(prefix, stat, usage=usage)
    compact_primary = _option_hitter_primary(prefix, stat, usage=None)
    secondary = _option_hitter_secondary(stat, woba=woba, xwoba=xwoba)

    def render(primary, detail=None):
        lines = [base_text]
        if primary:
            lines.append(primary)
        if detail:
            lines.append(detail)
        lines.append(player_link)
        return "\n".join(lines)

    # Keep expected/process context ahead of G/GS if a long description forces
    # us to choose. PA + slash/OPS are preserved as long as possible.
    attempts = [
        render(full_primary, secondary),
        render(compact_primary, secondary),
        render(full_primary),
        render(compact_primary),
    ]
    for post in attempts:
        if len(post) <= max_len:
            return post

    return build_post_with_optional_stats(
        base_text,
        player_link,
        _labeled_stats(selected, prefix, pitcher=False),
        pitcher=False,
        max_len=max_len,
    )


def build_option_pitcher_post(base_text: str, player_link: str, prefix: str,
                              selected: dict, usage=None, saber=None,
                              max_len=infra.MAX_POST_LEN):
    """Render an optioned pitcher as usage/results plus rate-stat context."""
    stat = (selected or {}).get("splitStats") or {}
    full_primary = _option_pitcher_primary(prefix, stat, usage=usage)
    compact_primary = _option_pitcher_primary(prefix, stat, usage=None)
    secondary = _option_pitcher_secondary(stat, saber=saber)

    def render(primary, detail=None):
        lines = [base_text]
        if primary:
            lines.append(primary)
        if detail:
            lines.append(detail)
        lines.append(player_link)
        return "\n".join(lines)

    attempts = [
        render(full_primary, secondary),
        render(compact_primary, secondary),
        render(full_primary),
        render(compact_primary),
    ]
    for post in attempts:
        if len(post) <= max_len:
            return post

    return build_post_with_optional_stats(
        base_text,
        player_link,
        _labeled_stats(selected, prefix, pitcher=True),
        pitcher=True,
        max_len=max_len,
    )


def build_special_transaction_post(tx: dict, category: str, cache: dict, now_la):
    base_text = infra.build_base_tx_text(tx)
    person_id = infra.extract_tx_player_id(tx)
    if not person_id:
        return base_text

    link = infra.player_url(person_id)
    details = domain.fetch_player_details(person_id, cache=cache)
    pitcher = infra.is_pitcher(details)
    tx_date = infra.txn_date_obj(tx) or now_la.date()
    season = tx_date.year
    season_start = f"{season}-01-01"
    stats_line = None

    if category == "optioned":
        start = domain.latest_prior_transaction_date(
            person_id,
            tx,
            lambda hist: (
                domain.is_recalled_transaction(hist)
                or domain.is_contract_selected_transaction(hist)
            ),
            cache=cache,
        )
        if start:
            selected, ok = domain.fetch_player_stat(
                person_id, pitcher, 1, "byDateRange", season,
                cache=cache, start_date=start, end_date=tx_date,
            )
            prefix = f"MLB since {_short_date_label(start)}"
            if selected and not pitcher:
                usage, _usage_ok = statcast.fetch_mlb_usage(
                    person_id, False, season, start, tx_date, cache=cache
                )
                stat = selected.get("splitStats") or {}
                pa = infra._safe_int(stat.get("plateAppearances")) or 0
                woba = None
                xwoba = None
                if pa >= OPTION_XWOBA_MIN_PA:
                    xwoba, woba, _denom, _x_ok = statcast.fetch_date_bounded_xwoba(
                        person_id, False, season, start, tx_date, cache=cache
                    )
                return build_option_hitter_post(
                    base_text, link, prefix, selected,
                    usage=usage, woba=woba, xwoba=xwoba,
                    max_len=infra.MAX_POST_LEN,
                )
            if selected and pitcher:
                usage, _usage_ok = statcast.fetch_mlb_usage(
                    person_id, True, season, start, tx_date, cache=cache
                )
                saber = None
                if _option_pitcher_has_advanced_sample(selected.get("splitStats") or {}):
                    saber, _saber_ok = pitching.fetch_pitcher_sabermetrics(
                        person_id, season, start, tx_date,
                        cache=cache, team_id=infra.TEAM_ID,
                    )
                return build_option_pitcher_post(
                    base_text, link, prefix, selected,
                    usage=usage, saber=saber,
                    max_len=infra.MAX_POST_LEN,
                )
            stats_line = _labeled_stats(selected, prefix, pitcher)
            if not stats_line and ok:
                stats_line = f"{prefix}: did not appear"
        if not stats_line:
            selected, _ok = domain.fetch_player_stat(
                person_id, pitcher, 1, "byDateRange", season,
                cache=cache, start_date=season_start, end_date=tx_date,
            )
            prefix = f"{season} MLB"
            if selected and not pitcher:
                usage, _usage_ok = statcast.fetch_mlb_usage(
                    person_id, False, season, season_start, tx_date, cache=cache
                )
                stat = selected.get("splitStats") or {}
                pa = infra._safe_int(stat.get("plateAppearances")) or 0
                woba = None
                xwoba = None
                if pa >= OPTION_XWOBA_MIN_PA:
                    xwoba, woba, _denom, _x_ok = statcast.fetch_date_bounded_xwoba(
                        person_id, False, season, season_start, tx_date, cache=cache
                    )
                return build_option_hitter_post(
                    base_text, link, prefix, selected,
                    usage=usage, woba=woba, xwoba=xwoba,
                    max_len=infra.MAX_POST_LEN,
                )
            if selected and pitcher:
                usage, _usage_ok = statcast.fetch_mlb_usage(
                    person_id, True, season, season_start, tx_date, cache=cache
                )
                saber = None
                if _option_pitcher_has_advanced_sample(selected.get("splitStats") or {}):
                    saber, _saber_ok = pitching.fetch_pitcher_sabermetrics(
                        person_id, season, season_start, tx_date,
                        cache=cache, team_id=infra.TEAM_ID,
                    )
                return build_option_pitcher_post(
                    base_text, link, prefix, selected,
                    usage=usage, saber=saber,
                    max_len=infra.MAX_POST_LEN,
                )
            stats_line = _labeled_stats(selected, prefix, pitcher)

    elif category == "recalled":
        start = domain.latest_prior_transaction_date(
            person_id, tx, domain.is_optioned_transaction, cache=cache
        )
        if start:
            selected, _ok = domain.fetch_highest_milb_stat(
                person_id, pitcher, "byDateRange", season,
                cache=cache, start_date=start, end_date=tx_date,
            )
            if selected:
                level = infra._clean_text(selected.get("levelToken")) or "MiLB"
                stats_line = _labeled_stats(
                    selected, f"{level} since {_short_date_label(start)}", pitcher
                )
        if not stats_line:
            selected, _ok = domain.fetch_highest_milb_stat(
                person_id, pitcher, "byDateRange", season,
                cache=cache, start_date=season_start, end_date=tx_date,
            )
            if selected:
                level = infra._clean_text(selected.get("levelToken")) or "MiLB"
                stats_line = _labeled_stats(selected, f"{season} {level}", pitcher)

    elif category == "dfa":
        selected, _ok = domain.fetch_player_stat(
            person_id, pitcher, 1, "byDateRange", season,
            cache=cache, start_date=season_start, end_date=tx_date,
        )
        stats_line = _labeled_stats(selected, f"{season} MLB", pitcher)

    elif category == "contract_selected":
        selected, _ok = domain.fetch_highest_milb_stat(
            person_id, pitcher, "byDateRange", season,
            cache=cache, start_date=season_start, end_date=tx_date,
        )
        if selected:
            level = infra._clean_text(selected.get("levelToken")) or "MiLB"
            stats_line = _labeled_stats(selected, f"{season} {level}", pitcher)

    return build_post_with_optional_stats(
        base_text, link, stats_line, pitcher, max_len=infra.MAX_POST_LEN
    )


def build_posts(new_txns, player_cache=None, season_mode=False, now_la=None):
    """Build posts without monkey-patching legacy core classification/stats."""
    cache = player_cache if player_cache is not None else {}
    now_la = now_la or infra._la_now()
    separate_posts = []
    grouped = []

    for tx in sorted(new_txns, key=infra.txn_id):
        # Jersey-number changes are administrative noise, including the mass
        # Jackie Robinson Day switch to No. 42. Marking fetched IDs seen is
        # handled by bot.py even when this builder intentionally emits nothing.
        if domain.is_number_change_transaction(tx):
            continue

        # Incoming waiver claims benefit from the same date-bounded player
        # context used for signings. Outgoing claims remain grouped/plain so a
        # DFA followed by a claim does not repeat the same player evaluation.
        if (
            domain.is_claimed_off_waivers_transaction(tx)
            and infra._safe_int(infra._get_in(tx, "toTeam", "id")) == infra.TEAM_ID
        ):
            base_text = infra.build_base_tx_text(tx)
            person_id = infra.extract_tx_player_id(tx)
            if not person_id:
                separate_posts.append(base_text)
                continue
            details = domain.fetch_player_details(person_id, cache=cache)
            tx_date = infra.txn_date_obj(tx) or now_la.date()
            enrichment = build_signing_enrichment(details, tx_date, cache=cache)
            separate_posts.append(
                build_signing_post(
                    base_text,
                    infra.player_url(person_id),
                    enrichment,
                    max_len=infra.MAX_POST_LEN,
                )
            )
            continue

        category = domain.classify_transaction(tx, season_mode=season_mode)

        if category == "signing":
            base_text = infra.build_base_tx_text(tx)
            person_id = infra.extract_tx_player_id(tx)
            if not person_id:
                separate_posts.append(base_text)
                continue
            details = domain.fetch_player_details(person_id, cache=cache)
            tx_date = infra.txn_date_obj(tx) or now_la.date()
            enrichment = build_signing_enrichment(details, tx_date, cache=cache)
            separate_posts.append(
                build_signing_post(
                    base_text,
                    infra.player_url(person_id),
                    enrichment,
                    max_len=infra.MAX_POST_LEN,
                )
            )
            continue

        if category:
            separate_posts.append(
                build_special_transaction_post(tx, category, cache, now_la)
            )
        else:
            grouped.append(tx)

    grouped_posts = infra.pack_posts(infra.build_date_group_blocks(grouped))
    return [
        post for post in (grouped_posts + separate_posts)
        if post and post.strip()
    ]
