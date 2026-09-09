import json
import os
import tempfile
from pathlib import Path

STATE_DIR = Path(".notion-sync")
STATE_FILE = STATE_DIR / "state.json"

STATE_DIR.mkdir(exist_ok=True)


def load_state() -> dict:
    """
    Load the sync state, tolerating a missing or corrupted file.

    A state.json that failed to write cleanly (process killed mid-write,
    disk full, manual edit gone wrong) should trigger a full resync on
    the next run rather than crashing every run from then on.
    """
    if not STATE_FILE.exists():
        return {}

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)

    except (json.JSONDecodeError, OSError) as ex:
        print(
            f"⚠ {STATE_FILE} is unreadable ({ex}); "
            "starting from an empty state (full resync)."
        )
        return {}

    if not isinstance(state, dict):
        print(
            f"⚠ {STATE_FILE} did not contain a JSON object; "
            "starting from an empty state (full resync)."
        )
        return {}

    return state


def save_state(state: dict) -> None:
    """
    Write state.json atomically.

    Writes to a temporary file in the same directory, then swaps it into
    place with os.replace - atomic on both POSIX and Windows, and only
    atomic because the temp file lives on the same filesystem as the
    destination. A process killed mid-write can therefore never leave a
    truncated/corrupted state.json behind: either the old file or the
    fully-written new one exists, never something in between.
    """
    fd, tmp_path = tempfile.mkstemp(
        dir=STATE_DIR,
        prefix="state-",
        suffix=".json.tmp",
    )

    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)

        os.replace(tmp_path, STATE_FILE)

    except BaseException:
        Path(tmp_path).unlink(missing_ok=True)
        raise
