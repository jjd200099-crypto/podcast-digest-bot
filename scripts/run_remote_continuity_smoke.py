"""Upload a temporary code overlay and run read-only live replay via Railway SSH.

This never replaces running code. All mutable test state lives under /tmp.
Only non-secret source files travel through command arguments.
"""

import base64
import io
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = ["src/news_officer/research_agent.py", "src/news_officer/research_context.py",
         "src/news_officer/agent_runtime.py", "scripts/smoke_agent_continuity.py"]


def main():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in FILES:
            archive.write(ROOT / name, name)
    payload = base64.b64encode(buffer.getvalue()).decode()
    code = f"""import base64,io,zipfile,tempfile,pathlib,runpy,news_officer
with tempfile.TemporaryDirectory(prefix='agent-code-replay-') as d:
 z=zipfile.ZipFile(io.BytesIO(base64.b64decode({payload!r})))
 z.extractall(d)
 news_officer.__path__.insert(0,str(pathlib.Path(d)/'src/news_officer'))
 runpy.run_path(str(pathlib.Path(d)/'scripts/smoke_agent_continuity.py'),run_name='__main__')
"""
    result = subprocess.run([
        "npx", "--yes", "@railway/cli@5.49.2", "ssh",
        "-p", "635628cc-885b-4718-bdb1-ec85799dde0c", "-s", "news-officer", "-e", "production",
        "--", "python", "-c", code,
    ], cwd=ROOT, check=False)
    sys.exit(result.returncode)


if __name__ == "__main__":
    main()
