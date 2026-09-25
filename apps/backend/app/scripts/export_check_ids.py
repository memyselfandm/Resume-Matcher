"""Export the parse-check vocabulary the web UI must translate.

The parse-check report carries ids and parameters only; the frontend renders
every user-facing string from its locale files. This script writes the ids the
engine can emit (check ids, statuses, severities, field kinds, template ids,
heuristic profile ids, per-template outcomes) to a JSON file that a frontend
test reads to assert every id has a message in every locale.

Run from ``apps/backend``::

    uv run python -m app.scripts.export_check_ids          # rewrite the file
    uv run python -m app.scripts.export_check_ids --check  # exit 1 when stale

The check compares parsed JSON, so the file may be reformatted (for example
by the frontend's Prettier run) without becoming stale.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, get_args

from app.services.ats_parse.messages_en import FAIL_MESSAGES
from app.services.ats_parse.own_output import TemplateError, TemplateStatus
from app.services.ats_parse.profiles import PROFILES
from app.services.ats_parse.report import (
    Category,
    CheckStatus,
    Extractability,
    FieldStatus,
    Severity,
)
from app.services.ats_parse.templates import ALL_FIELD_KINDS, TEMPLATE_IDS

REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_OUTPUT = REPO_ROOT / "apps" / "frontend" / "tests" / "fixtures" / "ats-parse-check-ids.json"


def build_vocabulary() -> dict[str, Any]:
    """Collect every id the parse-check report and response can contain."""
    return {
        "check_ids": sorted(FAIL_MESSAGES),
        "categories": list(get_args(Category)),
        "severities": list(get_args(Severity)),
        "check_statuses": list(get_args(CheckStatus)),
        "extractability": list(get_args(Extractability)),
        "field_statuses": list(get_args(FieldStatus)),
        "field_kinds": sorted(ALL_FIELD_KINDS),
        "profile_ids": [profile.id for profile in PROFILES],
        "template_ids": list(TEMPLATE_IDS),
        "template_statuses": list(get_args(TemplateStatus)),
        "template_errors": list(get_args(TemplateError)),
    }


def render_vocabulary() -> str:
    """Serialize the vocabulary deterministically, with a trailing newline."""
    return json.dumps(build_vocabulary(), indent=2, ensure_ascii=False) + "\n"


def is_current(path: Path) -> bool:
    """Whether ``path`` holds the current vocabulary (formatting is ignored)."""
    try:
        return json.loads(path.read_text(encoding="utf-8")) == build_vocabulary()
    except (OSError, ValueError):
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit with status 1 when the file differs instead of rewriting it",
    )
    args = parser.parse_args(argv)
    if args.check:
        if not is_current(args.output):
            print(f"{args.output} is stale; run app.scripts.export_check_ids", file=sys.stderr)
            return 1
        return 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render_vocabulary(), encoding="utf-8")
    print(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
