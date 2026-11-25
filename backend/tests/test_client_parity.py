"""Drift guard: `backend/app/simulator_client.py` must stay byte-identical to
`verifier/simulator_client.py`.

`verifier/simulator_client.py` is the reference implementation that the 49-check
verification suite exercises. The backend uses a *copy* of it (the plan forbids
restructuring the verifier), so the two can silently drift. This test fails the
build when that happens.

If the client genuinely needs a fix: land it in `verifier/simulator_client.py`
first, re-run the 49-check suite (`docker compose exec -T client-verifier python
verify_simulator.py`), then re-copy it here.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_DIR.parent

# The reference client lives in ``verifier/`` in the repo. Inside the container
# the image bakes a copy to ``/app/reference/`` so the drift guard still works
# without a host bind-mount (this project's path contains spaces, which Docker
# Desktop refuses to mount).
_IN_CONTAINER_REFERENCE = BACKEND_DIR / "reference" / "simulator_client.py"
_REPO_REFERENCE = PROJECT_ROOT / "verifier" / "simulator_client.py"

REFERENCE_CLIENT = (
    _IN_CONTAINER_REFERENCE if _IN_CONTAINER_REFERENCE.is_file()
    else _REPO_REFERENCE
)
BACKEND_CLIENT = BACKEND_DIR / "app" / "simulator_client.py"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_reference_client_exists() -> None:
    assert REFERENCE_CLIENT.is_file(), f"missing reference client: {REFERENCE_CLIENT}"


def test_backend_client_exists() -> None:
    assert BACKEND_CLIENT.is_file(), f"missing backend client: {BACKEND_CLIENT}"


def test_client_copies_are_byte_identical() -> None:
    ref_hash = _sha256(REFERENCE_CLIENT)
    back_hash = _sha256(BACKEND_CLIENT)
    assert ref_hash == back_hash, (
        "simulator_client.py drift detected.\n"
        f"  verifier/simulator_client.py  sha256={ref_hash}\n"
        f"  backend/app/simulator_client.py sha256={back_hash}\n"
        "Fix the reference copy first, re-run the 49-check verification suite, "
        "then re-copy it into backend/app/."
    )


@pytest.mark.parametrize("filename", ["simulator_client.py"])
def test_client_files_have_identical_content(filename: str) -> None:
    assert REFERENCE_CLIENT.read_text(encoding="utf-8") == BACKEND_CLIENT.read_text(
        encoding="utf-8"
    ), f"{filename} content differs between verifier/ and backend/app/"
