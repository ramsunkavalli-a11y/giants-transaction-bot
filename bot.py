import os
import requests
from datetime import datetime, timedelta

TEAM_ID = 137  # SF Giants
MAX_POST_LEN = 300  # Bluesky post text limit


def mlb_transactions_url(days_back=120):
    end_date = datetime.utcnow().date()
    start_date = end_date - timedelta(days=days_back)
    return (
        "https://statsapi.mlb.com/api/v1/transactions"
        f"?teamId={TEAM_ID}&startDate={start_date}&endDate={end_date}"
    )


def bsky_create_session(identifier: str, app_password: str):
    url = "https://bsky.social/xrpc/com.atproto.server.createSession"
    r = requests.post(url, json={"identifier": identifier, "password": app_password}, timeout=20)
    r.raise_for_status()
    return r.json()


def bsky_post(access_jwt: str, did: str, text: str):
    url = "https://bsky.social/xrpc/com.atproto.repo.createRecord"
    payload = {
        "repo": did,
        "collection": "app.bsky.feed.post",
        "record": {
            "$type": "app.bsky.feed.post",
            "text": text,
            "createdAt": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        },
    }
    r = requests.post(url, json=payload, headers={"Authorization": f"Bearer {access_jwt}"}, timeout=20)
    r.raise_for_status()
    return r.json()


def load_last_id(path="last_id.txt") -> int:
    """Numeric transaction id checkpoint. Returns 0 if missing/blank."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            v = (f.read() or "").strip()
            return int(v) if v else 0
    except FileNotFoundError:
        return 0
    except Exception:
        return 0


def save_last_id(last_id: int, path="last_id.txt"):
    with open(path, "w", encoding="utf-8") as f:
        f.write(str(int(last_id)))


# ---------- StatsAPI field helpers ----------

def txn_id(t: dict) -> int:
    try:
        return int(t.get("id", 0))
    except Exception:
        return 0


def txn_date(t: dict) -> str:
    d = t.get("effectiveDate") or t.get("transactionDate") or t.get("date") or ""
    return str(d)[:10]  # YYYY-MM-DD


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
    # fallback heuristic
    return " traded " in (" " + txn_desc(t).lower() + " ")


# ---------- Trade incoming player extraction ----------

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

    # 1) Common shape: a list of "players" with person/fromTeam/toTeam
    candidates = []
    if isinstance(t.get("players"), list):
        candidates.extend(t["players"])

    # 2) Some variants use "playerTransactions" or similar
    if isinstance(t.get("playerTransactions"), list):
        candidates.extend(t["playerTransactions"])
    if isinstance(t.get("playerTransaction"), list):
        candidates.extend(t["playerTransaction"])

    # If we found structured candidates, filter for toTeam == TEAM_ID
    for p in candidates:
        if not isinstance(p, dict):
            continue

        # team routing
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

    # Deduplicate by id, preserve order
    seen = set()
    uniq = []
    for pl in incoming:
        if pl["id"] in seen:
            continue
        seen.add(pl["id"])
        uniq.append(pl)
    return uniq


# ---------- Post construction helpers ----------

def pack_posts(blocks, max_len=MAX_POST_LEN):
    """Greedy-pack blocks (multi-line strings) into posts."""
    posts = []
    cur = ""
    for block in blocks:
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
    - Trade (to SF): <desc>
      • Player A https://...
      • Player B https://...
    """
    # Ensure chronological order
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

            # One-line summary (no repeated date)
            cur_lines.append(f"- Trade: {desc}")

            # Neat incoming list (To-side)
            if incoming:
                # Use a different bullet to make it visually distinct
                for pl in incoming:
                    cur_lines.append(f"  • {pl['name']} {player_url(pl['id'])}")
            else:
                # If API doesn't provide structured incoming players, we keep it simple
                # (still posts the trade, just without links)
                pass
        else:
            cur_lines.append(f"- {desc}")

    flush()
    return blocks


def build_posts(new_txns):
    """
    Apply rules:
    - Non-SFA: group by date and pack into as few posts as possible
    - SFA: always its own post, include player link
    """
    sfa = []
    other = []

    for t in new_txns:
        if txn_typecode(t).upper() == "SFA":
            sfa.append(t)
        else:
            other.append(t)

    posts = []

    # Non-SFA date-grouped blocks packed into posts
    other_blocks = build_date_group_blocks(other)
    posts.extend(pack_posts(other_blocks))

    # Each SFA separately (with player link)
    for t in sorted(sfa, key=lambda x: txn_id(x)):
        d = txn_date(t)
        desc = txn_desc(t)

        # Try to find a player id/name
        pid = None
        name = None
        person = t.get("person") or {}
        if isinstance(person, dict):
            pid = _coerce_int(person.get("id"))
            name = person.get("fullName")

        # Fallbacks
        if pid is None:
            pid = _coerce_int(t.get("playerId") or t.get("player_id"))

        if name:
            name = " ".join(str(name).split()).strip()

        if pid:
            # Date only once
            line1 = d
            line2 = f"- {desc}"
            line3 = player_url(pid)
            posts.append("\n".join([line1, line2, line3]))
        else:
            posts.append("\n".join([d, f"- {desc}"]))

    return [p for p in posts if p.strip()]


def main():
    identifier = os.environ["BSKY_IDENTIFIER"]
    app_password = os.environ["BSKY_APP_PASSWORD"]

    last_posted_id = load_last_id()

    # Fetch transactions
    url = mlb_transactions_url()
    r = requests.get(url, timeout=20)
    r.raise_for_status()
    data = r.json()
    txns = data.get("transactions", [])

    if not txns:
        print("No transactions returned.")
        return

    # Only new since last run
    new_txns = [t for t in txns if txn_id(t) > last_posted_id]
    if not new_txns:
        print("No new transactions.")
        return

    # First-run safety: only post newest one
    if last_posted_id == 0 and len(new_txns) > 1:
        newest = max(new_txns, key=lambda t: txn_id(t))
        new_txns = [newest]

    posts_to_send = build_posts(new_txns)
    if not posts_to_send:
        print("Nothing to post after formatting.")
        return

    # Login once
    session = bsky_create_session(identifier, app_password)
    access_jwt = session["accessJwt"]
    did = session["did"]

    # Post in order
    for text in posts_to_send:
        bsky_post(access_jwt, did, text)
        print("Posted:\n", text, "\n---")

    # Update checkpoint to newest txn id we posted
    new_last_id = max(txn_id(t) for t in new_txns)
    save_last_id(new_last_id)
    print("Updated last_id.txt to:", new_last_id)


if __name__ == "__main__":
    main()
