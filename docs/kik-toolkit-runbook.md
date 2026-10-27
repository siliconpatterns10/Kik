# Kik toolkit runbook

Use when investigating the Process-desk meeting-ingestion pipeline, retrieving
meeting content, mapping attendees to user records, rendering meeting emails, or
performing a controlled send — including the scheduled meeting-digest run that
fetches the latest meeting, authors tailored copies, and mails each attendee.

Toolkit: `/root/Kik/` · Artifacts: `/root/kik-runs/` · Reference only: `/root/Process-desk/`

## Boundaries that are not negotiable

1. **No AI logic in the Python.** No LLM client, no summarisation, no semantic or
   fuzzy matching, no automatic action extraction, no autonomous recipient
   selection. The tools fetch, organise, validate, render and transport; the
   assistant reads the evidence, reasons, writes, and asks for approval.
2. **Read-only against production.** No inserts, updates, deletes, RPC, migrations,
   deployments, commits or pushes to `/root/Process-desk/`. Never run
   `/root/send_*.py` or `/root/sb_*.py` as part of a toolkit task.
3. **Two separate mail paths, and never a third.**
   - `send-review` is **test-only**: actual recipient is always
     `yaswanth.ampolu@siliconpatterns.com`, intended recipients are recorded but
     never contacted. No CC/BCC, no generic `--to`.
   - `deliver-attendees` is the **approved attendee path**. It sends each
     validated copy to its *own* matched address and requires
     `--approve-recipients --execute`. It refuses to run when the config's
     `mail.excluded_recipients` list is empty, refuses name-based or unresolved
     identities, refuses inactive users, and stops the whole remaining batch on
     any non-`ACCEPTED` outcome.
   - Do **not** write one-off ad-hoc sender scripts. On 2026-10-21 an attendee
     batch was sent that way; `deliver-attendees` exists so that never happens
     again. A one-off script is not an approved transport change.
4. **Absence is not evidence.** An empty log, a missing attendee entry, a null
   `meetingId`, or a `FAILED` webhook row says what a data source contains — not
   why something happened, and not that it did not happen.
5. `202 Accepted` from Graph means **accepted, not delivered**.

## Operational sequence

1. **`doctor`** — configuration, identifiers, timezone, fixture registry, secret
   hygiene. Add `--network` to check Supabase/Render reachability and Graph token
   acquisition; this never sends mail.
2. **`inspect --date <local day> --timezone Asia/Kolkata`** — midnight-to-midnight
   local interval converted to UTC. Reports meetings started in the window,
   webhook rows received in the window (including rows with `meetingId = null`),
   summaries created in the window, and per-meeting availability labels
   `BOTH` / `TRANSCRIPT_ONLY` / `SUMMARY_ONLY` / `NEITHER`. Add `--persist` to
   keep the report.
3. **Select the meeting.** Do not let the tool pick a default. Multiple matches
   return `MULTIPLE_MEETINGS` (exit 2) and require `--meeting-id` or `--all`.
4. **`fetch --meeting-id <id> [--include-raw-bodies]`** — complete bundle written
   to a new run directory: metadata, attendees, exact provider summary, raw
   transcript text, ordered segments, speaker labels, aliases, webhook rows,
   analysis runs, source IDs, content hashes, completeness.
   Check `webhooks.items[].linkage.method` — a failed row with a null `meetingId`
   is included via `PROVIDER_MEETING_ID_IN_PAYLOAD`, and its caveat says the
   association comes from the row's own payload, not a database relationship.
   Read preserved bodies with `webhook raw --run-id <id> --inbox-id <id>
   --list-keys` (structure, no values) or `--json-path <selector>`.
   `rawBodies[].hashMatchesStored` says whether the preserved body is
   byte-identical to what the database recorded.
5. **`identities --meeting-id <id>`** — exact email match, explicit alias, then
   unique exact normalised display name (labelled name-based). Unmatched,
   ambiguous and inactive are returned as findings, not resolved silently.
   `--bundle <file>` does the same from saved evidence with **no network and no
   credentials**.
6. **`transcript batches` / `transcript read`** — deterministic and lossless.
   Every source character lands in exactly one batch in source order; the same
   bundle and `--max-chars` always produce the same batch ids. Pass the *same*
   `--max-chars` to `read` or the boundaries move. Character counts only — no
   token estimate. `coverage.unbatched` lists unacknowledged batches, and an
   acknowledgement is not comprehension.
7. **Write the email plan by hand.** The agent authors `sharedNotes` and each
   recipient's `actions`, with `sourceReferences` carrying the exact quote. The
   toolkit never writes this file.
8. **`validate`** — schema, duplicate addresses, email format, user mappings,
   inactive users, source IDs, exact-quote matching against the referenced
   source, source-type labels, preserved shared content, URL schemes, hashes.
   It does not certify interpretation, ownership or truth.
9. **`render`** — styled HTML plus plain text, written under `/root/kik-runs/`.
   Preview artifacts and a manifest, no send.
10. **Present** intended content and the actual test recipient separately.
11. **Obtain explicit approval.**
12. **`send-review --input <plan> --person <intended> --execute`** — only after
    approval, once, to Yaswanth only.
