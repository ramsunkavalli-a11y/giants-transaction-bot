"""Compact roster-mechanics context for transaction posts.

The existing post builder answers "how has the player performed?". This layer
adds one concise answer to "what does this move mean?" without changing the
underlying transaction text, ordering, or stat enrichment.
"""

from datetime import date

import acquisition_intelligence as acquisition
import bot_core as infra
import mlb_domain as domain
import roster_intelligence as roster


def _ordinal(number: int) -> str:
    n = int(number)
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _before_current(hist: dict, current_tx: dict) -> bool:
    hist_date = infra.txn_date_obj(hist)
    cur_date = infra.txn_date_obj(current_tx)
    if not hist_date or not cur_date:
        return False
    if hist_date < cur_date:
        return True
    if hist_date > cur_date:
        return False
    hist_id = infra.txn_id(hist)
    cur_id = infra.txn_id(current_tx)
    if cur_id and hist_id:
        return hist_id < cur_id
    return hist is not current_tx


def _through_current(hist: dict, current_tx: dict) -> bool:
    if infra.txn_id(hist) == infra.txn_id(current_tx) and infra.txn_id(current_tx):
        return True
    return _before_current(hist, current_tx)


def _season_history(person_id: int, current_tx: dict, cache):
    tx_date = infra.txn_date_obj(current_tx)
    if not tx_date:
        return [], False
    return domain.fetch_player_transactions(
        person_id,
        f"{tx_date.year}-01-01",
        tx_date,
        cache=cache,
    )


def _count_through(history, current_tx, predicate) -> int:
    return sum(
        1 for hist in history
        if _through_current(hist, current_tx) and predicate(hist)
    )


def _latest_prior_date(history, current_tx, predicate):
    dates = [
        infra.txn_date_obj(hist)
        for hist in history
        if _before_current(hist, current_tx) and predicate(hist)
    ]
    dates = [d for d in dates if d]
    return max(dates) if dates else None


def _is_il_placement(tx: dict) -> bool:
    hay = f"{tx.get('typeDesc','')} {tx.get('description','')}".lower()
    if "rehab assignment" in hay or "injured list" not in hay:
        return False
    return "placed" in hay and " on the " in hay and "injured list" in hay


def _is_mlb_il_placement(tx: dict) -> bool:
    return _is_il_placement(tx) and roster.is_mlb_team_level_transaction(tx)


def _is_il_reinstatement(tx: dict) -> bool:
    hay = f"{tx.get('typeDesc','')} {tx.get('description','')}".lower()
    return (
        domain.is_reinstated_transaction(tx)
        and "injured list" in hay
        and roster.is_mlb_team_level_transaction(tx)
    )


def _current_il_stint_start(history, current_tx):
    """Return the MLB IL stint active immediately before current_tx."""
    active_start = None
    ordered = sorted(
        [hist for hist in history if _before_current(hist, current_tx)],
        key=lambda item: (infra.txn_date_obj(item) or date.min, infra.txn_id(item)),
    )
    for hist in ordered:
        if _is_mlb_il_placement(hist):
            active_start = infra.txn_date_obj(hist)
        elif _is_il_reinstatement(hist):
            active_start = None
        # A transfer to the 60-day IL does not restart the injury clock.
    return active_start


def _first_callup_part(details: dict, history, current_tx) -> str | None:
    tx_date = infra.txn_date_obj(current_tx)
    if not tx_date:
        return None
    debut = None
    try:
        value = details.get("mlbDebutDate")
        if value:
            debut = date.fromisoformat(str(value)[:10])
    except ValueError:
        debut = None
    if debut and debut < tx_date:
        return None
    had_prior_mlb_roster_move = any(
        _before_current(hist, current_tx)
        and (
            domain.is_contract_selected_transaction(hist)
            or domain.is_recalled_transaction(hist)
        )
        for hist in history
    )
    return None if had_prior_mlb_roster_move else "First MLB call-up"


def _option_part(history, current_tx) -> str | None:
    count = _count_through(history, current_tx, domain.is_optioned_transaction)
    if count < 2:
        return None
    tx_date = infra.txn_date_obj(current_tx)
    return f"{_ordinal(count)} option in {tx_date.year}" if tx_date else None


def _dfa_part(history, current_tx) -> str | None:
    count = _count_through(history, current_tx, domain.is_dfa_transaction)
    if count < 2:
        return None
    tx_date = infra.txn_date_obj(current_tx)
    return f"{_ordinal(count)} DFA in {tx_date.year}" if tx_date else None


def _waiver_part(history, current_tx) -> str | None:
    dfa_date = _latest_prior_date(history, current_tx, domain.is_dfa_transaction)
    tx_date = infra.txn_date_obj(current_tx)
    if not dfa_date or not tx_date:
        return None
    elapsed = (tx_date - dfa_date).days
    if elapsed < 0 or elapsed > 14:
        return None
    if elapsed == 0:
        return "Claimed same day as DFA"
    if elapsed == 1:
        return "Claimed 1 day after DFA"
    return f"Claimed {elapsed} days after DFA"


def _outright_part(person_id: int, current_tx: dict, cache) -> str | None:
    tx_date = infra.txn_date_obj(current_tx)
    if not tx_date:
        return None
    history, ok = domain.fetch_player_transactions(
        person_id, "2000-01-01", tx_date, cache=cache
    )
    if not ok:
        return None
    count = _count_through(history, current_tx, domain.is_outrighted_transaction)
    if count == 1:
        return "First career outright"
    return None


