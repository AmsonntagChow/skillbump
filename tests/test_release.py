from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from skillbump.release import (  # noqa: E402
    ReleaseError,
    Version,
    build_archive,
    discover,
    plan,
    prepare,
    verify,
    verify_archive,
)


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def make_repository(root: Path, *, version: str = "1.0.0", stale_package: bool = False) -> None:
    name = "demo-skill"
    canonical = root / "skills" / name
    canonical.mkdir(parents=True)
    (canonical / "SKILL.md").write_text(
        "---\nname: demo-skill\ndescription: Demo release fixture.\n---\n\n# Demo\n",
        encoding="utf-8",
    )
    (canonical / "references").mkdir()
    (canonical / "references" / "rules.md").write_text("new rules\n", encoding="utf-8")

    plugin = root / "plugins" / name
    packaged = plugin / "skills" / name
    packaged.mkdir(parents=True)
    (packaged / "SKILL.md").write_text(
        "old skill\n" if stale_package else (canonical / "SKILL.md").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (packaged / "references").mkdir()
    (packaged / "references" / "rules.md").write_text(
        "old rules\n" if stale_package else "new rules\n", encoding="utf-8"
    )
    (plugin / "assets").mkdir()
    (plugin / "assets" / "logo.txt").write_text("logo\n", encoding="utf-8")

    write_json(
        plugin / ".codex-plugin" / "plugin.json",
        {
            "name": name,
            "version": version,
            "description": "Demo",
            "skills": "./skills/",
        },
    )
    write_json(
        root / ".claude-plugin" / "plugin.json",
        {"name": name, "version": version, "description": "Demo"},
    )
    write_json(
        root / ".claude-plugin" / "marketplace.json",
        {
            "name": "demo-marketplace",
            "plugins": [{"name": name, "source": ".", "version": version}],
        },
    )
    write_json(
        root / ".agents" / "plugins" / "marketplace.json",
        {
            "name": "demo-skill",
            "plugins": [
                {
                    "name": name,
                    "source": {"source": "local", "path": f"./plugins/{name}"},
                }
            ],
        },
    )
    submission = root / "submission" / "PLUGIN_DIRECTORY.md"
    submission.parent.mkdir()
    submission.write_text(
        "# Submission\n\n"
        f"Upload `dist/{name}-plugin-{version}.zip`.\n\n"
        "## Release notes\n\n"
        f"Initial {version} release.\n\n"
        "## Package contents\n\nFiles.\n",
        encoding="utf-8",
    )
    scripts = root / "scripts"
    scripts.mkdir()
    (scripts / "validate_repo.py").write_text("print('fixture validation passed')\n", encoding="utf-8")
    tests = root / "tests"
    tests.mkdir()
    (tests / "test_smoke.py").write_text(
        "import unittest\n\n"
        "class SmokeTest(unittest.TestCase):\n"
        "    def test_passes(self):\n"
        "        self.assertTrue(True)\n",
        encoding="utf-8",
    )


def visible_snapshot(root: Path) -> dict[str, bytes]:
    result: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and ".git" not in path.parts:
            result[path.relative_to(root).as_posix()] = path.read_bytes()
    return result


class VersionTests(unittest.TestCase):
    def test_strict_semver_and_bumps(self) -> None:
        version = Version.parse("1.2.3")
        self.assertEqual(str(version.bump("patch")), "1.2.4")
        self.assertEqual(str(version.bump("minor")), "1.3.0")
        self.assertEqual(str(version.bump("major")), "2.0.0")
        for invalid in ("1.01", "01.0.0", "1.0", "v1.0.0", "1.0.0-beta"):
            with self.subTest(invalid=invalid), self.assertRaises(ReleaseError):
                Version.parse(invalid)


class PlanTests(unittest.TestCase):
    def test_plan_is_read_only_and_reports_sync(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            make_repository(root, stale_package=True)
            before = visible_snapshot(root)
            result = plan(root)
            self.assertEqual(result["current_version"], "1.0.0")
            self.assertEqual(result["target_version"], "1.0.1")
            self.assertIn("update SKILL.md", result["sync_changes"])
            self.assertEqual(visible_snapshot(root), before)

    def test_manifest_mismatch_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            make_repository(root)
            path = root / ".claude-plugin" / "plugin.json"
            value = json.loads(path.read_text(encoding="utf-8"))
            value["version"] = "1.0.1"
            write_json(path, value)
            with self.assertRaisesRegex(ReleaseError, "not synchronized"):
                plan(root)


class PrepareTests(unittest.TestCase):
    def test_dry_run_leaves_live_repository_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            make_repository(root, stale_package=True)
            before = visible_snapshot(root)
            result = prepare(root, target_version="1.0.1", notes="Improves evidence rules.", dry_run=True)
            self.assertTrue(result["dry_run"])
            self.assertEqual(result["version"], "1.0.1")
            self.assertEqual(visible_snapshot(root), before)

    def test_prepare_updates_versions_syncs_and_packages(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            make_repository(root, stale_package=True)
            agents_before = (root / ".agents" / "plugins" / "marketplace.json").read_bytes()

            result = prepare(
                root,
                target_version="1.0.1",
                expect="1.0.0",
                notes="Improves evidence rules.",
                openai_submission="update",
            )

            self.assertFalse(result["dry_run"])
            layout = discover(root)
            self.assertEqual(str(layout.current), "1.0.1")
            self.assertEqual(
                (layout.canonical_skill / "SKILL.md").read_bytes(),
                (layout.packaged_skill / "SKILL.md").read_bytes(),
            )
            marketplace = json.loads(
                (root / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8")
            )
            self.assertEqual(marketplace["plugins"][0]["version"], "1.0.1")
            self.assertEqual(
                (root / ".agents" / "plugins" / "marketplace.json").read_bytes(), agents_before
            )
            archive = root / "dist" / "demo-skill-plugin-1.0.1.zip"
            self.assertTrue(archive.is_file())
            self.assertEqual(hashlib.sha256(archive.read_bytes()).hexdigest(), result["archive_sha256"])
            with zipfile.ZipFile(archive) as package:
                self.assertIn(".codex-plugin/plugin.json", package.namelist())
                self.assertNotIn("demo-skill/.codex-plugin/plugin.json", package.namelist())
            sheet = (root / "submission" / "PLUGIN_DIRECTORY.md").read_text(encoding="utf-8")
            self.assertIn("demo-skill-plugin-1.0.1.zip", sheet)
            self.assertIn("Version 1.0.1 update. Improves evidence rules.", sheet)
            self.assertTrue((root / "release" / "checklists" / "1.0.1.md").is_file())
            self.assertTrue(verify(root)["verified"])

    def test_initial_submission_is_not_mislabeled_as_update(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            make_repository(root)
            prepare(
                root,
                notes="Introduces the public Skill workflow.",
                openai_submission="initial",
            )
            sheet = (root / "submission" / "PLUGIN_DIRECTORY.md").read_text(encoding="utf-8")
            self.assertIn(
                "Initial submission at version 1.0.1. Introduces the public Skill workflow.",
                sheet,
            )
            self.assertNotIn("Version 1.0.1 update", sheet)

    def test_skip_submission_leaves_sheet_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            make_repository(root)
            sheet = root / "submission" / "PLUGIN_DIRECTORY.md"
            before = sheet.read_bytes()
            prepare(root, notes="Release without a portal submission.")
            self.assertEqual(sheet.read_bytes(), before)

    def test_failed_repository_check_leaves_live_bytes_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            make_repository(root)
            (root / "scripts" / "validate_repo.py").write_text(
                "raise SystemExit('expected failure')\n", encoding="utf-8"
            )
            before = visible_snapshot(root)
            with self.assertRaisesRegex(ReleaseError, "repository check failed"):
                prepare(root, notes="A release that must fail.")
            self.assertEqual(visible_snapshot(root), before)

    def test_repository_check_cannot_smuggle_an_input_change(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            make_repository(root)
            (root / "README.md").write_text("before\n", encoding="utf-8")
            (root / "scripts" / "validate_repo.py").write_text(
                "from pathlib import Path\nPath('README.md').write_text('after\\n')\n",
                encoding="utf-8",
            )
            before = visible_snapshot(root)
            with self.assertRaisesRegex(ReleaseError, "modified release inputs"):
                prepare(root, notes="A release that must fail.")
            self.assertEqual(visible_snapshot(root), before)

    def test_expect_guard_and_target_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            make_repository(root)
            with self.assertRaisesRegex(ReleaseError, "expected version"):
                prepare(root, notes="No release.", expect="0.9.0")
            with self.assertRaisesRegex(ReleaseError, "must be greater"):
                prepare(root, notes="No release.", target_version="1.0.0")

    def test_concurrent_live_change_blocks_publish(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            make_repository(root)
            from skillbump import release as release_module

            original = release_module._stage_release

            def mutate_after_stage(*args, **kwargs):
                result = original(*args, **kwargs)
                (root / "README.md").write_text("concurrent edit\n", encoding="utf-8")
                return result

            with mock.patch("skillbump.release._stage_release", side_effect=mutate_after_stage):
                with self.assertRaisesRegex(ReleaseError, "live worktree changed"):
                    prepare(root, notes="Concurrent test.")
            self.assertEqual(
                json.loads((root / ".claude-plugin" / "plugin.json").read_text())["version"],
                "1.0.0",
            )
            self.assertEqual((root / "README.md").read_text(), "concurrent edit\n")

    def test_expect_is_rechecked_after_acquiring_release_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            make_repository(root)
            from skillbump import release as release_module

            @mock.patch("skillbump.release.fcntl.flock")
            def advance_while_waiting(_mock_flock) -> None:
                original_lock = release_module.release_lock

                @release_module.contextlib.contextmanager
                def advancing_lock(lock_root):
                    with original_lock(lock_root):
                        for path in (
                            root / ".claude-plugin" / "plugin.json",
                            root / "plugins" / "demo-skill" / ".codex-plugin" / "plugin.json",
                        ):
                            value = json.loads(path.read_text(encoding="utf-8"))
                            value["version"] = "1.0.1"
                            write_json(path, value)
                        marketplace_path = root / ".claude-plugin" / "marketplace.json"
                        marketplace = json.loads(marketplace_path.read_text(encoding="utf-8"))
                        marketplace["plugins"][0]["version"] = "1.0.1"
                        write_json(marketplace_path, marketplace)
                        yield

                with mock.patch("skillbump.release.release_lock", advancing_lock):
                    with self.assertRaisesRegex(ReleaseError, "expected version"):
                        prepare(root, notes="Must observe the locked version.", expect="1.0.0")

            advance_while_waiting()

    def test_per_target_compare_and_swap_preserves_concurrent_manifest_edit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            make_repository(root, stale_package=True)
            packaged_before = visible_snapshot(root / "plugins" / "demo-skill" / "skills")
            from skillbump import release as release_module

            original = release_module._rename_noreplace
            codex = (
                root / "plugins" / "demo-skill" / ".codex-plugin" / "plugin.json"
            ).resolve()

            mutated = False

            def mutate_target_before_backup(
                source_parent_fd, source_name, destination_parent_fd, destination_name
            ):
                nonlocal mutated
                if not mutated and source_name == "plugin.json":
                    value = json.loads(codex.read_text(encoding="utf-8"))
                    value["concurrent_edit"] = "preserve me"
                    write_json(codex, value)
                    mutated = True
                return original(
                    source_parent_fd,
                    source_name,
                    destination_parent_fd,
                    destination_name,
                )

            with mock.patch(
                "skillbump.release._rename_noreplace",
                side_effect=mutate_target_before_backup,
            ):
                with self.assertRaises(ReleaseError):
                    prepare(root, notes="CAS regression test.")

            self.assertEqual(
                json.loads(codex.read_text(encoding="utf-8"))["concurrent_edit"],
                "preserve me",
            )
            self.assertEqual(
                visible_snapshot(root / "plugins" / "demo-skill" / "skills"),
                packaged_before,
            )

    def test_compare_and_swap_detects_byte_identical_inode_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            make_repository(root)
            from skillbump import release as release_module

            original = release_module._rename_noreplace
            codex = (
                root / "plugins" / "demo-skill" / ".codex-plugin" / "plugin.json"
            ).resolve()
            original_bytes = codex.read_bytes()
            original_inode = codex.stat().st_ino
            replaced = False

            def replace_before_backup(
                source_parent_fd, source_name, destination_parent_fd, destination_name
            ):
                nonlocal replaced
                if not replaced and source_name == "plugin.json":
                    replacement = codex.parent / ".identical-replacement"
                    replacement.write_bytes(original_bytes)
                    os.replace(replacement, codex)
                    replaced = True
                return original(
                    source_parent_fd,
                    source_name,
                    destination_parent_fd,
                    destination_name,
                )

            with mock.patch(
                "skillbump.release._rename_noreplace", side_effect=replace_before_backup
            ):
                with self.assertRaises(ReleaseError):
                    prepare(root, notes="Identity CAS regression test.")
            self.assertEqual(codex.read_bytes(), original_bytes)
            self.assertNotEqual(codex.stat().st_ino, original_inode)

    def test_directory_copy_failure_never_exposes_partial_live_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            make_repository(root, stale_package=True)
            before = visible_snapshot(root)
            from skillbump import release as release_module

            def fail_after_partial_copy(source_fd, destination_fd, *, label):
                descriptor = os.open(
                    "partial.txt",
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=destination_fd,
                )
                os.close(descriptor)
                raise OSError("forced directory copy failure")

            with mock.patch(
                "skillbump.release._copy_directory_contents",
                side_effect=fail_after_partial_copy,
            ):
                with self.assertRaisesRegex(ReleaseError, "forced directory copy failure"):
                    prepare(root, notes="Private staging regression test.")
            self.assertEqual(visible_snapshot(root), before)
            self.assertEqual(list(root.glob(".skillbump-txn-*")), [])

    def test_rollback_does_not_overwrite_edit_made_after_publish(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            make_repository(root)
            from skillbump import release as release_module

            original_assert = release_module._assert_state_at
            codex = (
                root / "plugins" / "demo-skill" / ".codex-plugin" / "plugin.json"
            ).resolve()

            checklist_assertions = 0

            def fail_after_publication(target, expected):
                nonlocal checklist_assertions
                result = original_assert(target, expected)
                if target.path.name == "1.0.1.md":
                    checklist_assertions += 1
                if checklist_assertions == 4:
                    value = json.loads(codex.read_text(encoding="utf-8"))
                    value["concurrent_edit"] = "preserve me"
                    write_json(codex, value)
                    raise ReleaseError("forced live verification failure")
                return result

            with mock.patch(
                "skillbump.release._assert_state_at", side_effect=fail_after_publication
            ):
                with self.assertRaisesRegex(ReleaseError, "rollback also failed"):
                    prepare(root, notes="Rollback CAS regression test.")

            value = json.loads(codex.read_text(encoding="utf-8"))
            self.assertEqual(value["version"], "1.0.1")
            self.assertEqual(value["concurrent_edit"], "preserve me")

    def test_rollback_rechecks_object_after_moving_it_to_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            make_repository(root)
            from skillbump import release as release_module

            original_assert = release_module._assert_state_at
            original_rename = release_module._rename_noreplace
            codex = (
                root / "plugins" / "demo-skill" / ".codex-plugin" / "plugin.json"
            ).resolve()
            mutated = False

            checklist_assertions = 0

            def fail_after_publication(target, expected):
                nonlocal checklist_assertions
                result = original_assert(target, expected)
                if target.path.name == "1.0.1.md":
                    checklist_assertions += 1
                if checklist_assertions == 4:
                    raise ReleaseError("force rollback")
                return result

            def mutate_before_failed_move(
                source_parent_fd, source_name, destination_parent_fd, destination_name
            ):
                nonlocal mutated
                if (
                    not mutated
                    and source_name == "plugin.json"
                    and destination_name.startswith("failed-")
                ):
                    value = json.loads(codex.read_text(encoding="utf-8"))
                    value["concurrent_edit"] = "preserve during rollback"
                    write_json(codex, value)
                    mutated = True
                return original_rename(
                    source_parent_fd,
                    source_name,
                    destination_parent_fd,
                    destination_name,
                )

            with mock.patch(
                "skillbump.release._assert_state_at", side_effect=fail_after_publication
            ), mock.patch(
                "skillbump.release._rename_noreplace",
                side_effect=mutate_before_failed_move,
            ):
                with self.assertRaisesRegex(ReleaseError, "rollback also failed"):
                    prepare(root, notes="Rollback move CAS regression test.")

            value = json.loads(codex.read_text(encoding="utf-8"))
            self.assertEqual(value["concurrent_edit"], "preserve during rollback")

    def test_rollback_does_not_resurrect_concurrently_deleted_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            make_repository(root)
            from skillbump import release as release_module

            original_assert = release_module._assert_state_at
            codex = (
                root / "plugins" / "demo-skill" / ".codex-plugin" / "plugin.json"
            ).resolve()
            checklist_assertions = 0
            deleted = False

            def delete_after_publication(target, expected):
                nonlocal checklist_assertions, deleted
                result = original_assert(target, expected)
                if target.path.name == "1.0.1.md":
                    checklist_assertions += 1
                if checklist_assertions == 4 and not deleted:
                    codex.unlink()
                    deleted = True
                    raise ReleaseError("forced failure after concurrent delete")
                return result

            with mock.patch(
                "skillbump.release._assert_state_at", side_effect=delete_after_publication
            ):
                with self.assertRaisesRegex(ReleaseError, "rollback also failed"):
                    prepare(root, notes="Concurrent deletion regression test.")
            self.assertFalse(codex.exists())
            self.assertEqual(len(list(root.resolve().glob(".skillbump-txn-*"))), 1)

    def test_keyboard_interrupt_rolls_back_all_published_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            make_repository(root, stale_package=True)
            before = visible_snapshot(root)
            from skillbump import release as release_module

            original_assert = release_module._assert_state_at
            checklist_assertions = 0
            interrupted = False

            def interrupt_after_publication(target, expected):
                nonlocal checklist_assertions, interrupted
                result = original_assert(target, expected)
                if target.path.name == "1.0.1.md":
                    checklist_assertions += 1
                if checklist_assertions == 4 and not interrupted:
                    interrupted = True
                    raise KeyboardInterrupt()
                return result

            with mock.patch(
                "skillbump.release._assert_state_at",
                side_effect=interrupt_after_publication,
            ):
                with self.assertRaises(KeyboardInterrupt):
                    prepare(root, notes="Interrupt regression test.")
            self.assertEqual(visible_snapshot(root), before)

    def test_versionless_claude_marketplace_stays_versionless(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            make_repository(root)
            path = root / ".claude-plugin" / "marketplace.json"
            value = json.loads(path.read_text(encoding="utf-8"))
            del value["plugins"][0]["version"]
            write_json(path, value)
            prepare(root, notes="Versionless marketplace entry.")
            value = json.loads(path.read_text(encoding="utf-8"))
            self.assertNotIn("version", value["plugins"][0])


class ArchiveTests(unittest.TestCase):
    def test_archive_is_reproducible(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plugin = root / "plugin"
            (plugin / ".codex-plugin").mkdir(parents=True)
            (plugin / ".codex-plugin" / "plugin.json").write_text("{}\n")
            (plugin / "skills" / "demo").mkdir(parents=True)
            (plugin / "skills" / "demo" / "SKILL.md").write_text("demo\n")
            first = root / "first.zip"
            second = root / "second.zip"
            self.assertEqual(build_archive(plugin, first), build_archive(plugin, second))
            self.assertEqual(first.read_bytes(), second.read_bytes())

    @unittest.skipIf(os.name == "nt", "symlink semantics differ on Windows")
    def test_archive_rejects_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plugin = root / "plugin"
            plugin.mkdir()
            (plugin / "file.txt").write_text("content\n")
            (plugin / "link.txt").symlink_to("file.txt")
            with self.assertRaisesRegex(ReleaseError, "symlinks"):
                build_archive(plugin, root / "plugin.zip")

    def test_verifier_rejects_zip_member_marked_as_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plugin = root / "plugin"
            plugin.mkdir()
            source = plugin / "file.txt"
            source.write_text("../../outside\n", encoding="utf-8")
            archive_path = root / "plugin.zip"
            info = zipfile.ZipInfo("file.txt")
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr(info, source.read_bytes())
            with self.assertRaisesRegex(ReleaseError, "not a regular file"):
                verify_archive(plugin, archive_path)

    @unittest.skipIf(os.name == "nt", "symlink semantics differ on Windows")
    def test_prepare_rejects_dist_symlink_without_writing_outside_repo(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "repository"
            outside = base / "outside"
            root.mkdir()
            outside.mkdir()
            make_repository(root)
            (root / "dist").symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(ReleaseError, "symlink"):
                prepare(root, notes="Must stay inside the repository.")
            self.assertEqual(list(outside.iterdir()), [])

    @unittest.skipIf(os.name == "nt", "dir_fd semantics differ on Windows")
    def test_parent_fd_prevents_symlink_swap_from_redirecting_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "repository"
            outside = base / "outside"
            root.mkdir()
            outside.mkdir()
            make_repository(root)
            from skillbump import release as release_module

            original = release_module._open_output_parent
            swapped = False

            def swap_dist_after_pin(
                root_fd, repository_root, target, *, create_parents=True
            ):
                nonlocal swapped
                parent_fd = original(
                    root_fd,
                    repository_root,
                    target,
                    create_parents=create_parents,
                )
                if (
                    not swapped
                    and create_parents
                    and target.parent.name == "dist"
                ):
                    dist = root.resolve() / "dist"
                    dist.rename(root.resolve() / "dist-pinned")
                    dist.symlink_to(outside, target_is_directory=True)
                    swapped = True
                return parent_fd

            with mock.patch(
                "skillbump.release._open_output_parent", side_effect=swap_dist_after_pin
            ):
                with self.assertRaises(ReleaseError):
                    prepare(root, notes="Pinned parent regression test.")
            self.assertEqual(list(outside.iterdir()), [])

    def test_root_level_plugin_packages_only_standard_plugin_members(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            make_repository(root)
            nested_plugin = root / "plugins" / "demo-skill"
            shutil.copytree(nested_plugin / ".codex-plugin", root / ".codex-plugin")
            shutil.copytree(nested_plugin / "assets", root / "assets")
            shutil.rmtree(root / "plugins")
            (root / "README.md").write_text("repository docs\n", encoding="utf-8")

            result = prepare(root, notes="Root package release.")

            archive_path = root / result["archive"]
            with zipfile.ZipFile(archive_path) as archive:
                names = archive.namelist()
            self.assertIn(".codex-plugin/plugin.json", names)
            self.assertIn("skills/demo-skill/SKILL.md", names)
            self.assertNotIn("README.md", names)
            self.assertFalse(any(name.startswith("dist/") for name in names))
            self.assertFalse(any(name.startswith("release/") for name in names))
            self.assertTrue(verify(root)["verified"])


if __name__ == "__main__":
    unittest.main()
