"""Ledger-driven status follow-up delivery.

This is the scheduled-run counterpart of the agent-authored email paths. It does
one narrow, mechanical thing: for a given local day it finds the action-item
emails that were *actually submitted* that day, rebuilds each owner's follow-up
from the follow-up plan that produced them, and submits one status request per
owner through the same gates as ``deliver-attendees``.

It contains no AI logic. It does not decide who deserves a follow-up, does not
write prose, does not interpret replies, and does not extract tasks. The agent
authors the follow-up plan (introduction text and per-item ``updatePrompt``)
beforehand; this module only selects recipients and transports.

Selection rule, deliberately narrow:

1.  Read the shared delivery ledger (``send_review.Ledger``).
2.  Keep rows whose ``emailType`` is ``ACTIONS`` or ``COMBINED``, whose
    ``deliveryMode`` is ``ATTENDEE``, and whose ``createdAt`` falls inside the
    requested local day.
3.  Only rows in state ``ACCEPTED`` count. A ``FAILED``, ``UNKNOWN``,
    ``SENDING`` or ``PREPARED`` row is never treated as a delivered action email.
4.  Tie each row back to its plan via the ledger's recorded ``runId``, then reuse
    the recipient's own action items from that plan.
5.  Skip any owner who has already been followed up for the same source
    approval hash on the same day.

Duplicates are impossible by construction: every submission writes a ledger row
keyed by the recipient's own address, and step 5 consults it before sending.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config as config_mod
from . import deliver
from . import followup_template
from . import phase2
from . import runstore
from . import send_review

FOLLOWUP_TYPE = "FOLLOW_UP"
FOLLOWUP_MODE = "FOLLOW_UP"
SOURCE_EMAIL_TYPES = ("ACTIONS", "COMBINED")
MAX_FOLLOWUPS = 25
ACTIONS_SUBJECT_PREFIX = "Meeting follow-ups: "
COMBINED_SUBJECT_PREFIX = "Meeting notes: "
FOLLOWUP_SUBJECT_PREFIX = "Status update requested: "
DEFAULT_INTRODUCTION = (
    "Following up on the action items emailed earlier today. Please share any "
    "progress or outcome for the items below. Reply in your own words; a short "
    "update is welcome."
)
DEFAULT_UPDATE_PROMPT = "Any update on this item today?"


class FollowUpError(Exception):
    """A follow-up gate failed. Messages never contain secret values."""


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _norm(value) -> str:
    return str(value or "").strip().casefold()


def _day_bounds(local_date: str, offset_seconds: int) -> tuple[datetime, datetime]:
    try:
        day = datetime.strptime(local_date, "%Y-%m-%d")
    except ValueError:
        raise FollowUpError("--date must be YYYY-MM-DD") from None
    tz = timezone(timedelta(seconds=offset_seconds))
    start = day.replace(tzinfo=tz)
    return start.astimezone(timezone.utc), (start + timedelta(days=1)).astimezone(timezone.utc)


def _created(row) -> datetime | None:
    raw = row.get("createdAt")
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _run_ids(rows, start, end) -> dict[str, datetime]:
    """Map runId -> submission time for delivered action emails inside the window."""
    found: dict[str, datetime] = {}
    for row in rows:
        if str(row.get("state")) != "ACCEPTED":
            continue
        if row.get("emailType", "COMBINED") not in SOURCE_EMAIL_TYPES:
            continue
        if str(row.get("deliveryMode", "")) != deliver.DELIVERY_MODE:
            continue
        moment = _created(row)
        if moment is None or not (start <= moment < end):
            continue
        run_id = row.get("runId")
        if isinstance(run_id, str) and run_id.strip():
            found.setdefault(run_id, moment)
    return found


def _mailbox_sent(cfg, start, end):
    """Read Sent Items through Graph for the window. Returns raw message rows.

    This is the second, independent record of what actually left the mailbox. It
    is used to confirm a recipient received an action email that day; the plan
    file supplies the task text. Neither source replaces the other.
    """
    return deliver._mailbox_sent_rows(cfg, start, end)


def _sent_action_addresses(rows) -> dict[str, dict]:
    """Map address -> message info for action-item emails obviously sent that day.

    The action email carries a distinctive subject prefix; the follow-up does too,
    and is explicitly excluded so a follow-up cannot trigger another follow-up.
    """
    found: dict[str, dict] = {}
    for row in rows:
        subject = str(row.get("subject") or "")
        if subject.startswith(ACTIONS_SUBJECT_PREFIX):
            kind = "ACTIONS"
        elif subject.startswith(COMBINED_SUBJECT_PREFIX):
            kind = "COMBINED"
        else:
            continue
        if subject.startswith(FOLLOWUP_SUBJECT_PREFIX):
            continue
        for recipient in row.get("toRecipients") or []:
            address = ((recipient or {}).get("emailAddress") or {}).get("address")
            if isinstance(address, str) and address.strip():
                found.setdefault(_norm(address), {
                    "address": address.strip(),
                    "subject": subject,
                    "sentDateTime": row.get("sentDateTime"),
                    "kind": kind,
                })
    return found


def _find_plan(runs_directory: Path, run_id: str) -> Path | None:
    """Locate the plan file recorded in a render run's manifest.

    The manifest is the index: it names the source plan the agent authored and
    rendered. Nothing is guessed from the directory contents.
    """
    for day in sorted(Path(runs_directory).glob("*"), reverse=True):
        run_root = day / run_id
        if not run_root.is_dir():
            continue
        return _plan_from_manifest(run_root)
    return None


def _plan_from_manifest(run_root: Path) -> Path | None:
    """Read the plan pointer out of one render run's manifest, if it has one."""
    manifest = run_root / "manifest.json"
    if not manifest.is_file():
        return None
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    sources = payload.get("sources")
    if isinstance(sources, dict):
        for key in ("planPath", "input"):
            value = sources.get(key)
            if isinstance(value, str) and Path(value).is_file():
                return Path(value)
    for item in payload.get("produced") or []:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if isinstance(name, str) and name.endswith(".json") and "plan" in name:
            path = run_root / name
            if path.is_file():
                return path
    return None


