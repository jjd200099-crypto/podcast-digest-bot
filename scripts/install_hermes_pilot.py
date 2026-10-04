"""Provision only an explicit NEW pilot directory; never touch the bot's venv.

Usage: python scripts/install_hermes_pilot.py /absolute/new/pilot-directory
The pinned source archive is installed editable as required by Hermes upstream.
No shell installer, gateway, service, cron, or production setting is invoked.
"""
import json
import subprocess
import sys
import tarfile
import urllib.request
import venv
from pathlib import Path

COMMIT = "d177b119e9c56c9ddc0b7379ffce52341ec06584"


def main():
    target = Path(sys.argv[1])
    if not target.is_absolute() or target.exists():
        raise ValueError("Choose a NEW absolute pilot directory")
    target.mkdir(parents=True, mode=0o700)
    archive = target / "source.tar.gz"
    url = f"https://codeload.github.com/NousResearch/hermes-agent/tar.gz/{COMMIT}"
    with urllib.request.urlopen(url, timeout=120) as response, archive.open("wb") as out:
        while block := response.read(1024 * 1024):
            out.write(block)
    with tarfile.open(archive) as bundle:
        bundle.extractall(target, filter="data")
    source = target / ("hermes-agent-" + COMMIT)
    env = target / "venv"
    venv.EnvBuilder(with_pip=True).create(env)
    python = env / "bin/python"
    subprocess.run([str(python), "-m", "pip", "install", "--quiet", "--disable-pip-version-check",
                    "-e", str(source)], check=True)
    locked = subprocess.check_output([str(python), "-m", "pip", "freeze"], text=True)
    (target / "installed.txt").write_text(locked)
    print(json.dumps({"python": str(python), "commit": COMMIT}), flush=True)


if __name__ == "__main__":
    main()
