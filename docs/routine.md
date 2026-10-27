# Daily meeting routine — how the run is actually performed

This documents the end-to-end routine used to turn a finished meeting into
summary and action emails. It describes what the assistant does and which
endpoints it calls. It is a record of practice, not a specification of new
behaviour.

## Endpoints and credentials at a glance

- **Meeting data:** Supabase PostgREST, read-only.
  `SUPABASE_URL` + `SUPABASE_SERVICE_ROLE_SECRET_KEY` from `/root/.env`, named by
  `/root/Kik/config.toml`. Only `select=` with an explicit column list is used.
- **Mail:** Microsoft Graph, `https://graph.microsoft.com/v1.0/users/<sender>/sendMail`
  (submit) and `.../mailFolders/inbox/messages` / `.../mailFolders/sentitems/messages`
  (read). Token from `https://login.microsoftonline.com/<tenant>/oauth2/v2.0/token`
  using `CLIENT_SECRET`, `DIRECTORY_TENANT_ID`, `APPLICATION_CLIENT_ID`.
- **Sender:** `vlsi_delivery@siliconpatterns.com`. The service account is excluded
  from recipient lists in config.
- **Service logs (diagnosis only):** Render API, owner/service ids from config.
- **Frontend CTA in every email:** `https://delivery-desk-frontend.vercel.app`.

## The routine, in order

### 1. `doctor`
`python3 /root/Kik/kik.py --json doctor`

Confirms config load, runs-directory mode, timezone resolution, fixture registry,
Supabase/Render identifiers, mail policy, and that exactly one Graph secret
variable is selected. Never sends mail. `--network` additionally verifies
reachability and Graph token acquisition.

### 2. `inspect` — find the day's meetings
`python3 /root/Kik/kik.py --json inspect --date <YYYY-MM-DD> --timezone Asia/Kolkata`

Converts the local day to a UTC window (IST midnight-to-midnight is
`18:30Z previous day → 18:30Z same day`) and reports:

- meetings started inside the window;
- webhook rows received inside the window, **including rows with
  `meetingId = null`** — relevant because a failed `meeting.transcribed` row does
  not always carry a linked meeting;
- summaries created inside the window;
- per-meeting availability: `BOTH` / `TRANSCRIPT_ONLY` / `SUMMARY_ONLY` / `NEITHER`.

### 3. Select the meeting
Selection is explicit. Multiple matches return `MULTIPLE_MEETINGS` (exit 2) and
require `--meeting-id` or `--all`. A title alone never classifies a record.

### 4. `fetch` — pull the complete evidence bundle
`python3 /root/Kik/kik.py --json fetch --meeting-id <id>`

Writes a new run directory under `/root/kik-runs/<date>/<run-id>/` containing:
metadata, attendees with the attendance caveat, the exact provider summary, raw
transcript text, ordered segments with ids, speaker labels, aliases, webhook rows
with linkage method, analysis runs, source ids, content hashes, and completeness
flags. A run directory is never overwritten.

### 5. `identities` — map speakers and attendees to app users
`python3 /root/Kik/kik.py --json identities --meeting-id <id>`
(or `--bundle <file>` offline, with no network and no credentials)

Rules, in order: **exact normalised email**, then **explicit `speaker_aliases`
row**, then **unique exact normalised display name** (labelled as name-based).
Unmatched, ambiguous and inactive results are returned as findings and never
resolved silently. There is no fuzzy or spelling-distance matching. A test
asserts the old `SINGLE_EDIT` matcher is absent.

Practical note: a speaker label can fail to resolve even when the same person's
attendee record maps cleanly, because the alias table keys on the label string.
When that happened, the mapping was corroborated from the attendee/organizer
record and recorded as such — not silently written as an alias.

### 6. Read the transcript — in batches, all of them
```
python3 /root/Kik/kik.py --json transcript batches --bundle <bundle> --max-chars 12000 --persist
python3 /root/Kik/kik.py --json transcript read --bundle <bundle> --batch N --max-chars 12000
```

Batching is deterministic and lossless: every source character lands in exactly
one batch, in source order, and the same bundle with the same `--max-chars`
always produces the same batch ids. **The same `--max-chars` must be passed to
`read`**, or the boundaries move.

Counts are characters, not tokens — no tokeniser is run and no estimate is
reported. An acknowledgement records that a batch was read, not that it was
understood. Every batch is read in order; unread batches are work remaining.

### 7. Author the email plan by hand
The assistant writes `sharedNotes` and each recipient's `actions`, with
`sourceReferences` carrying the **exact quote**. The toolkit never writes this
file. Two plans are produced for a normal day:

- `summary-plan.json` — `emailType: SUMMARY`, `actions: []` for every recipient.
- `actions-plan.json` — `emailType: ACTIONS`, only owners with `ownerType: OWNER`
  actions.

### 8. `validate`
`python3 /root/Kik/kik.py --json validate --input <plan> --bundle <bundle>`

Checks schema, required fields, duplicate addresses, email format, user mapping
against the bundle, inactive users, source types, **exact-quote matching against
the referenced source**, preserved provider content, URL schemes, and the bundle
hash. It certifies mechanics only — `ownershipAndQualityCertified` stays `false`.
Interpretation, ownership and truth remain the assistant's responsibility.

### 9. `render`
`python3 /root/Kik/kik.py --json render --input <plan> --bundle <bundle>`

Writes styled HTML plus plain text per recipient into a new run directory, with
an `index.html` and `preview-manifest.json`. No send.

### 10. Dry run, then send
- Test path: `send-review --preview-manifest <m> --person <intended> --execute`
  always delivers to `yaswanth.ampolu@siliconpatterns.com` only. The approval hash
  is inspected with `--dry-run` first.
- Attendee path:
  `deliver-attendees --preview-manifest <m> --input <plan> --bundle <b>` first as
  a dry run, then with `--approve-recipients --execute`.

`deliver-attendees` enforces exact email/alias identity, active users, the config
exclusion list, no CC/BCC, one address per submission, artifact re-read and hash
re-check, and that each stored artifact equals a **fresh rendering** of that
recipient. A prior attempt for the same meeting and address blocks that
recipient; any non-`ACCEPTED` outcome stops the remaining batch. Nothing is
retried automatically.

### 11. Report honestly
`ACCEPTED` / `FAILED` / `UNKNOWN` are reported as they are. **Graph `202 Accepted`
means the submission was accepted, not that the mail reached an inbox.** An
`UNKNOWN` is never auto-resent.

## Reading and interpretation discipline

Things that look like conclusions but are not:

- A listed attendee is a participant record, not confirmed live attendance. The
  service account appearing in the list does not establish that it attended.
- A summary row with `source = PROVIDER` and `analysisRunId = null` is not
  evidence that the in-house analysis ran.
- A failed webhook row with `meetingId = null` says what that row contains, not
  why it failed.
- The empty transcript payload (`text = null`, `segments = []`, `speakers = []`,
  `words_count = 0`) is what the provider sent. The cause is not established.
- A record matching no registered fixture fingerprint is `UNKNOWN` — absence of
  recognition, not verification that the content is real.

Unclear ASR terms, uncertain names and ambiguous ownership are preserved and
flagged in the output rather than silently corrected.

## What is deliberately absent from the Python

No LLM client, no summarisation, no semantic or fuzzy matching, no automatic
action extraction, no autonomous recipient selection, and no ad-hoc sender
scripts. The assistant reads, reasons, writes and asks; the toolkit fetches,
organises, validates, renders and transports.