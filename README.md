# Kik

A read-only operational toolkit for investigating the Process-desk
meeting-ingestion pipeline: what the database actually holds, what the provider
actually sent, what the analysis actually did.

**It is not an AI application.** There is no LLM client here, no summarisation,
no semantic matching and no autonomous decision-making. The toolkit retrieves,
organises, validates, renders and transports. A separate assistant or task agent
reads the evidence this produces, reasons about it, writes the content, and asks
for approval before anything is sent.

## Boundaries

| Rule | Why |
|---|---|
| Read-only against production | No write method exists in the Supabase client. Only `GET`. |
| Never commit, push, deploy or migrate | `/root/Process-desk/` is reference material, not a target. |
| Credentials are never printed | Not a value, not a prefix, not a length. Errors name variables only. |
| Absence is not evidence | An empty log, a null `meetingId`, a missing attendee entry — each says what a source contains, never what happened. |
| One test recipient | Until the workflow is verified, every live message goes to the configured `test_recipient`. No CC, no BCC, no override. |

## Layout

```text
/root/Kik/
├── kik.py                  single entry point
├── config.toml             runtime config (0600, not committed)
├── config.example.toml     template
├── lib/                    the toolkit
│   ├── config.py           TOML config + KEY=VALUE env parsing
│   ├── http_client.py      urllib wrapper: timeouts, bounded retries
│   ├── supabase.py         read-only PostgREST access
│   ├── localdate.py        local midnight-to-midnight <-> UTC
│   ├── timezones.py        tz resolution + fixed-offset fallback
│   ├── fixtures.py         two-field fixture classification
│   ├── artifacts.py        atomic, private artifact writes
│   ├── status.py           status vocabulary + exit codes
│   ├── output.py           stdout/stderr split, redaction
│   ├── doctor.py           configuration diagnostics
│   ├── inspect_meetings.py per-day meeting and webhook report
│   ├── renderlogs.py       Render log retrieval
│   ├── runstore.py         run directories, manifests, hashing
│   ├── fetch.py            complete evidence bundle for one meeting
│   ├── people.py           attendee extraction + provider id extraction
│   ├── identities.py       exact-only attendee/label matching rules
│   ├── identity_command.py the `identities` command
│   ├── transcripts.py      deterministic, lossless batching
│   ├── transcript_command.py the `transcript` command
│   └── webhook_command.py  targeted reads of preserved raw bodies
├── templates/              HTML assets for the renderer
├── schemas/                JSON schemas for agent-authored input
├── tests/
│   ├── run_tests.py        stdlib runner (no pytest on this host)
│   ├── test_*.py
│   └── fixtures/           synthetic fixtures + fingerprint registry
└── runs/                   fallback artifacts dir (gitignored)
```

Artifacts normally go to `/root/kik-runs/<local-date>/<run-id>/`, configured by
`runs_directory`. Keep that path **outside any Git checkout**: it holds real
meeting content. A run directory is never reused or overwritten.

## Commands

```bash
# diagnostics
python3 /root/Kik/kik.py doctor                        # local config checks
python3 /root/Kik/kik.py doctor --network              # + reachability/auth, never mail

# one local day
python3 /root/Kik/kik.py inspect --date 2026-10-20 --timezone Asia/Kolkata
python3 /root/Kik/kik.py inspect --date 2026-10-15 --exclude-fixtures --persist
python3 /root/Kik/kik.py logs --date 2026-10-20 --level error --persist

# one meeting
python3 /root/Kik/kik.py fetch --meeting-id <id> --include-raw-bodies
python3 /root/Kik/kik.py fetch --date 2026-10-15            # lists, never picks
python3 /root/Kik/kik.py fetch --date 2026-10-15 --all      # one bundle each

# identities: online or from a saved bundle
python3 /root/Kik/kik.py identities --meeting-id <id>
python3 /root/Kik/kik.py identities --bundle <bundle.json>  # offline, no credentials

# transcripts (offline; batching needs no network)
python3 /root/Kik/kik.py transcript batches --bundle <bundle.json> --max-chars 16000
python3 /root/Kik/kik.py transcript read    --bundle <bundle.json> --batch 1

# preserved raw webhook bodies
python3 /root/Kik/kik.py webhook raw --run-id <id> --inbox-id <id> --list-keys
python3 /root/Kik/kik.py webhook raw --run-id <id> --inbox-id <id> --json-path data.transcript.text

# saved evidence
python3 /root/Kik/kik.py runs
```

Global flags come **before** the subcommand:

```bash
python3 /root/Kik/kik.py --json inspect --date 2026-10-20
python3 /root/Kik/kik.py --quiet doctor
python3 /root/Kik/kik.py --config /path/to/config.toml doctor
```

`--json` writes exactly one JSON document to stdout. Progress, warnings and
errors go to stderr. In JSON mode a warning appears twice by design: once as
data in the document's `warnings` array, once as a diagnostic line on stderr.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | `OK`, or `NO_MEETINGS` — a valid search that matched nothing is a success |
| 2 | `MULTIPLE_MEETINGS` — selection is required, the tool will not choose |
| 3 | `CONTENT_UNAVAILABLE` — the meeting exists with neither transcript nor summary |
| 4 | `IDENTITY_REVIEW_REQUIRED` |
| 64 | `USAGE_ERROR` |
| 66 | `NOT_FOUND` |
| 69 | `PROVIDER_ERROR` |
| 75 | `PARTIAL_RESULT` |
| 76 | `SEND_RESULT_UNKNOWN` — a submission may have started; never auto-retried |
| 77 | `AUTHENTICATION_ERROR` / `PERMISSION_ERROR` |
| 78 | `CONFIGURATION_ERROR` |

