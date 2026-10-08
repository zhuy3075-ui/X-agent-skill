"""Local Markdown research notes and a rebuildable metadata index. No X requests."""
from __future__ import annotations

import hashlib
import json
import os
import re
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Literal, Optional
from urllib.parse import quote, urlsplit
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from Scweet.config import ScweetConfig
from scweet_mcp.jobs import decode_cursor, encode_cursor
from scweet_mcp.store import StoreError

NoteKind = Literal["post", "daily", "material", "need", "resource", "combination", "report"]
FOLDERS = {"material": "素材", "need": "需求", "resource": "资源", "combination": "组合", "report": "报告"}
NOTE_ID = r"[a-z0-9][a-z0-9_-]{0,127}"
SECRET = re.compile(r"(?:auth_token|SCWEET_X_[A-Z0-9_]+)[\"']?\s*[=:]\s*[\"']?[A-Za-z0-9_-]{16,}|authorization[\"']?\s*[=:]\s*[\"']?Bearer\s+[A-Za-z0-9_.-]{16,}|gh[pousr]_[A-Za-z0-9_-]{20,}|github_pat_[A-Za-z0-9_]{20,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY", re.I)
CREDENTIAL_VALUE = re.compile(
    r"(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis)://[^\s/@:]+:[^\s/@]+@"
    r"|(?:[A-Z0-9_]*(?:api_key|api_token|password|passwd|secret))[\"']?\s*[=:]\s*[\"']?(?!<)[A-Za-z0-9_./+\-=]{3,}", re.I)


class ResearchNote(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern="^" + NOTE_ID + "$", max_length=128)
    kind: NoteKind
    title: str = Field(min_length=1, max_length=240)
    date: str = Field(description="Local publication day for posts, or the report day. YYYY-MM-DD.")
    timezone: str = "Asia/Shanghai"
    author_ids: list[str] = Field(default_factory=list, max_length=100)
    tags: list[str] = Field(default_factory=list, max_length=50)
    source_post_ids: list[str] = Field(default_factory=list, max_length=500)
    source_comment_ids: list[str] = Field(default_factory=list, max_length=500)
    source_urls: list[str] = Field(default_factory=list, max_length=100)
    related_note_ids: list[str] = Field(default_factory=list, max_length=500)
    evidence_status: Literal["verified", "partial", "unverified", "pending"] = "pending"
    comment_coverage: Literal["exhausted_visible", "partial", "not_collected", "unknown"] = "unknown"
    status: Literal["draft", "reviewed", "selected", "published", "archived"] = "draft"
    summary: str = Field(default="", max_length=2000)
    content: str = Field(min_length=1, max_length=50000, description="Authored Markdown analysis. No credentials or instructions copied from source posts.")

    @field_validator("date")
    @classmethod
    def calendar_day(cls, value):
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError("Use YYYY-MM-DD")
        try:
            date.fromisoformat(value)
        except ValueError:
            raise ValueError("Use YYYY-MM-DD with a real calendar date") from None
        return value

    @field_validator("timezone")
    @classmethod
    def time_zone(cls, value):
        try:
            ZoneInfo(value)
        except (ValueError, ZoneInfoNotFoundError):
            raise ValueError("Use an IANA timezone") from None
        return value

    @field_validator("author_ids", "source_post_ids", "source_comment_ids")
    @classmethod
    def platform_ids(cls, values):
        if any(not re.fullmatch(r"[1-9]\d{0,24}", value) for value in values):
            raise ValueError("Use numeric platform IDs")
        return list(dict.fromkeys(values))

    @field_validator("related_note_ids")
    @classmethod
    def references(cls, values):
        if any(not re.fullmatch(NOTE_ID, value) for value in values):
            raise ValueError("Use existing knowledge note IDs")
        return list(dict.fromkeys(values))

    @field_validator("tags")
    @classmethod
    def labels(cls, values):
        if any(not value.strip() or len(value) > 80 or "\n" in value for value in values):
            raise ValueError("Use short single-line tags")
        return list(dict.fromkeys(values))

    @field_validator("source_urls")
    @classmethod
    def public_urls(cls, values):
        for value in values:
            parsed = urlsplit(value)
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or len(value) > 2048:
                raise ValueError("Use public HTTPS source URLs without credentials")
        return list(dict.fromkeys(values))

    @field_validator("title")
    @classmethod
    def one_line(cls, value):
        if not value.strip() or any(c in value for c in ("\n", "\r", "\0")):
            raise ValueError("Use a nonempty single-line title")
        return value

    @field_validator("content")
    @classmethod
    def body(cls, value):
        if not value.strip():
            raise ValueError("Write the note analysis before saving")
        return value.rstrip("\n")

    @model_validator(mode="after")
    def evidence(self):
        if self.id in self.related_note_ids:
            raise ValueError("Use existing knowledge note IDs other than this note")
        if self.kind in {"post", "daily"} and len(self.author_ids) != 1:
            raise ValueError("Post and daily notes need one canonical author ID")
        if self.kind == "post" and (len(self.source_post_ids) != 1 or self.id != "post_" + self.source_post_ids[0]):
            raise ValueError("Use post_<source_post_id> for one post")
        if self.kind == "daily" and self.id != f"daily_{self.author_ids[0]}_{self.date}":
            raise ValueError("Use daily_<author_id>_<date> for a daily note")
        if self.kind == "combination" and self.evidence_status == "verified" and not (self.source_post_ids and self.source_comment_ids):
            raise ValueError("Verified combinations need both post and comment evidence; otherwise use pending or partial")
        strings = [item for value in self.model_dump().values() for item in (value if isinstance(value, list) else [value]) if isinstance(item, str)]
        if SECRET.search("\n".join(strings)) or CREDENTIAL_VALUE.search("\n".join(strings)):
            raise ValueError("Remove credential values before saving")
        return self


