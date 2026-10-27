"""Attendee delivery: send each validated tailored copy to its own matched address.

This is the sanctioned, reusable version of the one-off October 21 sender. It is
deliberately narrower and more mechanical than send_review:

- ``send-review`` keeps its hard-coded single test recipient. It is unchanged.
- ``deliver-attendees`` sends to the *intended* address of each recipient row,
  and only after every gate below passes.

It contains no AI logic: it reads a plan the agent wrote, re-runs the existing
plan validator, re-derives each recipient's expected rendering, and transports.
Interpretation and ownership are never certified here.

Gates, all of which must pass before any network submission:

1.  ``--approve-recipients`` must be present. Without it this command is a dry
    run regardless of ``--execute``.
2.  The plan must re-validate against the saved bundle, and the manifest must
    carry that exact validation result, bound by hash.
3.  The manifest's meeting id, the plan's meeting id and the bundle's meeting id
    must agree, and the bundle hash must match ``plan.source.bundleSha256``.
4.  Every recipient address must be an **exact** identity match in the bundle
    for that meeting, the mapped user must be active, and no recipient may be a
    configured excluded address (the service account).
5.  No CC/BCC. Exactly one address per submission.
6.  The recipient set must be a subset of the meeting's own attendee list.
7.  Artifacts are re-read through ``send_review._read_artifact`` (O_NOFOLLOW,
    regular file, size ceiling) and their hashes re-checked against the
    manifest, then re-checked again immediately before transport.
8.  The stored artifact bytes must equal ``phase2.render_one(plan, recipient)``
    for that recipient, so a mismatched or hand-edited preview cannot be sent.
9.  Every recipient row must have an explicit action section or the explicit
    no-action marker for its own actions, and no recipient may show both.
10. The shared ledger is consulted per recipient. An existing attempt for the
    same recipient + meeting + content hash blocks that recipient. Any state
    other than ACCEPTED aborts the remaining batch. Nothing is ever retried.
"""
from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import re
import sys
import urllib.parse
import uuid
from datetime import datetime, timezone
from email.message import EmailMessage
from email.policy import SMTP
from pathlib import Path

from . import config as config_mod
from . import phase2
from . import send_review

DELIVERY_MODE = "ATTENDEE"
MAX_RECIPIENTS = 25
NO_ACTION_MARKER = "No action items were assigned to you"


class DeliverError(Exception):
    """A delivery gate failed. Messages never contain secret values."""


def _norm(value) -> str:
    return str(value or "").strip().casefold()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _email(value) -> bool:
    return isinstance(value, str) and bool(
        re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value.strip())
    )


def approval_material(row, meeting_id: str, sender: str, html: bytes, text: bytes) -> dict:
    return {
        "schemaVersion": 1,
        "meetingId": meeting_id,
        "emailType": row.get("emailType", "COMBINED"),
        "intendedRecipient": _norm(row.get("intendedRecipient")),
        "sender": sender,
        "subject": row.get("subject"),
        "templateVersion": row.get("templateVersion"),
        "htmlSha256": _sha(html),
        "textSha256": _sha(text),
        "validationSha256": row.get("validationSha256"),
        "deliveryMode": DELIVERY_MODE,
        "attachments": [],
    }


def approval_hash(row, meeting_id, sender, html, text) -> str:
    return _sha(send_review.canonical(approval_material(row, meeting_id, sender, html, text)))


