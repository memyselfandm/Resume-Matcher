"""The exported parse-check vocabulary stays in sync with the engine."""

from __future__ import annotations

import json
from pathlib import Path

from app.scripts.export_check_ids import (
    DEFAULT_OUTPUT,
    build_vocabulary,
    is_current,
    main,
)
from app.services.ats_parse.messages_en import PASS_MESSAGES
from app.services.ats_parse.templates import TEMPLATE_IDS


def test_committed_vocabulary_matches_the_engine() -> None:
    # The frontend locale-coverage test reads this file; regenerate it with
    # `uv run python -m app.scripts.export_check_ids` when a check id changes.
    assert json.loads(DEFAULT_OUTPUT.read_text(encoding="utf-8")) == build_vocabulary()


def test_vocabulary_lists_every_catalogued_check_and_template() -> None:
    vocabulary = build_vocabulary()
    assert vocabulary["check_ids"] == sorted(PASS_MESSAGES)
    assert vocabulary["template_ids"] == list(TEMPLATE_IDS)
    assert vocabulary["field_statuses"] == ["found", "garbled", "missing", "not_rendered", "hidden"]
    assert vocabulary["profile_ids"] == [
        "workday",
        "taleo",
        "successfactors",
        "icims",
        "greenhouse",
        "lever",
    ]


def test_check_mode_reports_a_stale_file(tmp_path: Path) -> None:
    output = tmp_path / "ids.json"
    output.write_text("{}\n", encoding="utf-8")
    assert main(["--output", str(output), "--check"]) == 1
    assert main(["--output", str(output)]) == 0
    assert main(["--output", str(output), "--check"]) == 0
    # Reformatting (e.g. Prettier) does not make the file stale.
    output.write_text(json.dumps(build_vocabulary(), separators=(",", ":")), encoding="utf-8")
    assert is_current(output)
    assert json.loads(output.read_text(encoding="utf-8")) == build_vocabulary()
