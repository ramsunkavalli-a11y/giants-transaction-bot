"""Stable infrastructure and generic helpers for the Giants transaction bot.

Baseball-specific transaction semantics and player stat access live in
``mlb_domain.py``. Transaction-specific presentation lives in
``post_builder.py``. Keeping this module domain-agnostic prevents the bot from
having two competing implementations of MLB transaction logic.
"""

import os
import re
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

# -----------------------------
# Config
# -----------------------------
TEAM_ID = 137  # San Francisco Giants
MAX_POST_LEN = 300
REQUEST_TIMEOUT = 20
REQUEST_RETRIES = 3
SEASON_CACHE_KEY = "_season_mode"

_POST_DELAY_ENV = os.getenv("POST_DELAY_SECONDS", "2").strip()
try:
    POST_DELAY_SECONDS = float(_POST_DELAY_ENV) if _POST_DELAY_ENV else 2.0
except Exception:
    POST_DELAY_SECONDS = 2.0

_CUTOFF_ENV = os.getenv("TXN_CUTOFF_DATE", "2026-02-21").strip()
TXN_CUTOFF_DATE = datetime.strptime(_CUTOFF_ENV, "%Y-%m-%d").date()

# Production workflows check mutable state out from the dedicated bot-state
# branch. Local runs default to the repository directory for convenience.
STATE_DIR = os.environ.get("BOT_STATE_DIR") or os.path.dirname(os.path.abspath(__file__))
LAST_ID_PATH = os.path.join(STATE_DIR, "last_id.txt")
SEEN_IDS_PATH = os.path.join(STATE_DIR, "seen_ids.txt")


def require_state_files(*paths: str):
    """Fail safely instead of treating a missing production state checkout as empty."""
    missing = [path for path in paths if not os.path.isfile(path)]
    if missing:
        raise RuntimeError(
            "Production state is unavailable. Check out the bot-state branch "
            "and set BOT_STATE_DIR before running a posting workflow. Missing: "
            + ", ".join(missing)
        )


# -----------------------------
# Time / HTTP / season helpers
# -----------------------------
def _la_now():
    try:
        return datetime.now(ZoneInfo("America/Los_Angeles"))
    except Exception:
        return datetime.utcnow()


def mlb_transactions_url(days_back=120):
    end_date = _la_now().date()
    start_date = end_date - timedelta(days=days_back)

    if start_date < TXN_CUTOFF_DATE:
        start_date = TXN_CUTOFF_DATE
    if start_date > end_date:
        start_date = end_date

    return (
        "https://statsapi.mlb.com/api/v1/transactions"
        f"?teamId={TEAM_ID}&startDate={start_date}&endDate={end_date}"
    )


def request_json_with_retry(
    url: str,
    *,
    headers=None,
    timeout=REQUEST_TIMEOUT,
    retries=REQUEST_RETRIES,
):
    last_err = None
    for attempt in range(retries):
        try:
            response = requests.get(url, headers=headers, timeout=timeout)
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            last_err = exc
            if attempt < retries - 1:
                time.sleep(0.5 * (2 ** attempt))
    raise last_err


def in_season_mode(now_la=None, season_cache=None):
    """Return True during MLB regular season or postseason."""
    now_la = now_la or _la_now()
    season_cache = season_cache if season_cache is not None else {}
    cache_key = (SEASON_CACHE_KEY, now_la.date().isoformat())
    if cache_key in season_cache:
        return season_cache[cache_key]

    for year in (now_la.year, now_la.year - 1, now_la.year + 1):
        try:
            data = request_json_with_retry(
                "https://statsapi.mlb.com/api/v1/seasons"
                f"?sportId=1&season={year}"
            )
            seasons = data.get("seasons") or []
            if not seasons:
                continue
            info = seasons[0]
            regular_start = info.get("regularSeasonStartDate")
            regular_end = info.get("regularSeasonEndDate")
            postseason_start = (
                info.get("postSeasonStartDate")
                or info.get("postseasonStartDate")
                or regular_start
            )
            postseason_end = (
                info.get("postSeasonEndDate")
                or info.get("postseasonEndDate")
                or regular_end
            )
            if regular_start and regular_end:
                start = datetime.strptime(
                    (postseason_start or regular_start)[:10], "%Y-%m-%d"
                ).date()
                end = datetime.strptime(
                    (postseason_end or regular_end)[:10], "%Y-%m-%d"
                ).date()
                if start <= now_la.date() <= end:
                    season_cache[cache_key] = True
                    return True
        except Exception:
            pass

    # Fallback if season metadata is temporarily unavailable.
    try:
        start_date = (now_la.date() - timedelta(days=7)).isoformat()
        end_date = (now_la.date() + timedelta(days=7)).isoformat()
        for game_type in ("R", "P"):
            url = (
                "https://statsapi.mlb.com/api/v1/schedule"
                f"?sportId=1&gameType={game_type}"
                f"&startDate={start_date}&endDate={end_date}"
            )
            data = request_json_with_retry(url)
            if (data.get("totalGames") or 0) > 0:
                season_cache[cache_key] = True
                return True
    except Exception:
        pass

    season_cache[cache_key] = False
    return False


