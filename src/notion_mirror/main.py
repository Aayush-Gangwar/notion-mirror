import shutil
from pathlib import Path

from notion_mirror.markdown import RenderContext, blocks_to_markdown
from notion_mirror.notion_api import (
    discover_pages,
    download_file,
    get_block_children,
    get_title,
)
from notion_mirror.state import load_state, save_state
from notion_mirror.tree import (
    PageLayout,
    build_children_map,
    compute_layout,
    relative_link,
)

NOTES_DIR = Path("notes")
NOTES_DIR.mkdir(exist_ok=True)


def _is_safe_to_delete(candidate: Path, valid_dirs: list[Path]) -> bool:
    """
    A stale directory (from a page that moved/renamed, or disappeared
    from Notion) is only safe to recursively remove if no *currently
    valid* page's directory is the same path or nested underneath it.

    Sibling ordering from Notion's search results isn't guaranteed
    stable across runs, so a page's own computed path can shift between
    syncs even though nothing meaningful changed. Blindly rmtree-ing a
    "stale" path in that case could wipe out other pages that still
    legitimately live underneath it - this check is what stands between
    a rename and an accidental mass deletion.
    """
    return not any(
        candidate == valid_dir or valid_dir.is_relative_to(candidate)
        for valid_dir in valid_dirs
    )


def _remove_stale_dir(old_dir: Path, valid_dirs: list[Path]) -> None:
    if not old_dir.exists():
        return

    if not _is_safe_to_delete(old_dir, valid_dirs):
        print(
            f"⚠ Not removing {old_dir} - it still contains other "
            "currently-synced pages."
        )
        return

    shutil.rmtree(old_dir, ignore_errors=True)


def _make_context(
    layout: dict[str, PageLayout],
    page: PageLayout,
) -> RenderContext:
    def download_asset(url: str, stem: str) -> str | None:
        local_path = download_file(url, page.assets_dir, stem)

        if local_path is None:
            return None

        return relative_link(page.dir, local_path)

    def resolve_page_link(target_id: str) -> tuple[str, str] | None:
        target = layout.get(target_id)

        if target is None:
            return None

        return target.title, relative_link(page.dir, target.md_path)

    return RenderContext(
        download_asset=download_asset,
        resolve_page_link=resolve_page_link,
    )


def sync_pages():
    pages = discover_pages()
    pages_by_id, children_map, root_ids = build_children_map(pages)
    layout = compute_layout(pages_by_id, children_map, root_ids, NOTES_DIR)
    valid_dirs = [pl.dir for pl in layout.values()]

    state = load_state()

    updated = 0
    skipped = 0
    failed = 0
    seen_ids: set[str] = set()

    print(f"\nFound {len(pages)} page(s)\n")

    for page in pages:
        page_id = page["id"]
        seen_ids.add(page_id)

        title = get_title(page)
        edited = page["last_edited_time"]
        page_layout = layout[page_id]

        old_entry = state.get(page_id)

        unchanged = (
            old_entry is not None
            and old_entry.get("last_edited_time") == edited
            and old_entry.get("rel_path") == page_layout.rel_path
        )

        if unchanged:
            skipped += 1
            print(f"⏭ Skipped: {title}")
            continue

        print(f"🔄 Exporting: {title}")

        # A single page failing (an unexpected block shape, a filesystem
        # limit, a stubborn network error) must not lose everything else
        # in a large sync - isolate it, log it, and keep going. State is
        # saved after every export so a later crash/interruption doesn't
        # force re-exporting pages already completed in this run.
        try:
            page_layout.dir.mkdir(parents=True, exist_ok=True)

            if page_layout.assets_dir.exists():
                shutil.rmtree(page_layout.assets_dir)

            ctx = _make_context(layout, page_layout)
            blocks = get_block_children(page_id)
            body = blocks_to_markdown(blocks, get_block_children, ctx)

            markdown = f"# {title}\n\n{body}".rstrip() + "\n"
            page_layout.md_path.write_text(markdown, encoding="utf-8")

            old_rel_path = old_entry.get("rel_path") if old_entry else None

            if old_rel_path and old_rel_path != page_layout.rel_path:
                _remove_stale_dir(NOTES_DIR / old_rel_path, valid_dirs)

            state[page_id] = {
                "title": title,
                "rel_path": page_layout.rel_path,
                "last_edited_time": edited,
            }

            updated += 1

        except Exception as ex:
            failed += 1
            print(f"❌ Failed to export {title!r} ({page_id}): {ex}")
            continue

        finally:
            save_state(state)

    removed = 0

    for page_id in list(state.keys()):
        if page_id in seen_ids:
            continue

        old_rel_path = state[page_id].get("rel_path")

        if old_rel_path:
            _remove_stale_dir(NOTES_DIR / old_rel_path, valid_dirs)

        del state[page_id]
        removed += 1

    save_state(state)

    print("\n==========")
    print("SYNC DONE")
    print("==========")
    print(f"Updated : {updated}")
    print(f"Skipped : {skipped}")
    print(f"Failed  : {failed}")
    print(f"Removed : {removed}")


def main():
    sync_pages()


if __name__ == "__main__":
    main()
