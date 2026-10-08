"""Immediate MCP access to authored local research notes; the worker is not involved."""
from typing import Annotated, Any, Optional

from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from scweet_mcp.knowledge import KnowledgeLibrary, NoteKind, ResearchNote

NoteId = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,127}$", max_length=128)]
Hash = Annotated[Optional[str], Field(pattern=r"^[a-f0-9]{64}$")]


class SavedNote(BaseModel):
    note_id: str
    created: bool
    unchanged: bool
    relative_path: str
    library_root: str
    content_hash: str
    index_current: bool
    warning: Optional[str]


class NotePage(BaseModel):
    records: list[dict[str, Any]]
    count: int
    has_more: bool
    next_cursor: Optional[str]
    library_root: str
    index_available: bool


class ReadNote(BaseModel):
    note: ResearchNote
    content_hash: str
    relative_path: str
    library_root: str
    created_at: str
    updated_at: str


class RebuiltIndex(BaseModel):
    library_root: str
    note_count: int
    index_path: str


def register_knowledge_tools(server, config, invoke):
    library = KnowledgeLibrary(config.mcp_knowledge_base_dir, config.mcp_record_max_bytes)
    readonly = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
    write = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)

    @server.tool(annotations=write)
    async def save_research_note(note: ResearchNote, expected_hash: Hash = None) -> SavedNote:
        """Save one evidence-linked Markdown note locally, then rebuild navigation. Updating needs the current content_hash; identical notes are reused. No collection or publishing."""
        return SavedNote(**await invoke(library.save, note=note, expected_hash=expected_hash))

    @server.tool(annotations=readonly)
    async def get_research_note(note_id: NoteId) -> ReadNote:
        """Read one indexed note and its current content_hash before reuse or an update. Missing notes return a clear error."""
        return ReadNote(**await invoke(library.get, note_id=note_id))

    @server.tool(annotations=readonly)
    async def query_research_notes(
        kind: Optional[NoteKind] = None,
        author_id: Annotated[Optional[str], Field(pattern=r"^[1-9]\d{0,24}$")] = None,
        since: Annotated[Optional[str], Field(description="Inclusive local date YYYY-MM-DD.")] = None,
        until: Annotated[Optional[str], Field(description="Exclusive local date YYYY-MM-DD.")] = None,
        tag: Annotated[Optional[str], Field(max_length=80)] = None,
        text: Annotated[Optional[str], Field(max_length=512, description="Literal substring in title, summary and tags, not full-text or vector search.")] = None,
        limit: Annotated[int, Field(strict=True, ge=1, le=100)] = 50,
        cursor: Annotated[Optional[str], Field(max_length=4096)] = None,
    ) -> NotePage:
        """Filter derived note metadata, newest local date then ID first. Keep filters unchanged when following next_cursor. Manual edits need index rebuild."""
        return NotePage(**await invoke(library.query, kind=kind, author_id=author_id, since=since, until=until,
                                      tag=tag, text=text, limit=limit, cursor=cursor))

    @server.tool(annotations=write)
    async def rebuild_research_index() -> RebuiltIndex:
        """Initialize an empty local library or rebuild navigation after manual Markdown edits. Never changes note contents or collects X data."""
        return RebuiltIndex(**await invoke(library.rebuild))
