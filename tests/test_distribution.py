"""Distribution behavior tests use temporary source and install directories."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile


TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))
import package_skill
import install_skill
sys.path.pop(0)


class DistributionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="loom-distribution-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.repository = self.base / "repository"
        self.source = self.repository / "memory-loom"
        (self.source / "assets").mkdir(parents=True)
        (self.source / "scripts").mkdir()
        (self.source / "SKILL.md").write_text('---\nname: memory-loom\ndescription: "fixture"\n---\n# Skill\n', encoding="utf-8")
        (self.source / "assets" / "MEMORY.md").write_text("# 项目工作流\n", encoding="utf-8")
        (self.source / "scripts" / "memory.py").write_text("print('fixture')\n", encoding="utf-8")
        (self.repository / "VERSION").write_text("0.1.0\n", encoding="utf-8")
        (self.repository / "LICENSE").write_text("Fixture license\n", encoding="utf-8")
        self.destination = self.base / "profile" / "skills" / "memory-loom"

    def snapshot(self, path):
        return {p.relative_to(path).as_posix(): p.read_bytes() for p in path.rglob("*") if p.is_file()}

    def test_package_is_reproducible_and_checksum_matches(self):
        first = package_skill.build_package(self.repository, self.base / "first")
        for path in self.source.rglob("*"):
            os.utime(path, (1700000000, 1700000000))
        second = package_skill.build_package(self.repository, self.base / "second")
        data = Path(first["archive"]).read_bytes()
        self.assertEqual(data, Path(second["archive"]).read_bytes())
        self.assertEqual(hashlib.sha256(data).hexdigest(), first["sha256"])
        self.assertEqual(Path(first["checksums"]).read_text(), first["sha256"] + "  memory-loom-0.1.0.zip\n")
        with zipfile.ZipFile(first["archive"]) as archive:
            self.assertIsNone(archive.testzip())
            self.assertIn("memory-loom/LICENSE", archive.namelist())
            self.assertIn("memory-loom/VERSION", archive.namelist())
            self.assertTrue(all(info.date_time == (1980, 1, 1, 0, 0, 0) for info in archive.infolist()))

    def test_package_excludes_private_data_and_caches(self):
        for name in [".project-memory/tasks/secret.md", "assets/.project-memory/config.json", "scripts/__pycache__/cache.pyc", "assets/工作记录/private.md", ".DS_Store", "scripts/run.lock", "private.txt"]:
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("private sentinel", encoding="utf-8")
        result = package_skill.build_package(self.repository)
        with zipfile.ZipFile(result["archive"]) as archive:
            self.assertFalse(any(b"private sentinel" in archive.read(name) for name in archive.namelist()))

    def test_release_rejects_symlinked_source_file(self):
        outside = self.base / "outside.txt"
        outside.write_text("private", encoding="utf-8")
        (self.source / "assets" / "leak.md").symlink_to(outside)
        with self.assertRaises(package_skill.DistributionError):
            package_skill.build_package(self.repository)
        self.assertFalse((self.repository / "dist").exists())

    def test_package_cannot_write_inside_skill_source(self):
        before = self.snapshot(self.source)
        with self.assertRaises(package_skill.DistributionError):
            package_skill.build_package(self.repository, self.source / "assets" / "releases")
        self.assertEqual(before, self.snapshot(self.source))

    def test_install_is_idempotent_and_includes_license(self):
        first = install_skill.install(self.repository, self.destination)
        before = self.snapshot(self.destination)
        second = install_skill.install(self.repository, self.destination)
        self.assertEqual(first["action"], "installed")
        self.assertEqual(second["action"], "unchanged")
        self.assertEqual(before, self.snapshot(self.destination))
        self.assertEqual((self.destination / "LICENSE").read_text(), "Fixture license\n")
        self.assertFalse((self.destination.parent.parent / "skill-backups").exists())

    def test_update_requires_flag_and_keeps_backup_outside_discovery(self):
        install_skill.install(self.repository, self.destination)
        before = self.snapshot(self.destination)
        (self.repository / "VERSION").write_text("0.2.0\n", encoding="utf-8")
        (self.source / "assets" / "MEMORY.md").write_text("# Updated workflow\n", encoding="utf-8")
        with self.assertRaises(package_skill.DistributionError):
            install_skill.install(self.repository, self.destination)
        self.assertEqual(before, self.snapshot(self.destination))
        result = install_skill.install(self.repository, self.destination, update=True)
        backup = Path(result["backup"])
        self.assertEqual(result["action"], "updated")
        self.assertEqual(before, self.snapshot(backup))
        self.assertNotEqual(backup.parent, self.destination.parent)
        self.assertNotIn(self.destination.parent, backup.parents)
        self.assertEqual((self.destination / "VERSION").read_text(), "0.2.0\n")

    def test_local_edits_extra_files_and_missing_files_are_preserved(self):
        for change in ("edited", "added", "missing"):
            with self.subTest(change=change):
                destination = self.base / change / "skills" / "memory-loom"
                install_skill.install(self.repository, destination)
                if change == "edited":
                    (destination / "SKILL.md").write_text("User-customized skill", encoding="utf-8")
                elif change == "added":
                    (destination / "notes.md").write_text("User notes", encoding="utf-8")
                else:
                    (destination / "assets" / "MEMORY.md").unlink()
                before = self.snapshot(destination)
                with self.assertRaises(package_skill.DistributionError):
                    install_skill.install(self.repository, destination, update=True)
                self.assertEqual(before, self.snapshot(destination))

    def test_unknown_and_symlink_targets_are_not_overwritten(self):
        self.destination.mkdir(parents=True)
        (self.destination / "SKILL.md").write_text("Unknown content", encoding="utf-8")
        before = self.snapshot(self.destination)
        with self.assertRaises(package_skill.DistributionError):
            install_skill.install(self.repository, self.destination, update=True)
        self.assertEqual(before, self.snapshot(self.destination))
        link = self.destination.parent / "linked"
        link.symlink_to(self.destination, target_is_directory=True)
        with self.assertRaises(package_skill.DistributionError):
            install_skill.install(self.repository, link, update=True)
        self.assertEqual(before, self.snapshot(self.destination))

    def test_managed_symlinks_and_corrupt_manifest_are_rejected(self):
        install_skill.install(self.repository, self.destination)
        path = self.destination / "assets" / "MEMORY.md"
        path.unlink()
        path.symlink_to(self.source / "assets" / "MEMORY.md")
        with self.assertRaises(package_skill.DistributionError):
            install_skill.install(self.repository, self.destination, update=True)
        path.unlink()
        path.write_text("restored", encoding="utf-8")
        (self.destination / install_skill.MANIFEST).write_text("broken JSON", encoding="utf-8")
        with self.assertRaises(package_skill.DistributionError):
            install_skill.install(self.repository, self.destination, update=True)

    def test_runtime_caches_do_not_count_as_customizations(self):
        install_skill.install(self.repository, self.destination)
        cache = self.destination / "scripts" / "__pycache__"
        cache.mkdir()
        (cache / "memory.cpython-39.pyc").write_bytes(b"generated cache")
        self.assertEqual(install_skill.install(self.repository, self.destination)["action"], "unchanged")

    def test_live_install_lock_is_preserved(self):
        self.destination.parent.mkdir(parents=True)
        lock = self.destination.parent / ".memory-loom-install.lock"
        lock.write_text("another installer", encoding="utf-8")
        with self.assertRaises(package_skill.DistributionError):
            install_skill.install(self.repository, self.destination)
        self.assertEqual(lock.read_text(), "another installer")
        self.assertFalse(self.destination.exists())

    def test_cli_installs_only_to_explicit_temporary_target(self):
        result = subprocess.run([sys.executable, str(TOOLS / "install_skill.py"), "--destination", str(self.destination)], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(json.loads(result.stdout)["ok"])
        self.assertEqual(json.loads(result.stdout)["destination"], str(self.destination))


if __name__ == "__main__":
    unittest.main()
