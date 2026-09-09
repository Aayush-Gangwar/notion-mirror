import dataclasses
from typing import Any, Callable


Block = dict[str, Any]
ChildrenLoader = Callable[[str], list[Block]]

# (url, stem) -> Markdown-ready relative path, or None to fall back to `url`.
AssetDownloader = Callable[[str, str], "str | None"]

# page_id -> (title, Markdown-ready relative link), or None if unresolvable.
PageLinkResolver = Callable[[str], "tuple[str, str] | None"]


@dataclasses.dataclass
class RenderContext:
    """
    Per-page rendering context.

    Keeps markdown.py decoupled from the Notion client and the on-disk
    page tree: callers resolve URLs/links to their final Markdown form
    and hand the resolvers in, which also makes rendering easy to unit
    test without any network access.
    """

    download_asset: AssetDownloader
    resolve_page_link: PageLinkResolver


def escape_markdown(text: str) -> str:
    """
    Escape characters that could unintentionally alter Markdown.

    Newlines are preserved.
    """
    replacements = {
        "\\": "\\\\",
        "*": "\\*",
        "_": "\\_",
        "[": "\\[",
        "]": "\\]",
        "<": "\\<",
        ">": "\\>",
    }

    for old, new in replacements.items():
        text = text.replace(old, new)

    return text


def escape_table_cell(text: str) -> str:
    """
    Escape text for use inside a Markdown table cell.
    """
    return (
        text.replace("\\", "\\\\")
        .replace("|", "\\|")
        .replace("\n", "<br>")
    )


def normalize_language(language: str) -> str:
    """
    Normalize common Notion language names for fenced code blocks.
    """
    aliases = {
        "plain text": "text",
        "c++": "cpp",
        "c#": "csharp",
        "f#": "fsharp",
        "java/c/c++/c#": "text",
        "shell": "bash",
        "markup": "html",
        "typescript": "typescript",
        "javascript": "javascript",
    }

    normalized = language.strip().lower()
    return aliases.get(normalized, normalized.replace(" ", "-"))


def apply_annotations(
    text: str,
    annotations: dict[str, Any],
) -> str:
    """
    Convert Notion rich-text annotations to Markdown/HTML.
    """
    if not text:
        return ""

    if annotations.get("code"):
        text = "`" + text.replace("`", "\\`") + "`"
    else:
        text = escape_markdown(text)

    if annotations.get("bold"):
        text = f"**{text}**"

    if annotations.get("italic"):
        text = f"*{text}*"

    if annotations.get("strikethrough"):
        text = f"~~{text}~~"

    if annotations.get("underline"):
        text = f"<u>{text}</u>"

    return text


def rich_text_to_markdown(
    rich_text: list[dict[str, Any]],
) -> str:
    """
    Convert a Notion rich_text array to Markdown.
    """
    output: list[str] = []

    for item in rich_text:
        item_type = item.get("type")
        plain_text = item.get("plain_text", "")
        annotations = item.get("annotations", {})
        rendered = apply_annotations(plain_text, annotations)

        if item_type == "equation":
            expression = item.get("equation", {}).get(
                "expression",
                plain_text,
            )
            rendered = f"${expression}$"

        href = item.get("href")

        if href and not annotations.get("code"):
            rendered = f"[{rendered}]({href})"

        output.append(rendered)

    return "".join(output)


def get_text(block: Block) -> str:
    """
    Extract Markdown-formatted rich text from a text block.
    """
    block_type = block.get("type", "")
    payload = block.get(block_type, {})

    return rich_text_to_markdown(
        payload.get("rich_text", [])
    )


def get_media_url(block: Block) -> str | None:
    """
    Extract an external or Notion-hosted URL from a media block.
    """
    block_type = block.get("type", "")
    payload = block.get(block_type, {})
    source_type = payload.get("type")

    if source_type == "external":
        return payload.get("external", {}).get("url")

    if source_type == "file":
        return payload.get("file", {}).get("url")

    return None


