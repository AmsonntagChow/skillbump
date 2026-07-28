"""Author-side release preparation for standard Agent Skill plugin repositories."""

from __future__ import annotations

import contextlib
import ctypes
import dataclasses
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unicodedata
import uuid
import zipfile
from pathlib import Path
from typing import Any, Iterator, Sequence


SEMVER_PATTERN = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
PLUGIN_NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
ARCHIVE_VERSION_PATTERN = r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 5_000
MAX_ARCHIVE_MEMBER_BYTES = 100 * 1024 * 1024
MAX_ARCHIVE_TOTAL_BYTES = 512 * 1024 * 1024
MAX_ARCHIVE_BYTES = 100 * 1024 * 1024
FIXED_ZIP_TIME = (1980, 1, 1, 0, 0, 0)

ROOT_IGNORED_TREE_NAMES = {
    ".DS_Store",
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "venv",
}
ANYWHERE_IGNORED_TREE_NAMES = {".DS_Store", "__pycache__"}
ROOT_PLUGIN_ARCHIVE_NAMES = {
    ".app.json",
    ".codex-plugin",
    ".mcp.json",
    "assets",
    "hooks",
    "skills",
}


class ReleaseError(RuntimeError):
    """A release precondition or verification failed."""


class DuplicateKeyError(ValueError):
    """A JSON object contains the same key more than once."""


@dataclasses.dataclass(frozen=True, order=True)
class Version:
    major: int
    minor: int
    patch: int

    @classmethod
    def parse(cls, value: object, *, label: str = "version") -> "Version":
        if not isinstance(value, str) or SEMVER_PATTERN.fullmatch(value) is None:
            raise ReleaseError(
                f"{label} must be stable MAJOR.MINOR.PATCH SemVer without leading zeroes; "
                f"received {value!r}"
            )
        major, minor, patch = (int(part) for part in value.split("."))
        return cls(major, minor, patch)

    def bump(self, kind: str) -> "Version":
        if kind == "patch":
            return Version(self.major, self.minor, self.patch + 1)
        if kind == "minor":
            return Version(self.major, self.minor + 1, 0)
        if kind == "major":
            return Version(self.major + 1, 0, 0)
        raise ReleaseError(f"unsupported bump kind: {kind}")

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"


@dataclasses.dataclass(frozen=True)
class Layout:
    root: Path
    name: str
    current: Version
    canonical_skill: Path
    plugin_root: Path
    packaged_skill: Path
    codex_manifest: Path
    claude_manifest: Path | None
    claude_marketplace: Path | None
    submission_sheet: Path | None

    @property
    def archive(self) -> Path:
        return self.root / "dist" / f"{self.name}-plugin-{self.current}.zip"

    @property
    def checklist(self) -> Path:
        return self.root / "release" / "checklists" / f"{self.current}.md"

    def relative(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number {value!r} is not supported")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateKeyError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def load_json(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ReleaseError(f"expected a regular JSON file: {path}")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ReleaseError(f"cannot read {path}: {exc}") from exc
    if len(raw) > MAX_JSON_BYTES:
        raise ReleaseError(f"JSON file is unexpectedly large: {path}")
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, DuplicateKeyError, ValueError) as exc:
        raise ReleaseError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ReleaseError(f"JSON root must be an object: {path}")
    return value