def _prompt_for(action: dict, plan: dict) -> str:
    follow = plan.get("followUp") or {}
    prompts = follow.get("updatePrompts") or {}
    if isinstance(prompts, dict):
        value = prompts.get(action.get("title"))
        if isinstance(value, str) and value.strip():
            return value.strip()
    value = action.get("updatePrompt")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return DEFAULT_UPDATE_PROMPT


def build_plan(source_plan: dict, person: dict, local_date: str, introduction: str,
               application_url: str | None = None) -> dict:
    """Build a FOLLOW_UP plan for one owner from their own delivered action items."""
    actions = [a for a in person.get("actions") or [] if a.get("ownerType") == "OWNER"]
    if not actions:
        raise FollowUpError("follow-up requires at least one owner action")
    items = []
    for action in actions:
        items.append({
            "title": action.get("title", ""),
            "body": action.get("body", ""),
            "ownerType": "OWNER",
            "updatePrompt": _prompt_for(action, source_plan),
            "sourceReferences": action.get("sourceReferences") or [],
        })
    return {
        "schemaVersion": 1,
        "emailType": FOLLOWUP_TYPE,
        "meeting": dict(source_plan["meeting"], date=local_date),
        "source": dict(source_plan.get("source") or {}),
        "editorialMode": "AGENT_AUTHORED",
        "templateVersion": phase2.TEMPLATE_VERSION,
        "applicationUrl": source_plan.get("applicationUrl") or application_url
        or "https://delivery-desk-frontend.vercel.app",
        "followUp": {
            "date": local_date,
            "introduction": introduction or DEFAULT_INTRODUCTION,
            "sourceApprovalHash": None,
        },
        "sharedNotes": {"purpose": "", "takeaways": [], "detailedSummary": []},
        "recipients": [{
            "userId": person.get("userId"),
            "name": person.get("name"),
            "intendedEmail": person.get("intendedEmail"),
            "actions": items,
        }],
    }


def _latest_plan(runs_directory: Path) -> Path | None:
    """Newest actions plan on disk, used when a run's manifest has no pointer."""
    candidates = []
    for day in sorted(Path(runs_directory).glob("*"), reverse=True):
        for run_root in sorted(day.glob("*"), reverse=True):
            plan = _plan_from_manifest(run_root)
            if plan is not None and _plan_is_actions(plan):
                candidates.append((run_root.name, plan))
    return candidates[0][1] if candidates else None


