"""Gate tests for lib.deliver — all offline, no network, no credentials.

Every test builds a tiny synthetic meeting with its own identity evidence and
preview artifacts. Nothing here touches real runs, the real ledger, or Graph.
"""
import json, os, shutil, tempfile
from pathlib import Path
from types import SimpleNamespace

from lib import deliver as dl
from lib import phase2


def build(root: Path, *, recipients, exclude_vlsi=False, tamper=None,
          states=None, active=None, add_vlsi_to_bundle=False, action_quote="Do a thing.",
          action_blob=None):
    """Create plan + manifest + bundle + artifacts for a synthetic meeting."""
    root.mkdir(parents=True, exist_ok=True)
    provider = action_blob or (
        "## Purpose\nTest purpose.\n## Takeaways\n- One takeaway.\n"
        f"## Action items\n- **{action_quote}.**: Ann Example\n"
    )
    plan = {
        "schemaVersion": 1,
        "meeting": {"id": "meet-1", "title": "Synthetic", "date": "2026-10-22", "timezone": "Asia/Kolkata"},
        "source": {"mode": "SUMMARY_ONLY"},
        "editorialMode": "PRESERVE_PROVIDER",
        "templateVersion": phase2.TEMPLATE_VERSION,
        "applicationUrl": "https://example.invalid/app",
        "sharedNotes": {
            "providerText": provider,
            "purpose": "Test purpose.",
            "takeaways": ["One takeaway."],
            "detailedSummary": [],
        },
        "recipients": [],
    }
    attendees = []
    for i, spec in enumerate(recipients):
        person = {
            "userId": spec["userId"], "name": spec["name"], "intendedEmail": spec["email"],
            "actions": spec.get("actions", []),
        }
        plan["recipients"].append(person)
        state = (states or {}).get(spec["email"], "EXACT_EMAIL")
        attendees.append({
            "label": spec["name"],
            "state": state,
            "emailVerified": state == "EXACT_EMAIL",
            "user": {"userId": spec["userId"], "email": spec["email"],
                     "displayName": spec["name"], "isActive": (active or {}).get(spec["email"], True)},
        })
    if add_vlsi_to_bundle:
        attendees.append({
            "label": "VLSI Delivery", "state": "EXACT_EMAIL", "emailVerified": True,
            "user": {"userId": "svc-1", "email": "vlsi_delivery@siliconpatterns.com",
                     "displayName": "VLSI Delivery", "isActive": True},
        })
    bundle = {
        "meeting": {"id": "meet-1"},
        "identities": {"attendees": attendees, "speakers": []},
        "summaries": {"count": 1, "items": [{
            "id": "sum-1", "template": "basic", "templateId": None, "source": "PROVIDER",
            "analysisRunId": None, "createdAt": "2026-10-22T00:00:00", "text": provider,
            "textSha256": phase2.hashlib.sha256(provider.encode()).hexdigest(),
            "charCount": len(provider),
        }]},
        "storedActionItems": {"count": 0, "items": []},
        "completeness": {"complete": True, "notes": []},
    }
    bundle_path = root / "bundle.json"
    bundle_path.write_text(json.dumps(bundle), encoding="utf-8")
    plan["source"] = {
        "mode": "SUMMARY_ONLY",
        "bundlePath": str(bundle_path),
        "bundleSha256": dl._sha(bundle_path.read_bytes()),
    }

    plan_path = root / "plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")

    validation = phase2.validate(plan, bundle)
    assert validation["valid"], validation["errors"]
    binding = {"result": validation, "source": plan["source"], "meeting": plan["meeting"],
               "editorialMode": plan["editorialMode"]}
    manifest = {
        "schemaVersion": 1,
        "runId": "20261022T000000Z-test01",
        "templateVersion": phase2.TEMPLATE_VERSION,
        "meeting": plan["meeting"],
        "editorialMode": plan["editorialMode"],
        "deliveryMode": "TEST_REVIEW",
        "validationBinding": binding,
        "validationSha256": dl._sha(dl.send_review.canonical(binding)),
        "validation": validation,
        "recipients": [],
        "sendPerformed": False,
    }
    for i, person in enumerate(plan["recipients"]):
        out = phase2.render_one(plan, person)
        html_name, text_name = f"recipient-{i+1:03d}.html", f"recipient-{i+1:03d}.txt"
        html_bytes, text_bytes = out["html"].encode(), out["text"].encode()
        if tamper == person["intendedEmail"]:
            html_bytes = html_bytes.replace(b"Meeting notes", b"Tampered notes", 1)
        (root / html_name).write_bytes(html_bytes)
        (root / text_name).write_bytes(text_bytes)
        manifest["recipients"].append({
            "name": person["name"], "userId": person["userId"],
            "intendedRecipient": person["intendedEmail"],
            "permittedActualTestRecipient": "yaswanth.ampolu@siliconpatterns.com",
            "subject": out["subject"], "htmlFile": html_name, "textFile": text_name,
            "htmlSha256": dl._sha(html_bytes), "textSha256": dl._sha(text_bytes),
        })
    manifest_path = root / "preview-manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return str(manifest_path), str(plan_path), str(bundle_path)


