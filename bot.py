"""Giants transaction bot entrypoint.

Runtime responsibilities are intentionally separated:
- bot_core: polling/state/Bluesky plumbing and stable generic helpers
- mlb_domain: verified StatsAPI transaction semantics and player data access
- post_builder: transaction-specific enrichment and presentation

This entrypoint orchestrates those layers directly; it does not monkey-patch
bot_core's legacy transaction/stat helpers.
"""

import bot_core as infra
import post_builder


def main():
    identifier = infra.os.environ["BSKY_IDENTIFIER"]
    app_password = infra.os.environ["BSKY_APP_PASSWORD"]

    last_posted_id = infra.load_last_id()
    seen_ids = infra.load_seen_ids()

    print("State paths:", infra.LAST_ID_PATH, infra.SEEN_IDS_PATH)
    print("Loaded last_id:", last_posted_id)
    print(
        "Loaded seen_ids:",
        len(seen_ids),
        ("max=" + str(max(seen_ids)) if seen_ids else ""),
    )
    print("POST_DELAY_SECONDS:", infra.POST_DELAY_SECONDS)

    data = infra.request_json_with_retry(infra.mlb_transactions_url())
    txns = data.get("transactions", [])
    if not txns:
        print("No transactions returned.")
        return

    candidate_txns = [
        tx for tx in txns
        if infra.txn_id(tx) > last_posted_id
        and infra.txn_id(tx) not in seen_ids
    ]

    new_txns = []
    skipped_cutoff = []
    for tx in candidate_txns:
        tx_date = infra.txn_date_obj(tx)
        if tx_date is None or tx_date < infra.TXN_CUTOFF_DATE:
            skipped_cutoff.append(tx)
        else:
            new_txns.append(tx)

    if skipped_cutoff:
        print(
            f"Skipped {len(skipped_cutoff)} transactions before "
            f"{infra.TXN_CUTOFF_DATE.isoformat()}."
        )

    all_fetched_ids = {
        infra.txn_id(tx) for tx in txns if infra.txn_id(tx) > 0
    }

    if not new_txns:
        print("No new transactions.")
        if all_fetched_ids:
            infra.save_seen_ids(seen_ids | all_fetched_ids)
            print("Updated seen_ids.txt (no new posts).")
        return

    cache = {}
    now_la = infra._la_now()
    season_mode = infra.in_season_mode(now_la=now_la, season_cache=cache)
    posts_to_send = post_builder.build_posts(
        new_txns,
        player_cache=cache,
        season_mode=season_mode,
        now_la=now_la,
    )

    if not posts_to_send:
        print("Nothing to post after formatting.")
        if all_fetched_ids:
            infra.save_seen_ids(seen_ids | all_fetched_ids)
            print("Updated seen_ids.txt (nothing to post after formatting).")
        return

    session = infra.bsky_create_session(identifier, app_password)
    access_jwt = session["accessJwt"]
    did = session["did"]
    print("Posting as DID:", did)

    for index, text in enumerate(posts_to_send):
        response = infra.bsky_post(access_jwt, did, text)
        uri = response.get("uri")
        cid = response.get("cid")
        print("Posted:\n", text, "\n---")
        print("CreateRecord uri:", uri, "cid:", cid)

        status, body_snip = infra.bsky_verify_record(access_jwt, did, uri)
        print("Verify getRecord status:", status)
        if status != 200:
            print("Verify getRecord body snippet:", body_snip)

        if (
            infra.POST_DELAY_SECONDS
            and index < len(posts_to_send) - 1
        ):
            infra.time.sleep(infra.POST_DELAY_SECONDS)

    new_last_id = max(infra.txn_id(tx) for tx in new_txns)
    infra.save_last_id(new_last_id)
    if all_fetched_ids:
        infra.save_seen_ids(seen_ids | all_fetched_ids)

    print("Updated last_id.txt to:", new_last_id)


if __name__ == "__main__":
    main()