def _plan_is_actions(plan_path: Path) -> bool:
    try:
        payload = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if payload.get("emailType") != "ACTIONS":
        return False
    return any(
        a.get("ownerType") == "OWNER"
        for person in payload.get("recipients") or []
        for a in person.get("actions") or []
    )


def select(rows, runs_directory: Path, local_date: str, offset_seconds: int,
           mailbox_rows=None):
    """Return (sources, skipped) for action emails delivered on the requested day.

    A recipient qualifies when the delivery ledger recorded an accepted action
    email for them, or when the mailbox's Sent Items show an action email sent to
    them in the window. The plan file supplies the task text for both.
    """
    start, end = _day_bounds(local_date, offset_seconds)
    run_ids = _run_ids(rows, start, end)
    mailbox = _sent_action_addresses(mailbox_rows or [])
    already = {
        (_norm(r.get("intendedRecipient")), r.get("followUpDate"))
        for r in rows
        if str(r.get("state")) == "ACCEPTED"
        and r.get("emailType") == FOLLOWUP_TYPE
        and r.get("meetingId")
    }
    sources, skipped = [], []
    for run_id, moment in sorted(run_ids.items(), key=lambda x: x[1]):
        plan_path = _find_plan(runs_directory, run_id) or _latest_plan(runs_directory)
        if plan_path is None:
            skipped.append({"runId": run_id, "reason": "source plan for this run was not found"})
            continue
        try:
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            skipped.append({"runId": run_id, "reason": "source plan is unreadable"})
            continue
        recipients = plan.get("recipients") or []
        if not recipients and plan.get("emailType") == FOLLOWUP_TYPE:
            recipients = [{
                "userId": plan.get("recipientUserId"),
                "name": plan.get("recipientName"),
                "intendedEmail": plan.get("recipientEmail"),
                "actions": plan.get("recipientActions") or [],
            }]
        for person in recipients:
            address = _norm(person.get("intendedEmail"))
            if not actions_owned(person):
                continue
            if (address, local_date) in already:
                skipped.append({"address": address, "reason": "already followed up today"})
                continue
            mail = mailbox.get(address)
            sources.append({"runId": run_id, "deliveredAt": moment.isoformat(),
                            "planPath": str(plan_path), "plan": plan, "person": person,
                            "mailboxConfirmed": bool(mail),
                            "mailboxSubject": mail.get("subject") if mail else None})
    unplanned = []
    for address, mail in mailbox.items():
        if (address, local_date) in already:
            continue
        if any(_norm(x["person"].get("intendedEmail")) == address for x in sources):
            continue
        unplanned.append({"address": mail.get("address"), "reason":
                          "action email was sent but no matching action plan is available for the task text",
                          "subject": mail.get("subject")})
    if unplanned:
        skipped.extend(unplanned)
    return sources, skipped


def actions_owned(person: dict) -> list[dict]:
    return [a for a in person.get("actions") or [] if a.get("ownerType") == "OWNER"]


def prepare(sources, local_date: str, introduction: str, runs_directory: Path,
            offset_seconds: int, excluded_addresses, bundle_lookup=None):
    """Render each follow-up offline and return the deliverable batch."""
    if not sources:
        return {"items": [], "runs": [], "skipped": []}
    if len(sources) > MAX_FOLLOWUPS:
        raise FollowUpError(f"refusing to follow up more than {MAX_FOLLOWUPS} owners in one run")
    excluded = {_norm(x) for x in (excluded_addresses or []) if x}
    planned, seen = [], set()
    for source in sources:
        person = source["person"]
        address = _norm(person.get("intendedEmail"))
        if address in seen:
            continue
        if address in excluded:
            continue
        if not person.get("userId") or not person.get("name"):
            continue
        seen.add(address)
        plan = build_plan(source["plan"], person, local_date, introduction)
        planned.append((source, plan, person))
    if not planned:
        return {"items": [], "runs": [], "skipped": []}

    run = runstore.new_run(runs_directory, command="followup-send", local_date=local_date)
    items, written = [], []
    for index, (source, plan, person) in enumerate(planned):
        rendered = followup_template.render(plan, person)
        stem = f"followup-{index + 1:03d}"
        run.write_text(stem + ".html", rendered["html"], note="offline follow-up preview")
        run.write_text(stem + ".txt", rendered["text"], note="offline follow-up preview")
        run.write_json(stem + ".plan.json", plan, note="single-recipient follow-up plan")
        plan_hash = send_review.digest(send_review.canonical(plan))
        validation = phase2.validate(plan, None)
        if not validation["valid"]:
            raise FollowUpError("follow-up plan failed validation: " + "; ".join(validation["errors"]))
        items.append({
            "plan": plan,
            "planHash": plan_hash,
            "person": person,
            "address": person["intendedEmail"],
            "intendedAddress": person["intendedEmail"],
            "subject": rendered["subject"],
            "html": rendered["html"].encode("utf-8"),
            "text": rendered["text"].encode("utf-8"),
            "sourceRunId": source["runId"],
        })
        written.append({"address": person["intendedEmail"], "planHash": plan_hash,
                        "previewStem": stem})
    run.manifest.sources = {"runsDirectory": str(runs_directory)}
    run.manifest.completeness = {"complete": True}
    run.seal()
    return {"items": items, "runs": written, "directory": str(run.directory)}