## Tests

```bash
python3 /root/Kik/tests/run_tests.py            # all
python3 /root/Kik/tests/run_tests.py test_config  # one module
```

The runner is stdlib-only because this host has no pytest and no pip. Tests use
synthetic fixtures and a loopback HTTP server; nothing in the suite touches
Supabase, Render, Microsoft Graph or the network at large.

## Local-day arithmetic

A day is a local midnight-to-midnight interval converted to UTC, always with an
inclusive start and an exclusive end. For `Asia/Kolkata`:

```text
2026-10-20 local  ->  2026-10-19T18:30:00Z  <= t <  2026-10-20T18:30:00Z
```

If the host has no tz database, resolution falls back to a table limited to
zones whose offset is **constant year-round** — for those, a fixed offset is not
an approximation, it is the zone. The result is tagged
`fixed-offset-fallback` and every command that used it warns.

A zone that observes DST is **refused**, not given its standard offset: the
offset would be wrong for part of the year and silently so. Any other unknown
zone is refused too. `Europe/London` on this host is an error, not `+00:00`.

## Fetch, identities and batching

`fetch` writes everything it retrieves into a fresh run directory and prints the
manifest. Two behaviours are deliberate:

- **Failed webhook rows with a null `meetingId` are included.** The provider
  meeting id is read from the row's own payload and the linkage method is
  recorded per row (`MEETING_FOREIGN_KEY` vs `PROVIDER_MEETING_ID_IN_PAYLOAD`),
  because a row without a foreign key is not evidence of a normal flow.
- **Raw webhook bodies are stored, not printed.** `--include-raw-bodies` writes
  each `rawBody` to `webhook-raw-<inbox-id>.json`, and the manifest records the
  sha256 and whether it matches the stored `rawBodyHash`. Use `webhook raw` to
  read one deliberately; `--list-keys` shows structure with **no values**.

`identities` maps only by `EXACT_EMAIL`, `EXPLICIT_ALIAS` or
`UNIQUE_EXACT_NAME`, and reports `UNMATCHED`, `AMBIGUOUS` and `INACTIVE` without
resolving them. There is **no fuzzy matching** — no single-character-edit rule,
no spelling distance. A near miss is returned as a labelled alternative, marked
"not a match and not a recipient", for an agent to judge. `--bundle` reproduces
a saved bundle with no network and no credentials.

`transcript batches` is deterministic and lossless: the same bundle and limit
always produce the same boundaries and batch ids, and every source character
appears in exactly one batch in source order. Report only **character** counts —
no token estimate is produced, because it would depend on a tokeniser this
toolkit does not run. A segment larger than the limit is split with explicit
`continuationIndex` / `continuationTotal` metadata rather than truncated.
`transcript read --acknowledge` records that a caller reported reading a batch;
it is not proof of comprehension, and the output says so.

## Fixture fingerprints

Two independent fields, and which one matched is always reported:

- `rawTextSha256` — sha256 over the exact `fullText` bytes, reproducible locally.
- `storedContentHash` — the `meeting_transcripts.contentHash` value by
  observation. **Its normalization algorithm and provenance are not
  established**, and it is not to be described as provider-versioned.

A match against either marks a record `KNOWN_FIXTURE`. No match means `UNKNOWN`,
which is *absence of recognition*, never "verified real". A title alone never
changes a classification. `--exclude-fixtures` withholds fixtures; without it
they are reported and labelled.

## Known limitations

- **No tzdata on this host.** Only constant-offset zones resolve, via the
  fallback; DST zones are refused. Install the tz database (or set
  `PYTHONTZPATH`) for any other timezone.
- **Template reference and active renderer.** A byte-identical snapshot of
  `Process-desk/src/mail/meeting-action-email.template.ts` is stored at
  `templates/meeting-action-email.template.ts` (SHA-256
  `13868ab89ccc6ebefb6ba1647560e5e5aaf70422f471ef35b74d02f129034044`).
  The active, Outlook-verified renderer is the Python `kik-py-2` port in
  `lib/phase2.py` (`render_one`), invoked by `kik.py render`. The `.ts` file is
  **reference only**: this host has no npm/node_modules, and copying it does
  not cause Kik to execute TypeScript. Keep the Python port aligned with the
  reference and verify changes with renderer/MIME tests plus Outlook review.
  No Python AI logic is involved.
- **`--json` position.** Must precede the subcommand.
- `transcript read` can reuse persisted `batches.json` plans (`--plan`); changed source content refuses replay.
- Phase 2 is implemented: `validate` and `render` perform offline validation and styled rendering. In `PRESERVE_PROVIDER`, shared sections are deterministically extracted and styled; the original provider Markdown remains evidence and its Action items section is excluded from the common body. Graph sending is restricted to the test recipient `yaswanth.ampolu@siliconpatterns.com`; colleagues are intended-person labels only. `send-review` defaults to offline dry-run. Show sender, intended person, actual recipient, subject, and hash; obtain explicit approval before one `--execute`. Inspect `--history` before repeats. 202 means accepted, not delivered; UNKNOWN blocks retry. Production expansion requires a later explicit policy change.
- **Render log retention.** An old date returns an empty result rather than an
  error; emptiness is reported as emptiness, not as absence of activity.
- **`inspect` does not fetch `rawBody`,** so it cannot link a failed webhook row
  to a meeting itself. `fetch` does. That is a deliberate size decision.