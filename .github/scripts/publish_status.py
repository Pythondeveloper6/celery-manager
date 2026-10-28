"""Determine release eligibility from PyPI, including unchanged-version retries."""
import os
import subprocess
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import urlopen

import tomllib
from packaging.version import Version


def needs_publish(name, version):
    url = f"https://pypi.org/pypi/{quote(name, safe='')}/{quote(version, safe='')}/json"
    try:
        with urlopen(url, timeout=30) as response:
            if response.status != 200:
                raise RuntimeError(f"Unexpected PyPI status: {response.status}")
    except HTTPError as exc:
        if exc.code == 404:
            return True
        raise
    # Network/server errors fail the job: never mistake an outage for absence.
    return False


def main():
    project = tomllib.loads(Path("pyproject.toml").read_text())["project"]
    current = project["version"]
    previous = tomllib.loads(subprocess.check_output(
        ["git", "show", "HEAD^:pyproject.toml"], text=True))["project"]["version"]
    if Version(current) < Version(previous):
        raise SystemExit("Release version must not decrease")
    publish = needs_publish(project["name"], current)
    with open(os.environ["GITHUB_OUTPUT"], "a") as output:
        print(f"version={current}", file=output)
        print(f"publish={str(publish).lower()}", file=output)
    print(f"Version {current}: {'not on PyPI; publish after CI' if publish else 'already on PyPI; skip'}")


if __name__ == "__main__":
    main()
