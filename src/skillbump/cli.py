"""Command-line interface for author-side Skill releases."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from .release import ReleaseError, plan, prepare, verify


def _add_version_options(parser: argparse.ArgumentParser) -> None:
    versions = parser.add_mutually_exclusive_group()
    versions.add_argument(
        "--bump",
        choices=("patch", "minor", "major"),
        default="patch",
        help="semantic version component to bump (default: patch)",
    )
    versions.add_argument(
        "--to",
        dest="target_version",
        metavar="X.Y.Z",
        help="explicit target version; must be greater than the current version",
    )
    parser.add_argument(
        "--expect",
        metavar="X.Y.Z",
        help="fail unless all manifests currently contain this version",
    )


def _add_openai_submission_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--openai-submission",
        choices=("initial", "update", "skip"),
        default="skip",
        help=(
            "prepare the submission sheet as an initial listing, an update to an existing "
            "listing, or leave it untouched (default: skip)"
        ),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="skillbump",
        description="Prepare a formal release after an Agent Skill author changes their Skill.",
    )
    parser.add_argument(
        "-C",
        "--repo",
        default=".",
        help="Skill/plugin repository root (default: current directory)",
    )
    parser.add_argument("--json", action="store_true", dest="json_output")
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan_parser = subparsers.add_parser("plan", help="show the release plan without writing")
    _add_version_options(plan_parser)
    _add_openai_submission_option(plan_parser)

    prepare_parser = subparsers.add_parser(
        "prepare", help="stage, test, package, and transactionally publish release files"
    )
    _add_version_options(prepare_parser)
    _add_openai_submission_option(prepare_parser)
    notes = prepare_parser.add_mutually_exclusive_group(required=True)
    notes.add_argument("--notes", help="release notes describing what changed")
    notes.add_argument("--notes-file", help="UTF-8 file containing release notes")
    prepare_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="run the complete preparation in a temporary copy without changing the repository",
    )
    prepare_parser.add_argument(
        "--skip-repo-checks",
        action="store_true",
        help="skip scripts/validate_repo.py and unit tests (built-in checks still run)",
    )
    prepare_parser.add_argument(
        "--allow-limited-evidence",
        action="store_true",
        help=(
            "permit a live release when repository checks are absent or explicitly skipped; "
            "the limitation is recorded in release evidence"
        ),
    )

    subparsers.add_parser("verify", help="verify synchronized manifests, Skill copy, and ZIP")
    return parser


def _read_notes(args: argparse.Namespace) -> str:
    if args.notes is not None:
        return args.notes
    try:
        return Path(args.notes_file).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ReleaseError(f"cannot read release notes file {args.notes_file}: {exc}") from exc


def _print_plan(result: dict[str, Any]) -> None:
    print(
        f"{result['name']}: {result['current_version']} -> {result['target_version']} "
        f"({result['bump']})"
    )
    print("Version targets:")
    for path in result["version_targets"]:
        print(f"  - {path}")
    changes = result["sync_changes"]
    if changes:
        print("Canonical -> packaged Skill sync:")
        for change in changes:
            print(f"  - {change}")
    else:
        print("Canonical and packaged Skill already match.")
    print(f"Archive: {result['archive']}")
    print(f"Checklist: {result['checklist']}")
    checks = result["repository_checks"]
    if checks:
        print("Repository release gates:")
        for check in checks:
            print(
                f"  - [{check['kind']}] {check['id']}: "
                f"{' '.join(check['argv'])}"
            )
    else:
        print(
            "Repository release gates: NOT CONFIGURED "
            "(live prepare requires --allow-limited-evidence)"
        )
    print(f"OpenAI submission sheet: {result['openai_submission']}")
    print("No commit, push, store upload, or publish action will be performed.")


def _print_prepared(result: dict[str, Any]) -> None:
    if result["dry_run"]:
        prefix = (
            "Dry run passed"
            if not result["limited_evidence"]
            else "Dry run completed with limited evidence"
        )
    else:
        prefix = (
            "Release prepared"
            if not result["limited_evidence"]
            else "Package prepared with explicitly accepted limited evidence"
        )
    print(f"{prefix}: {result['name']} {result['previous_version']} -> {result['version']}")
    for check in result["checks"]:
        print(f"PASS [{check['kind']}] {check['id']}: {' '.join(check['argv'])}")
    if result["repository_checks_status"] == "not_configured":
        print("Repository checks: NOT CONFIGURED")
    elif result["repository_checks_status"] == "skipped":
        print("Repository checks: SKIPPED BY USER")
    print(f"Archive: {result['archive']}")
    print(f"SHA-256: {result['archive_sha256']}")
    print(f"Release inputs SHA-256: {result['release_input_sha256']}")
    print(f"Evidence: {result['release_evidence']}")
    print(f"Checklist: {result['checklist']}")
    print(f"OpenAI submission sheet: {result['openai_submission']}")
    if result["dry_run"]:
        print("The live repository was not changed.")
    else:
        print("Prepared only; commit, push, portal submission, and publish were not performed.")


def _print_verified(result: dict[str, Any]) -> None:
    if result["release_evidence_verified"] and not result["limited_evidence"]:
        prefix = "Package and evidence bindings verified"
    elif result["release_evidence_verified"]:
        prefix = "Package verified with limited release evidence"
    else:
        prefix = "Package verified; release evidence record is missing"
    print(f"{prefix}: {result['name']} {result['version']}")
    print(f"Archive: {result['archive']}")
    print(f"SHA-256: {result['archive_sha256']}")


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "plan":
            result = plan(
                args.repo,
                bump=args.bump,
                target_version=args.target_version,
                expect=args.expect,
                openai_submission=args.openai_submission,
            )
        elif args.command == "prepare":
            result = prepare(
                args.repo,
                notes=_read_notes(args),
                bump=args.bump,
                target_version=args.target_version,
                expect=args.expect,
                dry_run=args.dry_run,
                run_checks=not args.skip_repo_checks,
                openai_submission=args.openai_submission,
                allow_limited_evidence=args.allow_limited_evidence,
            )
        else:
            result = verify(args.repo)
    except ReleaseError as exc:
        if args.json_output:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, sort_keys=True))
        else:
            print(f"skillbump: {exc}", file=sys.stderr)
        return 2

    if args.json_output:
        print(json.dumps({"ok": True, **result}, ensure_ascii=False, sort_keys=True))
    elif args.command == "plan":
        _print_plan(result)
    elif args.command == "prepare":
        _print_prepared(result)
    else:
        _print_verified(result)
    return 0
