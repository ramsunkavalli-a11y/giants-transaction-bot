# Giants Transaction Bot

Bluesky bot for San Francisco Giants transactions, with context beyond the raw MLB transaction feed.

The goal is to answer four questions quickly:

1. **What happened?**
2. **How has the player been performing?**
3. **What does the move mean mechanically?**
4. **What is noteworthy about it?**

The bot is intentionally conservative about inference. It prefers official transaction semantics and date-bounded data over guessing why the club made a move.

_Last major validation: 2026-08-16._

## Current behavior

### Transaction polling

`.github/workflows/run.yml` runs every 15 minutes and executes `bot.py`.

The transaction bot:

- Reads Giants transactions from MLB StatsAPI.
- Filters already-seen transaction IDs.
- Preserves transaction order.
- Consolidates multi-row trades that share a transaction ID.
- Suppresses low-value administrative noise such as number changes and generic organizational assignments.
- Posts to Bluesky.
- Persists transaction IDs immediately after a successful Bluesky CreateRecord call so a later verification failure cannot cause duplicate posting.
- Reads and writes production state on the dedicated `bot-state` branch, leaving `main` for production code.

### Performance context

`post_builder.py` adds date-bounded performance context without leaking future results into historical transaction posts.

Examples:

- **Optioned hitters:** MLB stint G/GS, PA, slash line/OPS; meaningful samples add wOBA/xwOBA, K%, BB%.
- **Optioned pitchers:** MLB stint G/GS, IP, ERA; meaningful samples add FIP/xFIP, K%, BB%.
- **Selected players:** current MiLB season performance.
- **DFA:** MLB year-to-date performance.
- **Incoming waiver claims/signings:** relevant MLB/MiLB context.
- **Trades:** consolidated rather than repeated as separate MLB API rows.

Statcast enrichment is optional and failures are not allowed to block the core transaction post.

### Roster-mechanics context

`transaction_context.py` adds compact context when it is useful and fits under the Bluesky character limit.

Currently supported:

- `2nd option in 2026` style option-count context.
- First MLB call-up detection.
- Repeated DFA context.
- Time from DFA to waiver claim.
- First-career outright detection.
- IL duration / original IL-start context.
- 40-man consequences for relevant MLB transactions.
- Current 40-man count when reliable and space permits.

Professional acquisitions may get **one** extra identity signal rather than a metric dump:

- Hitters: standout OAA when relevant, otherwise ZiPS ROS context when available.
- Pitchers: useful pitch-mix/velocity context when available, otherwise ZiPS ROS context.

The design rule is one useful extra fact, not a miniature scouting report on every move.

## 40-man roster tracker

`.github/workflows/roster-daily.yml` runs near 11 PM Los Angeles time and executes `roster_daily.py`.

If the Giants finish the calendar day below 40 players, it posts:

```text
40-man roster: 39/40
Open spot streak: Day 6
2026 total: 18 days
```

Rules:

- A calendar day counts once if the club finishes the day with at least one open 40-man spot.
- `38/40` is still one open-roster day, not two.
- Same-day create-and-fill transactions do not count as an open day.
- A return to 40/40 ends the streak.
- A later vacancy begins a new streak while season cumulative days continue.
- No daily post is sent when the roster is full.

### MLB 40-man data caveat

MLB's `40Man` roster feed includes players on the MLB 60-day IL even though they do not occupy a 40-man spot, so those players are excluded from the count.

A second edge case was discovered in August 2026: after **Miguel Mendez** was acquired from San Diego while on a minor-league IL/rehab path, MLB's Giants `40Man` feed temporarily omitted him even though his own transaction history showed a prior contract selection and no subsequent 40-man removal.

`roster_intelligence.py` therefore has a conservative reconciliation path for recent incoming trades:

- The player's own transaction history must independently establish real 40-man membership.
- A recent trade can preserve that membership if MLB's team roster feed temporarily loses the player.
- Minor-league 60-day IL transactions are explicitly prevented from creating fake 40-man membership.

This was validated against Mendez while rejecting false positives such as Henry Lalane and Marty Gair.

As of the 2026-08-16 validation, the reconciled Giants roster was **40/40**, with Mendez as the only MLB feed omission. The reconstructed total was **58 open-roster days**, with the latest streak ending Aug. 9.

## MLB transaction semantics

`mlb_domain.py` is the authoritative transaction-classification layer. Important StatsAPI meanings include:

| Code | Meaning |
|---|---|
| `SE` | Selected contract |
| `SC` | Status Change — **not** a signing |
| `OPT` | Optioned |
| `CU` | Recalled |
| `DES` | Designated for Assignment |
| `DFA` | Declared Free Agency — **not** DFA |
| `OUT` | Outrighted |
| `CLW` | Claimed Off Waivers |
| `REL` | Released |
| `RE` | Reinstated |
| `TR` | Trade |
| `NUM` | Number change |

Do not infer semantics from the code label alone when the domain layer already handles the distinction.

## Architecture

### `bot.py`
Thin transaction-poller orchestration:

`secrets -> state -> transactions -> candidates -> season mode -> build posts -> enrich context -> Bluesky -> persist represented transaction IDs`

### `bot_core.py`
Stable infrastructure and generic helpers:

- Los Angeles time handling.
- HTTP retries.
- season detection.
- Bluesky session/post/verification helpers.
- state I/O.
- generic transaction grouping and represented-ID matching.

