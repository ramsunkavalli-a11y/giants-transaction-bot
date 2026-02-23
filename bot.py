import os
import re
import time
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# -----------------------------
# Config
# -----------------------------
TEAM_ID = 137  # SF Giants
MAX_POST_LEN = 300  # Bluesky post text limit
REQUEST_TIMEOUT = 20
REQUEST_RETRIES = 3
SEASON_CACHE_KEY = "_season_mode"

# Delay between posts (helps reduce “spammy burst” behavior / racey feed rendering)
# Optional override via env POST_DELAY_SECONDS=2
_POST_DELAY_ENV = os.getenv("POST_DELAY_SECONDS", "2").strip()
try:
    POST_DELAY_SECONDS = float(_POST_DELAY_ENV) if _POST_DELAY_ENV else 2.0
except Exception:
    POST_DELAY_SECONDS = 2.0

PEOPLE_BASE_HYDRATE = "currentTeam,education,draft,rosterEntries,transactions"
PEOPLE_STATS_YEAR_BY_YEAR_HYDRATE = "stats(group=[hitting,pitching],type=[yearByYear])"
PEOPLE_STATS_BY_DATE_RANGE_HYDRATE = (
    "stats(group=[hitting,pitching],type=[byDateRange],startDate={start},endDate={end},season={season})"
)
PEOPLE_STATS_SEASON_HYDRATE = "stats(group=[hitting,pitching],type=[season,seasonAdvanced],season={season})"

LEVEL_RANK = {"MLB": 7, "AAA": 6, "AA": 5, "A+": 4, "A": 3, "Rk": 2, "CPX": 1, "DSL": 1}

# ---- Hard cutoff: do NOT post anything before this date ----
# Optional override via env TXN_CUTOFF_DATE=YYYY-MM-DD
_CUTOFF_ENV = os.getenv("TXN_CUTOFF_DATE", "2026-02-21").strip()
TXN_CUTOFF_DATE = datetime.strptime(_CUTOFF_ENV, "%Y-%m-%d").date()

# ---- Persist state next to this script (avoids cwd/path surprises) ----
STATE_DIR = os.path.dirname(os.path.abspath(__file__))
LAST_ID_PATH = os.path.join(STATE_DIR, "last_id.txt")
SEEN_IDS_PATH = os.path.join(STATE_DIR, "seen_ids.txt")


# -----------------------------
# Helpers
# -----------------------------
def mlb_transactions_url(days_back=120):
    end_date = datetime.utcnow().date()
    start_date = end_date - timedelta(days=days_back)

    # Avoid fetching earlier than cutoff (and avoid invalid ranges if run before cutoff)
    if start_date < TXN_CUTOFF_DATE:
        start_date = TXN_CUTOFF_DATE
    if start_date > end_date:
        start_date = end_date

    return (
        "https://statsapi.mlb.com/api/v1/transactions"
        f"?teamId={TEAM_ID}&startDate={start_date}&endDate={end_date}"
    )


