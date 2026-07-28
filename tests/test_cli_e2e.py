from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from test_release import make_repository, visible_snapshot


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class CliEndToEndTests(unittest.TestCase):
    def run_cli(self, root: Path, *arguments: str) -> dict:
        environment = os.environ.copy()
        installed = shutil.which("skillbump")
        if installed:
            command = [installed]
        else:
            command = [sys.executable, "-m", "skillbump"]
            existing = environment.get("PYTHONPATH")
            source = str(PROJECT_ROOT / "src")
            environment["PYTHONPATH"] = (
                source if not existing else source + os.pathsep + existing
            )
        completed = subprocess.run(
            [*command, "-C", str(root), "--json", *arguments],
            cwd=root,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(
            completed.returncode,
            0,
            msg=f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}",
        )
        return json.loads(completed.stdout)

    def test_plan_dry_run_prepare_verify_critical_journey(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            make_repository(root, stale_package=True)

            planned = self.run_cli(root, "plan", "--openai-submission", "skip")
            self.assertTrue(planned["ok"])
            self.assertEqual(planned["current_version"], "1.0.0")
            self.assertEqual(planned["target_version"], "1.0.1")
            self.assertEqual(planned["repository_checks_status"], "configured")
            self.assertIn("update SKILL.md", planned["sync_changes"])

            before = visible_snapshot(root)
            dry_run = self.run_cli(
                root,
                "prepare",
                "--to",
                "1.0.1",
                "--expect",
                "1.0.0",
                "--notes",
                "Exercises the installed CLI release journey.",
                "--openai-submission",
                "skip",
                "--dry-run",
            )
            self.assertTrue(dry_run["dry_run"])
            self.assertEqual(dry_run["repository_checks_status"], "passed")
            self.assertEqual(visible_snapshot(root), before)

            prepared = self.run_cli(
                root,
                "prepare",
                "--to",
                "1.0.1",
                "--expect",
                "1.0.0",
                "--notes",
                "Exercises the installed CLI release journey.",
                "--openai-submission",
                "skip",
            )
            self.assertFalse(prepared["dry_run"])
            self.assertEqual(prepared["repository_checks_status"], "passed")
            self.assertTrue((root / prepared["archive"]).is_file())
            self.assertTrue((root / prepared["release_evidence"]).is_file())

            verified = self.run_cli(root, "verify")
            self.assertTrue(verified["verified"])
            self.assertTrue(verified["release_evidence_verified"])
            self.assertEqual(verified["repository_checks_status"], "passed")


if __name__ == "__main__":
    unittest.main()