def get_caption(block: Block) -> str:
    block_type = block.get("type", "")
    payload = block.get(block_type, {})

    return rich_text_to_markdown(
        payload.get("caption", [])
    )


def indent_markdown(markdown: str, spaces: int = 2) -> str:
    """
    Indent nested Markdown while retaining empty lines.
    """
    if not markdown:
        return ""

    prefix = " " * spaces

    return "\n".join(
        prefix + line if line else prefix
        for line in markdown.splitlines()
    )


def plain_text_from_cell(cell: list[dict[str, Any]]) -> str:
    """
    Render table-cell rich text and make it safe for a pipe table.
    """
    return escape_table_cell(
        rich_text_to_markdown(cell)
    )


def render_table(
    block: Block,
    children_loader: ChildrenLoader,
) -> str:
    """
    Convert a Notion table and its table_row children to Markdown.
    """
    payload = block.get("table", {})
    rows = children_loader(block["id"])
    rendered_rows: list[list[str]] = []

    for row in rows:
        if row.get("type") != "table_row":
            continue

        cells = row.get("table_row", {}).get("cells", [])

        rendered_rows.append(
            [
                plain_text_from_cell(cell)
                for cell in cells
            ]
        )

    if not rendered_rows:
        return ""

    width = max(
        len(row)
        for row in rendered_rows
    )

    for row in rendered_rows:
        row.extend([""] * (width - len(row)))

    has_column_header = payload.get(
        "has_column_header",
        False,
    )

    if has_column_header:
        header = rendered_rows[0]
        body = rendered_rows[1:]
    else:
        header = ["" for _ in range(width)]
        body = rendered_rows

    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join(["---"] * width) + " |",
    ]

    for row in body:
        lines.append(
            "| " + " | ".join(row) + " |"
        )

    return "\n".join(lines)


def render_children(
    block: Block,
    children_loader: ChildrenLoader,
    ctx: "RenderContext | None",
    depth: int,
) -> str:
    """
    Recursively render a block's nested children.
    """
    if not block.get("has_children"):
        return ""

    if block.get("type") in {
        "table",
        "child_page",
        "child_database",
    }:
        return ""

    children = children_loader(block["id"])

    return blocks_to_markdown(
        children,
        children_loader,
        ctx,
        depth=depth + 1,
    )


def render_container(
    block: Block,
    children_loader: ChildrenLoader,
    ctx: "RenderContext | None",
    depth: int,
) -> str:
    """
    Render a pass-through container block (columns, synced blocks) by
    flowing its children as if they belonged to the parent directly.
    """
    children = children_loader(block["id"])

    return blocks_to_markdown(children, children_loader, ctx, depth)


def render_list_item(
    block: Block,
    marker: str,
    children_loader: ChildrenLoader,
    ctx: "RenderContext | None",
    depth: int,
) -> str:
    """
    Render list items and recursively indent nested children.
    """
    text = get_text(block)
    line = f"{marker} {text}".rstrip()

    children = render_children(
        block,
        children_loader,
        ctx,
        depth,
    )

    if children:
        line += "\n" + indent_markdown(children, 4)

    return line


def render_details(
    title: str,
    children: str,
    heading_level: int | None = None,
) -> str:
    """
    Render a toggle (or toggleable heading) as a collapsible section.

    There is no plain-Markdown equivalent, so this uses raw HTML, which
    GitHub renders correctly; Notion will show the tags literally if the
    Markdown is pasted back in.
    """
    summary = f"{'#' * heading_level} {title}".strip() if heading_level else title
    body = f"\n\n{children}\n\n" if children else "\n\n"

    return f"<details>\n<summary>{summary}</summary>{body}</details>"


def _resolve_media_link(
    url: str,
    block_id: str,
    ctx: "RenderContext | None",
) -> str:
    if ctx is None:
        return url

    resolved = ctx.download_asset(url, block_id)

    return resolved or url


