#!/usr/bin/env python3
"""Project-local Markdown memory helpers. Python 3.9+, standard library only."""

import argparse
import contextlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
from datetime import datetime
from urllib.parse import quote
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


ASSETS = Path(__file__).resolve().parent.parent / "assets"
MEMORY_DIR = ".project-memory"
ENTRY_START = "<!-- project-memory:start -->"
ENTRY_END = "<!-- project-memory:end -->"
TASKS_START = "<!-- project-memory:tasks:start -->"
TASKS_END = "<!-- project-memory:tasks:end -->"
CORE_FILES = ("MEMORY.md", "INDEX.md", "DIRECTION.md")
STATUSES = {"进行中", "已完成", "待继续", "受阻", "已取消"}
FIELDS = {"task_id", "project_id", "title", "created", "updated", "status", "summary", "keywords", "related"}


class MemoryError(Exception):
    pass


def project_root(value):
    root = Path(value).expanduser()
    if not root.is_absolute():
        raise MemoryError("--root 必须是明确的项目绝对路径")
    if root.is_symlink():
        raise MemoryError("项目根不能是符号链接，请提供实际项目目录")
    if not root.is_dir():
        raise MemoryError("项目根目录不存在：%s" % root)
    return root.resolve()


def safe_path(root, relative):
    """Reject traversal and symlinks in every managed component under root."""
    rel = Path(relative)
    if rel.is_absolute() or not rel.parts or ".." in rel.parts:
        raise MemoryError("必须使用项目内相对路径，不能包含 ..：%s" % relative)
    path = root
    for part in rel.parts:
        path = path / part
        if path.is_symlink():
            raise MemoryError("受管理路径不能是符号链接：%s" % path)
    return path


def read_text(path):
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise MemoryError("无法读取 %s：%s" % (path, exc))