# -----------------------------
# Generic value helpers
# -----------------------------
def _clean_text(value):
    text = " ".join(str(value or "").split()).strip()
    return text or None


def _get_in(data, *keys):
    current = data
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _safe_int(value):
    try:
        if value is None or value == "":
            return None
        return int(float(value))
    except Exception:
        return None


def _safe_float(value):
    try:
        if value is None or value == "":
            return None
        return float(value)
    except Exception:
        return None


def _coerce_int(value):
    try:
        return int(value)
    except Exception:
        return None


def is_pitcher(person: dict) -> bool:
    position = person.get("primaryPosition") or {}
    code = str(position.get("code") or "").upper()
    name = str(position.get("name") or "").lower()
    abbreviation = str(position.get("abbreviation") or "").upper()
    return code == "1" or abbreviation == "P" or "pitcher" in name


def school_clause(person: dict):
    education = person.get("education")
    if not isinstance(education, dict):
        return None
    colleges = education.get("colleges") or []
    highschools = education.get("highschools") or []
    school = None
    if colleges and isinstance(colleges[0], dict):
        school = _clean_text(colleges[0].get("name"))
    if not school and highschools and isinstance(highschools[0], dict):
        school = _clean_text(highschools[0].get("name"))
    return f"went to {school}" if school else None


def draft_clause(person: dict):
    drafts = person.get("drafts") or []
    pick = drafts[0] if drafts and isinstance(drafts[0], dict) else None
    if not pick:
        return None
    team = _clean_text(_get_in(pick, "team", "name"))
    year = _safe_int(pick.get("year"))
    round_num = _clean_text(pick.get("pickRound"))
    overall = _safe_int(pick.get("pickNumber"))
    if team and year and round_num and overall:
        return f"drafted by {team} ({year} R{round_num}/{overall})"
    if team and year:
        return f"drafted by {team} ({year})"
    return None


# -----------------------------
# Transaction field helpers
# -----------------------------
def txn_id(tx: dict) -> int:
    try:
        return int(tx.get("id", 0))
    except Exception:
        return 0


def txn_date(tx: dict) -> str:
    value = (
        tx.get("effectiveDate")
        or tx.get("transactionDate")
        or tx.get("date")
        or ""
    )
    return str(value)[:10]


def txn_date_obj(tx: dict):
    try:
        return datetime.strptime(txn_date(tx), "%Y-%m-%d").date()
    except Exception:
        return None


def txn_desc(tx: dict) -> str:
    description = tx.get("description") or tx.get("typeDesc") or ""
    return " ".join(str(description).split()).strip() or "Transaction"


def txn_typecode(tx: dict) -> str:
    return (tx.get("typeCode") or "").strip()


def extract_tx_player_id(tx: dict):
    person = tx.get("person") or {}
    person_id = _coerce_int(person.get("id")) if isinstance(person, dict) else None
    if person_id is None:
        person_id = _coerce_int(tx.get("playerId") or tx.get("player_id"))
    return person_id


def transaction_ids_represented_in_post(text: str, txns) -> set[int]:
    """Return transaction IDs visibly represented by one rendered post.

    Matching requires both the transaction date and description. The short
    prefix fallback covers the rare grouped line trimmed to the 300-char post
    limit without confusing same-description moves from different dates.
    """
    hay = str(text or "")
    represented = set()
    for tx in txns or []:
        txid = txn_id(tx)
        if txid <= 0:
            continue
        date_text = txn_date(tx)
        description = txn_desc(tx).strip()
        if not date_text or date_text not in hay or not description:
            continue
        if description in hay:
            represented.add(txid)
            continue
        if len(description) >= 80 and description[:80] in hay:
            represented.add(txid)
    return represented


def player_url(player_id: int) -> str:
    return f"https://www.mlb.com/player/{player_id}"


def build_base_tx_text(tx: dict):
    return "\n".join([txn_date(tx), f"- {txn_desc(tx)}"])


def is_trade(tx: dict) -> bool:
    type_code = txn_typecode(tx).upper()
    if type_code in {"TR", "TRADE"}:
        return True
    return " traded " in (" " + txn_desc(tx).lower() + " ")