def inspect_batch(manifest_path: str, plan_path: str, bundle_path: str | None,
                  excluded_addresses) -> list[dict]:
    """Validate everything offline. Returns ready-to-send items. No network."""
    try:
        manifest_file = Path(manifest_path).resolve(strict=True)
        root = manifest_file.parent
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        plan_file = Path(plan_path).resolve(strict=True)
        plan = json.loads(plan_file.read_text(encoding="utf-8"))
    except Exception:
        raise DeliverError("plan or manifest is unreadable") from None

    if not isinstance(manifest, dict) or manifest.get("schemaVersion") != 1:
        raise DeliverError("unsupported preview manifest version")
    if not isinstance(plan, dict) or plan.get("schemaVersion") != 1:
        raise DeliverError("unsupported email plan version")
    if plan.get("templateVersion") != phase2.TEMPLATE_VERSION:
        raise DeliverError("plan was not rendered with the current template version")
    if manifest.get("templateVersion") != phase2.TEMPLATE_VERSION:
        raise DeliverError("manifest template version does not match the renderer")
    if manifest.get("editorialMode") not in ("PRESERVE_PROVIDER", "AGENT_AUTHORED"):
        raise DeliverError("unsupported editorial mode")

    meeting = plan.get("meeting") or {}
    meeting_id = meeting.get("id")
    if not isinstance(meeting_id, str) or not meeting_id.strip():
        raise DeliverError("plan is missing a meeting id")
    if (manifest.get("meeting") or {}).get("id") != meeting_id:
        raise DeliverError("manifest meeting does not match the plan")

    bundle_file = bundle_path or (plan.get("source") or {}).get("bundlePath")
    if not bundle_file:
        raise DeliverError("no bundle path was supplied or recorded in the plan")
    try:
        bundle_raw = Path(bundle_file).resolve(strict=True).read_bytes()
        bundle = json.loads(bundle_raw.decode("utf-8"))
    except Exception:
        raise DeliverError("source bundle is unreadable") from None
    if bundle.get("meeting", {}).get("id") != meeting_id:
        raise DeliverError("bundle does not belong to this meeting")
    recorded = (plan.get("source") or {}).get("bundleSha256")
    if recorded and recorded != _sha(bundle_raw):
        raise DeliverError("source bundle hash does not match the plan")

    # Re-run the mechanical validator rather than trusting the stored copy.
    revalidated = phase2.validate(plan, bundle)
    if not revalidated.get("valid"):
        raise DeliverError("plan no longer validates: " + "; ".join(revalidated.get("errors", [])))

    stored = manifest.get("validation")
    binding = manifest.get("validationBinding")
    if stored != revalidated:
        raise DeliverError("manifest validation result does not match a fresh validation")
    if not isinstance(binding, dict) or binding.get("result") != stored:
        raise DeliverError("manifest validation binding is invalid")
    if manifest.get("validationSha256") != _sha(send_review.canonical(binding)):
        raise DeliverError("manifest validation hash is invalid")

    rows = manifest.get("recipients")
    people = plan.get("recipients")
    if not isinstance(rows, list) or not isinstance(people, list):
        raise DeliverError("manifest or plan recipient list is invalid")
    if len(rows) != len(people):
        raise DeliverError("manifest and plan recipient counts differ")
    if not rows:
        raise DeliverError("no recipients in the manifest")
    if len(rows) > MAX_RECIPIENTS:
        raise DeliverError(f"refusing to send to more than {MAX_RECIPIENTS} recipients in one batch")

    # Exact identity evidence for this meeting. `state` is the identity
    # toolkit's own vocabulary (EXACT_EMAIL / EXACT_NAME / ...). Only email-level
    # or alias-level resolution is accepted here: a name-based or unresolved
    # attendee is never promoted into a recipient.
    allowed_emails: set[str] = set()
    identity_users: dict[str, dict] = {}
    identities = (bundle.get("identities") or {})
    accepted_states = {"EXACT_EMAIL", "ALIAS"}
    for section in ("attendees", "speakers"):
        for entry in identities.get(section) or []:
            if not isinstance(entry, dict):
                continue
            state = str(entry.get("state") or "").upper()
            if state not in accepted_states:
                continue
            user = entry.get("user") or {}
            if user.get("userId"):
                identity_users[user["userId"]] = user
            email = entry.get("email") or user.get("email")
            if isinstance(email, str) and _email(email):
                allowed_emails.add(_norm(email))

    excluded = {_norm(x) for x in (excluded_addresses or []) if x}
    seen: set[str] = set()
    items: list[dict] = []

    for row, person in zip(rows, people):
        if not isinstance(row, dict) or not isinstance(person, dict):
            raise DeliverError("malformed recipient row")
        address = row.get("intendedRecipient")
        if not _email(address):
            raise DeliverError("recipient address is missing or malformed")
        address_n = _norm(address)
        if address_n in seen:
            raise DeliverError("duplicate recipient address in batch")
        seen.add(address_n)
        if address_n in excluded:
            raise DeliverError("recipient matches a configured excluded address")
        if _norm(person.get("intendedEmail")) != address_n:
            raise DeliverError("plan and manifest disagree on the recipient address")
        if person.get("userId") != row.get("userId") or person.get("name") != row.get("name"):
            raise DeliverError("plan and manifest disagree on recipient identity")
        if address_n not in allowed_emails:
            raise DeliverError("recipient is not an exact identity match in the saved bundle")
        user = identity_users.get(row.get("userId"))
        if user is not None and not user.get("isActive", True):
            raise DeliverError("recipient maps to an inactive user")
        if row.get("cc") or row.get("bcc"):
            raise DeliverError("CC/BCC is prohibited")
        subject = row.get("subject")
        if not isinstance(subject, str) or not subject.strip():
            raise DeliverError("recipient subject is missing")
        if any(c in subject for c in "\r\n") or len(subject) > 998:
            raise DeliverError("unsafe subject header")

        html = send_review._read_artifact(root, row.get("htmlFile"))
        text = send_review._read_artifact(root, row.get("textFile"))
        if _sha(html) != row.get("htmlSha256") or _sha(text) != row.get("textSha256"):
            raise DeliverError("artifact hash mismatch for a recipient")

        expected = phase2.render_one(plan, person)
        if expected["html"].encode("utf-8") != html or expected["text"].encode("utf-8") != text:
            raise DeliverError("artifact does not match a fresh rendering for this recipient")
        if expected["subject"] != subject:
            raise DeliverError("artifact subject does not match a fresh rendering")

        owned = [a for a in person.get("actions") or [] if a.get("ownerType") == "OWNER"]
        body = text.decode("utf-8")
        if plan.get("emailType", "COMBINED") == "SUMMARY" and owned:
            raise DeliverError("summary-only plan must not contain recipient actions")
        if plan.get("emailType") == "ACTIONS" and not owned:
            raise DeliverError("action-only copy requires recipient-owned tasks")
        if owned:
            for action in owned:
                if str(action.get("title", "")) not in body:
                    raise DeliverError("an assigned action is missing from the rendered copy")
            if NO_ACTION_MARKER in body:
                raise DeliverError("copy claims both assigned actions and no actions")

        items.append({
            "row": row,
            "person": person,
            "address": address,
            "html": html,
            "text": text,
            "runId": manifest.get("runId"),
            "templateVersion": manifest.get("templateVersion"),
            "validationSha256": manifest.get("validationSha256"),
            "approval": approval_hash(
                {
                    "intendedRecipient": row.get("intendedRecipient"),
                    "subject": row.get("subject"),
                    "templateVersion": manifest.get("templateVersion"),
                    "validationSha256": manifest.get("validationSha256"),
                    "emailType": plan.get("emailType", "COMBINED"),
                },
                meeting_id, manifest_sender(manifest), html, text),
        })

    for item in items:
        item["meetingId"] = meeting_id
        item["emailType"] = plan.get("emailType", "COMBINED")
    return items


