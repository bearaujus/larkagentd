"""Read deployment-provided skills and their accompanying files."""

import os
import re
from pathlib import Path

import yaml

NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
MAX_READ = 48 * 1024
MAX_LISTED = 200
DESCRIPTION_CHARS = 300
# Other files of a global skill folder the model may read (its references and SQL templates).
FILE_SUFFIXES = (".md", ".sql", ".yaml", ".yml", ".txt", ".json", ".csv")
MAX_FILES = 100
MAX_FILE_SEGMENTS = 5
MAX_FILE_NAME = 200


class SkillError(Exception):
    """Carries a fixed code the tool layer passes on."""


def front_matter(text):
    """(metadata dict, body) of a SKILL.md; metadata is {} when there is no valid block."""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end < 0:
        return {}, text
    try:
        meta = yaml.safe_load(text[3:end])
    except yaml.YAMLError:
        return {}, text
    return (meta if isinstance(meta, dict) else {}), text[end + 4 :].lstrip("\n")


def summary(name, text):
    meta, _ = front_matter(text)
    description = meta.get("description")
    description = " ".join(str(description).split()) if description else ""
    return {"name": name, "description": description[:DESCRIPTION_CHARS]}


class SkillStore:
    def __init__(self, roots):
        self.roots = [Path(r) for r in roots]

    def global_files(self):
        """{name: path} of every global skill; earlier roots win a duplicate name."""
        found = {}
        for root in self.roots:
            if not root.is_dir():
                continue
            for path in sorted(root.rglob("SKILL.md")):
                if len(path.relative_to(root).parts) > 4 or not path.is_file():
                    continue
                try:
                    meta, _ = front_matter(path.read_text(errors="replace")[:4000])
                except OSError:
                    continue
                name = str(meta.get("name") or path.parent.name)
                if NAME.fullmatch(name) and name not in found:
                    found[name] = path
        return found

    def list(self):
        files = self.global_files()
        return {
            "global": [
                summary(n, files[n].read_text(errors="replace")[:4000])
                for n in sorted(files)[:MAX_LISTED]
            ]
        }

    @staticmethod
    def folder_files(folder):
        """Sorted relative paths of the readable files of a skill folder, SKILL.md excluded.

        Hidden entries are skipped and symlinks are never followed out of the folder.
        """
        root = folder.resolve()
        found = []
        for current, dirs, names in os.walk(root, followlinks=False):
            here = Path(current)
            depth = len(here.relative_to(root).parts)
            dirs[:] = sorted(
                d
                for d in dirs
                if not d.startswith(".") and depth + 1 < MAX_FILE_SEGMENTS
            )
            for filename in sorted(names):
                path = here / filename
                relative = path.relative_to(root)
                if (
                    filename.startswith(".")
                    or filename == "SKILL.md"
                    and not relative.parent.parts
                    or path.suffix.lower() not in FILE_SUFFIXES
                    or not path.resolve().is_relative_to(root)
                    or not path.resolve().is_file()
                ):
                    continue
                found.append(relative.as_posix())
        return sorted(found)[:MAX_FILES]

    @staticmethod
    def read_folder_file(folder, file):
        """Text of one file inside a skill folder, or SkillError(bad_file / not_found)."""
        if (
            not isinstance(file, str)
            or not file
            or len(file) > MAX_FILE_NAME
            or "\\" in file
            or any(ord(c) < 32 or ord(c) == 127 for c in file)
        ):
            raise SkillError("bad_file")
        parts = file.split("/")
        if (
            len(parts) > MAX_FILE_SEGMENTS
            or any(p in ("", ".", "..") or p.startswith(".") for p in parts)
            or Path(file).suffix.lower() not in FILE_SUFFIXES
        ):
            raise SkillError("bad_file")
        root = folder.resolve()
        target = (folder / file).resolve()
        if not target.is_relative_to(root) or not target.is_file():
            raise SkillError("not_found")  # also for a link that leaves the folder
        return target.read_text(errors="replace")

    def read(self, name, file=None):
        if not isinstance(name, str) or not NAME.fullmatch(name):
            raise SkillError("bad_name")
        path = self.global_files().get(name)
        if file is not None:
            if path is None:
                raise SkillError("not_found")
            text = self.read_folder_file(path.parent, file)
            return {
                "name": name,
                "scope": "global",
                "file": file,
                "content": text[:MAX_READ],
                "truncated": len(text) > MAX_READ,
            }
        if path is None:
            raise SkillError("not_found")
        text = path.read_text(errors="replace")
        return {
            "name": name,
            "scope": "global",
            "content": text[:MAX_READ],
            "truncated": len(text) > MAX_READ,
            "files": self.folder_files(path.parent),
        }