PERSON = {"userId": "u1", "name": "Ann Example", "email": "ann@example.com",
          "actions": [{"ownerType": "OWNER", "title": "Do a thing.", "body": "Assigned in the meeting summary.",
                       "sourceReferences": [{"sourceType": "PROVIDER_SUMMARY", "sourceId": "sum-1", "quote": "Do a thing."}]}]}
NOACT = {"userId": "u2", "name": "Bob Example", "email": "bob@example.com", "actions": []}


def ledger_for(root):
    return dl.send_review.Ledger(root / "runs")


def test_dry_run_and_execute_shape():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        m, p, b = build(root / "art", recipients=[PERSON, NOACT])
        items = dl.inspect_batch(m, p, b, ["vlsi_delivery@siliconpatterns.com"])
        assert [x["address"] for x in items] == ["ann@example.com", "bob@example.com"]
        assert items[0]["approval"] != items[1]["approval"]


def test_no_action_recipient_is_accepted_and_action_recipient_has_action():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        m, p, b = build(root / "art", recipients=[PERSON, NOACT])
        items = dl.inspect_batch(m, p, b, [])
        assert "Do a thing." in items[0]["text"].decode()
        assert dl.NO_ACTION_MARKER in items[1]["text"].decode()


def test_service_account_is_refused_even_when_it_is_an_attendee():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        svc = {"userId": "svc-1", "name": "VLSI Delivery",
               "email": "vlsi_delivery@siliconpatterns.com", "actions": []}
        m, p, b = build(root / "art", recipients=[svc], add_vlsi_to_bundle=True)
        try:
            dl.inspect_batch(m, p, b, ["vlsi_delivery@siliconpatterns.com"])
        except dl.DeliverError as exc:
            assert "excluded" in str(exc)
        else:
            raise AssertionError("service account was not refused")


def test_name_based_identity_is_not_promoted_to_recipient():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        m, p, b = build(root / "art", recipients=[PERSON], states={"ann@example.com": "EXACT_NAME"})
        try:
            dl.inspect_batch(m, p, b, [])
        except dl.DeliverError as exc:
            assert "exact identity match" in str(exc)
        else:
            raise AssertionError("name-based identity was accepted")


def test_inactive_user_is_refused():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        # The plan validator already rejects an inactive user; prove the
        # delivery gate does too, independently of that validator.
        m, p, b = build(root / "art", recipients=[PERSON])
        bundle = json.loads(Path(b).read_text())
        bundle["identities"]["attendees"][0]["user"]["isActive"] = False
        Path(b).write_text(json.dumps(bundle))
        try:
            dl.inspect_batch(m, p, b, [])
        except dl.DeliverError as exc:
            assert "inactive" in str(exc) or "bundle" in str(exc)
        else:
            raise AssertionError("inactive user was accepted")


def test_tampered_artifact_is_refused():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        m, p, b = build(root / "art", recipients=[PERSON, NOACT], tamper="ann@example.com")
        try:
            dl.inspect_batch(m, p, b, [])
        except dl.DeliverError:
            pass
        else:
            raise AssertionError("tampered artifact was accepted")


def test_bundle_meeting_mismatch_is_refused():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        m, p, b = build(root / "art", recipients=[PERSON])
        bundle = json.loads(Path(b).read_text())
        bundle["meeting"]["id"] = "other-meeting"
        Path(b).write_text(json.dumps(bundle))
        try:
            dl.inspect_batch(m, p, b, [])
        except dl.DeliverError as exc:
            assert "bundle" in str(exc)
        else:
            raise AssertionError("foreign bundle was accepted")


def _env():
    return SimpleNamespace(require=lambda name: "x")


def _cfg(root):
    return SimpleNamespace(
        mail=SimpleNamespace(sender="vlsi_delivery@siliconpatterns.com"),
        runs_directory=root / "runs",
        env=_env(),
    )