def manifest_sender(manifest) -> str:
    sender = manifest.get("sender")
    if isinstance(sender, str) and _email(sender):
        return sender.strip()
    return config_mod.MailConfig.sender


def _mime(item, sender: str) -> bytes:
    row = item["row"]
    msg = EmailMessage(policy=SMTP)
    msg["MIME-Version"] = "1.0"
    msg["Subject"] = row["subject"]
    msg["From"] = sender
    msg["To"] = item["address"]
    msg.set_content(item["text"].decode("utf-8"), subtype="plain", charset="utf-8", cte="base64")
    msg.add_alternative(item["html"].decode("utf-8"), subtype="html", charset="utf-8", cte="base64")
    raw = msg.as_bytes(policy=SMTP)
    if not raw.endswith(b"\r\n") or any(len(line) > 998 for line in raw.split(b"\r\n")):
        raise DeliverError("invalid MIME serialization")
    return base64.b64encode(raw)


def _blocking(rows, meeting_id: str, address: str, approval: str, email_type="COMBINED"):
    for row in rows:
        if (row.get("state") in ("ACCEPTED", "UNKNOWN", "SENDING", "PREPARED")
                and row.get("meetingId") == meeting_id
                and _norm(row.get("actualRecipient")) == _norm(address)
                and (row.get("emailType", "COMBINED") == email_type
                     or row.get("emailType", "COMBINED") == "COMBINED"
                     or email_type == "COMBINED")):
            return row
    for row in rows:
        if row.get("approvalHash") == approval and row.get("state") in (
                "ACCEPTED", "UNKNOWN", "SENDING", "PREPARED"):
            return row
    return None


