"""The container entrypoint's environment, which is what makes PUID work.

Running as a non-root PUID is a supported, .env-driven choice, and it is worth
testing at this level because the way it breaks is invisible from Python: the
process starts, serves and scans perfectly, and only the caches quietly stop
persisting because they point somewhere the dropped uid cannot write.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ENTRYPOINT = Path(__file__).resolve().parents[1] / "docker" / "entrypoint.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("sh") is None or not ENTRYPOINT.is_file(),
    reason="needs a POSIX shell and the entrypoint script",
)


def entrypoint_env(tmp_path, **overrides) -> dict[str, str]:
    """Run the entrypoint as far as ``start()`` and report the environment.

    ``MUSICDROME_DROPPED`` is what the script sets before re-executing itself
    through gosu, so it selects the second pass — which is precisely the one
    whose environment matters, because it is the pass running as PUID rather
    than as root. ``uvicorn`` is stubbed so the script stops there.
    """
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir(parents=True)
    stub = stub_dir / "uvicorn"
    # The umask is reported from inside the stub because that is the only place
    # it can be observed: start() reaches uvicorn through exec, so nothing in
    # the script runs after it to be inspected.
    stub.write_text("#!/bin/sh\necho \"MUSICDROME_UMASK=$(umask)\"\nenv\n")
    stub.chmod(0o755)

    result = subprocess.run(
        ["sh", str(ENTRYPOINT)],
        capture_output=True,
        text=True,
        env={
            "PATH": f"{stub_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "MUSICDROME_DROPPED": "1",
            # What the container actually starts with, and what gosu leaves
            # untouched on the way through. Seeding it here is what makes these
            # tests a reproduction rather than a check that nothing set it.
            "HOME": "/root",
            **overrides,
        },
    )
    assert result.returncode == 0, f"entrypoint failed: {result.stderr}"
    return dict(
        line.split("=", 1) for line in result.stdout.splitlines() if "=" in line
    )


def test_the_cache_does_not_follow_root_home_through_the_drop(tmp_path):
    """gosu does not set HOME (tianon/gosu#3, #14), so it stays root's.

    yt-dlp reads XDG_CACHE_HOME and falls back to ~/.cache, so left alone its
    player and EJS script cache lands in /root/.cache — which uid 1000 cannot
    write. Nothing fails loudly; the cache just stops persisting, and every
    boot re-fetches and re-solves what it already had.
    """
    env = entrypoint_env(tmp_path)

    assert env["HOME"] == "/config"
    assert env["XDG_CACHE_HOME"] == "/config/.cache"


def test_the_cache_lands_in_the_mounted_volume(tmp_path):
    """/config is the volume, so the cache now survives a restart too."""
    env = entrypoint_env(tmp_path)

    for name in ("HOME", "XDG_CACHE_HOME", "DENO_DIR"):
        value = env.get(name)
        if value is None:
            continue  # DENO_DIR comes from the image, not the script
        assert value.startswith("/config"), f"{name}={value} escapes the volume"


def test_the_environment_does_not_depend_on_who_we_run_as(tmp_path):
    """A cache path that changes with PUID is a cache that moves when you
    switch, stranding whatever the previous uid wrote."""
    as_root = entrypoint_env(tmp_path / "root", PUID="0", PGID="0")
    as_user = entrypoint_env(tmp_path / "user", PUID="1000", PGID="1000")

    assert as_root["HOME"] == as_user["HOME"]
    assert as_root["XDG_CACHE_HOME"] == as_user["XDG_CACHE_HOME"]


def test_umask_defaults_to_group_and_world_readable(tmp_path):
    """022 is what lets Plex, Navidrome and Jellyfin read what we download."""
    assert entrypoint_env(tmp_path)["MUSICDROME_UMASK"] == "0022"


def test_umask_is_configurable(tmp_path):
    """002 for a library shared with a group, per .env.example."""
    assert entrypoint_env(tmp_path, UMASK="002")["MUSICDROME_UMASK"] == "0002"
