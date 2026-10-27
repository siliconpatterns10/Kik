# The action-item follow-up task

How the scheduled follow-up works, how it was built, and the task prompt verbatim.

## What it does

Once a day, after the morning action-item emails have gone out, the task emails
**each owner their own tasks again** and asks for a status update, using the
styled status-update template.

Order of operations inside the single command:

1. Resolve the requested local day to a UTC window.
2. Read the shared delivery ledger **and** the `vlsi_delivery@` mailbox
   **Sent Items via Microsoft Graph** for that window. The mailbox is the
   independent record of what actually left the mailbox; the ledger is the record
   of what the toolkit submitted. Neither source replaces the other.
3. Identify recipients whose **action-item email was sent that same day**.
4. Ignore anything already followed up for that owner that day.
5. Rebuild each owner's follow-up **from that owner's own tasks**, read from the
   plan that produced their action email.
6. Render it with the stored styled status-update template and submit **one email
   per owner to that owner's own address**.

## Command

```
python3 /root/Kik/kik.py --json followup-send --date $(TZ=Asia/Kolkata date +%F) --execute
```

Useful flags:

| Flag | Effect |
|---|---|
| `--date <YYYY-MM-DD>` | Required. The local day whose action emails are followed up. |
| `--timezone <IANA>` | Day-boundary zone; defaults to config. |
| `--introduction <text>` | Opening paragraph; a neutral default is used otherwise. |
| `--only <address>` | Restrict to one owner (test flows). |
| `--test-recipient` | Submit every follow-up to the configured test recipient instead of the owner. |
| `--execute` | Actually submit. Omitted means dry run. |
| `--history` | Print the shared delivery ledger. |

## Statuses it reports

- `DRY_RUN` — nothing submitted.
- `NOTHING_TO_DO` — no action emails were sent that day, or every owner was
  already followed up.
- `COMPLETE` — with `accepted` count and per-recipient results.
- `STOPPED` — a non-accepted outcome; the remaining owners were **not** attempted
  and must not be retried automatically.

Each result carries `intendedRecipient`, `intendedOwner`, `attemptId`, `state`
(`ACCEPTED` / `FAILED` / `UNKNOWN` / `BLOCKED`), `outcome` and `graphRequestId`.

## How it was built

The transport is a dedicated toolkit command, not a script written per run. An
ad-hoc sender used on 2026-10-21 is precisely why `deliver-attendees` exists; the
same rule applies here.

Files:

- `lib/followup_send.py` — day window, ledger + mailbox selection, plan lookup,
  follow-up plan construction, rendering, delivery and ledger writes.
- `lib/followup_template.py` — the styled status-update template.
- `lib/deliver.py` — `_mailbox_sent_rows()` reads Sent Items; `_graph_token()`
  and `_graph_send()` are shared transport.
- `lib/phase2.py` — `render` now records `planPath` in the run manifest so a
  later run can trace a ledger entry back to the plan that produced it.
- `kik.py` — the `followup-send` subcommand.

### Deliberate safety properties

- **Off by default.** Dry run unless `--execute` is passed.
- **One submission per owner per day**, enforced through the ledger.
- **Duplicate detection keys on the intended owner**, not on the address actually
  submitted. This was a real defect: with test re-routing, keying on the submitted
  address collapsed seven owners into one and silently skipped six.
- **No retries.** A `FAILED`, `UNKNOWN` or interrupted submission stops the batch
  and is left for manual review. `BLOCKED` recipients are skipped, not resent.
- **No AI logic.** It selects and transports; it does not extract tasks, write
  prose, interpret replies or choose recipients autonomously.
- **Test routing** (`--test-recipient`) sends everything to the configured test
  address while the ledger still records each real owner.

### Testing performed

- Dry run against a real day: found the mailbox messages for the window and
  selected only owners whose action email had been sent that day.
- A live test routed every follow-up to the test recipient: all seven were
  accepted, each ledger row recording the real owner.
- The test ledger rows were removed afterwards so they could not block a real run.
- `python3 tests/run_tests.py` — 201 tests pass.

## Task prompt (verbatim)

The scheduled task runs the command and reports. The prompt is time-neutral: the
schedule is set in the task UI, not in the prompt text.

```text
Send the daily status follow-up to everyone who was emailed action items today.

Run this single command on the local sandbox device (stdio-only, no interactive input):

    python3 /root/Kik/kik.py --json followup-send --date $(TZ=Asia/Kolkata date +%F) --execute

What the command already does on its own — do not do any of this yourself:
- resolves today's local date and the day's UTC window;
- reads the delivery ledger and the vlsi_delivery@siliconpatterns.com mailbox Sent Items through Microsoft Graph to find the action-item emails sent earlier today;
- selects only the owners whose action email was actually sent today, skipping anyone already followed up today;
- rebuilds each owner's follow-up from that owner's own action items in the plan that produced their email;
- renders it with the stored styled status-update template and submits one email per owner to that owner's own address.

Rules for this task:
- Do not write, edit, or improvise any sending code, and do not use any other send path. The command above is the only transport.
- Do not send anything if the command reports NOTHING_TO_DO.
- Do not retry a recipient the command reports as FAILED, UNKNOWN, or BLOCKED, and do not attempt to "fix" a blocked batch by hand.
- Do not modify the delivery ledger, the plans, or any file under /root/Kik.

Then report, in your reply:
- the command's status and the local date it used;
- how many owners were found and how many were emailed;
- each recipient address with its accepted/failed/unknown state;
- anything the command listed as skipped, and why;
- any error text verbatim.

State plainly that Graph acceptance means submitted, not confirmed delivered. Do not claim inbox delivery. If the command fails or reports zero recipients, say so directly rather than reporting success.
```

## Known limitations

- **A test copy appears in Sent Items as an action email with no matching plan.**
  The tool lists it as skipped rather than inventing tasks for it.
- **Plan lookup depends on the run manifest.** Runs rendered before `planPath` was
  recorded have no pointer; a newest-actions-plan fallback is used, and anything
  unmatched is reported as skipped rather than guessed.
- **The follow-up restates the tasks that were emailed.** It does not
  independently re-derive tasks from the meeting, and it does not interpret the
  replies it receives.
- **Graph acceptance is not delivery.** An `ACCEPTED` row means the submission was
  accepted.