### `mlb_domain.py`
MLB StatsAPI domain layer:

- transaction classification.
- player details/history.
- MLB/MiLB stats access.
- transaction semantics.

### `post_builder.py`
Transaction-specific presentation and historical/date-bounded stat context.

### `transaction_context.py`
Roster-mechanics and noteworthy-event enrichment layered on top of the stable post builder.

### `roster_intelligence.py`
40-man membership/count logic, daily historical counts, trade reconciliation, and protection against affiliate-IL false positives.

### `roster_daily.py`
Nightly open-spot streak/cumulative tracker and Bluesky posting.

### `acquisition_intelligence.py`
Selective acquisition signal selection: OAA/ZiPS for hitters; pitch arsenal/ZiPS for pitchers.

### `statcast_domain.py`
Optional Statcast/Savant enrichment, including date-bounded expected-stat reconstruction. Designed to fail safely.

### `pitching_domain.py`
Date-bounded pitcher sabermetric helpers, including FIP/xFIP.

## State files

These are production state and should not be deleted or manually reset casually:

- `seen_ids.txt` — transaction IDs already represented by successful posts.
- `last_id.txt` — legacy/high-water checkpoint used alongside seen IDs.
- `roster_daily_state.json` — last successful daily 40-man tracker post/state.

The bot intentionally commits state to the `bot-state` branch so GitHub Actions runs remain idempotent without an external database. `main` contains code only; do not manually reset the state branch casually.

For a local posting run, check out `bot-state` separately and point `BOT_STATE_DIR` to that checkout. The entrypoints fail safely when their production-state files are absent instead of treating a missing checkout as new state.

## Branches and workflow hygiene

- **`main`** — production code. Use short-lived `feature/*` branches and pull requests for code changes.
- **`bot-state`** — production state only. Scheduled workflows are the only intended writers.
- **`audit/*`** — temporary reconciliation work, or preferably GitHub Actions artifacts; do not commit probes or generated audit output to `main`.

The unit-test workflow runs for pushes to `main` and pull requests targeting it. The scheduled bot workflows serialize through a shared concurrency group before writing `bot-state`.

## GitHub Actions

Production workflows kept in the repository:

- **`run.yml`** — 15-minute transaction poller.
- **`roster-daily.yml`** — nightly 40-man open-spot tracker.
- **`unit-tests.yml`** — compile + offline regression suite on pushes/PRs.

Old one-off probe, dry-run and validation workflows are not part of the current tree. GitHub may continue to display historical workflow names in the Actions UI because workflow-run metadata is retained even after the YAML file is removed.

## Reliability principles

1. **Do not future-leak stats.** Historical transaction posts use data available through the transaction date.
2. **Do not let optional enrichment block the transaction.** Core post first; Statcast/projection extras are expendable.
3. **Persist after successful posting.** A verification failure must not create a duplicate on retry.
4. **Prefer transaction history over assumptions.** Especially for 40-man status and IL mechanics.
5. **Do not infer causal roster relationships unless they are effectively established.** Same-day moves are not automatically cause/effect.
6. **Stay under 300 characters.** Context is dropped by priority if necessary.
7. **Regression-test discovered MLB API quirks.** Real edge cases should become tests instead of undocumented special knowledge.

## Tests

The `tests/` directory contains offline regression coverage for:

- transaction-code semantics.
- temporal/date-bounded behavior.
- option hitter and pitcher context.
- trade consolidation.
- transaction presentation/order.
- administrative-assignment filtering.
- partial-post state persistence.
- roster intelligence and 40-man reconciliation.
- nightly roster tracking.
- transaction-context enrichment.
- acquisition intelligence.

Run locally with:

```bash
python -m compileall -q .
python -m unittest discover -s tests -v
```

## Configuration

Required GitHub Actions secrets:

- `BSKY_IDENTIFIER`
- `BSKY_APP_PASSWORD`

Python version: **3.11**.

Primary external data sources are MLB StatsAPI and Baseball Savant/Statcast endpoints used by the domain modules.

## Next steps

Prioritized, not committed promises:

1. **Persistent roster ledger** — maintain a clearer daily internal history of 40-man membership, active roster, IL and option status rather than reconstructing everything repeatedly.
2. **Deeper roster mechanics** — option-year history, DFA/waiver lifecycle, outright rights when reliably determinable, retroactive IL dates and eligibility dates.
3. **Better paired-move context** — explicitly connect a 60-day IL move to the corresponding contract selection only when the relationship is strongly established.
4. **Trade-specific presentation** — better handling for large multi-player trades, potentially as a compact thread rather than forcing everything into one post.
5. **Calendar-event intelligence** — dedicated handling for Rule 5 protection/draft, non-tenders, arbitration, Opening Day cutdown, September expansion, draft signings and international signings.
6. **Rule 5 tracker** — active-day progress toward the Rule 5 roster requirement when applicable.
7. **Source reconciliation/anomaly detection** — flag impossible roster counts or disagreements between MLB team rosters, player transaction histories and secondary roster sources before publishing.
8. **Reusable historical fixtures** — preserve representative real transaction payloads for deterministic regression tests without needing repeated live API probes.

The long-term target is not maximum data density. It is a reliable Giants transaction account that explains the transaction, the player's recent performance, the roster consequence, and the one thing that makes the move interesting.
