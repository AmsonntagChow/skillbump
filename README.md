# SkillBump

Release an Agent Skill you own without forgetting a manifest, shipping a stale
copy, or uploading the wrong ZIP.

SkillBump is an author-side release harness for the standard Claude Code and
ChatGPT/Codex plugin layout. After you edit `skills/<name>/`, it can prepare a
formal release such as `1.0.0 → 1.0.1` in one transaction:

1. verify that the current Claude and Codex versions agree;
2. synchronize the canonical Skill into the packaged Codex plugin;
3. bump every existing explicit version field;
4. record release notes and, only for an explicit OpenAI `initial` or `update`
   submission, update the portal submission sheet;
5. show and run the repository's release gates in a temporary copy;
6. build and byte-check a deterministic public ZIP;
7. bind the check results to the exact release-input and ZIP SHA-256 values; and
8. write a reviewable checklist plus machine-readable evidence record.

It deliberately does **not** commit, tag, push, upload, submit, or publish.

## The version is `1.0.1`, not `1.01`

Public plugin versions use semantic versioning: `MAJOR.MINOR.PATCH`. A patch
release after `1.0.0` is therefore `1.0.1`. SkillBump rejects shortened or
zero-padded forms such as `1.01`.

## Quick start

Python 3.11 or newer is required on macOS, Linux, or WSL.

```bash
python -m pip install /path/to/skillbump

# Read-only plan. Patch is the default.
skillbump -C /path/to/my-skill-repo plan

# Exercise the whole release in a temporary copy first.
skillbump -C /path/to/my-skill-repo prepare \
  --to 1.0.1 \
  --notes "Adds one-line problem summaries and tighter evidence rules." \
  --openai-submission update \
  --dry-run

# Publish the verified files into the local worktree.
skillbump -C /path/to/my-skill-repo prepare \
  --to 1.0.1 \
  --expect 1.0.0 \
  --notes "Adds one-line problem summaries and tighter evidence rules." \
  --openai-submission update

skillbump -C /path/to/my-skill-repo verify
```

Use `--bump minor` or `--bump major` instead of `--to` when appropriate.
`--expect` is an optional compare-and-swap guard for scripts and CI.
`--openai-submission` accepts `initial`, `update`, or `skip` and defaults to
`skip`. Choose `initial` only for a plugin that has no existing directory
listing, and choose `update` only after confirming the existing listing.

If neither `scripts/validate_repo.py` nor `tests/` exists, a dry run reports
limited evidence and a live release stops. `--allow-limited-evidence` is an
explicit escape hatch for a package-only release; the limitation is written to
the evidence record and checklist. `--skip-repo-checks` has the same requirement.

## Supported repository layout

SkillBump intentionally supports one convention instead of guessing arbitrary
release systems:

```text
.claude-plugin/
  plugin.json                       # optional, explicit version is bumped
  marketplace.json                  # optional, existing entry version is bumped
.agents/plugins/marketplace.json    # optional, never given a fake version
skills/<name>/                      # canonical Skill
plugins/<name>/
  .codex-plugin/plugin.json         # required public manifest
  skills/<name>/                    # synchronized packaged copy
submission/PLUGIN_DIRECTORY.md      # optional; changed only for initial/update
scripts/validate_repo.py            # optional standard repository check
tests/                              # optional unittest discovery
release/evidence/<version>.json     # generated, evidence bound to release inputs
```

A root-level `.codex-plugin/plugin.json` is also supported when the repository
itself is the plugin package.

If a Claude marketplace entry intentionally omits `version` and uses the Git
commit SHA as its release identity, SkillBump leaves that entry versionless.
It never adds a `version` field to `.agents/plugins/marketplace.json`; Codex
plugin identity lives in `.codex-plugin/plugin.json`.

## Commands

```text
skillbump [-C REPO] [--json] plan
  [--bump patch|minor|major | --to X.Y.Z]
  [--expect X.Y.Z]
  [--openai-submission initial|update|skip]

skillbump [-C REPO] [--json] prepare
  [--bump patch|minor|major | --to X.Y.Z]
  [--expect X.Y.Z]
  (--notes TEXT | --notes-file PATH)
  [--openai-submission initial|update|skip]
  [--dry-run]
  [--skip-repo-checks]
  [--allow-limited-evidence]

skillbump [-C REPO] [--json] verify
```

- `plan` is read-only and runs no repository code.
- `prepare --dry-run` copies the working tree, including the author's current
  uncommitted Skill edits, into a temporary directory and performs the full
  release there.
- `prepare` repeats that flow, confirms the live inputs did not change, then
  transactionally publishes only the version files, packaged Skill, ZIP,
  evidence record, checklist, and the submission sheet when `initial` or
  `update` was selected.
- `verify` checks version agreement, canonical/package equality, every ZIP
  member byte, and—when present—the release evidence against the current input
  bytes. It reports missing evidence honestly rather than treating package
  integrity as proof of behavior.

The built-in repository gate adapters are deliberately narrow and shell-free:

```text
python -B scripts/validate_repo.py
python -B -m unittest discover -s tests -v
```

They run only when those conventional paths exist, and `plan` prints both exact
commands before execution. SkillBump removes common credential variables from
their environment and rejects any check that changes release inputs. These are
still repository-owned programs, not sandboxed untrusted code; inspect them
before running a release.

