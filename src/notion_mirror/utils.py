import re


def safe_filename(name: str) -> str:
    """
    Convert page title into a valid filename.
    """

    name = re.sub(
        r'[<>:"/\\|?*]',
        "_",
        name,
    )

    return name.strip().rstrip(".")