def digest(payload):
    return hashlib.sha256(payload).hexdigest()


def escape(value):
    value = str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    for char in ("\\", "[", "]", "|", "*", "_", "`"):
        value = value.replace(char, "\\" + char)
    return value.replace("\n", " ")


class KnowledgeLibrary:
    def __init__(self, root, max_bytes=None):
        self.root = Path(root).expanduser().resolve()
        self.max_bytes = max_bytes or ScweetConfig().mcp_record_max_bytes

    def safe_path(self, relative):
        path = self.root / relative
        if not path.resolve().is_relative_to(self.root):
            raise StoreError("KB_PATH_ERROR: The note path leaves the configured library. Remove unsafe links and retry.")
        return path

    def layout(self):
        self.root.mkdir(parents=True, exist_ok=True)
        for folder in ("博主", "素材", "需求", "资源", "组合", "报告", "_模板", ".history"):
            self.safe_path(folder).mkdir(exist_ok=True)
        readme = self.safe_path("README.md")
        if not readme.exists():
            self.atomic(readme, ("# 精致清单\n\n本地 Markdown 研究知识库。当前结构不代表已经采集或分析。\n\n"
                "入口：[总索引](INDEX.md)。博主目录保存稳定帖子卡和日报；素材、需求、资源、组合、报告单独归档。\n\n"
                "元数据用 YAML frontmatter（字段值写成 JSON），保留数字博主／帖子 ID、日期、主题标签与来源。\n"
                "Agent 从正文与评论证据生成组合；没有评论证据时标记待验证，不能编造痛点。\n\n"
                "手工修改笔记后调用 rebuild_research_index。INDEX.md 和 index.json 是可重建导航，不是原始采集数据。\n"
                "实际内容仅保留本地，不上传 Git。数据库原始数据仍在 PostgreSQL。\n").encode())

    @contextmanager
    def writing(self):
        self.layout()
        lock = self.safe_path(".write.lock")
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            raise StoreError("KB_BUSY: The library lock exists. Retry after the writer finishes. If it crashed, stop library writers, inspect the lock PID, and remove only a confirmed stale lock.") from None
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump({"pid": os.getpid(), "created_at": datetime.now(timezone.utc).isoformat()}, stream)
            yield
        finally:
            lock.unlink(missing_ok=True)

    def atomic(self, path, payload, expected_previous=None, check_previous=False):
        self.safe_path(path.relative_to(self.root))
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + "." + str(uuid4()) + ".tmp")
        try:
            fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            if check_previous:
                current = path.read_bytes() if path.exists() else None
                if current != expected_previous:
                    raise StoreError("KB_CONFLICT: The note changed during saving. Read it again and merge; avoid editing while MCP writes.")
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def relative(self, note):
        if note.kind == "post":
            return f"博主/{note.author_ids[0]}/帖子/{note.id}.md"
        if note.kind == "daily":
            return f"博主/{note.author_ids[0]}/日报/{note.date}.md"
        return f"{FOLDERS[note.kind]}/{note.id}.md"

    def read_document(self, path):
        try:
            path = self.safe_path(path.relative_to(self.root))
            if path.stat().st_size > self.max_bytes:
                raise ValueError()
            raw = path.read_bytes()
            text = raw.decode("utf-8-sig").replace("\r\n", "\n")
            if not text.startswith("---\n"):
                raise ValueError()
            header, content = text[4:].split("\n---\n", 1)
            metadata = {}
            for line in header.splitlines():
                key, separator, value = line.partition(":")
                if not separator or key in metadata:
                    raise ValueError()
                metadata[key] = json.loads(value.strip())
            schema = metadata.pop("kb_schema")
            if type(schema) is not int or schema != 1 or "content" in metadata:
                raise ValueError()
            created_at, updated_at = metadata.pop("created_at"), metadata.pop("updated_at")
            note = ResearchNote(**metadata, content=content)
            return note, raw, created_at, updated_at
        except StoreError:
            raise
        except (OSError, ValueError, KeyError, TypeError):
            raise StoreError("KB_FORMAT_ERROR: A note has invalid metadata or exceeds the size limit. Fix its frontmatter before rebuilding.") from None

    def scan(self):
        records = {}
        for folder in ("博主", *FOLDERS.values()):
            base = self.safe_path(folder)
            if not base.exists():
                continue
            for path in base.rglob("*.md"):
                note, raw, created_at, updated_at = self.read_document(path)
                if note.id in records:
                    raise StoreError("KB_DUPLICATE: Two Markdown files share a note ID. Keep one canonical file before rebuilding.")
                records[note.id] = {**note.model_dump(exclude={"content"}), "relative_path": path.relative_to(self.root).as_posix(),
                    "content_hash": digest(raw), "created_at": created_at, "updated_at": updated_at}
        return records

    def write_index(self, records):
        ordered = sorted(records.values(), key=lambda row: (row["date"], row["id"]), reverse=True)
        lines = ["# 精致清单总索引", "", f"已保存 {len(ordered)} 条研究笔记。空索引不代表已经分析。", "",
                 "## 按日期与类型", "", "| 日期 | 类型 | 标题 | 博主 | 状态 |", "| --- | --- | --- | --- | --- |"]
        labels = {"post": "帖子分析", "daily": "每日汇总", "material": "素材", "need": "需求", "resource": "资源", "combination": "发布组合", "report": "决策报告"}
        for row in ordered:
            link = f"[{escape(row['title'])}]({quote(row['relative_path'], safe='/')})"
            lines.append(f"| {row['date']} | {labels[row['kind']]} | {link} | {', '.join(row['author_ids']) or '跨博主／未关联'} | {row['status']} |")
        for title, field in (("按博主", "author_ids"), ("按主题", "tags")):
            lines.extend(["", "## " + title, ""])
            values = sorted({value for row in ordered for value in row[field]})
            for value in values:
                lines.extend(["### " + escape(value), ""])
                lines.extend(f"- {row['date']} [{escape(row['title'])}]({quote(row['relative_path'], safe='/')})" for row in ordered if value in row[field])
                lines.append("")
        self.atomic(self.safe_path("INDEX.md"), ("\n".join(lines) + "\n").encode())
        self.atomic(self.safe_path("index.json"), (json.dumps({"kb_schema": 1, "notes": ordered}, ensure_ascii=False, indent=2) + "\n").encode())

    def initialize(self):
        return self.rebuild()

    def rebuild(self):
        try:
            with self.writing():
                records = self.scan()
                self.write_index(records)
                return {"library_root": str(self.root), "note_count": len(records), "index_path": "INDEX.md"}
        except OSError:
            raise StoreError("KB_IO_ERROR: Cannot rebuild the library index. Check folder permissions and disk space.") from None

    def save(self, note, expected_hash=None):
        try:
            with self.writing():
                records = self.scan()
                for reference in note.related_note_ids:
                    if reference not in records and reference != note.id:
                        raise StoreError("KB_REFERENCE_MISSING: Save referenced notes first, or correct related_note_ids.")
                relative = self.relative(note)
                path = self.safe_path(relative)
                existing = records.get(note.id)
                previous = None
                created_at = datetime.now(timezone.utc).isoformat()
                if existing:
                    if existing["relative_path"] != relative:
                        raise StoreError("KB_CONFLICT: An existing note uses this ID at a different canonical path. Keep its kind and author identity.")
                    old_note, previous, created_at, _ = self.read_document(path)
                    if expected_hash and expected_hash != digest(previous):
                        raise StoreError("KB_CONFLICT: The note changed after it was read. Read it again and merge your changes.")
                    if old_note == note:
                        self.write_index(records)
                        return {"note_id": note.id, "created": False, "unchanged": True, "relative_path": relative,
                                "library_root": str(self.root), "content_hash": digest(previous), "index_current": True, "warning": None}
                    if not expected_hash:
                        raise StoreError("KB_CONFLICT: Read the existing note and provide its content_hash before updating it.")
                    self.atomic(self.safe_path(f".history/{note.id}/{digest(previous)}.md"), previous)
                elif expected_hash:
                    raise StoreError("KB_CONFLICT: The expected previous note is missing. Read the library before saving.")
                metadata = {"kb_schema": 1, **note.model_dump(exclude={"content"}), "created_at": created_at,
                            "updated_at": datetime.now(timezone.utc).isoformat()}
                payload = ("---\n" + "\n".join(f"{key}: {json.dumps(value, ensure_ascii=False)}" for key, value in metadata.items())
                           + "\n---\n" + note.content + "\n").encode()
                if len(payload) > self.max_bytes:
                    raise StoreError("INVALID_INPUT: The Markdown note exceeds mcp_record_max_bytes. Split the report into linked notes.")
                self.atomic(path, payload, expected_previous=previous, check_previous=True)
                warning = None
                try:
                    self.write_index(self.scan())
                except (StoreError, OSError):
                    warning = "The note is saved, but its index is not current. Fix folder access or note metadata, then rebuild_research_index."
                return {"note_id": note.id, "created": existing is None, "unchanged": False, "relative_path": relative,
                        "library_root": str(self.root), "content_hash": digest(payload), "index_current": warning is None, "warning": warning}
        except OSError:
            raise StoreError("KB_IO_ERROR: Cannot save the note. Check folder permissions and disk space; read the note before retrying.") from None

    def index(self):
        path = self.safe_path("index.json")
        if not path.exists():
            return []
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if type(value["kb_schema"]) is not int or value["kb_schema"] != 1 or not isinstance(value["notes"], list):
                raise ValueError()
            seen = set()
            for row in value["notes"]:
                if not isinstance(row, dict) or not isinstance(row.get("relative_path"), str) or not re.fullmatch(r"[a-f0-9]{64}", row.get("content_hash", "")):
                    raise ValueError()
                self.safe_path(row["relative_path"])
                note = ResearchNote(**{key: field for key, field in row.items() if key in ResearchNote.model_fields and key != "content"}, content="Index metadata")
                if note.id in seen:
                    raise ValueError()
                seen.add(note.id)
            return value["notes"]
        except (OSError, ValueError, KeyError, TypeError):
            raise StoreError("KB_INDEX_ERROR: Rebuild the library index before querying notes.") from None

    def get(self, note_id):
        if not re.fullmatch(NOTE_ID, note_id):
            raise StoreError("INVALID_INPUT: Use a knowledge note ID, not a file path.")
        record = next((row for row in self.index() if row["id"] == note_id), None)
        if not record:
            raise StoreError("NOT_FOUND: No indexed note has that ID. Rebuild the index after manual additions.")
        note, raw, created, updated = self.read_document(self.safe_path(record["relative_path"]))
        if note.id != note_id:
            raise StoreError("KB_INDEX_ERROR: The indexed note identity changed. Rebuild the index before reading.")
        return {"note": note.model_dump(), "content_hash": digest(raw), "relative_path": record["relative_path"],
                "library_root": str(self.root), "created_at": created, "updated_at": updated}

    def query(self, kind=None, author_id=None, since=None, until=None, tag=None, text=None, limit=50, cursor=None):
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise StoreError("INVALID_INPUT: Use a note page limit from 1 to 100.")
        try:
            for value in (since, until):
                if value:
                    ResearchNote.calendar_day(value)
            if since and until and since >= until:
                raise ValueError()
        except ValueError:
            raise StoreError("INVALID_INPUT: Use real YYYY-MM-DD bounds; since must precede until.") from None
        filters = {"kind": kind, "author_id": author_id, "since": since, "until": until, "tag": tag, "text": text}
        rows = [row for row in self.index() if (not kind or row["kind"] == kind)
                and (not author_id or author_id in row["author_ids"]) and (not since or row["date"] >= since)
                and (not until or row["date"] < until) and (not tag or tag in row["tags"])
                and (not text or text.casefold() in (row["title"] + " " + row["summary"] + " " + " ".join(row["tags"])).casefold())]
        rows.sort(key=lambda row: (row["date"], row["id"]), reverse=True)
        if cursor:
            values = decode_cursor(cursor, filters)
            if len(values) != 2 or not all(isinstance(value, str) for value in values):
                raise StoreError("INVALID_INPUT: Use the returned note cursor with unchanged filters.")
            rows = [row for row in rows if (row["date"], row["id"]) < tuple(values)]
        selected = rows[:limit]
        more = len(rows) > limit
        return {"records": selected, "count": len(selected), "has_more": more,
                "next_cursor": encode_cursor(filters, [selected[-1]["date"], selected[-1]["id"]]) if more else None,
                "library_root": str(self.root), "index_available": self.safe_path("index.json").exists()}
