---
name: skillbump
description: Prepare a formal release of an Agent Skill or skills-only plugin owned by the user after its contents change. Use when an author asks to bump a Skill/plugin version such as 1.0.0 to 1.0.1, synchronize Claude Code and ChatGPT/Codex manifests, rebuild the public ZIP, update release notes, verify a release package, or ask how their updated Skill reaches either plugin marketplace. Do not use to pull an official upstream Skill, install someone else's Skill, review Skill quality, or publish without explicit authority.
---

# SkillBump

Turn an author's finished Skill edits into a verified, reviewable plugin release. The Python harness owns version synchronization and packaging; this Skill owns the conversational release workflow.

Before running commands, confirm that the `skillbump` executable is installed from this repository or that the repository checkout is available with its `src` directory on `PYTHONPATH`. If neither is true, report the installation prerequisite instead of imitating the transaction with ad hoc manifest edits.

## Establish release intent

Confirm that the user owns or maintains the target repository. Locate its root and make sure it contains the standard canonical Skill and Codex plugin package expected by SkillBump.

Before mutation, establish three facts that materially affect the release:

1. the bump level or explicit target version;
2. release notes that accurately state what changed;
3. the OpenAI submission intent: `initial`, `update`, or `skip`.

Do not ask again when the user already supplied a fact. Default the OpenAI intent to `skip` when the user did not request portal preparation. If the user requests OpenAI submission work but has not said whether this is the first listing or an update to an existing one, ask; never infer portal state from a Git version. If the user says `1.01`, explain briefly that the valid semantic version is `1.0.1` and use `1.0.1` only when that is clearly the intended patch release.

Use `initial` only when no same-name listing exists in the OpenAI directory. Use `update` only after the user confirms an existing listing; preserve its plugin `name`, use a different valid SemVer, and describe what changed. Use `skip` for a release that should not alter the OpenAI submission sheet.

Use patch for backwards-compatible fixes or refinements, minor for backwards-compatible new capability, and major for a breaking contract. Do not infer a major or minor release from file count alone.

## Inspect without changing anything

Read Git status and preserve unrelated work. Then run:

```bash
skillbump -C <repo> plan --bump patch --openai-submission skip
```

Or, for an explicit target:

```bash
skillbump -C <repo> plan --to 1.0.1 --expect 1.0.0 \
  --openai-submission <initial|update|skip>
```

Review the plan for:

- the current and target versions;
- every existing Claude and Codex version target;
- canonical-to-packaged Skill drift;
- the future ZIP and checklist paths;
- which repository validator and unit tests will run.

The Codex marketplace file `.agents/plugins/marketplace.json` normally has no plugin release version. Never add one merely to make all files look symmetrical.

Stop if existing manifests disagree, the package identity changed, paths are ambiguous, or the requested target is not greater than the current release.

## Prove the release in a temporary copy

Run a full dry run before publishing files into the worktree:

```bash
skillbump -C <repo> prepare \
  --to <target-version> \
  --expect <current-version> \
  --notes "<accurate release notes>" \
  --openai-submission <initial|update|skip> \
  --dry-run
```

The harness copies the author's current worktree, synchronizes the Skill, bumps versions, conditionally updates the submission sheet for `initial` or `update`, runs conventional repository checks, builds the ZIP, and verifies every archive byte without modifying the live repository. With `skip`, keep the submission sheet unchanged.

Treat a failed validator or test as a release blocker. Do not bypass it by editing the generated archive, weakening a check, or using `--skip-repo-checks` unless the user explicitly accepts that narrower evidence and has another trusted validation path.

## Prepare the local release

After the dry run passes, run the same command without `--dry-run`:

```bash
skillbump -C <repo> prepare \
  --to <target-version> \
  --expect <current-version> \
  --notes "<accurate release notes>" \
  --openai-submission <initial|update|skip>
```

Then run:

```bash
skillbump -C <repo> verify
```

Inspect the full Git diff, the generated `release/checklists/<version>.md`, the ZIP member list, and its SHA-256. Confirm that:

- all existing explicit manifests contain exactly the target version;
- the canonical and packaged Skill copies match;
- the ZIP is rooted directly at `.codex-plugin/`, `assets/`, and `skills/` rather than an extra wrapper directory;
- the readiness report matches the chosen OpenAI submission intent; for `initial` or `update`, the notes use that exact status, while `skip` leaves the submission sheet unchanged;
- no unrelated user change was overwritten.

## Respect publishing boundaries

Preparing a release does not authorize Git or store mutations.

- Commit, tag, push, or open a pull request only when the user asks for that action.
- Never merge automatically.
- Never claim the public store is updated because the repository version changed.

For a custom Claude Code Git marketplace, the author commits and pushes the bumped plugin and any marketplace file they own. Users can refresh the marketplace and plugin, or receive it through enabled auto-update:

```bash
claude plugin marketplace update <marketplace>
claude plugin update <plugin>@<marketplace>
```

For an approved plugin in the official Claude Community Marketplace, push the reviewed release to the plugin's own repository. Anthropic's community catalog pins plugins to commits, updates that pin through CI, and syncs the public catalog nightly. Do not edit Anthropic's marketplace entry or claim immediate propagation. See `https://code.claude.com/docs/en/plugins`.

For the OpenAI universal Plugins Directory shared by ChatGPT and Codex, a GitHub push alone is insufficient and no public release-upload API is currently documented. For `initial`, upload the ZIP as an initial submission. For `update`, keep the existing plugin name, upload the new-version ZIP as an update, and state what changed. In both cases, use `https://platform.openai.com/plugins`, submit for review, and select Publish after approval. For `skip`, perform no portal action and do not modify the submission sheet. SkillBump has no portal-upload, review, or publish action.

For a Git-backed Codex authoring marketplace, refresh only its repository source snapshot with:

```bash
codex plugin marketplace upgrade <marketplace-name>
```

Do not report that this command replaced an already installed cached plugin; the public documentation only promises a marketplace source upgrade.

For local cache testing, work from a disposable local copy, give its manifest a cachebuster such as `<formal-version>+codex.local-<timestamp>`, reinstall with `codex plugin add <plugin>@<marketplace>`, and start a new task/thread. Do not consume another formal SemVer or upload the cachebuster build. Follow `https://github.com/openai/codex/blob/main/codex-rs/skills/src/assets/samples/plugin-creator/references/installing-and-updating.md`.

## Report the outcome

Lead with the concrete release state:

- `PREPARED`: local manifests, package, tests, ZIP, and checklist passed;
- `DRY RUN ONLY`: proof passed but the live worktree is unchanged;
- `BLOCKED`: state the single blocking mismatch or failed check;
- `PUBLISHED`: use only after the relevant store itself confirms publication.

Include the old and new version, archive path and SHA-256, checks run, OpenAI submission intent and readiness, Git/store actions not performed, and the smallest next manual action. Keep custom Claude marketplace refresh, Claude community propagation, Codex source refresh/local reinstall, and OpenAI portal review distinct.