def render_block(
    block: Block,
    children_loader: ChildrenLoader,
    ctx: "RenderContext | None" = None,
    depth: int = 0,
) -> str:
    """
    Convert one Notion block to GitHub-flavored Markdown.
    """
    block_type = block.get("type", "")
    payload = block.get(block_type, {})

    if block_type == "paragraph":
        text = get_text(block)
        children = render_children(
            block,
            children_loader,
            ctx,
            depth,
        )

        if text and children:
            return f"{text}\n\n{children}"

        return text or children

    if block_type == "heading_1":
        text = get_text(block)

        if payload.get("is_toggleable"):
            children = render_children(
                block,
                children_loader,
                ctx,
                depth,
            )
            return render_details(text, children, 1)

        return f"# {text}".rstrip()

    if block_type == "heading_2":
        text = get_text(block)

        if payload.get("is_toggleable"):
            children = render_children(
                block,
                children_loader,
                ctx,
                depth,
            )
            return render_details(text, children, 2)

        return f"## {text}".rstrip()

    if block_type == "heading_3":
        text = get_text(block)

        if payload.get("is_toggleable"):
            children = render_children(
                block,
                children_loader,
                ctx,
                depth,
            )
            return render_details(text, children, 3)

        return f"### {text}".rstrip()

    if block_type == "bulleted_list_item":
        return render_list_item(
            block,
            "-",
            children_loader,
            ctx,
            depth,
        )

    if block_type == "numbered_list_item":
        return render_list_item(
            block,
            "1.",
            children_loader,
            ctx,
            depth,
        )

    if block_type == "to_do":
        checked = payload.get("checked", False)
        marker = "- [x]" if checked else "- [ ]"

        text = get_text(block)
        line = f"{marker} {text}".rstrip()

        children = render_children(
            block,
            children_loader,
            ctx,
            depth,
        )

        if children:
            line += "\n" + indent_markdown(children, 4)

        return line

    if block_type == "code":
        language = normalize_language(
            payload.get("language", "text")
        )
        code = "".join(
            item.get("plain_text", "")
            for item in payload.get("rich_text", [])
        )
        caption = rich_text_to_markdown(
            payload.get("caption", [])
        )

        fence = "```"

        if "```" in code:
            fence = "````"

        rendered = (
            f"{fence}{language}\n"
            f"{code}\n"
            f"{fence}"
        )

        if caption:
            rendered += f"\n\n*{caption}*"

        return rendered

    if block_type == "quote":
        text = get_text(block)
        children = render_children(
            block,
            children_loader,
            ctx,
            depth,
        )

        combined = text

        if children:
            combined = f"{combined}\n\n{children}".strip()

        return "\n".join(
            f"> {line}" if line else ">"
            for line in combined.splitlines()
        )

    if block_type == "callout":
        icon = payload.get("icon") or {}
        icon_text = ""

        if icon.get("type") == "emoji":
            icon_text = icon.get("emoji", "") + " "

        text = icon_text + get_text(block)
        children = render_children(
            block,
            children_loader,
            ctx,
            depth,
        )

        combined = text

        if children:
            combined = f"{text}\n\n{children}"

        return "\n".join(
            f"> {line}" if line else ">"
            for line in combined.splitlines()
        )

    if block_type == "toggle":
        title = get_text(block)
        children = render_children(
            block,
            children_loader,
            ctx,
            depth,
        )

        return render_details(
            title,
            children,
        )

    if block_type == "divider":
        return "---"

    if block_type == "equation":
        expression = payload.get("expression", "")
        return f"$$\n{expression}\n$$"

    if block_type == "table":
        return render_table(block, children_loader)

    if block_type == "image":
        url = get_media_url(block)
        caption = get_caption(block)
        alt_text = caption or "image"

        if not url:
            return "<!-- Image URL unavailable -->"

        link = _resolve_media_link(url, block["id"], ctx)
        rendered = f"![{alt_text}]({link})"

        if caption:
            rendered += f"\n\n*{caption}*"

        return rendered

    if block_type == "file":
        url = get_media_url(block)
        caption = get_caption(block)
        name = caption or "Download file"

        if not url:
            return "<!-- File URL unavailable -->"

        link = _resolve_media_link(url, block["id"], ctx)
        return f"[{name}]({link})"

    if block_type == "pdf":
        url = get_media_url(block)
        caption = get_caption(block)
        name = caption or "View PDF"

        if not url:
            return "<!-- PDF URL unavailable -->"

        link = _resolve_media_link(url, block["id"], ctx)
        return f"[{name}]({link})"

    if block_type == "video":
        url = get_media_url(block)
        caption = get_caption(block)
        name = caption or "View video"

        if not url:
            return "<!-- Video URL unavailable -->"

        link = _resolve_media_link(url, block["id"], ctx)
        return f"[{name}]({link})"

    if block_type == "audio":
        url = get_media_url(block)
        caption = get_caption(block)
        name = caption or "Play audio"

        if not url:
            return "<!-- Audio URL unavailable -->"

        link = _resolve_media_link(url, block["id"], ctx)
        return f"[{name}]({link})"

    if block_type == "bookmark":
        url = payload.get("url")
        caption = get_caption(block)
        name = caption or url or "Bookmark"

        return f"[{name}]({url})" if url else ""

    if block_type == "embed":
        url = payload.get("url")
        caption = get_caption(block)
        name = caption or url or "Embed"

        return f"[{name}]({url})" if url else ""

    if block_type == "link_preview":
        url = payload.get("url")

        return f"[{url}]({url})" if url else ""

    if block_type == "child_page":
        title = payload.get("title", "Untitled")
        page_id = block.get("id", "")
        link = None

        if ctx is not None:
            resolved = ctx.resolve_page_link(page_id)

            if resolved:
                _, link = resolved

        if not link:
            link = f"https://www.notion.so/{page_id.replace('-', '')}"

        return f"[{title}]({link})"

    if block_type == "child_database":
        # Database rows are out of scope for this sync; skip silently.
        return ""

    if block_type == "link_to_page":
        target_type = payload.get("type", "")
        target_id = payload.get(target_type, "")
        title, link = None, None

        if ctx is not None and target_id:
            resolved = ctx.resolve_page_link(target_id)

            if resolved:
                title, link = resolved

        if not target_id:
            return ""

        if not link:
            link = f"https://www.notion.so/{target_id.replace('-', '')}"

        return f"[{title or 'Linked page'}]({link})"

    if block_type in {"column_list", "column", "synced_block"}:
        return render_container(block, children_loader, ctx, depth)

    if block_type in {"table_of_contents", "breadcrumb"}:
        # No static Markdown equivalent for these live Notion widgets.
        return ""

    print(
        f"⚠ Unsupported block type: {block_type} "
        f"({block.get('id')})"
    )

    return f"<!-- Unsupported block: {block_type} -->"


_LIST_TYPES = {"bulleted_list_item", "numbered_list_item", "to_do"}


def blocks_to_markdown(
    blocks: list[Block],
    children_loader: ChildrenLoader,
    ctx: "RenderContext | None" = None,
    depth: int = 0,
) -> str:
    """
    Convert a sibling list of blocks into Markdown.

    Consecutive list items of the same kind are joined tightly (single
    newline); everything else gets a blank line between blocks, matching
    normal Markdown/Notion export spacing.
    """
    rendered: list[str] = []
    previous_type: str | None = None

    for block in blocks:
        text = render_block(block, children_loader, ctx, depth)
        block_type = block.get("type")

        if not text:
            previous_type = block_type
            continue

        if (
            rendered
            and previous_type == block_type
            and block_type in _LIST_TYPES
        ):
            rendered.append("\n" + text)
        elif rendered:
            rendered.append("\n\n" + text)
        else:
            rendered.append(text)

        previous_type = block_type

    return "".join(rendered)
