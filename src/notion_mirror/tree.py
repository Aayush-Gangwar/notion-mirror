import dataclasses
import os
from pathlib import Path
from typing import Any
from urllib.parse import quote

from notion_mirror.notion_api import get_title
from notion_mirror.utils import safe_filename

Page = dict[str, Any]


@dataclasses.dataclass
class PageLayout:
    page_id: str
    title: str
    dir: Path
    md_path: Path
    assets_dir: Path
    rel_path: str  # posix-style path, relative to the notes root


def build_children_map(
    pages: list[Page],
) -> tuple[dict[str, Page], dict[str, list[str]], list[str]]:
    """
    Group pages by parent page.

    Returns (pages_by_id, children_map, root_ids). Pages whose parent
    cannot be resolved among the discovered pages are treated as
    top-level roots (with a warning) rather than dropped.
    """
    pages_by_id = {page["id"]: page for page in pages}
    children_map: dict[str, list[str]] = {}
    root_ids: list[str] = []

    for page in pages:
        page_id = page["id"]
        parent = page.get("parent", {})
        parent_type = parent.get("type")

        if parent_type == "workspace":
            root_ids.append(page_id)
            continue

        parent_id = parent.get("page_id")

        if parent_id and parent_id in pages_by_id:
            children_map.setdefault(parent_id, []).append(page_id)
        else:
            print(
                f"⚠ Page {page_id} ({get_title(page)}) has an "
                "unresolved parent; treating it as top-level."
            )
            root_ids.append(page_id)

    return pages_by_id, children_map, root_ids


# Keep individual path segments well short of Windows' ~260-char MAX_PATH,
# since pages can be nested many levels deep. The full title is still kept
# in the rendered Markdown (as the "# <Title>" heading) - only the on-disk
# folder name is shortened.
_MAX_SEGMENT_LENGTH = 60


def _unique_dir(parent_dir: Path, title: str, used: set[str]) -> Path:
    base = safe_filename(title) or "Untitled"
    base = base[:_MAX_SEGMENT_LENGTH].rstrip() or "Untitled"
    candidate = base
    suffix = 2

    while candidate.casefold() in used:
        candidate = f"{base} ({suffix})"
        suffix += 1

    used.add(candidate.casefold())

    return parent_dir / candidate


def compute_layout(
    pages_by_id: dict[str, Page],
    children_map: dict[str, list[str]],
    root_ids: list[str],
    notes_dir: Path,
) -> dict[str, PageLayout]:
    """
    Assign every page a directory, nesting child pages under their
    parent's directory. Sibling name collisions get a " (2)", " (3)", ...
    suffix.
    """
    layout: dict[str, PageLayout] = {}

    def visit(page_id: str, parent_dir: Path, used_names: set[str]) -> None:
        page = pages_by_id[page_id]
        title = get_title(page)
        page_dir = _unique_dir(parent_dir, title, used_names)

        layout[page_id] = PageLayout(
            page_id=page_id,
            title=title,
            dir=page_dir,
            md_path=page_dir / "index.md",
            assets_dir=page_dir / "assets",
            rel_path=page_dir.relative_to(notes_dir).as_posix(),
        )

        child_used_names: set[str] = set()

        for child_id in children_map.get(page_id, []):
            visit(child_id, page_dir, child_used_names)

    root_used_names: set[str] = set()

    for root_id in root_ids:
        visit(root_id, notes_dir, root_used_names)

    return layout


def relative_link(from_dir: Path, to_path: Path) -> str:
    """
    Build a Markdown-safe relative link from a page's directory to
    another file (another page's index.md, or an asset).
    """
    rel = os.path.relpath(to_path, from_dir)

    return "/".join(
        quote(part) for part in Path(rel).parts
    )
