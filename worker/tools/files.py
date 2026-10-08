"""File tools, sandboxed to the workspace/ folder.

All paths are relative to the workspace. Any path that resolves outside it
(``../../etc/passwd``, absolute paths, symlink tricks) is rejected, so the
agent cannot read or overwrite arbitrary files on the machine.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import Any

from pypdf import PdfReader

from ..runtime.context import RunContext
from ..safety.policy import ProposedAction
from .base import ErrorKind, Tool, ToolError, ToolResult, schema

MAX_READ_CHARS = 20000


def safe_path(root: Path, rel: str) -> Path:
    root = root.resolve()
    p = (root / rel.replace("\\", "/").lstrip("/")).resolve()
    if p != root and root not in p.parents:
        raise ToolError(ErrorKind.PERMISSION, f"Path '{rel}' is outside the workspace.",
                        "Use a path relative to the workspace, e.g. 'downloads/INV-1.pdf'.")
    return p


def read_any(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        reader = PdfReader(str(path))
        pages = [f"--- page {i + 1} ---\n{(pg.extract_text() or '').strip()}" for i, pg in enumerate(reader.pages)]
        return "\n".join(pages)
    if suffix in {".png", ".jpg", ".jpeg", ".gif", ".zip", ".xlsx", ".docx", ".bin"}:
        raise ToolError(ErrorKind.INVALID_INPUT, f"Cannot read binary file type '{suffix}' as text.")
    return path.read_text(encoding="utf-8", errors="replace")


class FileList(Tool):
    name = "file_list"
    description = "List files in a workspace folder (default: the workspace root), recursively."
    input_schema = schema({"folder": {"type": "string", "description": "Relative folder, e.g. 'inbox'"}})

    async def run(self, args: dict[str, Any], ctx: RunContext) -> ToolResult:
        folder = safe_path(ctx.workspace, args.get("folder") or ".")
        if not folder.exists():
            raise ToolError(ErrorKind.NOT_FOUND, f"Folder '{args.get('folder')}' does not exist.")
        root = ctx.workspace.resolve()
        files = [f"{p.relative_to(root).as_posix()}  ({p.stat().st_size:,} bytes)"
                 for p in sorted(folder.rglob("*")) if p.is_file() and p.name != ".gitkeep"]
        return ToolResult.success("\n".join(files) if files else "(no files)")


class FileRead(Tool):
    name = "file_read"
    description = "Read a text, CSV, JSON, Markdown or PDF file from the workspace (PDF text is extracted)."
    input_schema = schema({"path": {"type": "string"}}, ["path"])

    async def run(self, args: dict[str, Any], ctx: RunContext) -> ToolResult:
        p = safe_path(ctx.workspace, args["path"])
        if not p.is_file():
            raise ToolError(ErrorKind.NOT_FOUND, f"File '{args['path']}' not found.",
                            "Use file_list to see which files exist.")
        text = read_any(p)
        more = f"\n[... truncated at {MAX_READ_CHARS} chars ...]" if len(text) > MAX_READ_CHARS else ""
        return ToolResult.success(f"File: {args['path']}\n---\n{text[:MAX_READ_CHARS]}{more}")


class FileWrite(Tool):
    name = "file_write"
    description = ("Write a text file into the workspace (e.g. a CSV report into outbox/). Provide either "
                   "'content' (raw text) or 'rows' (list of objects, written as CSV with a header row).")
    input_schema = schema({
        "path": {"type": "string", "description": "Relative path, e.g. 'outbox/report.csv'"},
        "content": {"type": "string"},
        "rows": {"type": "array", "items": {"type": "object"}},
    }, ["path"])

    async def run(self, args: dict[str, Any], ctx: RunContext) -> ToolResult:
        p = safe_path(ctx.workspace, args["path"])
        await ctx.guard.authorize(ProposedAction("file_write", f"Write file {args['path']}"), ctx)
        if args.get("rows") is not None:
            rows = args["rows"]
            if not rows:
                raise ToolError(ErrorKind.INVALID_INPUT, "'rows' is empty.")
            buf = io.StringIO()
            fieldnames: list[str] = []
            for r in rows:
                fieldnames += [k for k in r if k not in fieldnames]
            w = csv.DictWriter(buf, fieldnames=fieldnames)
            w.writeheader()
            w.writerows(rows)
            content = buf.getvalue()
        elif args.get("content") is not None:
            content = args["content"]
        else:
            raise ToolError(ErrorKind.INVALID_INPUT, "Provide 'content' or 'rows'.")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8", newline="")
        ctx.record_action(f"Wrote file {args['path']}", path=args["path"], bytes=len(content.encode()))
        return ToolResult.success(f"Wrote {len(content):,} characters to {args['path']}.", side_effect=True,
                                  data={"path": args["path"], "preview": content[:500]})


FILE_TOOLS: list[type[Tool]] = [FileList, FileRead, FileWrite]
