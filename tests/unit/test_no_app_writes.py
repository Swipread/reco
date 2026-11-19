"""The reco never writes to schema `app`: its role is read-only there (backend migration V5)."""

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src"
APP_WRITE = re.compile(r"\b(INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+app\.", re.IGNORECASE)


def test_no_insert_update_delete_on_app_in_src():
    offenders = [
        f"{path.relative_to(SRC)}: {match.group(0)}"
        for path in sorted(SRC.rglob("*.py"))
        for match in APP_WRITE.finditer(path.read_text())
    ]
    assert offenders == []