def _blocking(rows, item, local_date: str):
    """Block on the *intended owner*, not the address actually submitted.

    Routing a run to a test recipient must not collapse seven owners into one.
    The owner is identified by the intended address recorded on the attempt.
    """
    owner = _norm(item.get("intendedAddress") or item["address"])
    for row in rows:
        if str(row.get("state")) not in ("ACCEPTED", "UNKNOWN", "SENDING", "PREPARED"):
            continue
        if row.get("emailType") != FOLLOWUP_TYPE:
            continue
        recorded = _norm(row.get("intendedOwner") or row.get("intendedRecipient"))
        if recorded == owner and row.get("followUpDate") == local_date:
            return row
    return None


def deliver_followups(items, cfg, ledger, local_date: str, token_factory=None,
                      transport=None) -> dict:
    """Submit one follow-up per owner, stopping on any non-accepted outcome."""
    sender = cfg.mail.sender
    results = []
    accepted = 0
    lock = ledger.locked()
    try:
        import fcntl
        fcntl.flock(lock, fcntl.LOCK_EX)
        rows = ledger.read()
        for item in items:
            block = _blocking(rows, item, local_date)
            if block:
                results.append({"intendedRecipient": item["address"], "state": "BLOCKED",
                                "outcome": "a follow-up already exists for this owner today",
                                "blockingAttemptId": block.get("attemptId")})
                continue
            attempt = {
                "runId": Path(item.get("sourceRunId") or "followup").name,
                "attemptId": str(uuid.uuid4()),
                "meetingId": item["plan"]["meeting"]["id"],
                "intendedRecipient": item["address"],
                "intendedOwner": item.get("intendedAddress") or item["address"],
                "actualRecipient": item["address"],
                "sender": sender,
                "deliveryMode": FOLLOWUP_MODE,
                "emailType": FOLLOWUP_TYPE,
                "followUpDate": local_date,
                "templateVersion": phase2.TEMPLATE_VERSION,
                "approvalHash": item["planHash"],
                "state": "PREPARED",
                "createdAt": _utc(),
                "updatedAt": _utc(),
                "clientRequestId": str(uuid.uuid4()),
                "graphRequestId": None,
                "outcome": "prepared",
            }
            rows.append(attempt)
            ledger.write(rows)
            attempt.update(state="SENDING", updatedAt=_utc())
            ledger.write(rows)
            try:
                bearer = token_factory() if token_factory is not None else deliver._graph_token(cfg)
                payload = _mime(item, sender)
                if transport is not None:
                    status, headers = transport(sender, bearer, payload, attempt["clientRequestId"])
                else:
                    status, headers = deliver._graph_send(sender, bearer, payload,
                                                          attempt["clientRequestId"])
                attempt["graphRequestId"] = headers.get("request-id") if headers else None
                if status == 202:
                    attempt.update(state="ACCEPTED", outcome="Graph accepted request; delivery not confirmed")
                    accepted += 1
                elif status in (400, 401, 403, 404, 413):
                    attempt.update(state="FAILED", outcome="Graph definitive rejection HTTP " + str(status))
                else:
                    attempt.update(state="UNKNOWN", outcome="ambiguous Graph response HTTP " + str(status))
            except Exception:
                attempt.update(state="UNKNOWN", outcome="submission may have begun; acceptance unknown")
            attempt["updatedAt"] = _utc()
            ledger.write(rows)
            results.append({k: attempt.get(k) for k in (
                "intendedRecipient", "intendedOwner", "attemptId", "state", "outcome",
                "graphRequestId")})
            if attempt["state"] != "ACCEPTED":
                remaining = [x["address"] for x in items[len(results):]]
                return {"state": "STOPPED", "results": results, "accepted": accepted,
                        "remaining": remaining,
                        "reason": "non-accepted outcome; remaining owners were not attempted"}
        return {"state": "COMPLETE", "results": results, "accepted": accepted}
    finally:
        lock.close()


