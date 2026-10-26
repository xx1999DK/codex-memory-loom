#!/usr/bin/env python3
"""Install Memory Loom locally, preserving earlier versions and local edits."""

import argparse
import contextlib
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tempfile
import uuid

from package_skill import DistributionError, SKILL_NAME, release_payload, verify_skill


MANIFEST = ".memory-loom-install.json"


def hashes(payload):
    return {name: hashlib.sha256(content).hexdigest() for name, content in sorted(payload.items())}


def installed_files(directory):
    files = {}
    for current, dirs, names in os.walk(str(directory), followlinks=False):
        for name in dirs + names:
            path = Path(current) / name
            if path.is_symlink():
                raise DistributionError("安装副本包含符号链接，未修改：%s" % path)
        dirs[:] = [name for name in dirs if name != "__pycache__"]
        for name in names:
            path = Path(current) / name
            relative = path.relative_to(directory).as_posix()
            if relative == MANIFEST or name == ".DS_Store" or path.suffix in {".pyc", ".pyo"}:
                continue
            if not path.is_file():
                raise DistributionError("安装副本含非普通文件：%s" % path)
            files[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return files


def read_manifest(destination):
    path = destination / MANIFEST
    if not path.is_file() or path.is_symlink():
        raise DistributionError("目标不是此安装器管理的副本；请另选目录或先手动备份，未覆盖")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError):
        raise DistributionError("安装 manifest 损坏，未修改")
    if not isinstance(manifest, dict) or manifest.get("format") != 1 or manifest.get("skill") != SKILL_NAME or not isinstance(manifest.get("files"), dict):
        raise DistributionError("安装 manifest 不受支持，未修改")
    for name, digest in manifest["files"].items():
        path = PurePosixPath(name)
        if not name or path.is_absolute() or ".." in path.parts or "\\" in name or name == MANIFEST or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise DistributionError("安装 manifest 含无效路径或摘要，未修改")
    return manifest


@contextlib.contextmanager
def install_lock(destination):
    path = destination.parent / ("." + destination.name + "-install.lock")
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise DistributionError("安装锁已存在；请确认另一安装进程已结束后再处理：%s" % path)
    owned = os.fstat(fd)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(json.dumps({"pid": os.getpid(), "created": datetime.now(timezone.utc).isoformat()}))
        yield
    finally:
        try:
            now = path.lstat()
            if now.st_ino == owned.st_ino and now.st_dev == owned.st_dev:
                path.unlink()
        except FileNotFoundError:
            pass


def install(repository, destination=None, update=False):
    version, payload = release_payload(Path(repository))
    wanted = hashes(payload)
    destination = Path(destination) if destination is not None else Path.home() / ".agents" / "skills" / SKILL_NAME
    destination = Path(os.path.abspath(str(destination.expanduser())))
    if destination.is_symlink():
        raise DistributionError("安装目标不能是符号链接，未修改")
    if destination.parent == Path(destination.anchor):
        raise DistributionError("请勿将 skill 直接安装在文件系统根目录")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with install_lock(destination):
        backup = None
        if destination.exists():
            if not destination.is_dir():
                raise DistributionError("安装目标不是目录，未修改")
            manifest = read_manifest(destination)
            actual = installed_files(destination)
            if actual != manifest["files"]:
                raise DistributionError("安装副本存在本地修改、缺失或额外文件；请先合并或另选目录，未覆盖")
            verify_skill((destination / "SKILL.md").read_text(encoding="utf-8"))
            if actual == wanted:
                return {"ok": True, "action": "unchanged", "version": version, "destination": str(destination)}
            if not update:
                raise DistributionError("目标已有不同版本；确认更新请使用 --update，旧版会保留备份")
            backup_root = destination.parent.parent / "skill-backups"
            if backup_root.is_symlink():
                raise DistributionError("备份目录不能是符号链接")
            backup_root.mkdir(parents=True, exist_ok=True)
            backup = backup_root / (SKILL_NAME + "-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8])
        staging = Path(tempfile.mkdtemp(prefix=".memory-loom-stage-", dir=str(destination.parent)))
        try:
            for relative, content in sorted(payload.items()):
                path = staging / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            manifest = {"format": 1, "skill": SKILL_NAME, "version": version, "files": wanted}
            (staging / MANIFEST).write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
            if backup is not None:
                destination.rename(backup)
            try:
                staging.rename(destination)
            except OSError:
                if backup is not None and not destination.exists():
                    backup.rename(destination)
                raise
        finally:
            if staging.exists():
                shutil.rmtree(staging)
        result = {"ok": True, "action": "updated" if backup is not None else "installed", "version": version, "destination": str(destination), "files": len(payload)}
        if backup is not None:
            result["backup"] = str(backup)
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, help="完整目标 skill 目录，默认 ~/.agents/skills/memory-loom")
    parser.add_argument("--update", action="store_true", help="更新受管副本并保留旧版备份")
    args = parser.parse_args(argv)
    try:
        result = install(Path(__file__).resolve().parent.parent, args.destination, args.update)
    except (DistributionError, OSError, UnicodeError, TypeError) as exc:
        result = {"ok": False, "error": str(exc)}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
