# Security policy

## Reporting

Use GitHub private vulnerability reporting for issues that could overwrite
files outside the selected repository, bypass the live-worktree comparison,
publish an unverified archive, leak credentials into repository checks, or
escape rollback. If private reporting is unavailable, open a public issue with
no exploit details and request a private contact channel.

Include the SkillBump version or commit, a minimal inert repository fixture,
the exact command, expected behavior, observed behavior, and operating system.
Do not include real credentials or private source code.

## Trust model

SkillBump treats the selected repository as author-controlled release input.
It never fetches or installs upstream Skills, invokes a shell, commits, pushes,
uploads, or publishes. `plan` executes no repository code.

`prepare` may execute only two conventional repository-owned checks when they
exist: `scripts/validate_repo.py` and Python unittest discovery under `tests/`.
They run in a temporary copy with a reduced environment and without shell
parsing. That is not an operating-system sandbox: a malicious test or validator
could still access the machine using the author's user permissions. Review
repository code before running a release, or use `--skip-repo-checks` and run
trusted checks separately in an appropriate sandbox.

The release transaction acquires a repository-scoped lock before reading the
current version, fingerprints the live worktree, prepares and verifies outputs
in a temporary copy, and confirms the fingerprint again. It also snapshots each
declared output before staging and compares that target again immediately before
replacement. During publication it pins the repository and every output parent
with symlink-rejecting directory descriptors, moves existing targets into a
private transaction with atomic no-replace operations, and validates each moved
object before installing anything. Existing targets are retained until
replacement succeeds. Failure or interruption triggers rollback; rollback moves
and revalidates an installed object before deletion, so a later concurrent edit
is preserved.
A concurrent conflict or catastrophic filesystem fault may leave a visible
`.skillbump-txn-*` recovery directory; preserve it until the affected paths are
inspected and restored.

ZIP packaging rejects symlinks (including output-parent symlinks), path
traversal, Unicode/control ambiguities, case-normalization collisions,
unsupported entry types, and public-directory size/count violations. Each ZIP
entry must be marked as a regular file and is compared byte-for-byte with the
packaged plugin before success is reported.

## Supported environments

Python 3.11+ on macOS, Linux, and WSL is supported. Native Windows is not yet
supported because the transaction lock and directory replacement model relies
on POSIX filesystem semantics. On WSL, keep the repository on the native Linux
filesystem rather than a default `/mnt/c` DrvFS mount.