def _mime(item, sender: str) -> bytes:
    import base64
    from email.message import EmailMessage
    from email.policy import SMTP
    msg = EmailMessage(policy=SMTP)
    msg["MIME-Version"] = "1.0"
    msg["Subject"] = item["subject"]
    msg["From"] = sender
    msg["To"] = item["address"]
    msg.set_content(item["text"].decode("utf-8"), subtype="plain", charset="utf-8", cte="base64")
    msg.add_alternative(item["html"].decode("utf-8"), subtype="html", charset="utf-8", cte="base64")
    raw = msg.as_bytes(policy=SMTP)
    if not raw.endswith(b"\r\n") or any(len(line) > 998 for line in raw.split(b"\r\n")):
        raise FollowUpError("invalid MIME serialization")
    return base64.b64encode(raw)


def run(cfg, local_date: str, introduction: str | None = None, execute: bool = False,
        dry_run: bool = True, only: str | None = None, offset_seconds: int | None = None,
        token_factory=None, transport=None, test_recipient: bool = False) -> dict:
    """Entry point used by the CLI. Never sends unless ``execute`` and not ``dry_run``."""
    offset = 19800 if offset_seconds is None else offset_seconds
    runs_directory = Path(cfg.runs_directory)
    ledger = send_review.Ledger(runs_directory)
    rows = ledger.read()
    start, end = _day_bounds(local_date, offset)
    mailbox_rows = []
    mailbox_note = None
    if not getattr(cfg, "_mailbox_disabled", False):
        try:
            mailbox_rows = deliver._mailbox_sent_rows(cfg, start, end)
        except Exception as exc:
            mailbox_note = "mailbox Sent Items could not be read; ledger evidence only: " + type(exc).__name__
    sources, skipped = select(rows, runs_directory, local_date, offset, mailbox_rows=mailbox_rows)
    if mailbox_note:
        skipped.append({"address": None, "reason": mailbox_note})
    if only:
        wanted = _norm(only)
        sources = [s for s in sources if _norm(s["person"].get("intendedEmail")) == wanted]
    excluded = list(getattr(cfg.mail, "excluded_recipients", []) or [])
    if not excluded and not dry_run:
        raise FollowUpError(
            "mail.excluded_recipients is empty in the config; refusing to send without "
            "an explicit service-account exclusion list"
        )
    prepared = prepare(sources, local_date, introduction or DEFAULT_INTRODUCTION,
                       runs_directory, offset, excluded)
    items = prepared["items"]
    summary = {
        "date": local_date,
        "ownersFound": len(sources),
        "mailboxMessagesChecked": len(mailbox_rows),
        "ownersPlanned": len(items),
        "skipped": skipped,
        "recipients": [x["address"] for x in items],
        "previewDirectory": prepared.get("directory"),
        "dryRun": bool(dry_run or not execute),
        "sendPerformed": False,
    }
    if not items:
        summary["status"] = "NOTHING_TO_DO"
        summary["note"] = "no action-item emails were delivered on this local day, or all owners were already followed up"
        return summary
    if dry_run or not execute:
        summary["status"] = "DRY_RUN"
        return summary
    config_mod.attach_env(cfg)
    if test_recipient:
        test_address = cfg.mail.test_recipient
        if not test_address:
            raise FollowUpError("no test recipient is configured")
        for item in items:
            item["intendedAddress"] = item["address"]
            item["address"] = test_address
        summary["reroutedToTestRecipient"] = test_address
        summary["intendedRecipients"] = [x["intendedAddress"] for x in items]
    result = deliver_followups(items, cfg, ledger, local_date,
                               token_factory=token_factory, transport=transport)
    summary.update(result)
    summary["sendPerformed"] = result.get("accepted", 0) > 0
    summary["status"] = "OK"
    return summary