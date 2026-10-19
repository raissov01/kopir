"""Memory records: one Markdown file per fact, YAML-ish frontmatter.

Only the subset of YAML the records use is parsed: top-level `key: value`
lines and one level of indented keys under `metadata:`.
"""
import datetime as dt
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from .textnorm import tokens

TYPES = {"user", "feedback", "project", "reference"}
LINK = re.compile(r"\[\[([^\]|#]+)")


@dataclass
class Record:
    path: Path
    name: str
    description: str
    meta: dict
    body: str
    head: dict = field(default_factory=dict)

    @property
    def stem(self) -> str:
        return self.path.stem

    @property
    def type(self) -> str:
        return self.meta.get("type", "")

    def doc(self) -> list[str]:
        # Name and description say what the record is about; weight them.
        head = tokens(f"{self.stem} {self.name} {self.description}")
        return head * 3 + tokens(self.body)


def split_frontmatter(text: str) -> tuple[list[str], str] | None:
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---", 4)
    if end < 0:
        return None
    after = text.find("\n", end + 1)
    body = text[after + 1:] if after >= 0 else ""
    return text[4:end].splitlines(), body


def parse_head(lines: list[str]) -> tuple[dict, dict]:
    head, meta, in_meta = {}, {}, False
    for line in lines:
        if not line.strip():
            continue
        indented = line[:1] in (" ", "\t")
        key, sep, value = line.strip().partition(":")
        if not sep:
            continue
        value = value.strip().strip('"').strip("'")
        if indented and in_meta:
            meta[key] = value
        elif not indented:
            in_meta = key == "metadata"
            if not in_meta:
                head[key] = value
    return head, meta


def read(path: Path) -> Record | None:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    parts = split_frontmatter(text)
    if parts is None:
        return Record(path, path.stem, "", {}, text)
    head, meta = parse_head(parts[0])
    if "type" in head and "type" not in meta:
        meta["type"] = head["type"]
    return Record(path, head.get("name", path.stem), head.get("description", ""),
                  meta, parts[1], head)


def load(memory_dir: str, index_files: list[str]) -> list[Record]:
    root = Path(memory_dir)
    if not root.is_dir():
        return []
    skip = set(index_files)
    out = []
    for p in sorted(root.glob("*.md")):
        if p.name in skip:
            continue
        rec = read(p)
        if rec is not None:
            out.append(rec)
    return out


def check(memory_dir: str, index_files: list[str]) -> list[tuple[str, str, str]]:
    """(level, file, message). `error` breaks the format, `warn` is advice."""
    recs = load(memory_dir, index_files)
    known = {r.stem for r in recs} | {r.name for r in recs}
    known |= {k.replace("_", "-") for k in known} | {k.replace("-", "_") for k in known}
    out = []
    for r in recs:
        f = r.path.name
        if not r.head:
            out.append(("error", f, "no frontmatter"))
            continue
        if not r.description:
            out.append(("error", f, "no description"))
        if r.type not in TYPES:
            out.append(("error", f, f"metadata.type is {r.type or 'missing'}"))
        if not r.meta.get("source"):
            out.append(("warn", f, "no metadata.source"))
        obs = r.meta.get("observed_at", "")
        if not obs:
            out.append(("warn", f, "no metadata.observed_at"))
        elif not re.fullmatch(r"\d{4}-\d{2}-\d{2}", obs):
            out.append(("error", f, f"observed_at is not YYYY-MM-DD: {obs}"))
        for link in LINK.findall(r.body):
            if link.strip() not in known:
                out.append(("warn", f, f"[[{link.strip()}]] does not resolve"))
    index = Path(memory_dir) / "MEMORY.md"
    if index.is_file():
        for target in re.findall(r"\]\(([^)]+\.md)\)", index.read_text(encoding="utf-8")):
            if not (Path(memory_dir) / target).is_file():
                out.append(("error", "MEMORY.md", f"links missing {target}"))
    return out


def backfill(memory_dir: str, index_files: list[str], *, dry_run: bool,
             source: str = "") -> list[str]:
    """Add metadata.source and metadata.observed_at where they are missing.

    The date comes from `metadata.modified` when the agent recorded one, the
    file time otherwise. A record with an `originSessionId` was written by a
    Claude Code session; for the rest `source` is used when given.
    """
    changed = []
    for r in load(memory_dir, index_files):
        if not r.head or (r.meta.get("source") and r.meta.get("observed_at")):
            continue
        add = {}
        if not r.meta.get("source"):
            add["source"] = ("claude" if r.meta.get("originSessionId")
                             else source or "unknown")
        if not r.meta.get("observed_at"):
            mod = r.meta.get("modified", "")[:10]
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", mod):
                mod = dt.date.fromtimestamp(r.path.stat().st_mtime).isoformat()
            add["observed_at"] = mod
        changed.append(f"{r.path.name}: " + ", ".join(f"{k}={v}" for k, v in add.items()))
        if not dry_run:
            _set_meta(r.path, add)
    return changed


def _set_meta(path: Path, add: dict) -> None:
    text = path.read_text(encoding="utf-8")
    lines, body = split_frontmatter(text) or ([], text)
    new = [f"  {k}: {v}" for k, v in add.items()]
    for i, line in enumerate(lines):
        if line.split(":")[0].strip() == "metadata" and line[:1] not in (" ", "\t"):
            j = i + 1
            while j < len(lines) and lines[j][:1] in (" ", "\t"):
                j += 1
            lines[j:j] = new
            break
    else:
        lines += ["metadata:"] + new
    stat = path.stat()
    tmp = path.with_suffix(".md.kopir-tmp")
    tmp.write_text("---\n" + "\n".join(lines) + "\n---\n" + body, encoding="utf-8")
    tmp.replace(path)
    # Keep the file time: it is what the next backfill would fall back to.
    os.utime(path, (stat.st_atime, stat.st_mtime))