13. **`deliver-attendees --preview-manifest <m> --input <plan> --bundle <b>`** —
    dry run first, always. It prints sender, meeting id, recipient count and the
    exact recipient list with `sendPerformed: false`. Then, only under explicit
    instruction for that meeting, repeat with `--approve-recipients --execute`.
    Each recipient is submitted once; ledger entries carry
    `deliveryMode: ATTENDEE` with the recipient's own address as both intended and
    actual. A prior attempt for the same meeting and address blocks that
    recipient; any non-`ACCEPTED` result stops the rest of the batch.
14. **Report** `ACCEPTED` / `FAILED` / `UNKNOWN` accurately. Never auto-resend an
    `UNKNOWN`.

## Interpretation discipline

| Tempting claim | Correct statement |
|---|---|
| "The bot attended" | `vlsi_delivery@…` appears in the attendee list and as `sourceAccountEmail`. Attendance is not established. |
| "The bot did not join" | `teams@maestrolabs.com` is absent from the stored attendee list. That is absence from that list only. |
| "The analysis ran" | A summary row exists with `source = PROVIDER` and `analysisRunId = null`. No AI run is evidenced. |
| "The webhook never arrived" | No matching backend log line was returned for that owner, service, interval and filter. |
| "The transcript event failed because the audio was empty" | The recorded payload has `text = null`, `segments = []`, `speakers = []`, `words_count = 0`. That is what the provider sent, not why. |
| "The transcript had no speech" | `words_count = 0` is the provider's own count in that payload. A cause is not established. |
| "This content is real" | No registered fixture fingerprint matched, so the record is `UNKNOWN` — not verified real. |

## Fixture fingerprints

Two separate fields; report which one matched.

- `rawTextSha256` — local `sha256` over exact `fullText` bytes.
- `storedContentHash` — the `meeting_transcripts.contentHash` column by
  observation. **Its normalization algorithm and provenance are not established.**
  Do not call it "provider-versioned".

Neither match means `UNKNOWN`. A title alone never classifies. Exclusion happens
only with `--exclude-fixtures`; otherwise the classification is displayed.

## Known environment quirks on this host

- **`zoneinfo` has no tzdata.** Only zones with a constant year-round offset
  resolve, via an explicit fallback that tags its source and warns. **A DST zone
  is refused** — `Europe/London` on this host is an error, not `+00:00`, because
  the standard offset would be silently wrong for part of the year.
- **Global flags come before the subcommand:** `python3 /root/Kik/kik.py --json doctor`.
- Render logs: `resource=` (plain), not `resource[]`, which returns
  `400 unknown resource type`. Pagination is via `hasMore` / `nextEndTime`, and
  the boundary entry must be de-duplicated. Nest's ANSI escapes are stripped
  before grouping.
- PostgREST filters must be operators, not bare values: `status=eq.FAILED`, not
  `status=FAILED`, which returns `PGRST100`. Use the `eq()` / `is_null()` helpers.
- No pip, npm, or node_modules — stdlib only. The TypeScript email template cannot
  be executed here; the Python renderer is a documented port.
- **Adding a nested subparser can silently break dispatch** if it reuses a `dest`
  already used by a parent subparsers action — argparse lets the last-parsed
  value win. `kik.py` uses a private top-level dest for this reason.

## Saved evidence

`python3 /root/Kik/kik.py runs` lists every run newest-first. A run directory is
never reused: each `fetch`, each `--persist`, and each batch plan gets a new one.
Everything a bundle contains can be re-read offline with
`identities --bundle`, `transcript batches --bundle` and `webhook raw`, with no
network access and no credentials — verified under `env -i`.

## Environment: where the credentials actually live

- Config: `/root/Kik/config.toml` (0600). Env file: **`/root/.env`** (0644 at
  time of writing), named by `env_file` in the config.
- Mail credentials are read by name from `[mail]`: `secret_env_name =
  "CLIENT_SECRET"`, `tenant_env_name = "DIRECTORY_TENANT_ID"`,
  `client_id_env_name = "APPLICATION_CLIENT_ID"`. `CLIENT_SECRET` is verified
  working; `CLIENT_SECRET_VALUE` returned `AADSTS7000215` and is deliberately not
  listed.
- `/root/.env` also holds unrelated production credentials (database, Redis,
  Supabase, Resend, Vercel, Render, webhook signing). Read only the names needed,
  never `cat` the file into output, never print values or lengths, never enable
  shell tracing.
- Supabase REST for read-only evidence: `SUPABASE_URL` + `SUPABASE_SECRET_KEY`.
- The Python never runs a shell; the assistant runs shell commands. Passing
  credentials through arguments or environment variables visible in `ps` is not
  acceptable — read them inside the process that needs them.

## Investigating outside the toolkit

The scripts are helpers, not a restriction. For anything the toolkit does not
cover, direct read-only queries against PostgREST (`/rest/v1/<table>?select=…`)
and the Render API are legitimate, provided the boundaries above still hold.
Prefer `select=` with an explicit column list over `select=*`.