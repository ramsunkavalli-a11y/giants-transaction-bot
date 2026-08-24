"""Nightly Giants 40-man open-spot streak post.

This runs separately from the transaction poller near the end of the Los
Angeles calendar day. A same-day create-and-fill does not count as an open
roster day; the goal is to measure days the club actually finishes with unused
40-man capacity.
"""

import json
import os
from collections import defaultdict
from datetime import date, timedelta

import bot_core as infra
import mlb_domain as domain
import roster_intelligence as roster

STATE_PATH = os.path.join(infra.STATE_DIR, "roster_daily_state.json")
POST_HOUR_LA = 23


def load_state(path=STATE_PATH):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_state(state, path=STATE_PATH):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(tmp, path)


def is_due_time(now_la) -> bool:
    return now_la.hour == POST_HOUR_LA


def _is_40man_add(tx: dict, team_id: int) -> bool:
    if domain.is_contract_selected_transaction(tx):
        return True
    if roster.is_mlb_team_d60_return_transaction(tx, team_id=team_id):
        return True
    if (
        domain.is_claimed_off_waivers_transaction(tx)
        and infra._safe_int(infra._get_in(tx, "toTeam", "id")) == int(team_id)
    ):
        return True
    if domain.is_signing_transaction(tx):
        hay = f"{tx.get('typeDesc','')} {tx.get('description','')}".lower()
        if "major league contract" in hay:
            return True
    return False


def normalize_isolated_transaction_dips(counts: dict, transactions: list[dict],
                                         team_id: int) -> dict:
    """Repair rare historical snapshots taken between same-day 40-man moves.

    Conservative rule: only an isolated sub-40 day surrounded by full days,
    with an explicit MLB-club 60-day-IL spot creation and a 40-man addition.
    Affiliate 60-day IL moves never qualify.
    """
    fixed = dict(counts)
    ordered = sorted(fixed)
    by_date = defaultdict(list)
    for tx in transactions or []:
        d = infra.txn_date_obj(tx)
        if d:
            by_date[d].append(tx)

    for index in range(1, len(ordered) - 1):
        d = ordered[index]
        prev_d = ordered[index - 1]
        next_d = ordered[index + 1]
        if fixed.get(d, 40) >= 40:
            continue
        if fixed.get(prev_d, 0) < 40 or fixed.get(next_d, 0) < 40:
            continue
        txs = by_date.get(d, [])
        if (
            any(
                roster.is_mlb_team_d60_create_transaction(tx, team_id=team_id)
                for tx in txs
            )
            and any(_is_40man_add(tx, team_id) for tx in txs)
        ):
            fixed[d] = 40
    return fixed


def _apply_trade_windows(members: dict, windows: list[dict], as_of_date: date):
    adjusted = dict(members)
    additions = []
    for window in windows or []:
        start = roster._as_date(window.get("start"))
        end_exclusive = roster._as_date(window.get("end_exclusive"))
        pid = infra._safe_int(window.get("person_id"))
        if not start or not pid or as_of_date < start:
            continue
        if end_exclusive and as_of_date >= end_exclusive:
            continue
        if pid not in adjusted:
            adjusted[pid] = window.get("name") or str(pid)
            additions.append(window)
    return adjusted, additions


def current_adjusted_count(team_id: int, today: date, windows: list[dict], cache):
    """Use the undated live roster for today, then apply trade reconciliation."""
    members, ok = roster.fetch_40man_members(team_id, None, cache=cache)
    if not ok:
        return None, [], False
    adjusted, additions = _apply_trade_windows(members, windows, today)
    return min(40, len(adjusted)), additions, True


