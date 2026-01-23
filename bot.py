import os
import requests
from datetime import datetime, timedelta

TEAM_ID = 137  # SF Giants


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


def load_last_id(path="last_id.txt"):
    try:
        with open(path, "r", encoding="utf-8") as f:
            v = f.read().strip()
            return v or None
    except FileNotFoundError:
        return None


def save_last_id(last_id: str, path="last_id.txt"):
    with open(path, "w", encoding="utf-8") as f:
        f.write(str(last_id))


def main():
    identifier = os.environ["BSKY_IDENTIFIER"]
    app_password = os.environ["BSKY_APP_PASSWORD"]

    # Fetch MLB transactions
    url = mlb_transactions_url()
    data = requests.get(url, timeout=20).json()
    txns = data.get("transactions", [])

    # Sort newest first
    txns.sort(key=lambda t: t.get("date", ""), reverse=True)

    last_posted = load_last_id()

    new_items = []
    for t in txns:
        tid = str(t.get("id") or f'{t.get("date")}-{t.get("description","")}')
        if last_posted and tid == last_posted:
            break
        new_items.append({
            "id": tid,
            "date": t.get("date", ""),
            "desc": t.get("description") or t.get("typeDesc") or "Giants transaction"
        })

    # Post oldest → newest
    new_items.reverse()

    if not new_items:
        print("No new transactions.")
        return

    # First run safety: only post newest one
    if last_posted is None and len(new_items) > 1:
        new_items = [new_items[-1]]

    # Login once
    session = bsky_create_session(identifier, app_password)
    access_jwt = session["accessJwt"]
    did = session["did"]

    for item in new_items:
        text = (
            f"Giants transaction ({item['date']}): {item['desc']}\n\n"
            "Source: mlb.com/giants/roster/transactions"
        )
        bsky_post(access_jwt, did, text)
        print("Posted:", item["id"])
        save_last_id(item["id"])


if __name__ == "__main__":
    main()
