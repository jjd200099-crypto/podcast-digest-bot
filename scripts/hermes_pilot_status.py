"""Read only pilot logs, never environment variables or unrelated files."""
import subprocess
import sys

arg = sys.argv[1] if len(sys.argv) > 1 else "--list"
if arg == "--list":
    code = "import pathlib,json; print(json.dumps([{'path':str(p),'bytes':p.stat().st_size} for p in sorted(pathlib.Path('/tmp').glob('hermes-pilot-*.log'),key=lambda p:p.stat().st_mtime)[-8:]]))"
elif arg == "--sessions":
    code = "import pathlib,sqlite3,json; paths=list(pathlib.Path('/tmp').glob('agent-continuity-*/hermes-pilot/*/sessions.sqlite3')); print(json.dumps([{'path':str(p),'roles':sqlite3.connect('file:'+str(p)+'?mode=ro',uri=True).execute('SELECT role,count(*) FROM messages GROUP BY role').fetchall()} for p in paths]))"
elif arg.startswith("/tmp/hermes-pilot-") and arg.endswith(".log") and "/" not in arg[5:]:
    code = f"import pathlib; print(pathlib.Path({arg!r}).read_text()[-12000:])"
else:
    raise ValueError("Expected --list or exact /tmp/hermes-pilot-*.log")
result = subprocess.run(["npx", "--yes", "@railway/cli@5.49.2", "ssh",
                         "-p", "635628cc-885b-4718-bdb1-ec85799dde0c", "-s", "news-officer", "-e", "production",
                         "--", "python", "-c", code], check=False)
sys.exit(result.returncode)