def json_bytes(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _ensure_plain_directory(path: Path, *, label: str) -> None:
    if path.is_symlink() or not path.is_dir():
        raise ReleaseError(f"{label} must be a real directory: {path}")


def _inside(root: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _marketplace_entry(path: Path, name: str) -> tuple[dict[str, Any], dict[str, Any]]:
    marketplace = load_json(path)
    plugins = marketplace.get("plugins")
    if not isinstance(plugins, list):
        raise ReleaseError(f"{path}: plugins must be an array")
    matches = [
        item for item in plugins if isinstance(item, dict) and item.get("name") == name
    ]
    if len(matches) != 1:
        raise ReleaseError(f"{path}: expected exactly one plugin named {name!r}")
    return marketplace, matches[0]


def discover(root: str | os.PathLike[str]) -> Layout:
    root_path = Path(root).expanduser().resolve()
    _ensure_plain_directory(root_path, label="repository root")

    claude_path = root_path / ".claude-plugin" / "plugin.json"
    claude_manifest: dict[str, Any] | None = None
    name: str | None = None
    if claude_path.exists():
        claude_manifest = load_json(claude_path)
        if isinstance(claude_manifest.get("name"), str):
            name = claude_manifest["name"]

    candidates: list[Path] = []
    root_codex = root_path / ".codex-plugin" / "plugin.json"
    if root_codex.is_file() and not root_codex.is_symlink():
        candidates.append(root_codex)
    plugins_root = root_path / "plugins"
    if plugins_root.is_dir() and not plugins_root.is_symlink():
        candidates.extend(sorted(plugins_root.glob("*/.codex-plugin/plugin.json")))

    loaded_candidates: list[tuple[Path, dict[str, Any]]] = []
    for candidate in candidates:
        manifest = load_json(candidate)
        if name is None or manifest.get("name") == name:
            loaded_candidates.append((candidate, manifest))
    if len(loaded_candidates) != 1:
        detail = ", ".join(path.relative_to(root_path).as_posix() for path, _ in loaded_candidates)
        raise ReleaseError(
            "expected exactly one Codex plugin manifest matching the plugin name"
            + (f"; found {detail}" if detail else "")
        )

    codex_path, codex_manifest = loaded_candidates[0]
    if name is None:
        raw_name = codex_manifest.get("name")
        if not isinstance(raw_name, str):
            raise ReleaseError(f"{codex_path}: name must be a string")
        name = raw_name
    if PLUGIN_NAME_PATTERN.fullmatch(name) is None:
        raise ReleaseError(f"plugin name is not valid for the public directory: {name!r}")
    if codex_manifest.get("name") != name:
        raise ReleaseError("Claude and Codex manifest names do not match")
    if claude_manifest is not None and claude_manifest.get("name") != name:
        raise ReleaseError("Claude and Codex manifest names do not match")

    versions: list[tuple[str, Version]] = [
        (
            codex_path.relative_to(root_path).as_posix(),
            Version.parse(codex_manifest.get("version"), label=f"{codex_path} version"),
        )
    ]
    if claude_manifest is not None:
        versions.append(
            (
                claude_path.relative_to(root_path).as_posix(),
                Version.parse(claude_manifest.get("version"), label=f"{claude_path} version"),
            )
        )

    marketplace_path = root_path / ".claude-plugin" / "marketplace.json"
    effective_marketplace: Path | None = None
    if marketplace_path.exists():
        _, entry = _marketplace_entry(marketplace_path, name)
        if "version" in entry:
            versions.append(
                (
                    marketplace_path.relative_to(root_path).as_posix(),
                    Version.parse(
                        entry.get("version"), label=f"{marketplace_path} plugin version"
                    ),
                )
            )
            effective_marketplace = marketplace_path

    first_version = versions[0][1]
    mismatches = [f"{label}={version}" for label, version in versions if version != first_version]
    if mismatches:
        rendered = ", ".join(f"{label}={version}" for label, version in versions)
        raise ReleaseError(f"release versions are not synchronized: {rendered}")

    canonical = root_path / "skills" / name
    _ensure_plain_directory(canonical, label="canonical skill")
    if not (canonical / "SKILL.md").is_file():
        raise ReleaseError(f"canonical skill is missing SKILL.md: {canonical}")

    plugin_root = codex_path.parent.parent
    if not _inside(root_path, plugin_root):
        raise ReleaseError("Codex plugin root escapes the repository")
    _ensure_plain_directory(plugin_root, label="Codex plugin root")
    packaged = plugin_root / "skills" / name
    _ensure_plain_directory(packaged, label="packaged Codex skill")

    submission = root_path / "submission" / "PLUGIN_DIRECTORY.md"
    return Layout(
        root=root_path,
        name=name,
        current=first_version,
        canonical_skill=canonical,
        plugin_root=plugin_root,
        packaged_skill=packaged,
        codex_manifest=codex_path,
        claude_manifest=claude_path if claude_manifest is not None else None,
        claude_marketplace=effective_marketplace,
        submission_sheet=submission if submission.is_file() and not submission.is_symlink() else None,
    )


def _validate_path_text(relative: str) -> str:
    normalized = unicodedata.normalize("NFC", relative)
    if normalized != relative:
        raise ReleaseError(f"path must already be NFC-normalized: {relative!r}")
    if any(unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in relative):
        raise ReleaseError(f"path contains an unsupported control or format character: {relative!r}")
    return normalized


def _tree_files(
    root: Path,
    *,
    archive_rules: bool = False,
    included_top_level: set[str] | None = None,
) -> dict[str, Path]:
    _ensure_plain_directory(root, label="tree")
    files: dict[str, Path] = {}
    normalized_keys: dict[str, str] = {}
    total = 0
    for path in sorted(root.rglob("*")):
        relative_path = path.relative_to(root)
        if included_top_level is not None and relative_path.parts[0] not in included_top_level:
            continue
        if any(part in {".DS_Store", "__pycache__"} for part in relative_path.parts):
            continue
        if path.suffix in {".pyc", ".pyo"}:
            continue
        if path.is_symlink():
            raise ReleaseError(f"symlinks are not allowed in release trees: {path}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ReleaseError(f"unsupported filesystem entry in release tree: {path}")
        relative = _validate_path_text(relative_path.as_posix())
        collision_key = unicodedata.normalize("NFC", relative).casefold()
        if collision_key in normalized_keys:
            raise ReleaseError(
                "release paths collide after case/Unicode normalization: "
                f"{normalized_keys[collision_key]!r} and {relative!r}"
            )
        normalized_keys[collision_key] = relative
        size = path.stat().st_size
        if archive_rules:
            if len(relative_path.parts) > 20:
                raise ReleaseError(f"archive member is more than 20 path segments deep: {relative}")
            if len(relative.encode("utf-8")) > 1_024:
                raise ReleaseError(f"archive member path is too long: {relative}")
            if size > MAX_ARCHIVE_MEMBER_BYTES:
                raise ReleaseError(f"archive member exceeds 100 MiB: {relative}")
            total += size
        files[relative] = path
    if archive_rules:
        if not files:
            raise ReleaseError("plugin archive would be empty")
        if len(files) > MAX_ARCHIVE_ENTRIES:
            raise ReleaseError("plugin archive would exceed 5,000 entries")
        if total > MAX_ARCHIVE_TOTAL_BYTES:
            raise ReleaseError("plugin archive would exceed 512 MiB uncompressed")
    return files


def _plugin_files(
    plugin_root: Path,
    *,
    repository_root: Path | None,
    archive_rules: bool,
) -> dict[str, Path]:
    included = None
    if repository_root is not None and plugin_root.resolve() == repository_root.resolve():
        included = ROOT_PLUGIN_ARCHIVE_NAMES
    return _tree_files(
        plugin_root,
        archive_rules=archive_rules,
        included_top_level=included,
    )


def compare_trees(source: Path, destination: Path) -> list[str]:
    source_files = _tree_files(source)
    destination_files = _tree_files(destination)
    changes: list[str] = []
    for name in sorted(source_files.keys() - destination_files.keys()):
        changes.append(f"add {name}")
    for name in sorted(destination_files.keys() - source_files.keys()):
        changes.append(f"remove {name}")
    for name in sorted(source_files.keys() & destination_files.keys()):
        if source_files[name].read_bytes() != destination_files[name].read_bytes():
            changes.append(f"update {name}")
    return changes


def _set_manifest_version(path: Path, version: Version) -> None:
    manifest = load_json(path)
    manifest["version"] = str(version)
    path.write_bytes(json_bytes(manifest))


def _set_marketplace_version(path: Path, name: str, version: Version) -> None:
    marketplace, entry = _marketplace_entry(path, name)
    if "version" not in entry:
        raise ReleaseError(f"{path}: selected marketplace entry has no explicit version")
    entry["version"] = str(version)
    path.write_bytes(json_bytes(marketplace))


def _normalize_release_notes(notes: str) -> str:
    value = notes.strip()
    if not value:
        raise ReleaseError("release notes must not be empty")
    if len(value) > 4_000:
        raise ReleaseError("release notes must be at most 4,000 characters")
    if any(unicodedata.category(char) in {"Cc", "Cf", "Cs"} and char not in "\n\t" for char in value):
        raise ReleaseError("release notes contain unsupported control or format characters")
    return value


def _update_submission_sheet(
    path: Path,
    *,
    name: str,
    current: Version,
    target: Version,
    notes: str,
    submission_kind: str,
) -> None:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ReleaseError(f"cannot read submission sheet {path}: {exc}") from exc
    archive_re = re.compile(
        rf"dist/{re.escape(name)}-plugin-({ARCHIVE_VERSION_PATTERN})\.zip"
    )
    matches = list(archive_re.finditer(text))
    if len(matches) != 1:
        raise ReleaseError(
            f"{path}: expected exactly one dist/{name}-plugin-X.Y.Z.zip reference"
        )
    referenced = Version.parse(matches[0].group(1), label="submission archive version")
    if referenced != current:
        raise ReleaseError(
            f"{path}: submission archive version {referenced} does not match manifests {current}"
        )
    text = text[: matches[0].start(1)] + str(target) + text[matches[0].end(1) :]
    section = re.compile(r"(?ms)(^## Release notes\s*\n\n)(.*?)(?=\n## |\Z)")
    section_matches = list(section.finditer(text))
    if len(section_matches) != 1:
        raise ReleaseError(f"{path}: expected exactly one '## Release notes' section")
    if submission_kind == "update":
        body = f"Version {target} update. {_normalize_release_notes(notes)}\n"
    elif submission_kind == "initial":
        body = f"Initial submission at version {target}. {_normalize_release_notes(notes)}\n"
    else:
        raise ReleaseError(f"unsupported OpenAI submission kind: {submission_kind}")
    match = section_matches[0]
    text = text[: match.start()] + match.group(1) + body + text[match.end() :]
    path.write_text(text, encoding="utf-8")


def _sync_canonical(layout: Layout) -> None:
    if layout.canonical_skill.resolve() == layout.packaged_skill.resolve():
        return
    if layout.packaged_skill.exists():
        shutil.rmtree(layout.packaged_skill)
    layout.packaged_skill.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(layout.canonical_skill, layout.packaged_skill)


def build_archive(
    plugin_root: Path,
    target: Path,
    *,
    repository_root: Path | None = None,
) -> str:
    files = _plugin_files(
        plugin_root, repository_root=repository_root, archive_rules=True
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(
        target,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
        strict_timestamps=True,
    ) as archive:
        for relative, path in files.items():
            info = zipfile.ZipInfo(relative, FIXED_ZIP_TIME)
            info.create_system = 3
            mode = stat.S_IMODE(path.stat().st_mode)
            executable = bool(mode & 0o111)
            permissions = 0o755 if executable else 0o644
            info.external_attr = (stat.S_IFREG | permissions) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, path.read_bytes(), compresslevel=9)
    if target.stat().st_size > MAX_ARCHIVE_BYTES:
        raise ReleaseError("compressed plugin archive exceeds 100 MB")
    verify_archive(plugin_root, target, repository_root=repository_root)
    return hashlib.sha256(target.read_bytes()).hexdigest()


def verify_archive(
    plugin_root: Path,
    archive_path: Path,
    *,
    repository_root: Path | None = None,
) -> None:
    expected = _plugin_files(
        plugin_root, repository_root=repository_root, archive_rules=True
    )
    try:
        with zipfile.ZipFile(archive_path, "r") as archive:
            names = archive.namelist()
            if len(names) != len(set(names)):
                raise ReleaseError(f"archive contains duplicate members: {archive_path}")
            if names != sorted(expected):
                missing = sorted(set(expected) - set(names))
                extra = sorted(set(names) - set(expected))
                raise ReleaseError(
                    f"archive does not match plugin tree; missing={missing}, extra={extra}"
                )
            for name in names:
                if name.startswith("/") or "\\" in name or ".." in Path(name).parts:
                    raise ReleaseError(f"unsafe archive member path: {name!r}")
                info = archive.getinfo(name)
                mode = (info.external_attr >> 16) & 0xFFFF
                if info.create_system != 3 or not stat.S_ISREG(mode):
                    raise ReleaseError(f"archive member is not a regular file: {name}")
                expected_size = expected[name].stat().st_size
                if info.file_size != expected_size:
                    raise ReleaseError(
                        f"archive member size differs from plugin tree: {name}"
                    )
                if archive.read(name) != expected[name].read_bytes():
                    raise ReleaseError(f"archive content differs from plugin tree: {name}")
    except (OSError, zipfile.BadZipFile, KeyError) as exc:
        raise ReleaseError(f"invalid plugin archive {archive_path}: {exc}") from exc


def _submission_ready(layout: Layout) -> bool:
    if layout.submission_sheet is None:
        return False
    text = layout.submission_sheet.read_text(encoding="utf-8")
    expected = f"dist/{layout.name}-plugin-{layout.current}.zip"
    return expected in text


def _verify_layout(
    layout: Layout,
    *,
    require_archive: bool,
    require_submission: bool = False,
) -> None:
    refreshed = discover(layout.root)
    if refreshed.name != layout.name or refreshed.current != layout.current:
        raise ReleaseError("manifest identity changed during verification")
    drift = compare_trees(refreshed.canonical_skill, refreshed.packaged_skill)
    if drift:
        raise ReleaseError("packaged Skill differs from canonical Skill: " + ", ".join(drift))
    if require_archive:
        if not refreshed.archive.is_file():
            raise ReleaseError(f"release archive is missing: {refreshed.archive}")
        verify_archive(
            refreshed.plugin_root,
            refreshed.archive,
            repository_root=refreshed.root,
        )
    if require_submission and not _submission_ready(refreshed):
        expected = f"dist/{refreshed.name}-plugin-{refreshed.current}.zip"
        raise ReleaseError(f"submission sheet does not point to {expected}")


def _ignored(relative: Path) -> bool:
    if not relative.parts:
        return False
    if relative.parts[0] in ROOT_IGNORED_TREE_NAMES:
        return True
    if any(part in ANYWHERE_IGNORED_TREE_NAMES for part in relative.parts):
        return True
    if relative.parts[0].endswith(".egg-info"):
        return True
    if relative.name.startswith(".skillbump-") or relative.name == ".skillbump.release.lock":
        return True
    if relative.suffix in {".pyc", ".pyo"}:
        return True
    return False


def worktree_fingerprint(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        relative_path = path.relative_to(root)
        if path.is_symlink():
            raise ReleaseError(f"worktree symlinks are not supported: {relative_path.as_posix()}")
        if _ignored(relative_path):
            continue
        if path.is_dir():
            continue
        if not path.is_file():
            raise ReleaseError(f"unsupported worktree entry: {relative_path.as_posix()}")
        relative = _validate_path_text(relative_path.as_posix())
        payload = path.read_bytes()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(stat.S_IMODE(path.stat().st_mode)).encode("ascii"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(payload).digest())
    return digest.hexdigest()


def _copy_worktree(source: Path, destination: Path) -> None:
    def ignore(directory: str, names: list[str]) -> set[str]:
        base = Path(directory)
        ignored: set[str] = set()
        for name in names:
            relative = (base / name).relative_to(source)
            if _ignored(relative):
                ignored.add(name)
        return ignored

    shutil.copytree(source, destination, ignore=ignore)


@contextlib.contextmanager
def release_lock(root: Path) -> Iterator[None]:
    lock_root = Path(tempfile.gettempdir()) / "skillbump-release-locks"
    lock_root.mkdir(mode=0o700, exist_ok=True)
    if lock_root.is_symlink() or not lock_root.is_dir():
        raise ReleaseError(f"release lock directory is unsafe: {lock_root}")
    lock_stat = lock_root.stat()
    if lock_stat.st_uid != os.getuid():
        raise ReleaseError(f"release lock directory has unsafe ownership or permissions: {lock_root}")
    if stat.S_IMODE(lock_stat.st_mode) & 0o077:
        try:
            lock_root.chmod(0o700)
        except OSError as exc:
            raise ReleaseError(f"cannot secure release lock directory: {lock_root}") from exc
        lock_stat = lock_root.stat()
        if lock_stat.st_uid != os.getuid() or stat.S_IMODE(lock_stat.st_mode) != 0o700:
            raise ReleaseError(
                f"release lock directory has unsafe ownership or permissions: {lock_root}"
            )
    key = hashlib.sha256(str(root.resolve()).encode("utf-8")).hexdigest()
    lock_path = lock_root / f"{key}.lock"
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(lock_path, flags, 0o600)
    try:
        lock_file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(lock_file_stat.st_mode) or lock_file_stat.st_uid != os.getuid():
            raise ReleaseError(f"release lock file is unsafe: {lock_path}")
        if stat.S_IMODE(lock_file_stat.st_mode) & 0o077:
            os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ReleaseError(f"another SkillBump release is already running for {root}") from exc
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _subprocess_environment(temp_root: Path) -> dict[str, str]:
    environment: dict[str, str] = {}
    for key in (
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "WINDIR",
    ):
        if key in os.environ:
            environment[key] = os.environ[key]
    home = temp_root / "home"
    home.mkdir()
    environment.update(
        {
            "HOME": str(home),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "XDG_CACHE_HOME": str(home / ".cache"),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_DATA_HOME": str(home / ".local" / "share"),
        }
    )
    return environment


def _run_checked(
    argv: Sequence[str], *, cwd: Path, environment: dict[str, str], timeout: int = 180
) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            list(argv),
            cwd=cwd,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ReleaseError(f"repository check timed out: {' '.join(argv)}") from exc
    output = completed.stdout
    if len(output) > 200_000:
        output = output[-200_000:]
    if completed.returncode != 0:
        raise ReleaseError(
            f"repository check failed ({completed.returncode}): {' '.join(argv)}\n{output}"
        )
    return {"argv": list(argv), "exit_code": completed.returncode, "output": output.strip()}


def run_repository_checks(root: Path, temp_root: Path) -> list[dict[str, Any]]:
    environment = _subprocess_environment(temp_root)
    results: list[dict[str, Any]] = []
    validator = root / "scripts" / "validate_repo.py"
    if validator.is_file() and not validator.is_symlink():
        results.append(
            _run_checked(
                [sys.executable, "-B", "scripts/validate_repo.py"],
                cwd=root,
                environment=environment,
            )
        )
    tests = root / "tests"
    if tests.is_dir() and not tests.is_symlink():
        results.append(
            _run_checked(
                [sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests", "-v"],
                cwd=root,
                environment=environment,
            )
        )
    return results


def _target_version(current: Version, bump: str, explicit: str | None) -> Version:
    target = Version.parse(explicit, label="target version") if explicit else current.bump(bump)
    if target <= current:
        raise ReleaseError(f"target version {target} must be greater than current version {current}")
    return target


def plan(
    root: str | os.PathLike[str],
    *,
    bump: str = "patch",
    target_version: str | None = None,
    expect: str | None = None,
    openai_submission: str = "skip",
) -> dict[str, Any]:
    layout = discover(root)
    if expect is not None and Version.parse(expect, label="expected version") != layout.current:
        raise ReleaseError(f"expected version {expect}, but manifests contain {layout.current}")
    target = _target_version(layout.current, bump, target_version)
    if openai_submission not in {"initial", "update", "skip"}:
        raise ReleaseError(f"unsupported OpenAI submission kind: {openai_submission}")
    if openai_submission != "skip" and layout.submission_sheet is None:
        raise ReleaseError("OpenAI submission was requested, but no submission sheet exists")
    sync_changes = compare_trees(layout.canonical_skill, layout.packaged_skill)
    version_paths = [layout.relative(layout.codex_manifest)]
    if layout.claude_manifest is not None:
        version_paths.append(layout.relative(layout.claude_manifest))
    if layout.claude_marketplace is not None:
        version_paths.append(layout.relative(layout.claude_marketplace))
    checks: list[list[str]] = []
    if (layout.root / "scripts" / "validate_repo.py").is_file():
        checks.append(["python", "-B", "scripts/validate_repo.py"])
    if (layout.root / "tests").is_dir():
        checks.append(["python", "-B", "-m", "unittest", "discover", "-s", "tests", "-v"])
    return {
        "command": "plan",
        "root": str(layout.root),
        "name": layout.name,
        "current_version": str(layout.current),
        "target_version": str(target),
        "bump": bump if target_version is None else "explicit",
        "version_targets": sorted(version_paths),
        "canonical_skill": layout.relative(layout.canonical_skill),
        "packaged_skill": layout.relative(layout.packaged_skill),
        "sync_changes": sync_changes,
        "archive": f"dist/{layout.name}-plugin-{target}.zip",
        "checklist": f"release/checklists/{target}.md",
        "repository_checks": checks,
        "submission_sheet": (
            layout.relative(layout.submission_sheet) if layout.submission_sheet is not None else None
        ),
        "openai_submission": openai_submission,
        "will_commit": False,
        "will_push": False,
        "will_publish": False,
    }


def _checklist_markdown(
    layout: Layout,
    *,
    previous: Version,
    notes: str,
    sha256: str,
    archive_size: int,
    checks: list[dict[str, Any]],
    openai_submission: str,
) -> str:
    version_targets = [layout.relative(layout.codex_manifest)]
    if layout.claude_manifest is not None:
        version_targets.append(layout.relative(layout.claude_manifest))
    if layout.claude_marketplace is not None:
        version_targets.append(layout.relative(layout.claude_marketplace))
    lines = [
        f"# {layout.name} {layout.current} release checklist",
        "",
        "## Prepared",
        "",
        f"- Version: `{previous}` → `{layout.current}`",
        f"- Canonical Skill: `{layout.relative(layout.canonical_skill)}`",
        f"- Packaged Skill: `{layout.relative(layout.packaged_skill)}`",
        f"- Archive: `{layout.relative(layout.archive)}`",
        f"- Archive bytes: `{archive_size}`",
        f"- SHA-256: `{sha256}`",
        f"- OpenAI submission intent: `{openai_submission}`",
        "- Git commit, tag, push, store upload, review, and publish: not performed",
        "",
        "Version targets:",
        "",
    ]
    lines.extend(f"- `{path}`" for path in sorted(version_targets))
    lines.extend(["", "Repository checks:", ""])
    if checks:
        lines.extend(
            f"- PASS: `{' '.join(str(part) for part in check['argv'])}`" for check in checks
        )
    else:
        lines.append("- No standard repository validator or unit-test directory was found.")
    lines.extend(
        [
            "",
            "## Release notes",
            "",
            notes.strip(),
            "",
            "## Author actions",
            "",
            "- [ ] Review the complete Git diff and ZIP member list.",
            "- [ ] Cold-install and exercise the Claude package.",
            "- [ ] Cold-install and exercise the ChatGPT/Codex package.",
            "- [ ] Commit and push the reviewed release files.",
            "- [ ] For Claude marketplace distribution, refresh/update the marketplace plugin.",
            (
                "- [ ] In the OpenAI plugin portal, upload the ZIP as an update and submit it for review."
                if openai_submission == "update"
                else "- [ ] In the OpenAI plugin portal, create the initial submission and upload the ZIP."
                if openai_submission == "initial"
                else "- [ ] If targeting the OpenAI public directory, prepare an explicit initial/update submission first."
            ),
            "- [ ] After OpenAI approval, select Publish in the portal.",
            "",
        ]
    )
    return "\n".join(lines)


def _stage_release(
    stage_root: Path,
    *,
    target: Version,
    notes: str,
    run_checks: bool,
    temp_root: Path,
    openai_submission: str,
) -> tuple[Layout, list[dict[str, Any]], str]:
    layout = discover(stage_root)
    previous = layout.current
    _sync_canonical(layout)
    _set_manifest_version(layout.codex_manifest, target)
    if layout.claude_manifest is not None:
        _set_manifest_version(layout.claude_manifest, target)
    if layout.claude_marketplace is not None:
        _set_marketplace_version(layout.claude_marketplace, layout.name, target)
    if openai_submission != "skip":
        if layout.submission_sheet is None:
            raise ReleaseError("OpenAI submission was requested, but no submission sheet exists")
        _update_submission_sheet(
            layout.submission_sheet,
            name=layout.name,
            current=previous,
            target=target,
            notes=notes,
            submission_kind=openai_submission,
        )

    staged = discover(stage_root)
    intended_fingerprint = worktree_fingerprint(stage_root)
    checks = run_repository_checks(stage_root, temp_root) if run_checks else []
    if worktree_fingerprint(stage_root) != intended_fingerprint:
        raise ReleaseError("repository checks modified release inputs; refusing to publish them")

    staged.archive.parent.mkdir(parents=True, exist_ok=True)
    sha256 = build_archive(
        staged.plugin_root,
        staged.archive,
        repository_root=staged.root,
    )
    checklist = _checklist_markdown(
        staged,
        previous=previous,
        notes=notes,
        sha256=sha256,
        archive_size=staged.archive.stat().st_size,
        checks=checks,
        openai_submission=openai_submission,
    )
    staged.checklist.parent.mkdir(parents=True, exist_ok=True)
    staged.checklist.write_text(checklist, encoding="utf-8")
    _verify_layout(
        staged,
        require_archive=True,
        require_submission=openai_submission != "skip",
    )
    return staged, checks, sha256


@dataclasses.dataclass(frozen=True)
class _PathState:
    kind: str
    digest: str = ""
    device: int | None = None
    inode: int | None = None


@dataclasses.dataclass
class _InstallRecord:
    target: "_PinnedTarget"
    backup_name: str
    new_name: str
    intended: _PathState
    backup_captured: bool = False
    new_installed: bool = False


@dataclasses.dataclass
class _PinnedTarget:
    path: Path
    parent_fd: int
    name: str
    expected: _PathState


def _directory_open_flags() -> int:
    flags = os.O_RDONLY
    flags |= getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    return flags


def _file_read_flags() -> int:
    return os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)


def _rename_noreplace(
    source_parent_fd: int,
    source_name: str,
    destination_parent_fd: int,
    destination_name: str,
) -> None:
    for name in (source_name, destination_name):
        if name in {"", ".", ".."} or "/" in name or "\0" in name:
            raise ReleaseError(f"unsafe transaction entry name: {name!r}")
    libc = ctypes.CDLL(None, use_errno=True)
    source_bytes = os.fsencode(source_name)
    destination_bytes = os.fsencode(destination_name)
    if sys.platform == "darwin":
        try:
            rename = libc.renameatx_np
        except AttributeError as exc:  # pragma: no cover - unsupported old Darwin
            raise ReleaseError("this macOS runtime lacks atomic no-replace rename") from exc
        rename.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename.restype = ctypes.c_int
        result = rename(
            source_parent_fd,
            source_bytes,
            destination_parent_fd,
            destination_bytes,
            0x00000004,  # RENAME_EXCL
        )
    elif sys.platform.startswith("linux"):
        try:
            rename = libc.renameat2
        except AttributeError as exc:  # pragma: no cover - non-glibc Linux
            raise ReleaseError("this Linux runtime lacks atomic no-replace rename") from exc
        rename.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename.restype = ctypes.c_int
        result = rename(
            source_parent_fd,
            source_bytes,
            destination_parent_fd,
            destination_bytes,
            0x00000001,  # RENAME_NOREPLACE
        )
    else:  # pragma: no cover - guarded by the supported-platform contract
        raise ReleaseError("atomic no-replace rename is unsupported on this platform")
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number), destination_name)


def _hash_file_descriptor(descriptor: int, *, label: str) -> str:
    before = os.fstat(descriptor)
    if not stat.S_ISREG(before.st_mode):
        raise ReleaseError(f"release target is not a regular file: {label}")
    os.lseek(descriptor, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    digest.update(str(stat.S_IMODE(before.st_mode)).encode("ascii"))
    digest.update(b"\0")
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
    after = os.fstat(descriptor)
    if (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise ReleaseError(f"release target changed while it was inspected: {label}")
    return digest.hexdigest()


def _hash_regular_file(path: Path) -> str:
    try:
        descriptor = os.open(path, _file_read_flags())
    except OSError as exc:
        raise ReleaseError(f"cannot safely open release target {path}: {exc}") from exc
    try:
        return _hash_file_descriptor(descriptor, label=str(path))
    finally:
        os.close(descriptor)


def _directory_digest_descriptor(root_fd: int, *, label: str) -> str:
    digest = hashlib.sha256()
    root_before = os.fstat(root_fd)
    if not stat.S_ISDIR(root_before.st_mode):
        raise ReleaseError(f"release target is not a real directory: {label}")
    digest.update(str(stat.S_IMODE(root_before.st_mode)).encode("ascii"))
    digest.update(b"\0")

    def visit(directory_fd: int, relative_parent: Path) -> None:
        scan_fd = os.dup(directory_fd)
        try:
            with os.scandir(scan_fd) as iterator:
                entries = sorted(iterator, key=lambda entry: entry.name)
        except OSError as exc:
            raise ReleaseError(f"cannot inspect release directory {label}: {exc}") from exc
        finally:
            with contextlib.suppress(OSError):
                os.close(scan_fd)
        for entry in entries:
            relative_path = relative_parent / entry.name
            relative = _validate_path_text(relative_path.as_posix())
            try:
                entry_stat = os.stat(
                    entry.name, dir_fd=directory_fd, follow_symlinks=False
                )
            except OSError as exc:
                raise ReleaseError(f"cannot inspect release path {label}/{relative}: {exc}") from exc
            if stat.S_ISLNK(entry_stat.st_mode):
                raise ReleaseError(
                    f"release output trees cannot contain symlinks: {label}/{relative}"
                )
            if stat.S_ISDIR(entry_stat.st_mode):
                try:
                    child_fd = os.open(
                        entry.name, _directory_open_flags(), dir_fd=directory_fd
                    )
                except OSError as exc:
                    raise ReleaseError(
                        f"cannot safely open release directory {label}/{relative}: {exc}"
                    ) from exc
                try:
                    child_stat = os.fstat(child_fd)
                    digest.update(b"D\0")
                    digest.update(relative.encode("utf-8"))
                    digest.update(b"\0")
                    digest.update(str(stat.S_IMODE(child_stat.st_mode)).encode("ascii"))
                    digest.update(b"\0")
                    visit(child_fd, relative_path)
                finally:
                    os.close(child_fd)
            elif stat.S_ISREG(entry_stat.st_mode):
                try:
                    file_fd = os.open(entry.name, _file_read_flags(), dir_fd=directory_fd)
                except OSError as exc:
                    raise ReleaseError(
                        f"cannot safely open release file {label}/{relative}: {exc}"
                    ) from exc
                try:
                    digest.update(b"F\0")
                    digest.update(relative.encode("utf-8"))
                    digest.update(b"\0")
                    digest.update(
                        _hash_file_descriptor(
                            file_fd, label=f"{label}/{relative}"
                        ).encode("ascii")
                    )
                    digest.update(b"\0")
                finally:
                    os.close(file_fd)
            else:
                raise ReleaseError(f"unsupported release output entry: {label}/{relative}")

    visit(root_fd, Path())
    root_after = os.fstat(root_fd)
    if (
        root_before.st_dev,
        root_before.st_ino,
        root_before.st_mtime_ns,
    ) != (
        root_after.st_dev,
        root_after.st_ino,
        root_after.st_mtime_ns,
    ):
        raise ReleaseError(f"release directory changed while it was inspected: {label}")
    return digest.hexdigest()


def _directory_digest(root: Path) -> str:
    try:
        descriptor = os.open(root, _directory_open_flags())
    except OSError as exc:
        raise ReleaseError(f"cannot safely open release directory {root}: {exc}") from exc
    try:
        return _directory_digest_descriptor(descriptor, label=str(root))
    finally:
        os.close(descriptor)


def _snapshot_path(path: Path) -> _PathState:
    try:
        path_stat = os.lstat(path)
    except FileNotFoundError:
        return _PathState("missing")
    except OSError as exc:
        raise ReleaseError(f"cannot inspect release target {path}: {exc}") from exc
    if stat.S_ISLNK(path_stat.st_mode):
        raise ReleaseError(f"release target must not be a symlink: {path}")
    if stat.S_ISREG(path_stat.st_mode):
        try:
            descriptor = os.open(path, _file_read_flags())
        except OSError as exc:
            raise ReleaseError(f"cannot safely open release target {path}: {exc}") from exc
        try:
            digest = _hash_file_descriptor(descriptor, label=str(path))
            opened = os.fstat(descriptor)
            return _PathState("file", digest, opened.st_dev, opened.st_ino)
        finally:
            os.close(descriptor)
    if stat.S_ISDIR(path_stat.st_mode):
        try:
            descriptor = os.open(path, _directory_open_flags())
        except OSError as exc:
            raise ReleaseError(f"cannot safely open release directory {path}: {exc}") from exc
        try:
            digest = _directory_digest_descriptor(descriptor, label=str(path))
            opened = os.fstat(descriptor)
            return _PathState("directory", digest, opened.st_dev, opened.st_ino)
        finally:
            os.close(descriptor)
    raise ReleaseError(f"unsupported release target type: {path}")


def _assert_safe_output_path(root: Path, target: Path) -> None:
    try:
        relative = target.relative_to(root)
    except ValueError as exc:
        raise ReleaseError(f"release output escapes the repository: {target}") from exc
    if not relative.parts:
        raise ReleaseError("repository root cannot be replaced as a release output")
    current = root
    for part in relative.parts[:-1]:
        current = current / part
        try:
            current_stat = os.lstat(current)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ReleaseError(f"cannot inspect release output parent {current}: {exc}") from exc
        if stat.S_ISLNK(current_stat.st_mode):
            raise ReleaseError(f"release output parent must not be a symlink: {current}")
        if not stat.S_ISDIR(current_stat.st_mode):
            raise ReleaseError(f"release output parent must be a real directory: {current}")
    _snapshot_path(target)


def _snapshot_at(parent_fd: int, name: str, *, label: str) -> _PathState:
    try:
        entry_stat = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return _PathState("missing")
    except OSError as exc:
        raise ReleaseError(f"cannot inspect release target {label}: {exc}") from exc
    if stat.S_ISLNK(entry_stat.st_mode):
        raise ReleaseError(f"release target must not be a symlink: {label}")
    if stat.S_ISREG(entry_stat.st_mode):
        try:
            descriptor = os.open(name, _file_read_flags(), dir_fd=parent_fd)
        except OSError as exc:
            raise ReleaseError(f"cannot safely open release target {label}: {exc}") from exc
        try:
            digest = _hash_file_descriptor(descriptor, label=label)
            opened = os.fstat(descriptor)
            return _PathState("file", digest, opened.st_dev, opened.st_ino)
        finally:
            os.close(descriptor)
    if stat.S_ISDIR(entry_stat.st_mode):
        try:
            descriptor = os.open(name, _directory_open_flags(), dir_fd=parent_fd)
        except OSError as exc:
            raise ReleaseError(f"cannot safely open release directory {label}: {exc}") from exc
        try:
            digest = _directory_digest_descriptor(descriptor, label=label)
            opened = os.fstat(descriptor)
            return _PathState("directory", digest, opened.st_dev, opened.st_ino)
        finally:
            os.close(descriptor)
    raise ReleaseError(f"unsupported release target type: {label}")


def _assert_state_at(target: _PinnedTarget, expected: _PathState) -> None:
    current = _snapshot_at(target.parent_fd, target.name, label=str(target.path))
    if current != expected:
        raise ReleaseError(f"release target changed concurrently: {target.path}")


def _open_output_parent(
    root_fd: int,
    root: Path,
    target: Path,
    *,
    create_parents: bool = True,
) -> int:
    relative = target.relative_to(root)
    if not relative.parts:
        raise ReleaseError("repository root cannot be replaced as a release output")
    current_fd = os.dup(root_fd)
    try:
        for part in relative.parts[:-1]:
            if create_parents:
                try:
                    os.mkdir(part, 0o755, dir_fd=current_fd)
                except FileExistsError:
                    pass
                except OSError as exc:
                    raise ReleaseError(
                        f"cannot create release output parent {target.parent}: {exc}"
                    ) from exc
            try:
                next_fd = os.open(part, _directory_open_flags(), dir_fd=current_fd)
            except OSError as exc:
                raise ReleaseError(
                    f"release output parent must be a real, symlink-free directory: "
                    f"{target.parent} ({exc})"
                ) from exc
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except BaseException:
        os.close(current_fd)
        raise


def _pin_target(
    root_fd: int, root: Path, path: Path, expected: _PathState
) -> _PinnedTarget:
    parent_fd = _open_output_parent(root_fd, root, path)
    target = _PinnedTarget(path=path, parent_fd=parent_fd, name=path.name, expected=expected)
    try:
        _assert_state_at(target, expected)
        return target
    except BaseException:
        os.close(parent_fd)
        raise


def _assert_pinned_target_path(
    root_fd: int, root: Path, target: _PinnedTarget, expected: _PathState
) -> None:
    reopened_fd = _open_output_parent(
        root_fd,
        root,
        target.path,
        create_parents=False,
    )
    try:
        pinned_stat = os.fstat(target.parent_fd)
        reopened_stat = os.fstat(reopened_fd)
        if (pinned_stat.st_dev, pinned_stat.st_ino) != (
            reopened_stat.st_dev,
            reopened_stat.st_ino,
        ):
            raise ReleaseError(
                f"release output parent changed during publication: {target.path.parent}"
            )
        reopened_target = _PinnedTarget(
            path=target.path,
            parent_fd=reopened_fd,
            name=target.name,
            expected=expected,
        )
        _assert_state_at(reopened_target, expected)
    finally:
        os.close(reopened_fd)


def _copy_bytes(source_fd: int, destination_fd: int) -> None:
    os.lseek(source_fd, 0, os.SEEK_SET)
    while True:
        chunk = os.read(source_fd, 1024 * 1024)
        if not chunk:
            break
        view = memoryview(chunk)
        while view:
            written = os.write(destination_fd, view)
            if written <= 0:  # pragma: no cover - impossible without an OS fault
                raise OSError("zero-byte write while preparing release output")
            view = view[written:]


def _create_file_from_path(source: Path, target: _PinnedTarget) -> None:
    try:
        source_fd = os.open(source, _file_read_flags())
    except OSError as exc:
        raise ReleaseError(f"cannot safely open staged release file {source}: {exc}") from exc
    temporary_name = f".{target.name}.skillbump-new-{uuid.uuid4().hex}"
    temporary_created = False
    try:
        source_stat = os.fstat(source_fd)
        if not stat.S_ISREG(source_stat.st_mode):
            raise ReleaseError(f"staged release output must be a regular file: {source}")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        destination_fd = os.open(
            temporary_name, flags, 0o600, dir_fd=target.parent_fd
        )
        temporary_created = True
        try:
            _copy_bytes(source_fd, destination_fd)
            os.fchmod(destination_fd, stat.S_IMODE(source_stat.st_mode) & 0o777)
            os.fsync(destination_fd)
        finally:
            os.close(destination_fd)
        os.link(
            temporary_name,
            target.name,
            src_dir_fd=target.parent_fd,
            dst_dir_fd=target.parent_fd,
            follow_symlinks=False,
        )
    except OSError as exc:
        raise ReleaseError(f"cannot install release file {target.path}: {exc}") from exc
    finally:
        os.close(source_fd)
        if temporary_created:
            with contextlib.suppress(OSError):
                os.unlink(temporary_name, dir_fd=target.parent_fd)


def _copy_directory_contents(source_fd: int, destination_fd: int, *, label: str) -> None:
    scan_fd = os.dup(source_fd)
    try:
        with os.scandir(scan_fd) as iterator:
            names = sorted(entry.name for entry in iterator)
    finally:
        with contextlib.suppress(OSError):
            os.close(scan_fd)
    for name in names:
        relative_label = f"{label}/{name}"
        source_stat = os.stat(name, dir_fd=source_fd, follow_symlinks=False)
        if stat.S_ISLNK(source_stat.st_mode):
            raise ReleaseError(f"staged release directory contains a symlink: {relative_label}")
        if stat.S_ISREG(source_stat.st_mode):
            input_fd = os.open(name, _file_read_flags(), dir_fd=source_fd)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            try:
                output_fd = os.open(name, flags, 0o600, dir_fd=destination_fd)
                try:
                    _copy_bytes(input_fd, output_fd)
                    os.fchmod(output_fd, stat.S_IMODE(os.fstat(input_fd).st_mode) & 0o777)
                    os.fsync(output_fd)
                finally:
                    os.close(output_fd)
            finally:
                os.close(input_fd)
        elif stat.S_ISDIR(source_stat.st_mode):
            source_child = os.open(name, _directory_open_flags(), dir_fd=source_fd)
            try:
                os.mkdir(name, 0o700, dir_fd=destination_fd)
                destination_child = os.open(
                    name, _directory_open_flags(), dir_fd=destination_fd
                )
                try:
                    _copy_directory_contents(
                        source_child, destination_child, label=relative_label
                    )
                    os.fchmod(
                        destination_child,
                        stat.S_IMODE(os.fstat(source_child).st_mode) & 0o777,
                    )
                finally:
                    os.close(destination_child)
            finally:
                os.close(source_child)
        else:
            raise ReleaseError(f"unsupported staged release entry: {relative_label}")


def _create_directory_from_fd(
    source_fd: int, destination_parent_fd: int, destination_name: str, *, label: str
) -> None:
    source_stat = os.fstat(source_fd)
    if not stat.S_ISDIR(source_stat.st_mode):
        raise ReleaseError(f"staged release output must be a real directory: {label}")
    os.mkdir(destination_name, 0o700, dir_fd=destination_parent_fd)
    destination_fd = os.open(
        destination_name, _directory_open_flags(), dir_fd=destination_parent_fd
    )
    try:
        _copy_directory_contents(source_fd, destination_fd, label=label)
        os.fchmod(destination_fd, stat.S_IMODE(source_stat.st_mode) & 0o777)
    finally:
        os.close(destination_fd)


def _create_directory_from_path(source: Path, target: _PinnedTarget) -> None:
    try:
        source_fd = os.open(source, _directory_open_flags())
    except OSError as exc:
        raise ReleaseError(f"cannot safely open staged release directory {source}: {exc}") from exc
    try:
        _create_directory_from_fd(
            source_fd, target.parent_fd, target.name, label=str(source)
        )
    except OSError as exc:
        raise ReleaseError(f"cannot install release directory {target.path}: {exc}") from exc
    finally:
        os.close(source_fd)


def _remove_entry_at(parent_fd: int, name: str, *, label: str) -> None:
    state = _snapshot_at(parent_fd, name, label=label)
    if state.kind == "missing":
        return
    if state.kind == "file":
        os.unlink(name, dir_fd=parent_fd)
        return
    child_fd = os.open(name, _directory_open_flags(), dir_fd=parent_fd)
    try:
        scan_fd = os.dup(child_fd)
        try:
            with os.scandir(scan_fd) as iterator:
                names = sorted(entry.name for entry in iterator)
        finally:
            with contextlib.suppress(OSError):
                os.close(scan_fd)
        for child_name in names:
            _remove_entry_at(
                child_fd, child_name, label=f"{label}/{child_name}"
            )
    finally:
        os.close(child_fd)
    os.rmdir(name, dir_fd=parent_fd)


def _restore_entry_without_replace(
    source_parent_fd: int,
    source_name: str,
    destination: _PinnedTarget,
    *,
    source_label: str,
) -> _PathState:
    source_state = _snapshot_at(source_parent_fd, source_name, label=source_label)
    if source_state.kind == "missing":
        raise ReleaseError(f"recovery source is missing: {source_label}")
    _assert_state_at(destination, _PathState("missing"))
    _rename_noreplace(
        source_parent_fd,
        source_name,
        destination.parent_fd,
        destination.name,
    )
    destination_state = _snapshot_at(
        destination.parent_fd, destination.name, label=str(destination.path)
    )
    if destination_state != source_state:
        raise ReleaseError(
            f"recovery source changed while restoring {destination.path}; "
            "preserving its latest bytes at the live target"
        )
    return source_state


def _release_output_states(
    layout: Layout,
    *,
    target_version: Version,
    publish_submission_sheet: bool,
) -> dict[Path, _PathState]:
    paths = [layout.codex_manifest]
    if layout.claude_manifest is not None:
        paths.append(layout.claude_manifest)
    if layout.claude_marketplace is not None:
        paths.append(layout.claude_marketplace)
    if publish_submission_sheet and layout.submission_sheet is not None:
        paths.append(layout.submission_sheet)
    if layout.canonical_skill.resolve() != layout.packaged_skill.resolve():
        paths.append(layout.packaged_skill)
    paths.extend(
        [
            layout.root / "dist" / f"{layout.name}-plugin-{target_version}.zip",
            layout.root / "release" / "checklists" / f"{target_version}.md",
        ]
    )
    states: dict[Path, _PathState] = {}
    for path in paths:
        _assert_safe_output_path(layout.root, path)
        states[path] = _snapshot_path(path)
    return states


def _move_target_to_transaction(
    record: _InstallRecord,
    transaction_fd: int,
    transaction_label: str,
) -> None:
    _rename_noreplace(
        record.target.parent_fd,
        record.target.name,
        transaction_fd,
        record.backup_name,
    )
    moved = _snapshot_at(
        transaction_fd,
        record.backup_name,
        label=f"{transaction_label}/{record.backup_name}",
    )
    if moved != record.target.expected:
        try:
            _restore_entry_without_replace(
                transaction_fd,
                record.backup_name,
                record.target,
                source_label=f"{transaction_label}/{record.backup_name}",
            )
        except BaseException as restore_exc:
            raise ReleaseError(
                f"release target changed during backup and could not be restored: "
                f"{record.target.path} ({restore_exc})"
            ) from restore_exc
        raise ReleaseError(f"release target changed during backup: {record.target.path}")
    record.backup_captured = True


def _move_installed_to_failed(
    record: _InstallRecord,
    transaction_fd: int,
    transaction_label: str,
    *,
    index: int,
) -> str:
    failed_name = f"failed-{index}-{uuid.uuid4().hex}"
    _rename_noreplace(
        record.target.parent_fd,
        record.target.name,
        transaction_fd,
        failed_name,
    )
    moved = _snapshot_at(
        transaction_fd,
        failed_name,
        label=f"{transaction_label}/{failed_name}",
    )
    if moved != record.intended:
        try:
            _restore_entry_without_replace(
                transaction_fd,
                failed_name,
                record.target,
                source_label=f"{transaction_label}/{failed_name}",
            )
        except BaseException as restore_exc:
            raise ReleaseError(
                "target changed during rollback; preserving the moved target in the "
                f"recovery directory: {record.target.path} ({restore_exc})"
            ) from restore_exc
        raise ReleaseError(
            f"target changed during rollback and was restored without deletion: {record.target.path}"
        )
    return failed_name


def _rollback_install_records(
    records: list[_InstallRecord], transaction_fd: int, transaction_label: str
) -> list[str]:
    errors: list[str] = []
    for index, record in reversed(list(enumerate(records))):
        try:
            current = _snapshot_at(
                record.target.parent_fd,
                record.target.name,
                label=str(record.target.path),
            )
            backup = _snapshot_at(
                transaction_fd,
                record.backup_name,
                label=f"{transaction_label}/{record.backup_name}",
            )
            staged_new = _snapshot_at(
                transaction_fd,
                record.new_name,
                label=f"{transaction_label}/{record.new_name}",
            )

            # Recover phase flags if an asynchronous interruption landed just
            # after a successful no-replace rename and before the Python flag.
            if (
                not record.backup_captured
                and record.target.expected.kind != "missing"
                and backup == record.target.expected
            ):
                record.backup_captured = True
            if (
                not record.new_installed
                and staged_new.kind == "missing"
                and current == record.intended
            ):
                record.new_installed = True

            if record.new_installed:
                if current.kind == "missing":
                    raise ReleaseError(
                        "installed target was concurrently deleted; preserving its "
                        f"pre-release backup: {record.target.path}"
                    )
                if current != record.intended:
                    raise ReleaseError(
                        "installed target changed concurrently; preserving it and its "
                        f"pre-release backup: {record.target.path}"
                    )
                _move_installed_to_failed(
                    record,
                    transaction_fd,
                    transaction_label,
                    index=index,
                )
                record.new_installed = False
                if record.backup_captured:
                    restored = _restore_entry_without_replace(
                        transaction_fd,
                        record.backup_name,
                        record.target,
                        source_label=f"{transaction_label}/{record.backup_name}",
                    )
                    record.backup_captured = False
                    if restored != record.target.expected:
                        raise ReleaseError(
                            "the pre-release backup changed concurrently; its latest "
                            f"bytes were restored: {record.target.path}"
                        )
            elif record.backup_captured:
                if current.kind != "missing":
                    raise ReleaseError(
                        "target was recreated before rollback; preserving it and the "
                        f"pre-release backup: {record.target.path}"
                    )
                restored = _restore_entry_without_replace(
                    transaction_fd,
                    record.backup_name,
                    record.target,
                    source_label=f"{transaction_label}/{record.backup_name}",
                )
                record.backup_captured = False
                if restored != record.target.expected:
                    raise ReleaseError(
                        "the pre-release backup changed concurrently and its latest "
                        f"bytes were restored: {record.target.path}"
                    )
            elif current != record.target.expected:
                raise ReleaseError(
                    f"cannot safely restore release target: {record.target.path}"
                )
        except BaseException as rollback_exc:  # preserve user data on any rollback conflict
            errors.append(f"{record.target.path}: {rollback_exc}")
    return errors


def _publish_outputs(
    stage: Layout,
    live: Layout,
    *,
    publish_submission_sheet: bool,
    expected_states: dict[Path, _PathState],
    expected_root_identity: tuple[int, int],
) -> None:
    file_pairs: list[tuple[Path, Path]] = [(stage.codex_manifest, live.codex_manifest)]
    if stage.claude_manifest is not None and live.claude_manifest is not None:
        file_pairs.append((stage.claude_manifest, live.claude_manifest))
    if stage.claude_marketplace is not None and live.claude_marketplace is not None:
        file_pairs.append((stage.claude_marketplace, live.claude_marketplace))
    if (
        publish_submission_sheet
        and stage.submission_sheet is not None
        and live.submission_sheet is not None
    ):
        file_pairs.append((stage.submission_sheet, live.submission_sheet))
    file_pairs.extend(
        [
            (stage.archive, live.root / stage.archive.relative_to(stage.root)),
            (stage.checklist, live.root / stage.checklist.relative_to(stage.root)),
        ]
    )

    directory_pairs: list[tuple[Path, Path]] = []
    if stage.canonical_skill.resolve() != stage.packaged_skill.resolve():
        directory_pairs.append((stage.packaged_skill, live.packaged_skill))

    all_targets = [target for _, target in file_pairs + directory_pairs]
    if set(all_targets) != set(expected_states):
        raise ReleaseError("internal release target set changed during preparation")

    try:
        root_fd = os.open(live.root, _directory_open_flags())
    except OSError as exc:
        raise ReleaseError(f"cannot safely open repository root {live.root}: {exc}") from exc
    root_stat = os.fstat(root_fd)
    if (root_stat.st_dev, root_stat.st_ino) != expected_root_identity:
        os.close(root_fd)
        raise ReleaseError("repository root changed during release preparation")

    transaction_name = f".skillbump-txn-{uuid.uuid4().hex}"
    os.mkdir(transaction_name, 0o700, dir_fd=root_fd)
    transaction_label = str(live.root / transaction_name)
    transaction_fd = os.open(
        transaction_name, _directory_open_flags(), dir_fd=root_fd
    )
    records: list[_InstallRecord] = []
    pinned_targets: list[_PinnedTarget] = []
    keep_transaction = False
    try:
        for source, target in directory_pairs:
            pinned = _pin_target(
                root_fd, live.root, target, expected_states[target]
            )
            pinned_targets.append(pinned)
            source_state = _snapshot_path(source)
            if source_state.kind != "directory":
                raise ReleaseError(f"staged output is not a directory: {source}")
            new_name = f"new-{len(records)}-{uuid.uuid4().hex}"
            staged_target = _PinnedTarget(
                path=Path(transaction_label) / new_name,
                parent_fd=transaction_fd,
                name=new_name,
                expected=_PathState("missing"),
            )
            _create_directory_from_path(source, staged_target)
            intended = _snapshot_at(
                transaction_fd,
                new_name,
                label=f"{transaction_label}/{new_name}",
            )
            record = _InstallRecord(
                pinned, f"backup-{len(records)}", new_name, intended
            )
            records.append(record)
            _assert_state_at(pinned, pinned.expected)
            if pinned.expected.kind != "missing":
                _move_target_to_transaction(record, transaction_fd, transaction_label)
            _rename_noreplace(
                transaction_fd,
                record.new_name,
                pinned.parent_fd,
                pinned.name,
            )
            record.new_installed = True
            _assert_state_at(pinned, intended)

        for source, target in file_pairs:
            pinned = _pin_target(
                root_fd, live.root, target, expected_states[target]
            )
            pinned_targets.append(pinned)
            source_state = _snapshot_path(source)
            if source_state.kind != "file":
                raise ReleaseError(f"staged output is not a regular file: {source}")
            new_name = f"new-{len(records)}-{uuid.uuid4().hex}"
            staged_target = _PinnedTarget(
                path=Path(transaction_label) / new_name,
                parent_fd=transaction_fd,
                name=new_name,
                expected=_PathState("missing"),
            )
            _create_file_from_path(source, staged_target)
            intended = _snapshot_at(
                transaction_fd,
                new_name,
                label=f"{transaction_label}/{new_name}",
            )
            record = _InstallRecord(
                pinned, f"backup-{len(records)}", new_name, intended
            )
            records.append(record)
            _assert_state_at(pinned, pinned.expected)
            if pinned.expected.kind != "missing":
                _move_target_to_transaction(record, transaction_fd, transaction_label)
            _rename_noreplace(
                transaction_fd,
                record.new_name,
                pinned.parent_fd,
                pinned.name,
            )
            record.new_installed = True
            _assert_state_at(pinned, intended)

        path_stat = os.stat(live.root, follow_symlinks=False)
        if (
            stat.S_ISLNK(path_stat.st_mode)
            or (path_stat.st_dev, path_stat.st_ino) != expected_root_identity
        ):
            raise ReleaseError("repository root path changed during publication")
        for record in records:
            _assert_state_at(record.target, record.intended)
            _assert_pinned_target_path(
                root_fd, live.root, record.target, record.intended
            )
            if not record.new_installed:
                raise ReleaseError(
                    f"release target was not installed: {record.target.path}"
                )
            staged_new = _snapshot_at(
                transaction_fd,
                record.new_name,
                label=f"{transaction_label}/{record.new_name}",
            )
            if staged_new.kind != "missing":
                raise ReleaseError(
                    f"staged transaction output was not consumed: {record.target.path}"
                )
            if record.backup_captured:
                backup = _snapshot_at(
                    transaction_fd,
                    record.backup_name,
                    label=f"{transaction_label}/{record.backup_name}",
                )
                if backup != record.target.expected:
                    raise ReleaseError(
                        f"pre-release backup changed concurrently: {record.target.path}"
                    )
            elif record.target.expected.kind != "missing":
                raise ReleaseError(
                    f"pre-release backup was not captured: {record.target.path}"
                )
    except BaseException as exc:
        rollback_errors = _rollback_install_records(
            records, transaction_fd, transaction_label
        )
        if rollback_errors:
            keep_transaction = True
            raise ReleaseError(
                f"release failed ({exc}); rollback also failed: {'; '.join(rollback_errors)}; "
                f"recovery data remains at {transaction_label}"
            ) from exc
        if isinstance(exc, ReleaseError) or not isinstance(exc, Exception):
            raise
        raise ReleaseError(f"release publication failed: {exc}") from exc
    finally:
        for target in pinned_targets:
            with contextlib.suppress(OSError):
                os.close(target.parent_fd)
        os.close(transaction_fd)
        if not keep_transaction:
            with contextlib.suppress(OSError, ReleaseError):
                _remove_entry_at(root_fd, transaction_name, label=transaction_label)
        os.close(root_fd)


def prepare(
    root: str | os.PathLike[str],
    *,
    notes: str,
    bump: str = "patch",
    target_version: str | None = None,
    expect: str | None = None,
    dry_run: bool = False,
    run_checks: bool = True,
    openai_submission: str = "skip",
) -> dict[str, Any]:
    normalized_notes = _normalize_release_notes(notes)
    if openai_submission not in {"initial", "update", "skip"}:
        raise ReleaseError(f"unsupported OpenAI submission kind: {openai_submission}")
    root_path = Path(root).expanduser().resolve()
    with release_lock(root_path):
        # All release-state reads happen under the same lock as publication. A
        # second SkillBump process cannot advance the version between --expect
        # validation and staging.
        live = discover(root_path)
        live_root_stat = os.stat(live.root, follow_symlinks=False)
        if stat.S_ISLNK(live_root_stat.st_mode) or not stat.S_ISDIR(live_root_stat.st_mode):
            raise ReleaseError(f"repository root must be a real directory: {live.root}")
        expected_root_identity = (live_root_stat.st_dev, live_root_stat.st_ino)
        if expect is not None and Version.parse(expect, label="expected version") != live.current:
            raise ReleaseError(f"expected version {expect}, but manifests contain {live.current}")
        target = _target_version(live.current, bump, target_version)
        if openai_submission != "skip" and live.submission_sheet is None:
            raise ReleaseError("OpenAI submission was requested, but no submission sheet exists")
        expected_states = _release_output_states(
            live,
            target_version=target,
            publish_submission_sheet=openai_submission != "skip",
        )
        initial_fingerprint = worktree_fingerprint(live.root)
        with tempfile.TemporaryDirectory(prefix="skillbump-release-") as temporary:
            temp_root = Path(temporary)
            stage_root = temp_root / "repository"
            _copy_worktree(live.root, stage_root)
            staged, checks, sha256 = _stage_release(
                stage_root,
                target=target,
                notes=normalized_notes,
                run_checks=run_checks,
                temp_root=temp_root,
                openai_submission=openai_submission,
            )
            if openai_submission == "update":
                portal_step = "upload the ZIP as an update in the OpenAI plugin portal"
            elif openai_submission == "initial":
                portal_step = "upload the ZIP as an initial OpenAI plugin submission"
            else:
                portal_step = "choose initial or update before any OpenAI portal submission"
            result = {
                "command": "prepare",
                "dry_run": dry_run,
                "root": str(live.root),
                "name": live.name,
                "previous_version": str(live.current),
                "version": str(target),
                "archive": f"dist/{live.name}-plugin-{target}.zip",
                "archive_sha256": sha256,
                "archive_bytes": staged.archive.stat().st_size,
                "checklist": f"release/checklists/{target}.md",
                "openai_submission": openai_submission,
                "checks": checks,
                "next_steps": [
                    "review the Git diff and cold-install both packages",
                    "commit and push the reviewed release",
                    portal_step,
                    "after any OpenAI approval, publish it from the portal",
                ],
                "committed": False,
                "pushed": False,
                "published": False,
            }
            if dry_run:
                return result
            if worktree_fingerprint(live.root) != initial_fingerprint:
                raise ReleaseError("the live worktree changed during preparation; nothing was published")
            _publish_outputs(
                staged,
                live,
                publish_submission_sheet=openai_submission != "skip",
                expected_states=expected_states,
                expected_root_identity=expected_root_identity,
            )
            result["dry_run"] = False
            return result


def verify(root: str | os.PathLike[str]) -> dict[str, Any]:
    layout = discover(root)
    _verify_layout(layout, require_archive=True)
    sha256 = hashlib.sha256(layout.archive.read_bytes()).hexdigest()
    return {
        "command": "verify",
        "root": str(layout.root),
        "name": layout.name,
        "version": str(layout.current),
        "archive": layout.relative(layout.archive),
        "archive_sha256": sha256,
        "archive_bytes": layout.archive.stat().st_size,
        "submission_sheet_ready": _submission_ready(layout),
        "verified": True,
    }