def atomic_write(root, path, text):
    safe_path(root, path.relative_to(root))
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644
    fd, temporary = tempfile.mkstemp(prefix=".memory-write-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        safe_path(root, path.relative_to(root))
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextlib.contextmanager
def project_lock(root):
    path = safe_path(root, ".project-memory.lock")
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise MemoryError("项目记忆正被写入或有遗留锁：%s；先确认其他写入已结束，不要强制删除活动锁" % path)
    owned = os.fstat(fd)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(json.dumps({"pid": os.getpid(), "created": datetime.now().astimezone().isoformat()}, ensure_ascii=False))
        yield
    finally:
        try:
            current = path.lstat()
            if current.st_ino == owned.st_ino and current.st_dev == owned.st_dev:
                path.unlink()
        except FileNotFoundError:
            pass


def timezone(name):
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        raise MemoryError("无法识别时区：%s（系统需安装 IANA 时区数据）" % name)


def timestamp(config):
    return datetime.now(timezone(config["timezone"])).isoformat(timespec="seconds")


def valid_timestamp(value, label):
    if not isinstance(value, str):
        raise MemoryError("%s 必须是带时区的 ISO 8601 时间" % label)
    try:
        result = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
        if result.utcoffset() is None:
            raise ValueError("missing timezone")
        return result
    except (ValueError, TypeError):
        raise MemoryError("%s 必须是带时区的 ISO 8601 时间" % label)


def validate_config(root, config):
    if not isinstance(config, dict) or type(config.get("version")) is not int or config.get("version") != 1:
        raise MemoryError("记忆配置格式或版本不受支持，未修改现有内容")
    try:
        uuid.UUID(config["project_id"])
        timezone(config["timezone"])
        valid_timestamp(config["created"], "config.created")
        tasks = safe_path(root, config["tasks_dir"])
    except (KeyError, ValueError, TypeError, AttributeError) as exc:
        raise MemoryError("记忆配置缺失或字段无效：%s" % exc)
    if tasks in (root, root / MEMORY_DIR):
        raise MemoryError("任务目录必须是独立的子目录")
    if tasks.exists() and not tasks.is_dir():
        raise MemoryError("任务目录路径不是目录：%s" % tasks)
    # An explicit relative path may still cross into a separately initialized
    # child project. Its own marker establishes a boundary, even if damaged or
    # copied with the same UUID; do not read that configuration or its tasks.
    cursor = root
    for part in tasks.relative_to(root).parts:
        cursor = cursor / part
        boundary = safe_path(root, cursor.relative_to(root) / MEMORY_DIR / "config.json")
        if boundary.exists():
            raise MemoryError("任务目录跨入已独立初始化的子项目：%s" % cursor)
    return config


def load_config(root):
    path = safe_path(root, MEMORY_DIR + "/config.json")
    if not path.is_file():
        raise MemoryError("当前项目尚未初始化，或配置已损坏：%s" % path)
    try:
        config = json.loads(read_text(path))
    except json.JSONDecodeError as exc:
        raise MemoryError("记忆配置不是有效 JSON：%s" % exc)
    return validate_config(root, config)


def entry_path(root):
    override = safe_path(root, "AGENTS.override.md")
    regular = safe_path(root, "AGENTS.md")
    if override.exists() and read_text(override).strip():
        return override
    return regular


def entry_block():
    return """<!-- project-memory:start -->
## 项目长记忆

本项目已启用文件记忆。首次处理本项目任务或上下文丢失时，读取 `.project-memory/MEMORY.md` 的工作流；历史档案按需检索，不默认全量加载。记忆边界是此入口所在的项目根；不得把父目录、兄弟项目或独立嵌套项目的记忆混入当前项目。新目标建立任务档案，同目标续写原档案；任务结束或交接前同步档案、导航与有变化的方向总结。配置在 `.project-memory/config.json`，导航在 `.project-memory/INDEX.md`。
<!-- project-memory:end -->"""


def marked_region(text, start, end, label):
    if text.count(start) != 1 or text.count(end) != 1:
        raise MemoryError("%s 的管理标记缺失或重复，未覆盖文件" % label)
    left, right = text.index(start) + len(start), text.index(end)
    if right < left:
        raise MemoryError("%s 的管理标记顺序错误" % label)
    return left, right


def check_structure(root, config):
    for name in CORE_FILES:
        path = safe_path(root, MEMORY_DIR + "/" + name)
        if not path.is_file():
            raise MemoryError("记忆结构缺失：%s；保留现有数据，请人工修复" % name)
        if not read_text(path).strip():
            raise MemoryError("记忆文件为空：%s；保留现有数据，请人工修复" % name)
    if not safe_path(root, config["tasks_dir"]).is_dir():
        raise MemoryError("配置的任务目录不存在")


def init_project(root, tasks_dir=None, tz=None):
    with project_lock(root):
        memory = safe_path(root, MEMORY_DIR)
        if memory.exists():
            config = load_config(root)
            if tasks_dir is not None and Path(tasks_dir).as_posix() != config["tasks_dir"]:
                raise MemoryError("项目已初始化，不能通过 init 改变任务目录")
            if tz is not None and tz != config["timezone"]:
                raise MemoryError("项目已初始化，不能通过 init 改变时区")
            check_structure(root, config)
            index_parts(root)
            initialized = False
        else:
            config = {
                "version": 1,
                "project_id": str(uuid.uuid4()),
                "timezone": tz or "Asia/Shanghai",
                "tasks_dir": Path(tasks_dir or MEMORY_DIR + "/tasks").as_posix(),
            }
            config["created"] = timestamp(config)
            validate_config(root, config)
            templates = {name: read_text(ASSETS / name) for name in CORE_FILES}
            if any(not content.strip() for content in templates.values()):
                raise MemoryError("记忆模板为空，未创建项目记忆")
            marked_region(templates["INDEX.md"], TASKS_START, TASKS_END, "INDEX.md 模板")
            # Validate existing records before the first write. In particular,
            # foreign or damaged protocol records must not leave a half-init.
            task_path = safe_path(root, config["tasks_dir"])
            tasks, warnings, errors = read_tasks(root, config) if task_path.exists() else ([], [], [])
            if errors:
                raise MemoryError("现有任务档案需要先修复：" + "; ".join(errors))
            left, right = marked_region(templates["INDEX.md"], TASKS_START, TASKS_END, "INDEX.md 模板")
            templates["INDEX.md"] = templates["INDEX.md"][:left] + task_table(root, tasks) + templates["INDEX.md"][right:]
            initialized = True
        agents = entry_path(root)
        previous = read_text(agents) if agents.exists() else ""
        if ENTRY_START in previous or ENTRY_END in previous:
            marked_region(previous, ENTRY_START, ENTRY_END, agents.name)
            new_agents = previous
        else:
            new_agents = previous + ("\n\n" if previous and not previous.endswith("\n\n") else "") + entry_block() + "\n"
        if initialized:
            created_files, created_dirs = [], []
            try:
                for directory in (memory, safe_path(root, config["tasks_dir"])):
                    missing = []
                    cursor = directory
                    while not cursor.exists():
                        missing.append(cursor)
                        cursor = cursor.parent
                    for item in reversed(missing):
                        safe_path(root, item.relative_to(root)).mkdir()
                        created_dirs.append(item)
                files = dict(templates)
                files["config.json"] = json.dumps(config, ensure_ascii=False, indent=2) + "\n"
                for name, content in files.items():
                    path = memory / name
                    atomic_write(root, path, content)
                    created_files.append((path, content))
                if new_agents != previous:
                    atomic_write(root, agents, new_agents)
            except (MemoryError, OSError, ValueError, TypeError):
                # Roll back only files this call created and which remain
                # unchanged; preserve old records and any concurrent additions.
                for path, content in reversed(created_files):
                    try:
                        if not path.is_symlink() and read_text(path) == content:
                            path.unlink()
                    except (MemoryError, OSError):
                        pass
                for directory in reversed(created_dirs):
                    try:
                        directory.rmdir()  # Never removes nonempty user data.
                    except OSError:
                        pass
                raise
        elif new_agents != previous:
            atomic_write(root, agents, new_agents)
        return {"ok": True, "initialized": initialized, "root": str(root), "project_id": config["project_id"], "tasks_dir": config["tasks_dir"], "entrypoint": str(agents), "memory_dir": str(memory)}


def parse_frontmatter(text, path):
    if not text.startswith("---\n") and not text.startswith("---\r\n"):
        return None
    lines = text.splitlines()
    try:
        end = lines.index("---", 1)
    except ValueError:
        raise MemoryError("%s：frontmatter 未闭合" % path.name)
    metadata = {}
    for line in lines[1:end]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.partition(":")
        key = key.strip()
        if not separator or not key or key in metadata:
            raise MemoryError("%s：frontmatter 字段无效或重复" % path.name)
        try:
            metadata[key] = json.loads(value.strip())
        except json.JSONDecodeError:
            raise MemoryError("%s：%s 必须使用 JSON 编码的 YAML 值（字符串加双引号，列表用 []）" % (path.name, key))
    return metadata


def has_protocol_frontmatter(text):
    """Recognize our ownership fields before interpreting arbitrary old YAML."""
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        return False
    fields = {}
    for line in lines[1:]:
        if line == "---":
            break
        match = re.match(r'''^(?:([a-z_]+)|["']([a-z_]+)["'])\s*:(.*)''', line)
        if match:
            fields[match.group(1) or match.group(2)] = match.group(3).strip()
    if "task_id" in fields:
        return True
    if "project_id" not in fields:
        return False
    # Ordinary old YAML often has a business project_id. A protocol UUID or
    # multiple protocol-specific fields identifies damaged records with a lost
    # task_id, while an unrelated project_id alone remains a legacy document.
    try:
        uuid.UUID(json.loads(fields["project_id"]))
        return True
    except (ValueError, TypeError, AttributeError):
        return len({"updated", "summary", "keywords", "related"} & fields.keys()) >= 2


def validate_task(meta, path, config):
    if not isinstance(meta, dict):
        raise MemoryError("%s：任务 frontmatter 无效" % path.name)
    missing = FIELDS - meta.keys()
    if missing:
        raise MemoryError("%s：任务缺少字段 %s" % (path.name, ", ".join(sorted(missing))))
    for key in ("task_id", "project_id", "title", "summary"):
        if not isinstance(meta[key], str) or not meta[key].strip():
            raise MemoryError("%s：%s 必须是非空字符串" % (path.name, key))
    if meta["project_id"] != config["project_id"]:
        raise MemoryError("%s：project_id 不属于当前项目，拒绝混合项目记忆" % path.name)
    if not isinstance(meta["status"], str) or meta["status"] not in STATUSES:
        raise MemoryError("%s：未知任务状态" % path.name)
    for key in ("keywords", "related"):
        if not isinstance(meta[key], list) or any(not isinstance(v, str) for v in meta[key]):
            raise MemoryError("%s：%s 必须是字符串列表" % (path.name, key))
    created = valid_timestamp(meta["created"], path.name + ".created")
    updated = valid_timestamp(meta["updated"], path.name + ".updated")
    if updated < created:
        raise MemoryError("%s：updated 早于 created" % path.name)


def read_tasks(root, config):
    task_dir = safe_path(root, config["tasks_dir"])
    tasks, warnings, errors, seen = [], [], [], set()
    # Only direct task Markdown files: attachments, nested projects and other trees
    # are never traversed. Historical records can remain linked manually.
    for candidate in sorted(task_dir.iterdir()):
        if candidate.suffix.lower() != ".md":
            continue
        try:
            path = safe_path(root, candidate.relative_to(root))
            if not path.is_file():
                continue
            content = read_text(path)
            if not has_protocol_frontmatter(content):
                warnings.append("旧档案未自动索引，请按需人工链接：%s" % path.relative_to(root))
                continue
            metadata = parse_frontmatter(content, path)
            validate_task(metadata, path, config)
            if metadata["task_id"] in seen:
                raise MemoryError("重复 task_id：%s" % metadata["task_id"])
            seen.add(metadata["task_id"])
            tasks.append((path, metadata))
        except (MemoryError, TypeError) as exc:
            errors.append(str(exc))
    return tasks, warnings, errors


def table_cell(value):
    return str(value).replace("\r", " ").replace("\n", " ").replace("|", "\\|").replace("[", "\\[").replace("]", "\\]").replace("<", "&lt;").replace(">", "&gt;")


def task_table(root, tasks):
    lines = ["", "| 任务 | 状态 | 更新 | 摘要 | 关键词 | 关联任务 |", "| --- | --- | --- | --- | --- | --- |"]
    for path, meta in sorted(tasks, key=lambda item: (valid_timestamp(item[1]["updated"], "updated"), item[1]["task_id"]), reverse=True):
        relative = os.path.relpath(str(path), str(root / MEMORY_DIR))
        link = quote(Path(relative).as_posix(), safe="/.-_")
        lines.append("| [%s](%s) · `%s` | %s | %s | %s | %s | %s |" % (
            table_cell(meta["title"]), link, table_cell(meta["task_id"]), table_cell(meta["status"]),
            meta["updated"], table_cell(meta["summary"]), table_cell("、".join(meta["keywords"])), table_cell("、".join(meta["related"])),
        ))
    return "\n".join(lines) + "\n"


def index_parts(root):
    path = safe_path(root, MEMORY_DIR + "/INDEX.md")
    content = read_text(path)
    left, right = marked_region(content, TASKS_START, TASKS_END, "INDEX.md")
    return path, content, left, right


def rebuild_index(root, config):
    check_structure(root, config)
    tasks, warnings, errors = read_tasks(root, config)
    if errors:
        raise MemoryError("任务档案无效，索引未更新：" + "; ".join(errors))
    path, content, left, right = index_parts(root)
    revised = content[:left] + task_table(root, tasks) + content[right:]
    if content != revised:
        atomic_write(root, path, revised)
    return {"ok": True, "tasks": len(tasks), "index": str(path), "warnings": warnings}


def task_document(meta):
    header = "---\n" + "\n".join(key + ": " + json.dumps(value, ensure_ascii=False) for key, value in meta.items()) + "\n---\n"
    return header + """
# %s

## 目标与范围

%s

## 当前交接摘要

- 当前状态：进行中；仅建立记录，尚未实施。
- 已确认事实：待执行后补充；计划不能记为结果。
- 下一步：根据当前用户请求开展工作。

## 执行与关键决策

尚无实施记录。后续记录变更位置、决策原因、替代方案和必要的复现信息。

## 验证与结果

尚未验证。记录实际执行的检查、结果、证据及未覆盖的范围。

## 未完成事项与下一步

任务尚未开始实施。完成或交接前更新此处与顶部摘要。

## 更新记录

- %s：建立任务档案；尚未执行具体工作。
""" % (meta["title"], meta["summary"], meta["created"])


def create_task(root, title, summary, keywords):
    if not title.strip() or not summary.strip():
        raise MemoryError("任务标题和摘要不能为空")
    if any(char in title for char in "\r\n"):
        raise MemoryError("任务标题必须是单行")
    with project_lock(root):
        config = load_config(root)
        check_structure(root, config)
        _, _, errors = read_tasks(root, config)
        if errors:
            raise MemoryError("现有任务档案需要先修复：" + "; ".join(errors))
        index_parts(root)  # Fail before creating a task if navigation is damaged.
        now = timestamp(config)
        ident = uuid.uuid4().hex[:12]
        meta = {"task_id": "task-" + ident, "project_id": config["project_id"], "title": title.strip(), "created": now, "updated": now, "status": "进行中", "summary": summary.strip(), "keywords": keywords or [], "related": []}
        slug = re.sub(r"[^\w\-]+", "-", title, flags=re.UNICODE).strip("-_")[:64] or "task"
        path = safe_path(root, config["tasks_dir"]) / (now[:10] + "-" + slug + "-" + ident + ".md")
        # O_EXCL makes an ID collision an error, never an overwrite.
        with path.open("x", encoding="utf-8") as stream:
            stream.write(task_document(meta))
            stream.flush()
            os.fsync(stream.fileno())
        result = rebuild_index(root, config)
        result.update({"task": str(path), "task_id": meta["task_id"]})
        return result


def read_task_header(path):
    """Read only frontmatter; binary lines avoid decoding a buffered body."""
    try:
        with path.open("rb") as stream:
            first = stream.readline(65537)
            if first.rstrip(b"\r\n") != b"---":
                return ""  # A legacy record needs no content for filename search.
            lines, size = [first], len(first)
            while True:
                line = stream.readline(65537 - size)
                if not line:
                    break
                size += len(line)
                if size > 65536:
                    raise MemoryError("%s：任务元信息超过 64 KiB，需整理后再检索" % path.name)
                lines.append(line)
                if line.rstrip(b"\r\n") == b"---":
                    break
            return b"".join(lines).decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise MemoryError("无法读取 %s 元信息：%s" % (path, exc))


def search_tasks(root, query, limit=5, include_body=False):
    """Bounded, read-only keyword search. All terms must match a record."""
    if not isinstance(query, str) or not query.strip():
        raise MemoryError("检索词不能为空")
    if type(limit) is not int or not 1 <= limit <= 50:
        raise MemoryError("--limit 必须是 1 到 50 之间的整数")
    terms = list(dict.fromkeys(query.casefold().split()))
    config = load_config(root)
    task_dir = safe_path(root, config["tasks_dir"])
    if not task_dir.is_dir():
        raise MemoryError("配置的任务目录不存在")
    matches, errors, seen = [], [], set()
    legacy_count = 0
    for candidate in sorted(task_dir.iterdir()):
        if candidate.suffix.lower() != ".md":
            continue
        try:
            path = safe_path(root, candidate.relative_to(root))
            if not path.is_file():
                continue
            header = read_task_header(path)
            legacy = not has_protocol_frontmatter(header)
            if legacy:
                legacy_count += 1
                meta = {"task_id": None, "title": path.stem, "status": "未标注", "summary": "旧记录，未采用结构化任务元信息。"}
                fields = {"filename": (path.stem.casefold(), 2)}
                updated = float("-inf")
            else:
                meta = parse_frontmatter(header, path)
                validate_task(meta, path, config)
                if meta["task_id"] in seen:
                    raise MemoryError("重复 task_id：%s" % meta["task_id"])
                seen.add(meta["task_id"])
                fields = {
                    "task_id": (meta["task_id"].casefold(), 10),
                    "title": (meta["title"].casefold(), 8),
                    "keywords": (" ".join(meta["keywords"]).casefold(), 6),
                    "summary": (meta["summary"].casefold(), 3),
                }
                updated = valid_timestamp(meta["updated"], "updated").timestamp()
            if include_body:
                content = read_text(path)
                if not legacy:
                    lines = content.splitlines(keepends=True)
                    end = next((index for index, line in enumerate(lines[1:], 1) if line.rstrip("\r\n") == "---"), None)
                    if not lines or lines[0].rstrip("\r\n") != "---" or end is None:
                        raise MemoryError("%s：检索期间记录发生变化，请稍后重试" % path.name)
                    content = "".join(lines[end + 1:])
                fields["body"] = (content.casefold(), 1)
            if not all(any(term in content for content, weight in fields.values()) for term in terms):
                continue
            score = sum(weight for term in terms for content, weight in fields.values() if term in content)
            item = {key: meta[key] for key in ("task_id", "title", "status", "summary")}
            item.update({"path": path.relative_to(root).as_posix(), "legacy": legacy, "score": score})
            matches.append((score, updated, item))
        except (MemoryError, OSError, ValueError, TypeError) as exc:
            errors.append(str(exc))
    if errors:
        # Return no matches from a malformed/mixed project, rather than expose
        # partially trusted results as if the boundary had been verified.
        return {"ok": False, "errors": errors, "warnings": [], "results": []}
    matches.sort(key=lambda item: (-item[0], -item[1], item[2]["path"]))
    warnings = []
    if legacy_count:
        warnings.append("%d 份旧记录无结构化元信息；默认仅匹配文件名，可用 --include-body 扩展检索" % legacy_count)
    if safe_path(root, ".project-memory.lock").exists():
        warnings.append("存在项目写锁，检索可能看到更新中的记录；必要时稍后重试")
    return {"ok": True, "project_id": config["project_id"], "query": query, "include_body": include_body,
            "total_matches": len(matches), "results": [item[2] for item in matches[:limit]], "warnings": warnings}


def check_project(root):
    result = {"ok": False, "root": str(root), "errors": [], "warnings": []}
    try:
        config = load_config(root)
        check_structure(root, config)
        agents = entry_path(root)
        previous = read_text(agents)
        left, right = marked_region(previous, ENTRY_START, ENTRY_END, agents.name)
        if ".project-memory/MEMORY.md" not in previous[left:right]:
            result["errors"].append("生效入口缺少 MEMORY.md 引导")
        tasks, warnings, errors = read_tasks(root, config)
        result["warnings"].extend(warnings)
        result["errors"].extend(errors)
        _, content, left, right = index_parts(root)
        if content[left:right] != task_table(root, tasks):
            result["errors"].append("INDEX.md 的任务表与档案不一致；修复档案后运行 reindex")
        if safe_path(root, ".project-memory.lock").exists():
            result["warnings"].append("存在项目写锁，本次只读检查可能看到写入中的状态")
        result.update({"project_id": config["project_id"], "tasks": len(tasks), "entrypoint": str(agents)})
    except (MemoryError, OSError, ValueError, TypeError) as exc:
        result["errors"].append(str(exc))
    result["ok"] = not result["errors"]
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "new-task", "reindex", "check", "search"):
        sub = subs.add_parser(name)
        sub.add_argument("--root", required=True, help="显式项目绝对路径；不搜索父目录")
        if name == "init":
            sub.add_argument("--tasks-dir", help="项目内相对路径，默认 .project-memory/tasks")
            sub.add_argument("--timezone", help="IANA 时区，默认 Asia/Shanghai")
        elif name == "new-task":
            sub.add_argument("--title", required=True)
            sub.add_argument("--summary", required=True)
            sub.add_argument("--keywords", nargs="*", default=[])
        elif name == "search":
            sub.add_argument("--query", required=True, help="空格分词，所有词都须命中；不区分大小写")
            sub.add_argument("--limit", type=int, default=5, help="最多返回 1–50 条，默认 5")
            sub.add_argument("--include-body", action="store_true", help="额外检索正文；不输出全文")
    args = parser.parse_args(argv)
    try:
        root = project_root(args.root)
        if args.command == "init":
            result = init_project(root, args.tasks_dir, args.timezone)
        elif args.command == "new-task":
            result = create_task(root, args.title, args.summary, args.keywords)
        elif args.command == "reindex":
            with project_lock(root):
                result = rebuild_index(root, load_config(root))
        elif args.command == "search":
            result = search_tasks(root, args.query, args.limit, args.include_body)
        else:
            result = check_project(root)
    except (MemoryError, OSError, ValueError, TypeError) as exc:
        result = {"ok": False, "errors": [str(exc)], "warnings": []}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