## From a correct ZIP to behavioral evidence

A synchronized, reproducible ZIP proves packaging integrity. It does not prove
that a Skill still triggers, chooses the right workflow, or gives the right
answer. Put one or two critical user journeys into `scripts/validate_repo.py`
or `tests/` so the same gate runs locally and in CI before a release.

For deterministic behavior, use exact assertions. For LLM or Agent behavior,
wrap a small golden-set eval in one of those two adapters and make the command
exit nonzero when its calibrated threshold fails. If an LLM judge is genuinely
needed, calibrate it against held-out human labels and tolerate measured
variance; do not turn wording equality into a fake deterministic test.

Each successful live preparation writes
`release/evidence/<version>.json`. It records only the Skill identity, release
input SHA-256, ZIP path/size/SHA-256, named checks, and exit codes—no stdout,
timestamps, or secrets. `verify` recomputes those values, so changing a Skill,
manifest, validator, or test after the run makes the evidence stale. This is a
local reproducibility record, not a signed provenance attestation.

## What happens in each store

### Claude Code custom Git marketplace

Claude Code uses an explicit plugin version as its update key. After reviewing
the prepared diff, commit and push the bumped plugin and any custom marketplace
that the author owns. Users of that marketplace can refresh with:

```bash
claude plugin marketplace update <marketplace>
claude plugin update <plugin>@<marketplace>
```

If marketplace auto-update is enabled, Claude can refresh the marketplace and
installed plugin at startup. Pushing new files while keeping the same explicit
version does not update an installed cached plugin. See the
[Claude plugin versioning reference](https://code.claude.com/docs/en/plugins-reference#version-management).

### Claude Community Marketplace

An approved community plugin follows a different publication path. Push the
reviewed release to the plugin's own repository; Anthropic's community catalog
pins plugins to commits, automatically updates that pin through its CI, and
syncs the public catalog nightly. Do not edit Anthropic's marketplace entry or
claim immediate propagation. See the
[Claude plugin directory documentation](https://code.claude.com/docs/en/plugins).

### OpenAI universal Plugins Directory

The public directory shared by ChatGPT and Codex does not currently document a
release-upload API. A GitHub push alone never replaces the ZIP review and
publish flow. Select the portal intent explicitly before preparing the package:

- `initial`: no existing directory listing; identify the release as an initial
  submission in its notes;
- `update`: an existing same-name listing is confirmed; keep its plugin `name`,
  use a different valid SemVer, and describe what changed; or
- `skip`: the default; leave the OpenAI submission sheet unchanged.

For `initial` or `update`, upload the generated ZIP at
<https://platform.openai.com/plugins>, submit it for review, and select
**Publish** after approval. A repository version or Git push is not evidence
that any of those portal actions happened.

OpenAI documents this review/publish flow in
[Submit plugins](https://developers.openai.com/plugins/deploy/submission) and
documents the name/version checks in the
[submission error reference](https://developers.openai.com/plugins/deploy/submission-errors#skills-only-zip-upload-errors-and-warnings).

### Repo-backed or local Codex marketplace

Commit and push the updated `.codex-plugin/plugin.json` and package, then
refresh a Git marketplace with:

```bash
codex plugin marketplace upgrade <marketplace-name>
```

This command refreshes the Git marketplace source snapshot; it does not by
itself prove that an installed cached plugin was replaced.

For local cache testing, use a disposable local copy whose manifest version has
a cachebuster such as `1.0.1+codex.local-20260728-120000`, reinstall it with
`codex plugin add <plugin>@<marketplace>`, and start a new task/thread. Do not
consume another formal SemVer for local pickup, and do not upload the
cachebuster build as the public ZIP. See OpenAI's
[plugin packaging and marketplace guide](https://developers.openai.com/plugins/build/plugins)
and [local plugin update reference](https://github.com/openai/codex/blob/main/codex-rs/skills/src/assets/samples/plugin-creator/references/installing-and-updating.md).

## Safety and reproducibility

- All existing version targets must begin at exactly the same version.
- Stable three-part SemVer is required; the target must be greater.
- Plugin and worktree symlinks are rejected.
- Release checks run in a temporary copy with no shell expansion and a reduced
  environment.
- A live release with missing or deliberately skipped checks requires explicit
  limited-evidence acceptance and is labeled as such.
- The evidence record binds passed checks to the exact non-generated repository
  inputs and archive bytes; later input edits make `verify` fail as stale.
- A fingerprint prevents a concurrent live edit from being overwritten.
- Live output replacement is rollback-protected.
- ZIP entries are sorted, timestamped deterministically, path-normalized,
  size-limited, and compared byte-for-byte with the packaged plugin.
- Old ZIPs are retained. Git and store mutations always remain explicit author
  actions.

## Agent Skill

[`skills/skillbump`](skills/skillbump) is a companion Agent Skill for Codex and
Claude-compatible hosts. It resolves the intended bump, release notes, OpenAI
submission intent, and acceptable evidence level; runs the
plan/dry-run/prepare/verify loop; and explains the manual store boundary. The
Python harness enforces the release mechanics and evidence state; the Skill
makes the workflow conversational.

## License

MIT. See [LICENSE](LICENSE).