def extract_trade_incoming_players(tx: dict):
    """Return players whose trade destination is the Giants."""
    incoming = []

    def add_player(person_id, name):
        person_id = _coerce_int(person_id)
        if person_id is None:
            return
        player_name = " ".join(str(name or "").split()).strip()
        if not player_name:
            player_name = f"Player {person_id}"
        incoming.append({"id": person_id, "name": player_name})

    candidates = []
    # Real StatsAPI trade feeds emit one top-level transaction row per player
    # leg, with person/fromTeam/toTeam on the row itself. Older/nested shapes
    # are still supported below.
    if (
        isinstance(tx.get("person"), dict)
        or isinstance(tx.get("toTeam"), dict)
        or isinstance(tx.get("fromTeam"), dict)
    ):
        candidates.append(tx)
    if isinstance(tx.get("players"), list):
        candidates.extend(tx["players"])
    if isinstance(tx.get("playerTransactions"), list):
        candidates.extend(tx["playerTransactions"])
    if isinstance(tx.get("playerTransaction"), list):
        candidates.extend(tx["playerTransaction"])

    for item in candidates:
        if not isinstance(item, dict):
            continue
        to_team = item.get("toTeam") or item.get("teamTo") or item.get("to") or {}
        to_team_id = (
            (to_team.get("id") if isinstance(to_team, dict) else None)
            or item.get("toTeamId")
            or item.get("teamToId")
        )
        if _coerce_int(to_team_id) != TEAM_ID:
            continue

        person = item.get("person") or item.get("player") or {}
        person_id = (
            (person.get("id") if isinstance(person, dict) else None)
            or item.get("playerId")
            or item.get("personId")
        )
        name = (
            (person.get("fullName") if isinstance(person, dict) else None)
            or item.get("playerName")
            or item.get("name")
        )
        add_player(person_id, name)

    seen = set()
    unique = []
    for player in incoming:
        if player["id"] in seen:
            continue
        seen.add(player["id"])
        unique.append(player)
    return unique


def collapse_trade_records(txns):
    """Collapse StatsAPI's one-row-per-leg trade shape into one transaction.

    Real trade records share a transaction ID and description but repeat once
    for each player/cash leg. Merging their structured legs preserves incoming
    player links while preventing duplicate trade lines.
    """
    collapsed = []
    trade_groups = {}

    for tx in sorted(txns, key=txn_id):
        if not is_trade(tx):
            collapsed.append(tx)
            continue

        trade_id = txn_id(tx)
        key = ("id", trade_id) if trade_id > 0 else (
            "fallback", txn_date(tx), txn_desc(tx).strip().lower()
        )
        merged = trade_groups.get(key)
        if merged is None:
            merged = dict(tx)
            merged["playerTransactions"] = []
            trade_groups[key] = merged
            collapsed.append(merged)

        legs = merged["playerTransactions"]
        if (
            isinstance(tx.get("person"), dict)
            or isinstance(tx.get("toTeam"), dict)
            or isinstance(tx.get("fromTeam"), dict)
        ):
            legs.append({
                "person": tx.get("person"),
                "fromTeam": tx.get("fromTeam"),
                "toTeam": tx.get("toTeam"),
            })

        for field in ("players", "playerTransactions", "playerTransaction"):
            nested = tx.get(field)
            if isinstance(nested, list):
                legs.extend(item for item in nested if isinstance(item, dict))

    return collapsed


# -----------------------------
# Generic grouped-post construction
# -----------------------------
def split_oversized_date_block(block: str, max_len=MAX_POST_LEN):
    if len(block) <= max_len:
        return [block]

    lines = block.split("\n")
    if len(lines) < 2 or not lines[0] or not lines[0][0].isdigit():
        return [block[: max_len - 1] + "…"]

    header = lines[0]
    entries = []
    current_entry = []
    for line in lines[1:]:
        if line.startswith("- "):
            if current_entry:
                entries.append(current_entry)
            current_entry = [line]
        elif current_entry:
            current_entry.append(line)
        else:
            current_entry = [line]
    if current_entry:
        entries.append(current_entry)

    if not entries:
        return [block[: max_len - 1] + "…"]

    chunks = []
    current_lines = [header]
    for entry in entries:
        candidate = "\n".join(current_lines + entry)
        if len(candidate) <= max_len:
            current_lines.extend(entry)
            continue

        if len(current_lines) > 1:
            chunks.append("\n".join(current_lines))
            current_lines = [header]

        single = "\n".join(current_lines + entry)
        if len(single) <= max_len:
            current_lines.extend(entry)
        else:
            entry_text = "\n".join(entry)
            allowed = max_len - len(header) - 2
            trimmed = entry_text[: max(0, allowed - 1)] + "…"
            chunks.append(header + "\n" + trimmed)

    if len(current_lines) > 1:
        chunks.append("\n".join(current_lines))
    return chunks