def calculate_open_day_history(team_id: int, today: date, cache=None):
    cache = cache if cache is not None else {}
    start = roster.regular_season_start(team_id, today.year, cache=cache)
    if not start or start > today:
        return {}, {}, False

    windows, windows_ok = roster.build_trade_exception_windows(
        team_id, start, today, cache=cache
    )
    if not windows_ok:
        windows = []

    counts = {}
    reconciliations = {}
    yesterday = today - timedelta(days=1)
    if start <= yesterday:
        historical, historical_recon, ok = roster.daily_40man_counts(
            team_id, start, yesterday, cache=cache
        )
        if not ok:
            return {}, {}, False
        txs, tx_ok = roster.fetch_team_transactions(
            team_id, start, yesterday, cache=cache
        )
        if tx_ok:
            historical = normalize_isolated_transaction_dips(
                historical, txs, team_id
            )
        counts.update(historical)
        reconciliations.update(historical_recon)

    current, additions, current_ok = current_adjusted_count(
        team_id, today, windows, cache
    )
    if not current_ok or current is None:
        return {}, {}, False
    counts[today] = current
    if additions:
        reconciliations[today] = additions
    return counts, reconciliations, True


def summarize_counts(counts: dict, today: date):
    if today not in counts:
        return None
    current = counts[today]
    cumulative = sum(1 for d, count in counts.items() if d <= today and count < 40)
    streak = 0
    d = today
    while counts.get(d, 40) < 40:
        streak += 1
        d -= timedelta(days=1)
    return {
        "count": current,
        "open": current < 40,
        "streak": streak,
        "cumulative": cumulative,
    }


def build_post_text(summary: dict, season: int) -> str | None:
    if not summary or not summary.get("open"):
        return None
    cumulative = int(summary.get("cumulative") or 0)
    total_word = "day" if cumulative == 1 else "days"
    return "\n".join([
        f"40-man roster: {int(summary['count'])}/40",
        f"Open spot streak: Day {int(summary['streak'])}",
        f"{int(season)} total: {cumulative} {total_word}",
    ])


def main():
    now_la = infra._la_now()
    if not is_due_time(now_la):
        print("Not the 11 PM Los Angeles run; skipping.")
        return

    cache = {}
    if not infra.in_season_mode(now_la=now_la, season_cache=cache):
        print("Not in MLB season mode; skipping 40-man daily post.")
        return

    infra.require_state_files(STATE_PATH)
    today = now_la.date()
    state = load_state()
    if state.get("last_post_date") == today.isoformat():
        print("40-man daily post already sent for", today)
        return

    counts, reconciliations, ok = calculate_open_day_history(
        infra.TEAM_ID, today, cache=cache
    )
    if not ok:
        raise RuntimeError("Could not calculate 40-man open-day history")
    if reconciliations:
        print("40-man reconciliation days:", reconciliations)

    summary = summarize_counts(counts, today)
    text = build_post_text(summary, today.year)
    if not text:
        print("40-man is full; no daily open-spot post.")
        return
    if len(text) > infra.MAX_POST_LEN:
        raise RuntimeError("40-man daily post unexpectedly exceeds Bluesky limit")

    identifier = infra.os.environ["BSKY_IDENTIFIER"]
    app_password = infra.os.environ["BSKY_APP_PASSWORD"]
    session = infra.bsky_create_session(identifier, app_password)
    access_jwt = session["accessJwt"]
    did = session["did"]
    response = infra.bsky_post(access_jwt, did, text)
    uri = response.get("uri")
    print("Posted 40-man daily update:\n", text)

    # Persist immediately after CreateRecord succeeds so a later verification
    # failure cannot cause a duplicate manual rerun that same night.
    save_state({
        "last_post_date": today.isoformat(),
        "last_count": summary["count"],
        "last_streak": summary["streak"],
        "last_cumulative": summary["cumulative"],
        "last_post_text": text,
    })

    status, body_snip = infra.bsky_verify_record(access_jwt, did, uri)
    print("Verify getRecord status:", status)
    if status != 200:
        print("Verify getRecord body snippet:", body_snip)


if __name__ == "__main__":
    main()