def test_deliver_sends_each_recipient_to_their_own_address_once():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        m, p, b = build(root / "art", recipients=[PERSON, NOACT])
        cfg = _cfg(root)
        ledger = ledger_for(root)
        items = dl.inspect_batch(m, p, b, ["vlsi_delivery@siliconpatterns.com"])
        seen = []

        def transport(sender, token, payload, crid):
            seen.append((sender, token))
            return 202, {"request-id": "rid"}

        out = dl.deliver(items, cfg, ledger, m, p, b,
                         ["vlsi_delivery@siliconpatterns.com"],
                         token_factory=lambda: "tok", transport=transport)
        assert out["state"] == "COMPLETE" and out["accepted"] == 2
        assert seen == [("vlsi_delivery@siliconpatterns.com", "tok")] * 2
        rows = dl.send_review.history(ledger)
        assert [r["actualRecipient"] for r in rows] == ["ann@example.com", "bob@example.com"]
        assert all(r["state"] == "ACCEPTED" and r["deliveryMode"] == "ATTENDEE" for r in rows)


def test_second_run_for_same_recipients_is_blocked():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        m, p, b = build(root / "art", recipients=[PERSON])
        cfg = _cfg(root)
        ledger = ledger_for(root)
        items = dl.inspect_batch(m, p, b, [])
        calls = []

        def transport(sender, token, payload, crid):
            calls.append(1)
            return 202, {"request-id": "rid"}

        dl.deliver(items, cfg, ledger, m, p, b, [], token_factory=lambda: "t", transport=transport)
        again = dl.inspect_batch(m, p, b, [])
        try:
            dl.deliver(again, cfg, ledger, m, p, b, [], token_factory=lambda: "t", transport=transport)
        except dl.DeliverError as exc:
            assert "prior attempt" in str(exc)
        else:
            raise AssertionError("duplicate batch was not blocked")
        assert len(calls) == 1


def test_non_accepted_outcome_stops_remaining_batch():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        m, p, b = build(root / "art", recipients=[PERSON, NOACT])
        cfg = _cfg(root)
        ledger = ledger_for(root)
        items = dl.inspect_batch(m, p, b, [])
        calls = []

        def transport(sender, token, payload, crid):
            calls.append(1)
            return 500, {"request-id": "rid"} if len(calls) == 1 else {}

        out = dl.deliver(items, cfg, ledger, m, p, b, [], token_factory=lambda: "t", transport=transport)
        assert out["state"] == "STOPPED"
        assert out["remaining"] == ["bob@example.com"]
        assert len(calls) == 1
        rows = dl.send_review.history(ledger)
        assert rows[-1]["state"] == "UNKNOWN"


def test_token_failure_prevents_any_submission():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        m, p, b = build(root / "art", recipients=[PERSON])
        cfg = _cfg(root)
        ledger = ledger_for(root)
        items = dl.inspect_batch(m, p, b, [])

        def boom():
            raise dl.DeliverError("Graph token exchange failed")

        try:
            dl.deliver(items, cfg, ledger, m, p, b, [], token_factory=boom)
        except dl.DeliverError:
            pass
        else:
            raise AssertionError("token failure did not stop delivery")
        assert dl.send_review.history(ledger) == []


def test_ledger_corruption_blocks_sending():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        m, p, b = build(root / "art", recipients=[PERSON])
        cfg = _cfg(root)
        ledger = ledger_for(root)
        items = dl.inspect_batch(m, p, b, [])
        ledger.root.mkdir(parents=True, exist_ok=True)
        ledger.file.write_text("{broken", encoding="utf-8")
        try:
            dl.deliver(items, cfg, ledger, m, p, b, [], token_factory=lambda: "t",
                       transport=lambda *a: (202, {}))
        except dl.send_review.SendError:
            pass
        else:
            raise AssertionError("corrupt ledger did not block delivery")


def test_recipient_limit_is_enforced():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        people = [{"userId": f"u{i}", "name": f"User {i}", "email": f"u{i}@example.com", "actions": []}
                  for i in range(dl.MAX_RECIPIENTS + 1)]
        m, p, b = build(root / "art", recipients=people)
        try:
            dl.inspect_batch(m, p, b, [])
        except dl.DeliverError as exc:
            assert "more than" in str(exc)
        else:
            raise AssertionError("recipient ceiling not enforced")

def test_typed_duplicate_scope_preserves_legacy_blocks():
 row={'state':'ACCEPTED','meetingId':'m','actualRecipient':'a@example.com','approvalHash':'old','emailType':'SUMMARY'}
 assert dl._blocking([row],'m','a@example.com','new','SUMMARY') is row
 assert dl._blocking([row],'m','a@example.com','new','ACTIONS') is None
 assert dl._blocking([row],'m','a@example.com','new','COMBINED') is row
 legacy=dict(row);legacy.pop('emailType')
 assert dl._blocking([legacy],'m','a@example.com','new','ACTIONS') is legacy
 for state in ('UNKNOWN','SENDING','PREPARED'):
  uncertain=dict(row,state=state)
  assert dl._blocking([uncertain],'m','a@example.com','new','SUMMARY') is uncertain
