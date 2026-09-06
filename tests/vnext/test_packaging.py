"""Packaging and build checks that run real commands — O01, O02, O03, O14.

These are **opt-in**: they shell out to `uv` and `docker`, take minutes, and
need a working daemon. The default suite stays hermetic and fast, which is what
makes it safe to run constantly.

Enable with `LOOP_PACKAGING_TESTS=1 pytest tests/vnext/test_packaging.py`, or
run `scripts/acceptance_run.sh`, which sets it.

What each one protects is a failure that unit tests structurally cannot see: a
wheel that omits a package imports fine from the source tree and fails
everywhere else, and an image that runs as root looks identical until it writes
to a bind mount.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.skipif(
    os.environ.get("LOOP_PACKAGING_TESTS") != "1",
    reason="set LOOP_PACKAGING_TESTS=1 to run the real build checks")


def _run(command: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True,
                          cwd=kwargs.pop("cwd", REPO), timeout=900, **kwargs)


def _require(tool: str) -> str:
    path = shutil.which(tool)
    if path is None:
        pytest.skip(f"{tool} is not installed")
    return path


# --------------------------------------------------------------------------- #
# O01 — the lockfile installs
# --------------------------------------------------------------------------- #
def test_o01_a_locked_sync_succeeds():
    _require("uv")
    result = _run(["uv", "sync", "--locked", "--extra", "dev"])

    assert result.returncode == 0, result.stderr


def test_o01_the_lockfile_is_in_sync_with_pyproject():
    """`--locked` fails rather than silently re-resolving, which is the point."""
    _require("uv")
    result = _run(["uv", "lock", "--check"])

    assert result.returncode == 0, result.stdout + result.stderr


# --------------------------------------------------------------------------- #
# O02 — the wheel works outside the checkout
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def installed_wheel(tmp_path_factory) -> Path:
    """Build a wheel and install it into a venv outside the source tree."""
    _require("uv")
    workdir = tmp_path_factory.mktemp("wheelcheck")

    build = _run(["uv", "build", "--wheel", "--out-dir", str(workdir / "dist")])
    assert build.returncode == 0, build.stderr

    wheels = list((workdir / "dist").glob("*.whl"))
    assert len(wheels) == 1, wheels

    venv = _run(["uv", "venv", "--python", "3.12", str(workdir / ".venv")])
    assert venv.returncode == 0, venv.stderr

    python = workdir / ".venv" / "bin" / "python"
    install = _run(["uv", "pip", "install", "--python", str(python),
                    str(wheels[0])])
    assert install.returncode == 0, install.stderr
    return python


NEW_MODULES = (
    "loop.core.settings", "loop.ops.doctor", "loop.ops.backup",
    "loop.ops.retention", "loop.ops.perf", "loop.api.service",
    "loop.capabilities.travel.trip", "loop.capabilities.weather.bundle",
    "loop.services.learning", "loop.agents.seeker", "loop.ai.spend",
    "loop.runtime.routines", "loop.db.migrations", "loop.vault.gateway",
)


def test_o02_every_module_imports_from_the_installed_wheel(installed_wheel):
    """Run from `/` so the source tree cannot satisfy the import."""
    script = ("import importlib\n"
              + "\n".join(f"importlib.import_module({m!r})" for m in NEW_MODULES)
              + "\nprint('ok')")
    result = _run([str(installed_wheel), "-c", script], cwd="/")

    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout


def test_o02_pydantic_settings_is_installed_with_it(installed_wheel):
    result = _run([str(installed_wheel), "-c",
                   "import pydantic_settings; print('ok')"], cwd="/")
    assert result.returncode == 0, result.stderr


def test_o02_the_web_assets_ship_with_the_wheel(installed_wheel):
    """A wheel without templates imports fine and fails at the first request."""
    script = ("from pathlib import Path\n"
              "import web\n"
              "print((Path(web.__file__).parent / 'templates' / "
              "'index.html').exists())")
    result = _run([str(installed_wheel), "-c", script], cwd="/")

    assert result.stdout.strip() == "True", result.stdout + result.stderr


def test_o02_the_source_tree_is_not_on_the_path(installed_wheel):
    script = ("import sys; from pathlib import Path; "
              f"print(any(Path(p).resolve() == Path({str(REPO)!r}).resolve() "
              "for p in sys.path))")
    result = _run([str(installed_wheel), "-c", script], cwd="/")
    assert result.stdout.strip() == "False"


# --------------------------------------------------------------------------- #
# O03 — the image
# --------------------------------------------------------------------------- #
IMAGE = "loop-acceptance:test"


@pytest.fixture(scope="module")
def built_image() -> str:
    _require("docker")
    if _run(["docker", "info"]).returncode != 0:
        pytest.skip("the docker daemon is not running")
    result = _run(["docker", "build", "-t", IMAGE, "."])
    assert result.returncode == 0, result.stderr[-4000:]
    return IMAGE


def _in_image(image: str, command: list[str]) -> subprocess.CompletedProcess[str]:
    return _run(["docker", "run", "--rm", image, *command])


def test_o03_the_image_builds(built_image):
    assert built_image == IMAGE


def test_o03_the_container_runs_as_a_non_root_user(built_image):
    result = _in_image(built_image, ["id", "-u"])
    assert result.stdout.strip() != "0", result.stdout


def test_o03_the_loop_package_is_present_in_the_image(built_image):
    result = _in_image(built_image, [
        "python", "-c",
        "import loop.ops.doctor, loop.capabilities.travel.trip; print('ok')"])

    assert "ok" in result.stdout, result.stderr


def test_o03_the_legacy_core_directory_is_present(built_image):
    result = _in_image(built_image, ["sh", "-c", "test -d /app/core && echo yes"])
    assert "yes" in result.stdout


def test_o03_no_env_file_is_baked_in(built_image):
    result = _in_image(built_image, ["sh", "-c", "test -f /app/.env && echo yes || echo no"])
    assert result.stdout.strip() == "no"


def test_o03_the_data_directory_is_writable_by_the_run_user(built_image):
    result = _in_image(built_image, ["sh", "-c", "touch /app/data/probe && echo ok"])
    assert "ok" in result.stdout, result.stderr


# --------------------------------------------------------------------------- #
# O14 — the checks that gate a release
# --------------------------------------------------------------------------- #
def test_o14_the_hermetic_suite_passes():
    result = _run([sys.executable, "-m", "pytest", "tests/", "-p",
                   "no:cacheprovider", "-q", "--no-header"])
    assert result.returncode == 0, result.stdout[-4000:]


def test_o14_lint_passes():
    result = _run([sys.executable, "-m", "ruff", "check", "loop", "tests"])
    assert result.returncode == 0, result.stdout


def test_o14_types_pass():
    result = _run([sys.executable, "-m", "mypy", "loop"])
    assert result.returncode == 0, result.stdout


def test_o14_the_acceptance_matrix_totals_are_recomputed():
    """The summary is derived from the rows, so a stale total fails loudly."""
    result = _run([sys.executable, "scripts/acceptance_summary.py"])
    assert result.returncode == 0, result.stdout + result.stderr
