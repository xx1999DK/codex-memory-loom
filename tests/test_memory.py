"""Behavioral tests use isolated projects and never write to the user's projects."""

import importlib.util
import json
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest
from unittest import mock
import uuid


SCRIPT = Path(__file__).resolve().parents[1] / "memory-loom" / "scripts" / "memory.py"
spec = importlib.util.spec_from_file_location("project_memory", SCRIPT)
memory = importlib.util.module_from_spec(spec)
spec.loader.exec_module(memory)


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="project-memory-test-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / "项目 A"
        self.root.mkdir()

    def init(self, root=None, tasks=None):
        return memory.init_project(root or self.root, tasks_dir=tasks)

    def new(self, root=None, title="修复登录", summary="定位并修复登录故障"):
        return memory.create_task(root or self.root, title, summary, ["登录", "auth"])

    def config(self, root=None):
        return memory.load_config(root or self.root)

    def index(self):
        return self.root / ".project-memory" / "INDEX.md"

    def edit_meta(self, path, **changes):
        text = path.read_text(encoding="utf-8")
        meta = memory.parse_frontmatter(text, path)
        meta.update(changes)
        body = text.split("---", 2)[2]
        header = "---\n" + "\n".join(key + ": " + json.dumps(value, ensure_ascii=False) for key, value in meta.items()) + "\n---"
        path.write_text(header + body, encoding="utf-8")

    def test_init_idempotent_preserves_user_content_and_identity(self):
        original = "# Existing project\nDo not remove this.\n"
        agents = self.root / "AGENTS.md"
        agents.write_text(original, encoding="utf-8")
        first = self.init()
        workflow = self.root / ".project-memory" / "MEMORY.md"
        workflow.write_text(workflow.read_text(encoding="utf-8") + "\n项目自定义规则\n", encoding="utf-8")
        snapshot = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        second = self.init()
        self.assertFalse(second["initialized"])
        self.assertEqual(first["project_id"], second["project_id"])
        self.assertEqual(snapshot, {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*") if p.is_file()})
        self.assertTrue(agents.read_text(encoding="utf-8").startswith(original))
        self.assertEqual(agents.read_text(encoding="utf-8").count(memory.ENTRY_START), 1)
        self.assertTrue(memory.check_project(self.root)["ok"])

    def test_override_is_effective_and_regular_file_untouched(self):
        regular = self.root / "AGENTS.md"
        regular.write_text("# Keep me\n", encoding="utf-8")
        override = self.root / "AGENTS.override.md"
        override.write_text("# Actual instructions\n", encoding="utf-8")
        result = self.init()
        self.assertEqual(result["entrypoint"], str(override))
        self.assertEqual(regular.read_text(encoding="utf-8"), "# Keep me\n")
        self.assertTrue(memory.check_project(self.root)["ok"])

    def test_project_isolation_and_foreign_task_detection(self):
        other = self.base / "项目 B"
        other.mkdir()
        self.init()
        self.init(other)
        a = self.new()
        b = self.new(other)
        self.assertNotEqual(self.config()["project_id"], self.config(other)["project_id"])
        self.assertNotIn(b["task_id"], self.index().read_text(encoding="utf-8"))
        copied = self.root / self.config()["tasks_dir"] / "foreign.md"
        copied.write_bytes(Path(b["task"]).read_bytes())
        result = memory.check_project(self.root)
        self.assertFalse(result["ok"])
        self.assertTrue(any("不属于当前项目" in e for e in result["errors"]))
        self.assertTrue(Path(a["task"]).is_file())

    def test_same_title_creates_distinct_files_and_manual_graph_survives(self):
        self.init()
        original = self.index().read_text(encoding="utf-8") + "\n## 手工知识关系\n登录 → 会话状态\n"
        self.index().write_text(original, encoding="utf-8")
        first = self.new()
        first_bytes = Path(first["task"]).read_bytes()
        second = self.new()
        self.assertNotEqual(first["task"], second["task"])
        self.assertEqual(first_bytes, Path(first["task"]).read_bytes())
        index = self.index().read_text(encoding="utf-8")
        self.assertIn("登录 → 会话状态", index)
        self.assertIn(first["task_id"], index)
        self.assertIn(second["task_id"], index)
        self.assertTrue(memory.check_project(self.root)["ok"])

    def test_existing_records_are_preserved_and_reported(self):
        records = self.root / "工作记录"
        records.mkdir()
        legacy = records / "2025-01-01-旧任务.md"
        content = "# 旧任务\n用户记录，不可覆盖。\n"
        legacy.write_text(content, encoding="utf-8")
        self.init(tasks="工作记录")
        task = self.new()
        self.assertEqual(Path(task["task"]).parent, records)
        self.assertEqual(legacy.read_text(encoding="utf-8"), content)
        result = memory.check_project(self.root)
        self.assertTrue(result["ok"])
        self.assertTrue(result["warnings"])
        self.assertIn("../", self.index().read_text(encoding="utf-8"))

    def test_legacy_yaml_is_preserved_without_blocking_protocol_tasks(self):
        records = self.root / "工作记录"
        records.mkdir()
        legacy = records / "2025-01-01-旧任务.md"
        original = "---\ntitle: 旧记录\ndate: 2025-01-01\ntags:\n  - 登录\ndescription: |\n  task_id: 这是旧文档中的说明，不是顶层协议字段\n---\n# 历史结论\n保持原文。\n"
        legacy.write_text(original, encoding="utf-8")
        self.init(tasks="工作记录")
        task = self.new()
        result = memory.check_project(self.root)
        self.assertTrue(result["ok"])
        self.assertEqual(result["tasks"], 1)
        self.assertTrue(any("旧任务.md" in warning for warning in result["warnings"]))
        self.assertEqual(legacy.read_text(encoding="utf-8"), original)
        self.assertIn(task["task_id"], self.index().read_text(encoding="utf-8"))

    def test_damaged_protocol_metadata_is_not_misclassified_as_legacy(self):
        self.init()
        tasks = self.root / self.config()["tasks_dir"]
        broken = tasks / "broken.md"
        for content in ("---\ntask_id: unquoted-value\ntitle: 旧记录\n---\n", '---\nproject_id: "%s"\n' % self.config()["project_id"], '---\nproject_id: "broken"\nupdated: "broken"\nkeywords: []\n---\n'):
            with self.subTest(content=content):
                broken.write_text(content, encoding="utf-8")
                self.assertFalse(memory.check_project(self.root)["ok"])
                with self.assertRaises(memory.MemoryError):
                    self.new()
                self.assertEqual(list(tasks.iterdir()), [broken])

    def test_legacy_business_project_id_does_not_claim_protocol_ownership(self):
        records = self.root / "工作记录"
        records.mkdir()
        legacy = records / "ERP.md"
        original = '---\nproject_id: ERP\ntitle: 既有工作记录\n---\n既有业务项目。\n'
        legacy.write_text(original, encoding="utf-8")
        self.init(tasks="工作记录")
        self.new()
        self.assertTrue(memory.check_project(self.root)["ok"])
        self.assertEqual(legacy.read_text(), original)
        self.assertTrue(memory.search_tasks(self.root, "ERP")["results"][0]["legacy"])

    def test_path_traversal_and_symlinked_tasks_cannot_escape(self):
        outside = self.base / "outside"
        outside.mkdir()
        sentinel = outside / "keep.txt"
        sentinel.write_text("unchanged", encoding="utf-8")
        for invalid in ("../outside", str(outside), ".", ".project-memory"):
            with self.subTest(path=invalid), self.assertRaises(memory.MemoryError):
                self.init(tasks=invalid)
        (self.root / "escape").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(memory.MemoryError):
            self.init(tasks="escape/tasks")
        self.assertEqual(list(outside.iterdir()), [sentinel])
        self.assertFalse((self.root / ".project-memory").exists())

    def test_symlinked_memory_agents_and_task_are_rejected(self):
        outside = self.base / "outside"
        outside.mkdir()
        for relative, directory in ((".project-memory", True), ("AGENTS.md", False)):
            target = outside if directory else outside / "agents.txt"
            if not directory:
                target.write_text("preserve", encoding="utf-8")
            link = self.root / relative
            link.symlink_to(target, target_is_directory=directory)
            with self.subTest(path=relative), self.assertRaises(memory.MemoryError):
                self.init()
            link.unlink()
        self.init()
        task = self.new()
        original = Path(task["task"])
        copied = outside / "copied.md"
        copied.write_bytes(original.read_bytes())
        original.unlink()
        original.symlink_to(copied)
        self.assertFalse(memory.check_project(self.root)["ok"])

    def test_stale_index_and_invalid_metadata_are_detected_without_mutation(self):
        self.init()
        task = self.new()
        path = Path(task["task"])
        self.edit_meta(path, status="已完成", summary="已验证修复")
        snapshot = self.index().read_bytes()
        self.assertFalse(memory.check_project(self.root)["ok"])
        self.assertEqual(snapshot, self.index().read_bytes())
        memory.rebuild_index(self.root, self.config())
        self.assertTrue(memory.check_project(self.root)["ok"])
        self.edit_meta(path, status="Imaginary")
        self.assertFalse(memory.check_project(self.root)["ok"])
        before = self.index().read_bytes()
        with self.assertRaises(memory.MemoryError):
            memory.rebuild_index(self.root, self.config())
        self.assertEqual(before, self.index().read_bytes())

    def test_invalid_configuration_not_reinitialized(self):
        self.init()
        path = self.root / ".project-memory" / "config.json"
        for content in ("broken JSON", json.dumps({"version": 999})):
            path.write_text(content, encoding="utf-8")
            with self.subTest(content=content), self.assertRaises(memory.MemoryError):
                self.init()
            self.assertEqual(path.read_text(encoding="utf-8"), content)

    def test_active_lock_prevents_mutation_and_is_not_removed(self):
        lock = self.root / ".project-memory.lock"
        lock.write_text("another writer", encoding="utf-8")
        with self.assertRaises(memory.MemoryError):
            self.init()
        self.assertEqual(lock.read_text(encoding="utf-8"), "another writer")
        self.assertFalse((self.root / ".project-memory").exists())

    def test_duplicate_id_and_backwards_timestamps_detected(self):
        self.init()
        first = self.new()
        second = self.new(title="构建部署")
        self.edit_meta(Path(second["task"]), task_id=first["task_id"])
        self.assertFalse(memory.check_project(self.root)["ok"])
        self.edit_meta(Path(second["task"]), task_id="task-" + uuid.uuid4().hex[:12], updated="2000-01-01T00:00:00+08:00")
        self.assertFalse(memory.check_project(self.root)["ok"])

    def test_damaged_index_prevents_new_task_creation(self):
        self.init()
        self.index().write_text("User content without machine markers", encoding="utf-8")
        tasks = self.root / self.config()["tasks_dir"]
        with self.assertRaises(memory.MemoryError):
            self.new()
        self.assertEqual(list(tasks.iterdir()), [])
        self.assertEqual(self.index().read_text(encoding="utf-8"), "User content without machine markers")

    def test_init_does_not_search_or_change_parent(self):
        self.init()
        nested = self.root / "独立子项目"
        nested.mkdir()
        parent_snapshot = self.index().read_bytes()
        self.init(nested)
        self.new(nested)
        self.assertNotEqual(self.config()["project_id"], self.config(nested)["project_id"])
        self.assertEqual(parent_snapshot, self.index().read_bytes())

    def test_tasks_directory_cannot_cross_initialized_child_project(self):
        child = self.root / "独立子项目"
        child.mkdir()
        self.init(child)
        self.new(child)
        snapshot = {p.relative_to(child): p.read_bytes() for p in child.rglob("*") if p.is_file()}
        for directory in ("独立子项目", "独立子项目/工作记录", "独立子项目/.project-memory/tasks"):
            with self.subTest(directory=directory), self.assertRaises(memory.MemoryError):
                self.init(tasks=directory)
        self.assertFalse((self.root / ".project-memory").exists())
        self.assertEqual(snapshot, {p.relative_to(child): p.read_bytes() for p in child.rglob("*") if p.is_file()})
        # Normal default storage remains valid beside a nested project.
        self.init()
        self.new()
        self.assertTrue(memory.check_project(self.root)["ok"])
        config_path = self.root / ".project-memory" / "config.json"
        config = self.config()
        config["tasks_dir"] = "独立子项目/.project-memory/tasks"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        self.assertFalse(memory.check_project(self.root)["ok"])
        with self.assertRaises(memory.MemoryError):
            self.new()

    def test_cli_runs_and_reports_failure_as_json(self):
        for command in ("init", "check"):
            result = subprocess.run([sys.executable, str(SCRIPT), command, "--root", str(self.root)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertTrue(json.loads(result.stdout)["ok"])
        result = subprocess.run([sys.executable, str(SCRIPT), "init", "--root", "relative/path"], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(json.loads(result.stdout)["ok"])

    def test_init_preflights_existing_damaged_records_before_writing(self):
        records = self.root / "工作记录"
        records.mkdir()
        broken = records / "broken.md"
        broken.write_text('---\ntask_id: "broken"\n---\n', encoding="utf-8")
        before = broken.read_bytes()
        with self.assertRaises(memory.MemoryError):
            self.init(tasks="工作记录")
        self.assertEqual(broken.read_bytes(), before)
        self.assertFalse((self.root / ".project-memory").exists())
        self.assertFalse((self.root / "AGENTS.md").exists())
        self.assertFalse((self.root / ".project-memory.lock").exists())

    def test_init_write_failure_rolls_back_only_new_files_and_directories(self):
        records = self.root / "工作记录"
        records.mkdir()
        legacy = records / "old.md"
        legacy.write_text("# Preserved original", encoding="utf-8")
        agents = self.root / "AGENTS.md"
        agents.write_text("# Original instructions", encoding="utf-8")
        writer = memory.atomic_write

        def fail_entry(root, path, content):
            if path == agents:
                raise OSError("simulated disk write error")
            return writer(root, path, content)

        with mock.patch.object(memory, "atomic_write", side_effect=fail_entry), self.assertRaises(OSError):
            self.init(tasks="工作记录")
        self.assertEqual(legacy.read_text(), "# Preserved original")
        self.assertEqual(agents.read_text(), "# Original instructions")
        self.assertFalse((self.root / ".project-memory").exists())
        self.assertFalse((self.root / ".project-memory.lock").exists())
        self.assertTrue(self.init(tasks="工作记录")["ok"])

    def test_init_failure_removes_new_nested_task_directory(self):
        writer = memory.atomic_write

        def fail_config(root, path, content):
            if path.name == "config.json":
                raise OSError("simulated config error")
            return writer(root, path, content)

        with mock.patch.object(memory, "atomic_write", side_effect=fail_config), self.assertRaises(OSError):
            self.init(tasks="notes/archive/tasks")
        self.assertEqual(list(self.root.iterdir()), [])

    def test_empty_memory_file_is_detected_without_overwrite(self):
        self.init()
        workflow = self.root / ".project-memory" / "MEMORY.md"
        workflow.write_text("", encoding="utf-8")
        self.assertFalse(memory.check_project(self.root)["ok"])
        with self.assertRaises(memory.MemoryError):
            self.init()
        self.assertEqual(workflow.read_bytes(), b"")

    def test_index_orders_timestamps_by_instant_across_timezones(self):
        self.init()
        earlier = self.new(title="Earlier")
        later = self.new(title="Later")
        self.edit_meta(Path(earlier["task"]), created="2026-01-01T00:00:00+00:00", updated="2026-10-27T10:00:00+08:00")
        self.edit_meta(Path(later["task"]), created="2026-01-01T00:00:00Z", updated="2026-10-27T03:00:00Z")
        memory.rebuild_index(self.root, self.config())
        index = self.index().read_text()
        self.assertLess(index.index(later["task_id"]), index.index(earlier["task_id"]))

    def test_search_is_bounded_case_insensitive_and_read_only(self):
        self.init()
        for index in range(7):
            self.new(title="CSV 导出 %d" % index, summary="排查中文乱码")
        self.new(title="无关任务", summary="不应被匹配")
        snapshot = {p.relative_to(self.root): (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.rglob("*") if p.is_file()}
        result = memory.search_tasks(self.root, "csv 中文")
        self.assertTrue(result["ok"])
        self.assertEqual(result["total_matches"], 7)
        self.assertEqual(len(result["results"]), 5)
        self.assertEqual(len(memory.search_tasks(self.root, "csv 中文", limit=2)["results"]), 2)
        self.assertTrue(all("CSV" in item["title"] and not item["legacy"] for item in result["results"]))
        self.assertEqual(snapshot, {p.relative_to(self.root): (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.rglob("*") if p.is_file()})
        self.assertFalse((self.root / ".project-memory.lock").exists())

    def test_search_only_uses_body_when_explicit_and_does_not_output_body(self):
        self.init()
        task = self.new()
        path = Path(task["task"])
        path.write_text(path.read_text() + "\n内部细节 body-only-token，不应该输出全文。\n", encoding="utf-8")
        self.assertEqual(memory.search_tasks(self.root, "body-only-token")["results"], [])
        result = memory.search_tasks(self.root, "body-only-token", include_body=True)
        self.assertEqual(result["results"][0]["task_id"], task["task_id"])
        self.assertNotIn("内部细节", json.dumps(result, ensure_ascii=False))
        # A non-UTF-8 body does not affect metadata-only retrieval.
        path.write_bytes(path.read_bytes() + b"\n\xff\xfe")
        self.assertTrue(memory.search_tasks(self.root, "登录")["ok"])
        self.assertFalse(memory.search_tasks(self.root, "登录", include_body=True)["ok"])

    def test_search_legacy_filename_and_optional_body_are_supported(self):
        self.init()
        path = self.root / self.config()["tasks_dir"] / "旧版-CSV导出.md"
        original = "---\ntitle: 旧记录\n---\n只在正文出现的线索。\n"
        path.write_text(original, encoding="utf-8")
        result = memory.search_tasks(self.root, "csv")
        self.assertTrue(result["results"][0]["legacy"])
        self.assertIsNone(result["results"][0]["task_id"])
        self.assertTrue(result["warnings"])
        self.assertEqual(memory.search_tasks(self.root, "线索")["results"], [])
        self.assertEqual(len(memory.search_tasks(self.root, "线索", include_body=True)["results"]), 1)
        self.assertEqual(path.read_text(), original)

    def test_search_body_supports_crlf_records(self):
        self.init()
        task = self.new()
        path = Path(task["task"])
        header = memory.read_task_header(path)
        path.write_bytes((header + "first-body-marker\n").replace("\n", "\r\n").encode("utf-8"))
        result = memory.search_tasks(self.root, "first-body-marker", include_body=True)
        self.assertTrue(result["ok"])
        self.assertEqual(result["results"][0]["task_id"], task["task_id"])

    def test_search_never_traverses_peers_or_accepts_foreign_tasks(self):
        other = self.base / "ProjectB"
        other.mkdir()
        self.init()
        self.init(other)
        foreign = self.new(other, title="Foreign sentinel")
        self.assertEqual(memory.search_tasks(self.root, "sentinel", include_body=True)["results"], [])
        link = self.root / self.config()["tasks_dir"] / "foreign.md"
        link.symlink_to(Path(foreign["task"]))
        self.assertFalse(memory.search_tasks(self.root, "sentinel")["ok"])
        link.unlink()
        link.write_bytes(Path(foreign["task"]).read_bytes())
        result = memory.search_tasks(self.root, "sentinel")
        self.assertFalse(result["ok"])
        self.assertEqual(result["results"], [])

    def test_search_validates_input_and_cli_search_works(self):
        self.init()
        task = self.new()
        for limit in (0, -1, 51):
            with self.subTest(limit=limit), self.assertRaises(memory.MemoryError):
                memory.search_tasks(self.root, "登录", limit=limit)
        with self.assertRaises(memory.MemoryError):
            memory.search_tasks(self.root, "  ")
        result = subprocess.run([sys.executable, str(SCRIPT), "search", "--root", str(self.root), "--query", "AUTH 登录", "--limit", "1"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(json.loads(result.stdout)["results"][0]["task_id"], task["task_id"])


if __name__ == "__main__":
    unittest.main()
