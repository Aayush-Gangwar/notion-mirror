import base64
import binascii
import os
import re
import time
from pathlib import Path
from typing import Any, Callable, TypeVar
from urllib.parse import unquote_to_bytes, urlparse

import httpx
from dotenv import load_dotenv
from notion_client import Client
from notion_client.errors import APIResponseError

load_dotenv()

token = os.getenv("NOTION_TOKEN")

if not token:
    raise ValueError(
        "NOTION_TOKEN is missing. Add it to your .env file."
    )

notion = Client(auth=token)

T = TypeVar("T")

_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
_MAX_ATTEMPTS = 3

# Regular pages live directly in the workspace or under another page.
# Database rows have parent.type == "database_id" and are skipped for now.
_PAGE_PARENT_TYPES = {"workspace", "page_id"}


def _with_retry(func: Callable[[], T]) -> T:
    """
    Retry a Notion/HTTP call a few times on rate limits or transient errors.
    """
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            return func()

        except APIResponseError as ex:
            if (
                ex.status not in _RETRYABLE_STATUS_CODES
                or attempt == _MAX_ATTEMPTS
            ):
                raise

        except httpx.HTTPError:
            if attempt == _MAX_ATTEMPTS:
                raise

        time.sleep(2 ** (attempt - 1))

    raise RuntimeError("unreachable")  # pragma: no cover


def discover_pages() -> list[dict[str, Any]]:
    """
    Discover every regular page shared with the read-only Notion connection.

    Database rows are excluded. Handles API pagination.
    """
    pages: list[dict[str, Any]] = []
    cursor = None

    while True:
        request: dict[str, Any] = {
            "filter": {
                "property": "object",
                "value": "page",
            },
            "page_size": 100,
        }

        if cursor:
            request["start_cursor"] = cursor

        response = _with_retry(lambda: notion.search(**request))
        pages.extend(response.get("results", []))

        if not response.get("has_more"):
            break

        cursor = response.get("next_cursor")

        if not cursor:
            break

    return [
        page
        for page in pages
        if page.get("parent", {}).get("type") in _PAGE_PARENT_TYPES
    ]


def get_block_children(block_id: str) -> list[dict[str, Any]]:
    """
    Retrieve every immediate child of a page or block.

    Handles API pagination. Recursive traversal is performed
    by the Markdown renderer.
    """
    blocks: list[dict[str, Any]] = []
    cursor = None

    while True:
        request: dict[str, Any] = {
            "block_id": block_id,
            "page_size": 100,
        }

        if cursor:
            request["start_cursor"] = cursor

        response = _with_retry(
            lambda: notion.blocks.children.list(**request)
        )
        blocks.extend(response.get("results", []))

        if not response.get("has_more"):
            break

        cursor = response.get("next_cursor")

        if not cursor:
            break

    return blocks


def get_title(page: dict[str, Any]) -> str:
    """
    Extract a page title from its properties.
    """
    properties = page.get("properties", {})

    for value in properties.values():
        if value.get("type") != "title":
            continue

        title = "".join(
            item.get("plain_text", "")
            for item in value.get("title", [])
        ).strip()

        return title or "Untitled"

    return "Untitled"


_CONTENT_TYPE_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/svg+xml": ".svg",
    "image/webp": ".webp",
    "application/pdf": ".pdf",
    "video/mp4": ".mp4",
    "audio/mpeg": ".mp3",
}


def _guess_extension(url: str, content_type: str | None) -> str:
    """
    Guess a filesystem extension for a downloaded asset.
    """
    suffix = Path(urlparse(url).path).suffix

    if suffix and len(suffix) <= 6:
        return suffix

    main_type = (content_type or "").split(";")[0].strip()

    return _CONTENT_TYPE_EXTENSIONS.get(main_type, "")


_DATA_URI_RE = re.compile(r"^data:([^;,]*)?(;base64)?,(.*)$", re.DOTALL)


def _save_data_uri(data_uri: str, dest_dir: Path, stem: str) -> Path | None:
    """
    Decode an inline `data:` URI (e.g. a pasted image Notion stored
    inline rather than hosting) straight to disk.
    """
    match = _DATA_URI_RE.match(data_uri)

    if not match:
        print(f"⚠ Could not parse data URI for asset {stem}")
        return None

    content_type, is_base64, payload = match.groups()

    try:
        data = (
            base64.b64decode(payload)
            if is_base64
            else unquote_to_bytes(payload)
        )
    except (binascii.Error, ValueError) as ex:
        print(f"⚠ Failed to decode data URI for asset {stem} -> {ex}")
        return None

    extension = _CONTENT_TYPE_EXTENSIONS.get((content_type or "").strip(), "")

    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / f"{stem}{extension}"
    dest_path.write_bytes(data)

    return dest_path


def download_file(url: str, dest_dir: Path, stem: str) -> Path | None:
    """
    Download a Notion asset into dest_dir, returning the path written.

    Returns None if the download fails, so callers can fall back to
    linking the original (possibly expiring) URL instead.
    """
    if url.startswith("data:"):
        return _save_data_uri(url, dest_dir, stem)

    try:
        def _do_download() -> httpx.Response:
            with httpx.Client(follow_redirects=True, timeout=30) as client:
                response = client.get(url)
                response.raise_for_status()
                return response

        response = _with_retry(_do_download)

    except httpx.HTTPError as ex:
        print(f"⚠ Failed to download asset {url} -> {ex}")
        return None

    extension = _guess_extension(
        url,
        response.headers.get("content-type"),
    )

    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / f"{stem}{extension}"

    dest_path.write_bytes(response.content)

    return dest_path