def pack_posts(blocks, max_len=MAX_POST_LEN):
    expanded = []
    for block in blocks:
        expanded.extend(split_oversized_date_block(block, max_len=max_len))

    posts = []
    current = ""
    for block in expanded:
        candidate = block if not current else current + "\n" + block
        if len(candidate) <= max_len:
            current = candidate
            continue

        if current:
            posts.append(current)
        if len(block) > max_len:
            posts.append(block[: max_len - 1] + "…")
            current = ""
        else:
            current = block

    if current:
        posts.append(current)
    return posts


def build_date_group_blocks(txns):
    txns = collapse_trade_records(txns)
    blocks = []
    current_date = None
    current_lines = []

    def flush():
        nonlocal current_date, current_lines
        if current_date and current_lines:
            blocks.append(current_date + "\n" + "\n".join(current_lines))
        current_date = None
        current_lines = []

    for tx in txns:
        date_text = txn_date(tx)
        description = txn_desc(tx)

        if current_date is None:
            current_date = date_text
        elif date_text != current_date:
            flush()
            current_date = date_text

        if is_trade(tx):
            incoming = extract_trade_incoming_players(tx)
            current_lines.append(f"- Trade: {description}")
            for player in incoming:
                current_lines.append(
                    f"  • {player['name']} {player_url(player['id'])}"
                )
        else:
            current_lines.append(f"- {description}")

    flush()
    return blocks


# -----------------------------
# Bluesky
# -----------------------------
def bsky_create_session(identifier: str, app_password: str):
    url = "https://bsky.social/xrpc/com.atproto.server.createSession"
    response = requests.post(
        url,
        json={"identifier": identifier, "password": app_password},
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    return response.json()


def build_link_facets(text: str):
    facets = []
    for match in re.finditer(r"https?://\S+", text):
        raw_url = match.group(0)
        url = raw_url.rstrip(").,;!?:")
        if not url:
            continue

        char_start = match.start()
        char_end = char_start + len(url)
        byte_start = len(text[:char_start].encode("utf-8"))
        byte_end = len(text[:char_end].encode("utf-8"))
        facets.append({
            "index": {"byteStart": byte_start, "byteEnd": byte_end},
            "features": [
                {"$type": "app.bsky.richtext.facet#link", "uri": url}
            ],
        })
    return facets


def bsky_post(access_jwt: str, did: str, text: str):
    url = "https://bsky.social/xrpc/com.atproto.repo.createRecord"
    record = {
        "$type": "app.bsky.feed.post",
        "text": text,
        "createdAt": datetime.utcnow().isoformat(timespec="seconds") + "Z",
    }
    facets = build_link_facets(text)
    if facets:
        record["facets"] = facets

    payload = {
        "repo": did,
        "collection": "app.bsky.feed.post",
        "record": record,
    }
    response = requests.post(
        url,
        json=payload,
        headers={"Authorization": f"Bearer {access_jwt}"},
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    return response.json()


def bsky_verify_record(access_jwt: str, did: str, uri: str):
    if not uri or not isinstance(uri, str):
        return None, None
    try:
        rkey = uri.rsplit("/", 1)[-1].strip()
        if not rkey:
            return None, None
        response = requests.get(
            "https://bsky.social/xrpc/com.atproto.repo.getRecord",
            params={
                "repo": did,
                "collection": "app.bsky.feed.post",
                "rkey": rkey,
            },
            headers={"Authorization": f"Bearer {access_jwt}"},
            timeout=REQUEST_TIMEOUT,
        )
        return response.status_code, (response.text[:800] if response.text else "")
    except Exception as exc:
        return None, f"verify exception: {exc}"


# -----------------------------
# Persistent state
# -----------------------------
def _atomic_write_text(path: str, content: str):
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        handle.write(content)
    os.replace(temporary, path)


def load_last_id(path: str = LAST_ID_PATH) -> int:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            value = (handle.read() or "").strip()
            return int(value) if value else 0
    except Exception:
        return 0


def save_last_id(last_id: int, path: str = LAST_ID_PATH):
    _atomic_write_text(path, str(int(last_id)))


def load_seen_ids(path: str = SEEN_IDS_PATH) -> set[int]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            values = [line.strip() for line in handle if line.strip()]
        return {int(value) for value in values}
    except Exception:
        return set()


def save_seen_ids(ids: set[int], path: str = SEEN_IDS_PATH, keep_last=5000):
    ordered = sorted(ids)
    if keep_last and len(ordered) > keep_last:
        ordered = ordered[-keep_last:]
    text = "\n".join(str(value) for value in ordered)
    if ordered:
        text += "\n"
    _atomic_write_text(path, text)
