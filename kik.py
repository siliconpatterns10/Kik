#!/usr/bin/env python3
"""Kik - read-only meeting-ingestion toolkit.

This is an operational helper, not an application. It performs mechanical
operations only: loading configuration, querying Supabase read-only, retrieving
Render logs, extracting exact content, batching transcripts, resolving
identities by exact match, validating structured input, rendering HTML, and
(only after explicit approval) transporting mail.

It contains no LLM client, no summarisation, no semantic matching, and no
autonomous decisions. The assistant or task agent reads what this prints,
reasons about it, and writes the content.

Usage:
    python3 /root/Kik/kik.py <command> [options]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

TOOLKIT_ROOT = Path(__file__).resolve().parent
if str(TOOLKIT_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLKIT_ROOT))

from lib import config as config_mod  # noqa: E402
from lib import doctor as doctor_mod  # noqa: E402
from lib import fetch as fetch_mod  # noqa: E402
from lib import identity_command as identity_mod  # noqa: E402
from lib import inspect_meetings as inspect_mod  # noqa: E402
from lib import output as output_mod  # noqa: E402
from lib import runstore as runstore_mod  # noqa: E402
from lib import status as status_mod  # noqa: E402
from lib import renderlogs as logs_mod  # noqa: E402
from lib import transcript_command as transcript_mod  # noqa: E402
from lib import webhook_command as webhook_mod  # noqa: E402
from lib import phase2 as phase2_mod  # noqa: E402
from lib import send_review as send_mod  # noqa: E402
from lib import deliver as deliver_mod  # noqa: E402
import json as json_mod  # noqa: E402

PROGRAM = "kik.py"
VERSION = "0.1.0"
MAX_PRINT_CHARS = 20_000


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROGRAM,
        description="Read-only toolkit for investigating the meeting pipeline.",
        epilog=(
            "Every command supports --json for machine-readable output and "
            "--config to point at a non-default config file. Diagnostics go to "
            "stderr; JSON goes to stdout alone."
        ),
    )
    parser.add_argument("--version", action="version", version=f"{PROGRAM} {VERSION}")
    parser.add_argument(
        "--config",
        default=None,
        help="path to the config file (default: /root/Kik/config.toml, or "
        "config.example.toml when that is absent)",
    )
    parser.add_argument(
        "--json", action="store_true", help="emit one JSON document on stdout"
    )
    parser.add_argument(
        "--quiet", action="store_true", help="suppress progress messages on stderr"
    )

    # Each nested subparser level gets its own dest. Reusing one dest across
    # levels is what broke dispatch: argparse writes the last-parsed value, so
    # adding a nested subparser reset the top-level `command` to None for every
    # invocation. Renaming the inner dests keeps the names this module reads
    # unambiguous, and `_top_level_command` below reads the spelled name from a
    # private dest that no nested parser can touch.
    subparsers = parser.add_subparsers(dest="_kik_command", metavar="<command>")

    doctor = subparsers.add_parser(
        "doctor",
        help="check configuration; no network calls unless --network is given",
        description=(
            "Local configuration checks by default. --network adds connectivity "
            "and authentication checks. It never sends mail."
        ),
    )
    doctor.add_argument(
        "--network",
        action="store_true",
        help="also verify Supabase, Render and Graph reachability (never sends mail)",
    )
    doctor.add_argument(
        "--env",
        default=None,
        help="override the env file path from the config",
    )

    inspect_cmd = subparsers.add_parser(
        "inspect",
        help="report meetings and webhook activity for one local day",
        description=(
            "Computes the local midnight-to-midnight interval for --date, "
            "converts it to UTC, and reports meetings started in that window "
            "plus webhook activity received in that window."
        ),
    )
    inspect_cmd.add_argument("--date", required=True, help="local calendar day, YYYY-MM-DD")
    inspect_cmd.add_argument(
        "--timezone", default=None, help="IANA timezone (default: config timezone)"
    )
    inspect_cmd.add_argument(
        "--exclude-fixtures",
        action="store_true",
        help="omit records classified as known fixtures (classification is shown otherwise)",
    )
    inspect_cmd.add_argument(
        "--limit", type=int, default=50, help="max meetings to report (default 50)"
    )
    inspect_cmd.add_argument(
        "--persist",
        action="store_true",
        help="write the report into a new run directory under the artifacts root",
    )

    logs_cmd = subparsers.add_parser(
        "logs",
        help="retrieve Render service logs for one local day",
        description=(
            "Groups repeated messages and reports whether retrieval was complete. "
            "An absent log is not evidence that a webhook did not arrive."
        ),
    )
    logs_cmd.add_argument("--date", required=True, help="local calendar day, YYYY-MM-DD")
    logs_cmd.add_argument("--timezone", default=None, help="IANA timezone")
    logs_cmd.add_argument("--text", default=None, help="substring filter")
    logs_cmd.add_argument(
        "--level", default=None, help="filter by level label (error, warn, info)"
    )
    logs_cmd.add_argument("--limit", type=int, default=100, help="page size, max 100")
    logs_cmd.add_argument(
        "--max-pages", type=int, default=20, help="hard ceiling on pagination pages"
    )
    logs_cmd.add_argument(
        "--owner-id", default=None, help="override Render owner id from config"
    )
    logs_cmd.add_argument(
        "--service-id", default=None, help="override Render service id from config"
    )
    logs_cmd.add_argument(
        "--runtime",
        action="store_true",
        help="filter to container-level messages rather than app output",
    )
    logs_cmd.add_argument(
        "--persist",
        action="store_true",
        help="write the retrieved evidence into a new run directory",
    )

    fetch_cmd = subparsers.add_parser(
        "fetch",
        help="fetch the complete available evidence for one selected meeting",
        description=(
            "Writes a bundle into a new run directory under the artifacts root. "
            "Failed webhook rows with no linked meeting are included when the "
            "provider meeting id can be read from their own payload, and the "
            "linkage method is recorded per row. Nothing is overwritten."
        ),
    )
    fetch_cmd.add_argument("--meeting-id", default=None, help="database meeting id")
    fetch_cmd.add_argument(
        "--persist",
        action="store_true",
        help="accepted for symmetry; fetch always persists its bundle",
    )
    fetch_cmd.add_argument(
        "--date", default=None, help="local calendar day; requires --all when several match"
    )
    fetch_cmd.add_argument("--timezone", default=None, help="IANA timezone")
    fetch_cmd.add_argument(
        "--all",
        action="store_true",
        help="fetch every meeting that matches --date into separate bundles",
    )
    fetch_cmd.add_argument(
        "--include-raw-bodies",
        action="store_true",
        help="also store each related webhook raw body as a private artifact file",
    )
    fetch_cmd.add_argument(
        "--raw-body-limit",
        type=int,
        default=5_000_000,
        help="refuse to store a raw body larger than this many characters",
    )

    identities_cmd = subparsers.add_parser(
        "identities",
        help="map attendees and speaker labels to user records, exact rules only",
        description=(
            "Exact email, explicit alias, then a unique exact display name. "
            "Unmatched, ambiguous and inactive labels are reported, never resolved. "
            "--bundle reproduces a saved bundle with no network or credentials."
        ),
    )
    identities_cmd.add_argument("--meeting-id", default=None, help="database meeting id")
    identities_cmd.add_argument(
        "--bundle", default=None, help="path to a saved bundle; offline, no credentials"
    )

    transcript_cmd = subparsers.add_parser(
        "transcript",
        help="deterministic transcript batching over a saved bundle",
        description=(
            "Batching is deterministic and lossless: the same bundle and limit "
            "always produce the same boundaries and batch ids, and every source "
            "character appears in exactly one batch. No summarisation."
        ),
    )
    transcript_sub = transcript_cmd.add_subparsers(dest="transcript_command", metavar="<subcommand>")

    batches_cmd = transcript_sub.add_parser("batches", help="plan batches and report coverage")
    batches_cmd.add_argument("--bundle", required=True, help="path to a saved bundle")
    batches_cmd.add_argument("--max-chars", type=int, default=16000, help="characters per batch")
    batches_cmd.add_argument(
        "--persist", action="store_true", help="write the plan and batch files into a new run"
    )

    read_cmd = transcript_sub.add_parser("read", help="print one batch's exact source text")
    read_cmd.add_argument("--bundle", required=True, help="path to a saved bundle")
    read_cmd.add_argument("--batch", type=int, required=True, help="1-based batch number")
    read_cmd.add_argument("--max-chars", type=int, default=None, help="limit if no persisted --plan is supplied")
    read_cmd.add_argument("--plan", default=None, help="persisted batches.json plan; verifies source hash and reuses boundaries")
    read_cmd.add_argument(
        "--acknowledge",
        action="store_true",
        help="record an agent acknowledgement that this batch was read",
    )
    read_cmd.add_argument(
        "--persist", action="store_true", help="write the plan and batch files into a new run"
    )

    webhook_cmd = subparsers.add_parser(
        "webhook",
        help="read selected content out of a preserved raw webhook body",
        description=(
            "fetch stores raw bodies privately and prints only a digest. This "
            "command reads one body deliberately: by selector, or as a structure "
            "listing. The file is never modified."
        ),
    )
    webhook_sub = webhook_cmd.add_subparsers(dest="webhook_command", metavar="<subcommand>")
    raw_cmd = webhook_sub.add_parser("raw", help="read a stored raw webhook body")
    raw_cmd.add_argument("--run", default=None, help="a run directory written by fetch")
    raw_cmd.add_argument(
        "--run-id",
        default=None,
        help="a run id; resolved under the artifacts root, so the path need not be typed",
    )
    raw_cmd.add_argument("--inbox-id", default=None, help="meeting_webhook_inbox id")
    raw_cmd.add_argument("--path", default=None, help="any JSON file to read directly")
    raw_cmd.add_argument(
        "--json-path", default=None, help="selector, e.g. data.meeting.title"
    )
    raw_cmd.add_argument(
        "--list-keys", action="store_true", help="list key paths and types, with no values"
    )
    raw_cmd.add_argument(
        "--max-chars", type=int, default=MAX_PRINT_CHARS, help="display ceiling for a value"
    )

    validate_cmd = subparsers.add_parser("validate", help="mechanically validate an agent-authored email plan")
    validate_cmd.add_argument("--input", required=True, help="email-plan JSON")
    validate_cmd.add_argument("--bundle", default=None, help="optional source bundle for evidence checks")

    render_cmd = subparsers.add_parser("render", help="render validated plan to offline local previews")
    render_cmd.add_argument("--input", required=True, help="email-plan JSON")
    render_cmd.add_argument("--bundle", default=None, help="optional source bundle for evidence checks")

    send_cmd = subparsers.add_parser("send-review", help="dry-run or controlled test-recipient Graph review")
    send_cmd.add_argument("--preview-manifest")
    send_cmd.add_argument("--person")
    mode=send_cmd.add_mutually_exclusive_group(); mode.add_argument("--dry-run",action="store_true"); mode.add_argument("--execute",action="store_true")
    send_cmd.add_argument("--approved-hash")
    send_cmd.add_argument("--history",action="store_true")

    deliver_cmd = subparsers.add_parser(
        "deliver-attendees",
        help="send each validated tailored copy to its own matched attendee address",
        description=(
            "Batch attendee delivery. Requires --approve-recipients before any "
            "submission; without it the command is always a dry run. Every "
            "recipient must be an exact identity match for the meeting, active, "
            "and absent from the exclusion list. Each recipient is submitted "
            "once, to their own address, with no CC/BCC."
        ),
    )
    deliver_cmd.add_argument("--preview-manifest", required=True, help="render preview manifest JSON")
    deliver_cmd.add_argument("--input", required=True, help="validated email-plan JSON")
    deliver_cmd.add_argument("--bundle", default=None, help="source bundle; defaults to the plan's recorded bundle")
    deliver_cmd.add_argument(
        "--approve-recipients", action="store_true",
        help="required acknowledgement that this batch sends to the attendees themselves",
    )
    deliver_cmd.add_argument("--execute", action="store_true", help="submit; omitted means dry run")
    deliver_cmd.add_argument("--history", action="store_true", help="print the shared delivery ledger")

    runs_cmd = subparsers.add_parser(
        "runs", help="list artifact runs so saved evidence can be reused"
    )
    runs_cmd.add_argument("--date", default=None, help="limit to one local day")
    runs_cmd.add_argument("--command", default=None, help="limit to one command name")

    return parser


def resolve_config_path(raw: str | None) -> Path | None:
    if raw:
        return Path(raw).expanduser()
    if config_mod.DEFAULT_CONFIG_PATH.exists():
        return config_mod.DEFAULT_CONFIG_PATH
    if config_mod.EXAMPLE_CONFIG_PATH.exists():
        return config_mod.EXAMPLE_CONFIG_PATH
    return None


def load_config_or_report(output: output_mod.Output, raw_path: str | None):
    """Load config + env. On failure, print a CONFIGURATION_ERROR result.

    Returns (config, None) on success or (None, exit_code) on failure.
    """
    path = resolve_config_path(raw_path)
    try:
        cfg = config_mod.load_config(path)
    except config_mod.ConfigError as exc:
        result = status_mod.Result(
            command="config",
            status=status_mod.CONFIGURATION_ERROR,
            data={"configuration": {"configFile": str(path) if path else "(none)"}},
            findings=[status_mod.Finding("load-config", False, str(exc))],
        )
        return None, output_mod.emit_result(output, result)

    try:
        config_mod.attach_env(cfg)
    except config_mod.ConfigError as exc:
        result = status_mod.Result(
            command="config",
            status=status_mod.CONFIGURATION_ERROR,
            data={"configuration": {"configFile": str(cfg.path), "envFile": str(cfg.env_file)}},
            findings=[status_mod.Finding("load-env", False, str(exc))],
        )
        return None, output_mod.emit_result(output, result)

    return cfg, None


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    output = output_mod.Output(json_mode=args.json, quiet=args.quiet)

    command = args._kik_command

    if not command:
        parser.print_help(sys.stderr)
        return status_mod.exit_code_for(status_mod.USAGE_ERROR)

    if command in ("validate", "render"):
        try:
            plan_path = Path(args.input).expanduser()
            plan = json_mod.loads(plan_path.read_text(encoding="utf-8"))
            bundle = json_mod.loads(Path(args.bundle).expanduser().read_text(encoding="utf-8")) if args.bundle else None
            checked = phase2_mod.validate(plan, bundle)
            if command == "validate":
                return output_mod.emit_result(output, status_mod.Result(command="validate", status=status_mod.OK if checked['valid'] else status_mod.CONTENT_UNAVAILABLE, data=checked))
            if not checked['valid']:
                return output_mod.emit_result(output, status_mod.Result(command="render", status=status_mod.CONTENT_UNAVAILABLE, data=checked))
            cfg_path = resolve_config_path(args.config)
            cfg = config_mod.load_config(cfg_path)
            directory, checked = phase2_mod.render_run(plan, cfg.runs_directory, bundle)
            return output_mod.emit_result(output, status_mod.Result(command="render", status=status_mod.OK, data={"previewDirectory":directory,"validation":checked}))
        except (OSError, ValueError, KeyError, TypeError, config_mod.ConfigError) as exc:
            return output_mod.emit_result(output, status_mod.Result(command=command, status=status_mod.USAGE_ERROR, findings=[status_mod.Finding("plan",False,str(exc))]))

    if command == "runs":
        cfg, failure = load_config_or_report(output, args.config)
        if cfg is None: return failure or 1
        return output_mod.emit_result(output, _runs(cfg, date=args.date, command_filter=getattr(args, "command", None)))

    if command == "deliver-attendees":
        try:
            cfg_path = resolve_config_path(args.config)
            cfg = config_mod.load_config(cfg_path)
            ledger = send_mod.Ledger(cfg.runs_directory)
            if args.history:
                if args.execute: raise deliver_mod.DeliverError("--history cannot be combined with --execute")
                return output_mod.emit_result(output, status_mod.Result(command=command, status=status_mod.OK, data={"history": send_mod.history(ledger)}))
            excluded = list(getattr(cfg.mail, "excluded_recipients", []) or [])
            if not excluded:
                raise deliver_mod.DeliverError(
                    "mail.excluded_recipients is empty in the config; refusing to send without "
                    "an explicit service-account exclusion list"
                )
            items = deliver_mod.inspect_batch(args.preview_manifest, args.input, args.bundle, excluded)
            addresses = [x["address"] for x in items]
            if not args.approve_recipients or not args.execute:
                data = {"status": "DRY_RUN", "sender": cfg.mail.sender, "meetingId": items[0]["meetingId"],
                        "recipientCount": len(items), "recipients": addresses, "deliveryMode": deliver_mod.DELIVERY_MODE,
                        "approvedRecipients": bool(args.approve_recipients), "networkUsed": False, "sendPerformed": False,
                        "note": "no submission without --approve-recipients and --execute"}
                return output_mod.emit_result(output, status_mod.Result(command=command, status=status_mod.OK, data=data))
            config_mod.attach_env(cfg)
            result = deliver_mod.deliver(items, cfg, ledger, args.preview_manifest, args.input, args.bundle, excluded)
            return output_mod.emit_result(output, status_mod.Result(command=command, status=status_mod.OK, data=result))
        except Exception as exc:
            return output_mod.emit_result(output, status_mod.Result(command=command, status=status_mod.USAGE_ERROR, findings=[status_mod.Finding("deliver-attendees", False, str(exc))]))

    if command == "send-review":
        try:
            if args.history:
                if args.execute or args.dry_run or args.preview_manifest or args.person or args.approved_hash: raise send_mod.SendError("--history cannot be combined with preview/send options")
                cfg=config_mod.load_config(resolve_config_path(args.config)); ledger=send_mod.Ledger(cfg.runs_directory)
                return output_mod.emit_result(output,status_mod.Result(command=command,status=status_mod.OK,data={"history":send_mod.history(ledger)}))
            if not args.preview_manifest or not args.person: raise send_mod.SendError("--preview-manifest and --person are required")
            review=send_mod.inspect_manifest(args.preview_manifest,args.person)
            cfg_path=resolve_config_path(args.config); cfg=config_mod.load_config(cfg_path)
            ledger=send_mod.Ledger(cfg.runs_directory)

            if not args.execute:
                block=ledger.blocking(review['hash'])
                data={"status":"DRY_RUN","sender":review['sender'],"intendedRecipient":review['intended'],"actualRecipient":review['actual'],"subject":review['subject'],"approvalHash":review['hash'],"runId":review['runId'],"meetingId":review['meetingId'],"deliveryMode":"TEST_REVIEW","duplicateStatus":"BLOCKED" if block else "ELIGIBLE","blockingAttemptId":block.get('attemptId') if block else None,"networkUsed":False,"sendPerformed":False}
                return output_mod.emit_result(output,status_mod.Result(command=command,status=status_mod.OK,data=data))
            if not args.approved_hash or args.approved_hash!=review['hash']:
                raise send_mod.SendError("matching --approved-hash is required")
            config_mod.attach_env(cfg)
            attempt=send_mod.execute(review,cfg,ledger,args.approved_hash,manifest_path=args.preview_manifest)
            state=attempt['state']; status=status_mod.OK if state in ('ACCEPTED','FAILED') else status_mod.PROVIDER_ERROR
            return output_mod.emit_result(output,status_mod.Result(command=command,status=status,data={"attempt":attempt}))
        except Exception as exc:
            return output_mod.emit_result(output,status_mod.Result(command=command,status=status_mod.USAGE_ERROR,findings=[status_mod.Finding("send-review",False,str(exc))]))

    cfg, failure = load_config_or_report(output, args.config)
    if cfg is None:
        return failure or 1

    if command == "doctor":
        if args.env:
            cfg.env_file = Path(args.env).expanduser()
            try:
                config_mod.attach_env(cfg)
            except config_mod.ConfigError as exc:
                result = status_mod.Result(
                    command="doctor",
                    status=status_mod.CONFIGURATION_ERROR,
                    data={"configuration": {"envFile": str(cfg.env_file)}},
                    findings=[status_mod.Finding("load-env", False, str(exc))],
                )
                return output_mod.emit_result(output, result)
        result = doctor_mod.run(cfg, network=args.network, output=output)
        return output_mod.emit_result(output, result)

    if command == "inspect":
        result = inspect_mod.run(
            cfg,
            date_text=args.date,
            timezone_name=args.timezone,
            exclude_fixtures=args.exclude_fixtures,
            limit=args.limit,
            persist=getattr(args, "persist", False),
            output=output,
        )
        return output_mod.emit_result(output, result)

    if command == "logs":
        result = logs_mod.run(
            cfg,
            date_text=args.date,
            timezone_name=args.timezone,
            text=args.text,
            level=args.level,
            limit=args.limit,
            max_pages=args.max_pages,
            owner_id=args.owner_id,
            service_id=args.service_id,
            runtime=args.runtime,
            persist=getattr(args, "persist", False),
            output=output,
        )
        return output_mod.emit_result(output, result)

    if command == "fetch":
        result = fetch_mod.run(
            cfg,
            meeting_id=args.meeting_id,
            date_text=args.date,
            timezone_name=args.timezone,
            all_matches=args.all,
            include_raw_bodies=args.include_raw_bodies,
            raw_body_limit=args.raw_body_limit,
            output=output,
        )
        return output_mod.emit_result(output, result)

    if command == "identities":
        result = identity_mod.run(
            cfg,
            meeting_id=args.meeting_id,
            bundle_path=args.bundle,
            output=output,
        )
        return output_mod.emit_result(output, result)

    if command == "transcript":
        if not args.transcript_command:
            parser.parse_args(["transcript", "--help"])
            return status_mod.exit_code_for(status_mod.USAGE_ERROR)
        if args.transcript_command == "batches":
            result = transcript_mod.run_batches(
                cfg,
                bundle_path=args.bundle,
                max_chars=args.max_chars,
                persist=args.persist,
                output=output,
            )
        else:
            result = transcript_mod.run_read(
                cfg,
                bundle_path=args.bundle,
                batch_number=args.batch,
                max_chars=args.max_chars,
                acknowledge=args.acknowledge,
                plan_path=args.plan,
                persist=args.persist,
                output=output,
            )
        return output_mod.emit_result(output, result)

    if command == "webhook":
        if not args.webhook_command:
            parser.parse_args(["webhook", "--help"])
            return status_mod.exit_code_for(status_mod.USAGE_ERROR)
        result = webhook_mod.run_raw(
            cfg,
            run_directory=args.run,
            run_id=args.run_id,
            inbox_id=args.inbox_id,
            path=args.path,
            json_path=args.json_path,
            list_keys=args.list_keys,
            max_chars=args.max_chars,
            output=output,
        )
        return output_mod.emit_result(output, result)

    if command in ("validate", "render"):
        try:
            plan_path = Path(args.input).expanduser()
            plan = json_mod.loads(plan_path.read_text(encoding="utf-8"))
            bundle = json_mod.loads(Path(args.bundle).expanduser().read_text(encoding="utf-8")) if args.bundle else None
            if command == "validate":
                checked = phase2_mod.validate(plan, bundle)
                result = status_mod.Result(command="validate", status=status_mod.OK if checked['valid'] else status_mod.CONTENT_UNAVAILABLE, data=checked)
                return output_mod.emit_result(output, result)
            directory, checked = phase2_mod.render_run(plan, cfg.runs_directory, bundle)
            result = status_mod.Result(command="render", status=status_mod.OK, data={"previewDirectory":directory,"validation":checked})
            return output_mod.emit_result(output, result)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            result = status_mod.Result(command=command, status=status_mod.USAGE_ERROR, findings=[status_mod.Finding("plan",False,str(exc))])
            return output_mod.emit_result(output,result)

    if command == "runs":
        result = _runs(cfg, date=args.date, command_filter=getattr(args, "command", None))
        return output_mod.emit_result(output, result)

    parser.print_help(sys.stderr)
    return status_mod.exit_code_for(status_mod.USAGE_ERROR)


def _runs(cfg, *, date: str | None, command_filter: str | None) -> status_mod.Result:
    discovered = runstore_mod.discover_runs(cfg.runs_directory, local_date=date)
    if command_filter:
        discovered = [r for r in discovered if r.command == command_filter]
    return status_mod.Result(
        command="runs",
        status=status_mod.OK if discovered else status_mod.NO_MEETINGS,
        data={
            "runsDirectory": str(cfg.runs_directory),
            "count": len(discovered),
            "runs": [r.as_dict() for r in discovered],
        },
        notes=[
            "a run directory is never overwritten; each fetch and each persisted "
            "batch plan produces a new one"
        ]
        if discovered
        else ["no runs written yet"],
    )


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        raise SystemExit(130) from None