def _il_parts(history, current_tx):
    tx_date = infra.txn_date_obj(current_tx)
    if not tx_date:
        return []
    parts = []
    start = _current_il_stint_start(history, current_tx)
    if (
        roster.is_mlb_team_d60_create_transaction(current_tx)
        and start
        and start < tx_date
    ):
        parts.append(f"Out since {start.strftime('%b')} {start.day}")
    if _is_il_reinstatement(current_tx) and start:
        elapsed = (tx_date - start).days
        if elapsed > 0:
            parts.append(f"Returns after {elapsed} days on IL")
    return parts


def _is_incoming_claim(tx: dict) -> bool:
    return (
        domain.is_claimed_off_waivers_transaction(tx)
        and infra._safe_int(infra._get_in(tx, "toTeam", "id")) == infra.TEAM_ID
    )


def _is_professional_acquisition(tx: dict) -> bool:
    code = (tx.get("typeCode") or "").strip().upper()
    if _is_incoming_claim(tx) or domain.is_acquired_transaction(tx):
        return True
    if domain.is_signing_transaction(tx) and code != "SGN":
        return True
    return False


def _acquisition_part(person_id: int, details: dict, tx: dict, cache) -> str | None:
    if not _is_professional_acquisition(tx):
        return None
    tx_date = infra.txn_date_obj(tx)
    if not tx_date:
        return None
    return acquisition.best_acquisition_part(
        person_id,
        infra.is_pitcher(details),
        tx_date.year,
        cache=cache,
    )


def _forty_man_parts(tx: dict, cache) -> list[str]:
    affects_40man = (
        domain.is_dfa_transaction(tx)
        or roster.is_mlb_team_d60_create_transaction(tx)
        or domain.is_contract_selected_transaction(tx)
        or _is_incoming_claim(tx)
        or roster.is_mlb_team_d60_return_transaction(tx)
    )
    if not affects_40man:
        return []
    tx_date = infra.txn_date_obj(tx)
    count, additions, ok = roster.adjusted_40man_count(
        infra.TEAM_ID, as_of_date=tx_date, cache=cache
    )
    if not ok or count is None:
        return []
    if additions:
        print("40-man reconciliation additions:", additions)
    # The after-move count says everything the directional phrase would say,
    # without repeating that a selection, claim, DFA, or 60-day move changes
    # the roster.
    return [f"40-man: {count}/40"]


def context_parts_for_transaction(tx: dict, cache=None, now_la=None) -> list[str]:
    """Return ordered, optional context pieces for one transaction."""
    cache = cache if cache is not None else {}
    person_id = infra.extract_tx_player_id(tx)
    if not person_id:
        return []
    history, _ok = _season_history(person_id, tx, cache)
    details = None
    parts = []

    if domain.is_optioned_transaction(tx):
        part = _option_part(history, tx)
        if part:
            parts.append(part)

    if domain.is_contract_selected_transaction(tx):
        details = domain.fetch_player_details(person_id, cache=cache)
        part = _first_callup_part(details, history, tx)
        if part:
            parts.append(part)

    if domain.is_dfa_transaction(tx):
        part = _dfa_part(history, tx)
        if part:
            parts.append(part)

    if _is_incoming_claim(tx):
        part = _waiver_part(history, tx)
        if part:
            parts.append(part)

    if domain.is_outrighted_transaction(tx):
        part = _outright_part(person_id, tx, cache)
        if part:
            parts.append(part)

    parts.extend(_il_parts(history, tx))

    # Professional acquisitions get at most one player-identity nugget. It is
    # deliberately ahead of the generic 40-man count in the fit priority.
    if _is_professional_acquisition(tx):
        details = details or domain.fetch_player_details(person_id, cache=cache)
        part = _acquisition_part(person_id, details, tx, cache)
        if part:
            parts.append(part)

    parts.extend(_forty_man_parts(tx, cache))
    return parts


def _insert_context(post: str, parts: list[str], max_len: int) -> str:
    if not parts:
        return post
    lines = post.splitlines()
    insert_at = len(lines)
    if lines and lines[-1].startswith("https://www.mlb.com/player/"):
        insert_at = len(lines) - 1

    # Drop lower-priority tail pieces until the context fits. The first piece
    # is always the transaction-specific insight; 40-man count is deliberately
    # easiest to drop.
    candidates = list(parts)
    while candidates:
        context = " | ".join(candidates)
        trial = lines[:insert_at] + [context] + lines[insert_at:]
        rendered = "\n".join(trial)
        if len(rendered) <= max_len:
            return rendered
        candidates.pop()
    return post


def enrich_posts(posts, new_txns, cache=None, now_la=None,
                 max_len=infra.MAX_POST_LEN):
    """Add context to posts that map cleanly to one transaction.

    Grouped multi-transaction posts are intentionally left alone, except a
    same-player, same-day incoming claim followed by an option. That is one
    connected move, so claim context (including the after-move 40-man count)
    remains useful.
    """
    cache = cache if cache is not None else {}
    by_id = {infra.txn_id(tx): tx for tx in new_txns if infra.txn_id(tx)}
    enriched = []
    for post in posts:
        covered = infra.transaction_ids_represented_in_post(post, new_txns)
        tx = None
        if len(covered) == 1:
            tx = by_id.get(next(iter(covered)))
        elif len(covered) == 2:
            paired = [by_id.get(txid) for txid in covered]
            claim = next((item for item in paired if item and _is_incoming_claim(item)), None)
            option = next((item for item in paired if item and domain.is_optioned_transaction(item)), None)
            if (
                claim and option
                and infra.txn_date(claim) == infra.txn_date(option)
                and infra.extract_tx_player_id(claim) == infra.extract_tx_player_id(option)
            ):
                tx = claim
        if not tx:
            enriched.append(post)
            continue
        parts = context_parts_for_transaction(tx, cache=cache, now_la=now_la)
        enriched.append(_insert_context(post, parts, max_len))
    return enriched