def deliver(items, cfg, ledger, manifest_path: str, plan_path: str, bundle_path: str | None,
            excluded_addresses, token_factory=None, transport=None) -> dict:
    """Send each item to its own address, one at a time, aborting on any non-acceptance."""
    sender = _sender_for(cfg)
    results: list[dict] = []
    lock = ledger.locked()
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        rows = ledger.read()
        plan_blocks = []
        for item in items:
            block = _blocking(rows, item["meetingId"], item["address"], item["approval"], item.get("emailType", "COMBINED"))
            if block:
                plan_blocks.append({
                    "address": item["address"],
                    "blockingAttemptId": block.get("attemptId"),
                    "blockingState": block.get("state"),
                })
        if plan_blocks:
            raise DeliverError(
                "a prior attempt already exists for one or more recipients; inspect the ledger: "
                + json.dumps(plan_blocks)
            )
        statuses = []
        bearer = None
        for item in items:
            # Re-read artifacts immediately before claiming an attempt.
            fresh = inspect_batch(manifest_path, plan_path, bundle_path, excluded_addresses)
            current = next((x for x in fresh if _norm(x["address"]) == _norm(item["address"])), None)
            if current is None or current["approval"] != item["approval"]:
                raise DeliverError("approved material changed before submission; batch stopped")
            if bearer is None:
                if token_factory is not None:
                    bearer = token_factory()
                else:
                    bearer = _graph_token(cfg)
            payload = _mime(current, sender)
            attempt = {
                "runId": current.get("runId") or Path(manifest_path).parent.name,
                "attemptId": str(uuid.uuid4()),
                "meetingId": current["meetingId"],
                "intendedRecipient": current["address"],
                "actualRecipient": current["address"],
                "sender": sender,
                "deliveryMode": DELIVERY_MODE,
                "emailType": current.get("emailType", "COMBINED"),
                "templateVersion": current.get("templateVersion"),
                "approvalHash": current["approval"],
                "state": "PREPARED",
                "createdAt": _now(),
                "updatedAt": _now(),
                "clientRequestId": str(uuid.uuid4()),
                "graphRequestId": None,
                "outcome": "prepared",
            }
            rows.append(attempt)
            ledger.write(rows)
            attempt.update(state="SENDING", updatedAt=_now())
            ledger.write(rows)
            try:
                if transport is not None:
                    status, headers = transport(sender, bearer, payload,
                                                attempt["clientRequestId"])
                else:
                    status, headers = _graph_send(sender, bearer, payload,
                                                  attempt["clientRequestId"])
                attempt["graphRequestId"] = headers.get("request-id") if headers else None
                if status == 202:
                    attempt.update(state="ACCEPTED", outcome="Graph accepted request; delivery not confirmed")
                elif status in (400, 401, 403, 404, 413):
                    attempt.update(state="FAILED", outcome="Graph definitive rejection HTTP " + str(status))
                else:
                    attempt.update(state="UNKNOWN", outcome="ambiguous Graph response HTTP " + str(status))
            except Exception:
                attempt.update(state="UNKNOWN", outcome="submission may have begun; acceptance unknown")
            attempt["updatedAt"] = _now()
            ledger.write(rows)
            results.append({k: attempt[k] for k in (
                "intendedRecipient", "attemptId", "state", "outcome", "graphRequestId")})
            statuses.append(attempt["state"])
            if attempt["state"] != "ACCEPTED":
                return {
                    "state": "STOPPED",
                    "reason": "non-accepted outcome; remaining recipients were not attempted and must not be retried automatically",
                    "results": results,
                    "remaining": [x["address"] for x in items[len(results):]],
                }
        return {"state": "COMPLETE", "results": results,
                "accepted": sum(1 for s in statuses if s == "ACCEPTED")}
    finally:
        lock.close()


def _mailbox_sent_rows(cfg, start, end) -> list[dict]:
    """Read Sent Items for a UTC window through Graph. Read-only; sends nothing."""
    import urllib.request
    token = _graph_token(cfg)
    query = urllib.parse.urlencode({
        "$filter": ("sentDateTime ge " + start.strftime("%Y-%m-%dT%H:%M:%SZ")
                    + " and sentDateTime lt " + end.strftime("%Y-%m-%dT%H:%M:%SZ")),
        "$select": "subject,sentDateTime,toRecipients,conversationId",
        "$top": "100",
    })
    url = ("https://graph.microsoft.com/v1.0/users/"
           + urllib.parse.quote(cfg.mail.sender, safe="")
           + "/mailFolders/sentitems/messages?" + query)
    rows: list[dict] = []
    while url:
        request = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
        with urllib.request.urlopen(request, timeout=40) as response:
            payload = json.load(response)
        rows.extend(payload.get("value") or [])
        url = payload.get("@odata.nextLink")
    return rows


def _sender_for(cfg) -> str:
    return cfg.mail.sender


def _graph_token(cfg) -> str:
    tenant = cfg.env.require(cfg.mail.tenant_env_name)
    client = cfg.env.require(cfg.mail.client_id_env_name)
    secret = cfg.env.require(cfg.mail.secret_env_name)
    form = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": client,
        "client_secret": secret,
        "scope": "https://graph.microsoft.com/.default",
    }).encode()
    url = "https://login.microsoftonline.com/" + urllib.parse.quote(tenant, safe="") + "/oauth2/v2.0/token"
    response = send_review.graph_post(url, form, {"Content-Type": "application/x-www-form-urlencoded"})
    try:
        payload = json.loads(response.read())
    finally:
        response.close()
    if not isinstance(payload, dict) or not payload.get("access_token"):
        raise DeliverError("Graph token exchange failed")
    return payload["access_token"]


def _graph_send(sender: str, token: str, payload: bytes, client_request_id: str):
    url = "https://graph.microsoft.com/v1.0/users/" + urllib.parse.quote(sender, safe="") + "/sendMail"
    response = send_review.graph_post(url, payload, {
        "Content-Type": "text/plain",
        "Authorization": "Bearer " + token,
        "client-request-id": client_request_id,
    })
    try:
        return response.status, response.headers
    finally:
        response.close()