def bsky_create_session(identifier: str, app_password: str):
    url = "https://bsky.social/xrpc/com.atproto.server.createSession"
    r = requests.post(url, json={"identifier": identifier, "password": app_password}, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return r.json()


def request_json_with_retry(url: str, *, headers=None, timeout=REQUEST_TIMEOUT, retries=REQUEST_RETRIES):
    last_err = None
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=headers, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except Exception as exc:
            last_err = exc
            if attempt < retries - 1:
                time.sleep(0.5 * (2 ** attempt))
    raise last_err


def is_signing_transaction(tx: dict) -> bool:
    tc = (tx.get("typeCode") or "").strip().upper()
    td = (tx.get("typeDesc") or "").strip().lower()
    desc = (tx.get("description") or "").strip().lower()
    hay = f"{td} {desc}"

    negative = ("assigned", "designated", "released", "traded", "waiver", "optioned", "outright")
    if any(w in hay for w in negative):
        return False

    if tc in {"SFA", "SMC", "S", "SC", "FA"}:
        return True

    positive = ("signed", "contract selected", "minor league contract", "major league contract", "free agent signing")
    return any(w in hay for w in positive)


def is_optioned_transaction(tx: dict) -> bool:
    tc = (tx.get("typeCode") or "").strip().upper()
    hay = f"{tx.get('typeDesc','')} {tx.get('description','')}".lower()
    if "signed" in hay:
        return False
    return tc in {"OPT"} or "optioned" in hay


def is_recalled_transaction(tx: dict) -> bool:
    tc = (tx.get("typeCode") or "").strip().upper()
    hay = f"{tx.get('typeDesc','')} {tx.get('description','')}".lower()
    return tc in {"RCL", "REC"} or "recalled" in hay


def is_dfa_transaction(tx: dict) -> bool:
    tc = (tx.get("typeCode") or "").strip().upper()
    hay = f"{tx.get('typeDesc','')} {tx.get('description','')}".lower()
    return tc in {"DFA"} or "designated for assignment" in hay


def is_contract_selected_transaction(tx: dict) -> bool:
    tc = (tx.get("typeCode") or "").strip().upper()
    hay = f"{tx.get('typeDesc','')} {tx.get('description','')}".lower()
    if "assigned" in hay and "contract selected" not in hay:
        return False
    return tc in {"SC", "CS"} or "contract selected" in hay


def _la_now():
    try:
        return datetime.now(ZoneInfo("America/Los_Angeles"))
    except Exception:
        return datetime.utcnow()


def in_season_mode(now_la=None, season_cache=None):
    """
    Determine whether we're in regular season or postseason.

    Uses:
    - seasons endpoint first (preferred)
    - schedule endpoint as fallback heuristic
    """
    now_la = now_la or _la_now()
    season_cache = season_cache if season_cache is not None else {}
    cache_key = (SEASON_CACHE_KEY, now_la.date().isoformat())
    if cache_key in season_cache:
        return season_cache[cache_key]

    season_year = now_la.year
    for y in (season_year, season_year - 1, season_year + 1):
        try:
            data = request_json_with_retry(f"https://statsapi.mlb.com/api/v1/seasons?sportId=1&season={y}")
            seasons = data.get("seasons") or []
            if not seasons:
                continue
            info = seasons[0]
            rs = info.get("regularSeasonStartDate")
            re_ = info.get("regularSeasonEndDate")
            ps = info.get("postSeasonStartDate") or info.get("postseasonStartDate") or rs
            pe = info.get("postSeasonEndDate") or info.get("postseasonEndDate") or re_
            if rs and re_:
                start = datetime.strptime(ps[:10], "%Y-%m-%d").date() if ps else datetime.strptime(rs[:10], "%Y-%m-%d").date()
                end = datetime.strptime(pe[:10], "%Y-%m-%d").date() if pe else datetime.strptime(re_[:10], "%Y-%m-%d").date()
                if start <= now_la.date() <= end:
                    season_cache[cache_key] = True
                    return True
        except Exception:
            pass

    try:
        d0 = (now_la.date() - timedelta(days=7)).isoformat()
        d1 = (now_la.date() + timedelta(days=7)).isoformat()
        for gt in ("R", "P"):
            url = f"https://statsapi.mlb.com/api/v1/schedule?sportId=1&gameType={gt}&startDate={d0}&endDate={d1}"
            data = request_json_with_retry(url)
            if (data.get("totalGames") or 0) > 0:
                season_cache[cache_key] = True
                return True
    except Exception:
        pass

    season_cache[cache_key] = False
    return False


def _clean_text(v):
    txt = " ".join(str(v or "").split()).strip()
    return txt or None


def _get_in(d, *keys):
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur


def _safe_int(v):
    try:
        if v is None or v == "":
            return None
        return int(float(v))
    except Exception:
        return None


def _safe_float(v):
    try:
        if v is None or v == "":
            return None
        return float(v)
    except Exception:
        return None


def today_pacific_date():
    try:
        return datetime.now(ZoneInfo("America/Los_Angeles")).date()
    except Exception:
        return datetime.utcnow().date()


def calculate_age_clause(birth_date_str: str):
    if not birth_date_str:
        return None
    try:
        b = datetime.strptime(str(birth_date_str)[:10], "%Y-%m-%d").date()
    except Exception:
        return None
    t = today_pacific_date()
    months = (t.year - b.year) * 12 + (t.month - b.month)
    if t.day < b.day:
        months -= 1
    if months < 0:
        return None
    years = months // 12
    rem_months = months % 12
    return f"is {years}y {rem_months}m old"


def get_player_payload(person_id: int, hydrate: str, cache: dict):
    key = (person_id, hydrate)
    if key in cache:
        return cache[key]
    url = f"https://statsapi.mlb.com/api/v1/people/{person_id}?hydrate={hydrate}"
    try:
        data = request_json_with_retry(url)
        person = (data.get("people") or [None])[0] or {}
    except Exception:
        person = {}
    cache[key] = person
    return person


def get_player_details(person_id: int, cache: dict):
    return get_player_payload(person_id, PEOPLE_BASE_HYDRATE, cache)


def get_player_year_by_year(person_id: int, cache: dict):
    return get_player_payload(person_id, PEOPLE_STATS_YEAR_BY_YEAR_HYDRATE, cache)


def get_player_by_date_range(person_id: int, season_year: int, start_date: str, end_date: str, cache: dict):
    hydrate = PEOPLE_STATS_BY_DATE_RANGE_HYDRATE.format(start=start_date, end=end_date, season=season_year)
    return get_player_payload(person_id, hydrate, cache)


def get_player_season_stats(person_id: int, season_year: int, cache: dict):
    hydrate = PEOPLE_STATS_SEASON_HYDRATE.format(season=season_year)
    return get_player_payload(person_id, hydrate, cache)


def is_pitcher(person: dict) -> bool:
    pp = person.get("primaryPosition") or {}
    code = str(pp.get("code") or "").upper()
    name = str(pp.get("name") or "").lower()
    abbr = str(pp.get("abbreviation") or "").upper()
    if code == "1" or abbr == "P" or "pitcher" in name:
        return True
    return False


def level_token_from_split(split: dict):
    raw = " ".join(
        str(x or "")
        for x in [
            _get_in(split, "level", "name"),
            _get_in(split, "sport", "name"),
            _get_in(split, "league", "name"),
            _get_in(split, "league", "abbreviation"),
        ]
    ).lower()
    mapping = [
        ("major", "MLB"),
        ("triple", "AAA"),
        ("double", "AA"),
        ("high-a", "A+"),
        ("high a", "A+"),
        ("single-a", "A"),
        ("single a", "A"),
        ("rookie", "Rk"),
        ("dsl", "DSL"),
        ("complex", "CPX"),
    ]
    for key, token in mapping:
        if key in raw:
            return token
    abbr = str(_get_in(split, "league", "abbreviation") or "").upper()
    if abbr in {"MLB", "AAA", "AA", "A+", "A", "DSL"}:
        return abbr
    return None


def has_appearances(split: dict, pitcher: bool):
    stat = split.get("stat") or {}
    if pitcher:
        gp = _safe_int(stat.get("gamesPitched"))
        ip = _safe_float(stat.get("inningsPitched"))
        return (gp or 0) > 0 or (ip or 0) > 0
    pa = _safe_int(stat.get("plateAppearances"))
    ab = _safe_int(stat.get("atBats"))
    g = _safe_int(stat.get("gamesPlayed"))
    return (pa or 0) > 0 or (ab or 0) > 0 or (g or 0) > 0


def _extract_group_splits(stats_blocks, target_group: str):
    out = []
    for block in stats_blocks or []:
        if (block.get("group") or {}).get("displayName", "").lower() != target_group:
            continue
        for split in block.get("splits") or []:
            if isinstance(split, dict):
                out.append(split)
    return out


def _appearance_volume(split: dict, pitcher: bool):
    st = split.get("stat") or {}
    if pitcher:
        return _safe_float(st.get("inningsPitched")) or 0.0
    return float(
        _safe_int(st.get("plateAppearances"))
        or _safe_int(st.get("atBats"))
        or _safe_int(st.get("gamesPlayed"))
        or 0
    )


def select_last_level_appeared(stats_blocks, pitcher: bool):
    target_group = "pitching" if pitcher else "hitting"
    candidates = []
    for split in _extract_group_splits(stats_blocks, target_group):
        if not has_appearances(split, pitcher):
            continue
        season = _safe_int(split.get("season")) or 0
        token = level_token_from_split(split)
        token_rank = LEVEL_RANK.get(token, 0)
        vol = _appearance_volume(split, pitcher)
        team_name = _clean_text(_get_in(split, "team", "name") or _get_in(split, "organization", "name"))
        team_id = _safe_int(_get_in(split, "team", "id") or _get_in(split, "organization", "id")) or 0
        candidates.append((season, token_rank, vol, team_id, team_name or "", split))
    if not candidates:
        return None
    candidates.sort(key=lambda x: (x[0], x[1], x[2], x[3], x[4]), reverse=True)
    split = candidates[0][5]
    return {
        "seasonYear": _safe_int(split.get("season")),
        "orgName": _clean_text(_get_in(split, "team", "name") or _get_in(split, "organization", "name")),
        "levelToken": level_token_from_split(split),
        "splitStats": split.get("stat") or {},
        "split": split,
    }


def select_highest_level_appeared(stats_blocks, pitcher: bool):
    target_group = "pitching" if pitcher else "hitting"
    candidates = []
    for split in _extract_group_splits(stats_blocks, target_group):
        if not has_appearances(split, pitcher):
            continue
        token = level_token_from_split(split)
        token_rank = LEVEL_RANK.get(token, 0)
        vol = _appearance_volume(split, pitcher)
        team_name = _clean_text(_get_in(split, "team", "name") or _get_in(split, "organization", "name")) or ""
        team_id = _safe_int(_get_in(split, "team", "id") or _get_in(split, "organization", "id")) or 0
        candidates.append((token_rank, vol, team_id, team_name, split))
    if not candidates:
        return None
    candidates.sort(key=lambda x: (x[0], x[1], x[2], x[3]), reverse=True)
    split = candidates[0][4]
    return {
        "orgName": _clean_text(_get_in(split, "team", "name") or _get_in(split, "organization", "name")),
        "levelToken": level_token_from_split(split),
        "splitStats": split.get("stat") or {},
        "split": split,
        "seasonYear": _safe_int(split.get("season")),
    }


def select_last_level_split(stats_blocks, pitcher: bool):
    return select_last_level_appeared(stats_blocks, pitcher)


def format_stat_clause(stat: dict, pitcher: bool):
    if not stat:
        return None
    if pitcher:
        parts = []
        ip = _safe_float(stat.get("inningsPitched"))
        so = _safe_int(stat.get("strikeOuts"))
        bb = _safe_int(stat.get("baseOnBalls"))
        era = _safe_float(stat.get("era"))
        if ip is not None:
            ip_txt = str(int(ip)) if float(ip).is_integer() else f"{ip:.1f}".rstrip("0").rstrip(".")
            parts.append(f"{ip_txt}IP")
        if so is not None and bb is not None:
            parts.append(f"{so}/{bb}K/BB")
        elif so is not None:
            parts.append(f"{so}K")
        if era is not None:
            parts.append(f"{era:.2f}ERA")
        return " ".join(parts) if parts else None

    parts = []
    pa = _safe_int(stat.get("plateAppearances"))
    avg = _safe_float(stat.get("avg"))
    obp = _safe_float(stat.get("obp"))
    slg = _safe_float(stat.get("slg"))
    k_pct = _safe_float(stat.get("strikeoutPercentage"))
    bb_pct = _safe_float(stat.get("baseOnBallsPercentage"))
    if pa is not None:
        parts.append(f"{pa}PA")
    slash = []
    for v in (avg, obp, slg):
        if v is None:
            slash = []
            break
        slash.append(f"{v:.3f}")
    if slash:
        parts.append("/".join(slash))
    if k_pct is not None:
        parts.append(f"{k_pct:.1f}%K")
    if bb_pct is not None:
        parts.append(f"{bb_pct:.1f}%BB")
    return " ".join(parts) if parts else None


def shorten_stat_clause(stat_clause: str, pitcher: bool):
    if not stat_clause:
        return None
    parts = stat_clause.split()
    if pitcher:
        parts = [p for p in parts if "K/BB" not in p]
    else:
        parts = [p for p in parts if not p.endswith("%K") and not p.endswith("%BB")]
    out = " ".join(parts).strip()
    return out or None


def school_clause(person: dict):
    edu = person.get("education")
    if not isinstance(edu, dict):
        return None
    college = edu.get("colleges") or []
    hs = edu.get("highschools") or []
    school = None
    if college and isinstance(college[0], dict):
        school = _clean_text(college[0].get("name"))
    if not school and hs and isinstance(hs[0], dict):
        school = _clean_text(hs[0].get("name"))
    return f"went to {school}" if school else None


def draft_clause(person: dict):
    picks = person.get("drafts") or []
    pick = picks[0] if picks and isinstance(picks[0], dict) else None
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


def format_bio_line(name: str, clauses: dict):
    parts = [name]
    for key in ["age", "school", "draft", "last", "stat"]:
        val = clauses.get(key)
        if val:
            parts.append(val)
    return ", ".join(parts)


def build_signing_enrichment(person: dict, stats_blocks=None):
    name = _clean_text(person.get("fullName")) or "Player"
    age = calculate_age_clause(person.get("birthDate"))
    school = school_clause(person)
    draft = draft_clause(person)
    pitcher = is_pitcher(person)
    selected = select_last_level_appeared(stats_blocks if stats_blocks is not None else (person.get("stats") or []), pitcher=pitcher)
    last_full = None
    last_tiny = None
    stat_clause = None
    if selected:
        team = _clean_text(selected.get("orgName"))
        level = _clean_text(selected.get("levelToken"))
        if team and level:
            last_full = f"last: {team} {level}"
        elif team:
            last_full = f"last: {team}"
        elif level:
            last_full = f"last: {level}"
        if level:
            last_tiny = f"last: {level}"
        elif team:
            last_tiny = f"last: {team}"
        stat_clause = format_stat_clause(selected.get("splitStats") or {}, pitcher=pitcher)

    return {
        "name": name,
        "pitcher": pitcher,
        "clauses": {
            "age": age,
            "school": school,
            "draft": draft,
            "last": last_full,
            "last_tiny": last_tiny,
            "stat": stat_clause,
        },
    }


def build_signing_post(base_text: str, player_link: str, enrichment: dict, max_len=MAX_POST_LEN):
    name = enrichment.get("name") or "Player"
    clauses = dict(enrichment.get("clauses") or {})
    pitcher = bool(enrichment.get("pitcher"))

    def render(active):
        bio = format_bio_line(name, active)
        return f"{base_text}\nBio: {bio}\n{player_link}" if bio else f"{base_text}\n{player_link}"

    active = {
        "age": clauses.get("age"),
        "school": clauses.get("school"),
        "draft": clauses.get("draft"),
        "last": clauses.get("last"),
        "stat": clauses.get("stat"),
    }
    post = render(active)
    if len(post) <= max_len:
        return post

    for key in ["school", "draft"]:
        active[key] = None
        post = render(active)
        if len(post) <= max_len:
            return post

    if active.get("last"):
        active["last"] = clauses.get("last_tiny")
        post = render(active)
        if len(post) <= max_len:
            return post
        active["last"] = None
        post = render(active)
        if len(post) <= max_len:
            return post

    short_stat = shorten_stat_clause(active.get("stat"), pitcher=pitcher)
    if short_stat and short_stat != active.get("stat"):
        active["stat"] = short_stat
        post = render(active)
        if len(post) <= max_len:
            return post

    active["stat"] = None
    post = render(active)
    if len(post) <= max_len:
        return post

    active["age"] = None
    post = render(active)
    if len(post) <= max_len:
        return post

    min_post = f"{base_text}\n{player_link}"
    if len(min_post) >= max_len:
        return min_post[: max_len - 1] + "…"

    budget = max_len - len(min_post) - len("\nBio: ")
    bio_text = format_bio_line(name, {k: v for k, v in active.items() if v})
    if budget > 1 and bio_text:
        bio_text = bio_text[: budget - 1].rstrip() + "…"
        return f"{base_text}\nBio: {bio_text}\n{player_link}"
    return min_post


def extract_tx_player_id(tx: dict):
    person = tx.get("person") or {}
    pid = _coerce_int(person.get("id")) if isinstance(person, dict) else None
    if pid is None:
        pid = _coerce_int(tx.get("playerId") or tx.get("player_id"))
    return pid


def build_base_tx_text(tx: dict):
    return "\n".join([txn_date(tx), f"- {txn_desc(tx)}"])


def build_post_with_optional_stats(base_text: str, player_link: str, stats_line: str, pitcher: bool, max_len=MAX_POST_LEN):
    if stats_line:
        post = f"{base_text}\nStats: {stats_line}\n{player_link}"
        if len(post) <= max_len:
            return post

        short_stats = shorten_stat_clause(stats_line, pitcher=pitcher)
        if short_stats:
            post = f"{base_text}\nStats: {short_stats}\n{player_link}"
            if len(post) <= max_len:
                return post

    min_post = f"{base_text}\n{player_link}"
    if len(min_post) <= max_len:
        return min_post
    allowed_link = max(0, max_len - len(base_text) - 1)
    return f"{base_text}\n{player_link[:allowed_link]}"


def build_special_transaction_post(tx: dict, category: str, player_cache: dict, now_la):
    base_text = build_base_tx_text(tx)
    pid = extract_tx_player_id(tx)
    if not pid:
        return base_text

    link = player_url(pid)
    details = get_player_details(pid, player_cache)
    pitcher = is_pitcher(details)
    stats_line = None
    season_year = now_la.year

    if category in {"optioned", "recalled"}:
        end = now_la.date()
        start = end - timedelta(days=13)
        payload = get_player_by_date_range(pid, season_year, start.isoformat(), end.isoformat(), player_cache)
        sel = select_highest_level_appeared(payload.get("stats") or [], pitcher=pitcher)
        if sel:
            stats_line = format_stat_clause(sel.get("splitStats") or {}, pitcher=pitcher)

    elif category == "dfa":
        payload = get_player_season_stats(pid, season_year, player_cache)
        sel = select_last_level_appeared(payload.get("stats") or [], pitcher=pitcher)
        if sel:
            core = format_stat_clause(sel.get("splitStats") or {}, pitcher=pitcher)
            level = _clean_text(sel.get("levelToken"))
            if core and level:
                stats_line = f"{level} {core}"
            else:
                stats_line = core or (f"{level}" if level else None)

    elif category == "contract_selected":
        payload = get_player_season_stats(pid, season_year, player_cache)
        sel = select_highest_level_appeared(payload.get("stats") or [], pitcher=pitcher)
        if sel:
            core = format_stat_clause(sel.get("splitStats") or {}, pitcher=pitcher)
            level = _clean_text(sel.get("levelToken"))
            if core and level:
                stats_line = f"{level} {core}"
            else:
                stats_line = core or (f"{level}" if level else None)

    return build_post_with_optional_stats(base_text, link, stats_line, pitcher=pitcher, max_len=MAX_POST_LEN)


def build_link_facets(text: str):
    """Create Bluesky link facets so plain URLs are clickable in clients."""
    facets = []
    for m in re.finditer(r"https?://\S+", text):
        raw_url = m.group(0)
        url = raw_url.rstrip(").,;!?:")
        if not url:
            continue

        char_start = m.start()
        char_end = char_start + len(url)
        byte_start = len(text[:char_start].encode("utf-8"))
        byte_end = len(text[:char_end].encode("utf-8"))

        facets.append(
            {
                "index": {"byteStart": byte_start, "byteEnd": byte_end},
                "features": [{"$type": "app.bsky.richtext.facet#link", "uri": url}],
            }
        )
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

    payload = {"repo": did, "collection": "app.bsky.feed.post", "record": record}
    r = requests.post(url, json=payload, headers={"Authorization": f"Bearer {access_jwt}"}, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return r.json()


def bsky_verify_record(access_jwt: str, did: str, uri: str):
    """
    High-signal check:
    Immediately try to fetch the created record back from the server.
    - If this succeeds (200), the post exists server-side at that moment.
    - If it fails (404/400), something odd happened (or it was removed extremely fast).
    """
    if not uri or not isinstance(uri, str):
        return None, None

    try:
        # uri shape: at://did/app.bsky.feed.post/<rkey>
        rkey = uri.rsplit("/", 1)[-1].strip()
        if not rkey:
            return None, None

        get_url = "https://bsky.social/xrpc/com.atproto.repo.getRecord"
        r = requests.get(
            get_url,
            params={"repo": did, "collection": "app.bsky.feed.post", "rkey": rkey},
            headers={"Authorization": f"Bearer {access_jwt}"},
            timeout=REQUEST_TIMEOUT,
        )
        return r.status_code, (r.text[:800] if r.text else "")
    except Exception as exc:
        return None, f"verify exception: {exc}"


# -----------------------------
# State file helpers
# -----------------------------
def _atomic_write_text(path: str, content: str):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, path)


def load_last_id(path: str = LAST_ID_PATH) -> int:
    """Numeric transaction id checkpoint. Returns 0 if missing/blank."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            v = (f.read() or "").strip()
            return int(v) if v else 0
    except FileNotFoundError:
        return 0
    except Exception:
        return 0


def save_last_id(last_id: int, path: str = LAST_ID_PATH):
    _atomic_write_text(path, str(int(last_id)))


def load_seen_ids(path: str = SEEN_IDS_PATH) -> set[int]:
    """
    Rolling set of transaction IDs already handled.
    Returns an empty set if file is missing/invalid.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = [line.strip() for line in f if line.strip()]
        return {int(v) for v in raw}
    except FileNotFoundError:
        return set()
    except Exception:
        return set()


def save_seen_ids(ids: set[int], path: str = SEEN_IDS_PATH, keep_last=5000):
    ordered = sorted(ids)
    if keep_last and len(ordered) > keep_last:
        ordered = ordered[-keep_last:]
    txt = "\n".join(str(v) for v in ordered)
    if ordered:
        txt += "\n"
    _atomic_write_text(path, txt)


# -----------------------------
# StatsAPI field helpers
# -----------------------------
def txn_id(t: dict) -> int:
    try:
        return int(t.get("id", 0))
    except Exception:
        return 0


def txn_date(t: dict) -> str:
    d = t.get("effectiveDate") or t.get("transactionDate") or t.get("date") or ""
    return str(d)[:10]  # YYYY-MM-DD


def txn_date_obj(t: dict):
    """
    Parse a transaction's effective date into a date() object.
    Returns None if missing/invalid.
    """
    s = txn_date(t)
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except Exception:
        return None


def txn_desc(t: dict) -> str:
    desc = t.get("description") or t.get("typeDesc") or ""
    return " ".join(str(desc).split()).strip() or "Transaction"


def txn_typecode(t: dict) -> str:
    return (t.get("typeCode") or "").strip()


def player_url(player_id: int) -> str:
    return f"https://www.mlb.com/player/{player_id}"


def is_trade(t: dict) -> bool:
    tc = txn_typecode(t).upper()
    if tc in {"TR", "TRADE"}:
        return True
    return " traded " in (" " + txn_desc(t).lower() + " ")


# -----------------------------
# Trade incoming player extraction
# -----------------------------
def _coerce_int(x):
    try:
        return int(x)
    except Exception:
        return None


def extract_trade_incoming_players(t: dict):
    """
    Returns list of dicts: [{"id": 123, "name": "Player Name"}, ...]
    Only players whose toTeam == TEAM_ID (Giants).
    Tries several common StatsAPI shapes.
    """
    incoming = []

    def add_player(pid, name):
        pid = _coerce_int(pid)
        if pid is None:
            return
        nm = " ".join(str(name or "").split()).strip()
        if not nm:
            nm = f"Player {pid}"
        incoming.append({"id": pid, "name": nm})

    candidates = []
    if isinstance(t.get("players"), list):
        candidates.extend(t["players"])

    if isinstance(t.get("playerTransactions"), list):
        candidates.extend(t["playerTransactions"])
    if isinstance(t.get("playerTransaction"), list):
        candidates.extend(t["playerTransaction"])

    for p in candidates:
        if not isinstance(p, dict):
            continue

        to_team = p.get("toTeam") or p.get("teamTo") or p.get("to") or {}
        to_team_id = (
            (to_team.get("id") if isinstance(to_team, dict) else None)
            or p.get("toTeamId")
            or p.get("teamToId")
        )
        to_team_id = _coerce_int(to_team_id)

        if to_team_id != TEAM_ID:
            continue

        person = p.get("person") or p.get("player") or {}
        pid = (
            (person.get("id") if isinstance(person, dict) else None)
            or p.get("playerId")
            or p.get("personId")
        )
        name = (
            (person.get("fullName") if isinstance(person, dict) else None)
            or p.get("playerName")
            or p.get("name")
        )

        add_player(pid, name)

    seen = set()
    uniq = []
    for pl in incoming:
        if pl["id"] in seen:
            continue
        seen.add(pl["id"])
        uniq.append(pl)
    return uniq


# -----------------------------
# Post construction helpers
# -----------------------------
def split_oversized_date_block(block: str, max_len=MAX_POST_LEN):
    """Split a date-group block on transaction boundaries to avoid mid-line truncation."""
    if len(block) <= max_len:
        return [block]

    lines = block.split("\n")
    if len(lines) < 2 or not lines[0] or not lines[0][0].isdigit():
        return [block[: max_len - 1] + "…"]

    header = lines[0]
    tx_lines = lines[1:]

    entries = []
    cur_entry = []
    for line in tx_lines:
        if line.startswith("- "):
            if cur_entry:
                entries.append(cur_entry)
            cur_entry = [line]
        else:
            if cur_entry:
                cur_entry.append(line)
            else:
                cur_entry = [line]
    if cur_entry:
        entries.append(cur_entry)

    if not entries:
        return [block[: max_len - 1] + "…"]

    chunks = []
    cur_lines = [header]

    for entry in entries:
        entry_text = "\n".join(entry)
        candidate_lines = cur_lines + entry
        candidate_text = "\n".join(candidate_lines)

        if len(candidate_text) <= max_len:
            cur_lines = candidate_lines
            continue

        if len(cur_lines) > 1:
            chunks.append("\n".join(cur_lines))
            cur_lines = [header]

        single_text = "\n".join(cur_lines + entry)
        if len(single_text) <= max_len:
            cur_lines.extend(entry)
        else:
            allowed = max_len - len(header) - 2
            trimmed = entry_text[: max(0, allowed - 1)] + "…"
            chunks.append(header + "\n" + trimmed)

    if len(cur_lines) > 1:
        chunks.append("\n".join(cur_lines))

    return chunks


def pack_posts(blocks, max_len=MAX_POST_LEN):
    """Greedy-pack blocks (multi-line strings) into posts without cutting transaction lines."""
    expanded_blocks = []
    for block in blocks:
        expanded_blocks.extend(split_oversized_date_block(block, max_len=max_len))

    posts = []
    cur = ""
    for block in expanded_blocks:
        candidate = block if not cur else (cur + "\n" + block)
        if len(candidate) <= max_len:
            cur = candidate
        else:
            if cur:
                posts.append(cur)
            if len(block) > max_len:
                posts.append(block[: max_len - 1] + "…")
                cur = ""
            else:
                cur = block
    if cur:
        posts.append(cur)
    return posts


def build_date_group_blocks(txns):
    """
    Build date-grouped blocks where the date appears once per block, e.g.:

    2026-02-14
    - Optioned ...
    - Claimed ...

    Trade transactions inside this 'other' stream will format as:
    - Trade: <desc>
      • Player A https://...
      • Player B https://...
    """
    txns = sorted(txns, key=lambda t: txn_id(t))

    blocks = []
    cur_date = None
    cur_lines = []

    def flush():
        nonlocal cur_date, cur_lines, blocks
        if cur_date and cur_lines:
            blocks.append(cur_date + "\n" + "\n".join(cur_lines))
        cur_date = None
        cur_lines = []

    for t in txns:
        d = txn_date(t)
        desc = txn_desc(t)

        if cur_date is None:
            cur_date = d
        elif d != cur_date:
            flush()
            cur_date = d

        if is_trade(t):
            incoming = extract_trade_incoming_players(t)
            cur_lines.append(f"- Trade: {desc}")
            if incoming:
                for pl in incoming:
                    cur_lines.append(f"  • {pl['name']} {player_url(pl['id'])}")
        else:
            cur_lines.append(f"- {desc}")

    flush()
    return blocks


def build_posts(new_txns, player_cache=None, season_mode=False, now_la=None):
    """
    Apply rules:
    - Signing txns: always separate post with enrichment
    - In-season only: optioned/recalled/dfa/contract-selected get separate stats posts
    - Everything else: existing grouped behavior
    """
    player_cache = player_cache or {}
    now_la = now_la or _la_now()

    separate_posts = []
    grouped = []

    for t in sorted(new_txns, key=lambda x: txn_id(x)):
        if is_signing_transaction(t):
            d = txn_date(t)
            desc = txn_desc(t)
            base_text = "\n".join([d, f"- {desc}"])
            pid = extract_tx_player_id(t)
            if not pid:
                separate_posts.append(base_text)
                continue
            link = player_url(pid)
            details = get_player_details(pid, player_cache)
            yy = get_player_year_by_year(pid, player_cache)
            stats_blocks = yy.get("stats") or []
            enrichment = build_signing_enrichment(details, stats_blocks=stats_blocks)
            separate_posts.append(build_signing_post(base_text, link, enrichment, max_len=MAX_POST_LEN))
            continue

        category = None
        if season_mode:
            if is_optioned_transaction(t):
                category = "optioned"
            elif is_recalled_transaction(t):
                category = "recalled"
            elif is_dfa_transaction(t):
                category = "dfa"
            elif is_contract_selected_transaction(t):
                category = "contract_selected"

        if category:
            separate_posts.append(build_special_transaction_post(t, category, player_cache, now_la))
        else:
            grouped.append(t)

    other_blocks = build_date_group_blocks(grouped)
    grouped_posts = pack_posts(other_blocks)

    return [p for p in (grouped_posts + separate_posts) if p and p.strip()]


# -----------------------------
# Main
# -----------------------------
def main():
    identifier = os.environ["BSKY_IDENTIFIER"]
    app_password = os.environ["BSKY_APP_PASSWORD"]

    last_posted_id = load_last_id()
    seen_ids = load_seen_ids()

    # Helpful run-time visibility (shows up in Actions logs)
    print("State paths:", LAST_ID_PATH, SEEN_IDS_PATH)
    print("Loaded last_id:", last_posted_id)
    print("Loaded seen_ids:", len(seen_ids), ("max=" + str(max(seen_ids)) if seen_ids else ""))
    print("POST_DELAY_SECONDS:", POST_DELAY_SECONDS)

    url = mlb_transactions_url()
    data = request_json_with_retry(url)
    txns = data.get("transactions", [])

    if not txns:
        print("No transactions returned.")
        return

    # --- Key fix: ALWAYS respect last_id AND seen_ids ---
    # This prevents repeats even if seen_ids is stale, and still uses seen_ids as a guard.
    candidate_txns = [
        t for t in txns
        if txn_id(t) > last_posted_id and txn_id(t) not in seen_ids
    ]

    # Hard cutoff: do NOT post anything before TXN_CUTOFF_DATE
    new_txns = []
    skipped_cutoff = []
    for t in candidate_txns:
        d = txn_date_obj(t)
        if (d is None) or (d < TXN_CUTOFF_DATE):
            skipped_cutoff.append(t)
        else:
            new_txns.append(t)

    if skipped_cutoff:
        print(f"Skipped {len(skipped_cutoff)} transactions before {TXN_CUTOFF_DATE.isoformat()}.")

    # Always record what we fetched (helps dedupe even if nothing posts this run)
    all_fetched_ids = {txn_id(t) for t in txns if txn_id(t) > 0}

    if not new_txns:
        print("No new transactions.")
        if all_fetched_ids:
            save_seen_ids(seen_ids | all_fetched_ids)
            print("Updated seen_ids.txt (no new posts).")
        return

    player_cache = {}
    now_la = _la_now()
    season_mode = in_season_mode(now_la=now_la, season_cache=player_cache)

    posts_to_send = build_posts(new_txns, player_cache=player_cache, season_mode=season_mode, now_la=now_la)
    if not posts_to_send:
        print("Nothing to post after formatting.")
        # Still record fetched IDs so we don't churn forever
        if all_fetched_ids:
            save_seen_ids(seen_ids | all_fetched_ids)
            print("Updated seen_ids.txt (nothing to post after formatting).")
        return

    session = bsky_create_session(identifier, app_password)
    access_jwt = session["accessJwt"]
    did = session["did"]
    print("Posting as DID:", did)

    for i, text in enumerate(posts_to_send):
        resp = bsky_post(access_jwt, did, text)
        uri = resp.get("uri")
        cid = resp.get("cid")
        print("Posted:\n", text, "\n---")
        print("CreateRecord uri:", uri, "cid:", cid)

        # High-signal verification: immediately read it back
        status, body_snip = bsky_verify_record(access_jwt, did, uri)
        print("Verify getRecord status:", status)
        if status != 200:
            # This snippet is often very informative (404/400, etc.)
            print("Verify getRecord body snippet:", body_snip)

        # Small delay between posts (helps avoid bursty behavior and feed caching weirdness)
        if POST_DELAY_SECONDS and i < len(posts_to_send) - 1:
            time.sleep(POST_DELAY_SECONDS)

    new_last_id = max(txn_id(t) for t in new_txns)
    save_last_id(new_last_id)

    if all_fetched_ids:
        save_seen_ids(seen_ids | all_fetched_ids)

    print("Updated last_id.txt to:", new_last_id)


if __name__ == "__main__":
    main()
