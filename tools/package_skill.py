#!/usr/bin/env python3
"""Build a reproducible Memory Loom ZIP using only Python's standard library."""

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import tempfile
import zipfile


SKILL_NAME = "memory-loom"
SOURCE_DIRECTORIES = {"agents", "assets", "references", "scripts"}
PRIVATE_NAMES = {"__pycache__", ".project-memory", "项目记忆", "工作记录", "tasks", "attachments"}
VERSION_PATTERN = r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?"


class DistributionError(Exception):
    pass


def excluded(path):
    return any(part.startswith(".") or part in PRIVATE_NAMES for part in path.parts) or path.suffix.lower() in {".pyc", ".pyo", ".lock"}


def verify_skill(text):
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise DistributionError("SKILL.md 缺少 frontmatter")
    try:
        end = lines.index("---", 1)
    except ValueError:
        raise DistributionError("SKILL.md frontmatter 未闭合")
    names = [line.partition(":")[2].strip().strip("\"'") for line in lines[1:end] if line.startswith("name:")]
    if names != [SKILL_NAME]:
        raise DistributionError("目标技能名称必须是 memory-loom")


def release_payload(repository):
    """Return an allowlisted snapshot; do not traverse private trees or symlinks."""
    repository = Path(repository)
    source = repository / SKILL_NAME
    if source.is_symlink() or not source.is_dir():
        raise DistributionError("缺少真实的 memory-loom 源码目录")
    version_path = repository / "VERSION"
    if version_path.is_symlink():
        raise DistributionError("VERSION 不能是符号链接")
    version = version_path.read_text(encoding="utf-8").strip()
    if not re.fullmatch(VERSION_PATTERN, version):
        raise DistributionError("VERSION 不是受支持的版本号")
    payload = {}

    def collect(directory, relative):
        for candidate in sorted(directory.iterdir(), key=lambda p: p.name):
            rel = relative / candidate.name
            if excluded(rel):
                continue
            if candidate.is_symlink():
                raise DistributionError("分发文件不能是符号链接：%s" % rel)
            if candidate.is_dir():
                collect(candidate, rel)
            elif candidate.is_file():
                payload[rel.as_posix()] = candidate.read_bytes()
            else:
                raise DistributionError("分发路径不是普通文件：%s" % rel)

    entry = source / "SKILL.md"
    if entry.is_symlink() or not entry.is_file():
        raise DistributionError("SKILL.md 必须是普通文件")
    payload["SKILL.md"] = entry.read_bytes()
    verify_skill(payload["SKILL.md"].decode("utf-8"))
    for name in sorted(SOURCE_DIRECTORIES):
        directory = source / name
        if directory.is_symlink():
            raise DistributionError("分发目录不能是符号链接：%s" % name)
        if directory.exists():
            if not directory.is_dir():
                raise DistributionError("分发目录不是目录：%s" % name)
            collect(directory, Path(name))
    payload["VERSION"] = (version + "\n").encode("utf-8")
    license_path = repository / "LICENSE"
    if license_path.is_symlink():
        raise DistributionError("LICENSE 不能是符号链接")
    if license_path.is_file():
        payload["LICENSE"] = license_path.read_bytes()
    return version, payload


def atomic_write(path, data):
    path = Path(path)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise DistributionError("拒绝覆盖非普通文件：%s" % path)
    fd, temporary = tempfile.mkstemp(prefix=".loom-package-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def build_package(repository, output_dir=None):
    repository = Path(repository)
    version, payload = release_payload(repository)
    output = Path(output_dir) if output_dir is not None else repository / "dist"
    if output.is_symlink():
        raise DistributionError("输出目录不能是符号链接")
    source = (repository / SKILL_NAME).resolve()
    if output.resolve() == source or source in output.resolve().parents:
        raise DistributionError("输出目录不能位于 skill 源码内，避免把发布包再次打包")
    output.mkdir(parents=True, exist_ok=True)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for relative, content in sorted(payload.items()):
            entry = zipfile.ZipInfo(SKILL_NAME + "/" + relative, date_time=(1980, 1, 1, 0, 0, 0))
            entry.create_system = 3
            entry.external_attr = 0o100644 << 16
            entry.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(entry, content, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    name = "%s-%s.zip" % (SKILL_NAME, version)
    data = buffer.getvalue()
    digest = hashlib.sha256(data).hexdigest()
    destination = output / name
    checksums = output / "SHA256SUMS"
    # Validate both outputs before replacing either.
    for path in (destination, checksums):
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise DistributionError("拒绝覆盖非普通文件：%s" % path)
    atomic_write(destination, data)
    atomic_write(checksums, (digest + "  " + name + "\n").encode("ascii"))
    return {"ok": True, "version": version, "archive": str(destination.resolve()), "sha256": digest, "checksums": str(checksums.resolve()), "files": len(payload)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, help="输出目录，默认仓库 dist/")
    args = parser.parse_args(argv)
    try:
        result = build_package(Path(__file__).resolve().parent.parent, args.output_dir)
    except (DistributionError, OSError, UnicodeError) as exc:
        result = {"ok": False, "error": str(exc)}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
