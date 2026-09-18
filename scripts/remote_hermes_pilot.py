"""Temporary Railway pilot, no deployment/restart/messages/production DB writes.

prepare: provision a separate /tmp environment, returning a durable log path.
run HERMES_PYTHON BACKEND [CASES...]: upload current overlay and run replay.
"""
import base64
import hashlib
import io
import os
import re
import shlex
import subprocess
import sys
import zipfile
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    mode = sys.argv[1]
    if mode not in {"prepare", "run"}:
        raise ValueError("Expected prepare or run")
    buffer = io.BytesIO()
    names = [ROOT / "scripts/install_hermes_pilot.py"]
    if mode == "run":
        names += sorted((ROOT / "src/news_officer").glob('*.py'))
        names += [ROOT / "scripts/smoke_agent_continuity.py"]
        names += [ROOT / "scripts/smoke_runtime_conversation.py"]
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in names:
            archive.write(path, path.relative_to(ROOT))
    blob = base64.b64encode(buffer.getvalue()).decode()
    code = f'''import base64,io,zipfile,tempfile,pathlib,subprocess,os,sys,json
root=pathlib.Path(tempfile.mkdtemp(prefix="hermes-pilot-code-"))
zipfile.ZipFile(io.BytesIO(base64.b64decode({blob!r}))).extractall(root)
mode={mode!r}
if mode=="prepare":
 target=root/"runtime"
 print(json.dumps({{"python":str(target/"venv/bin/python")}}),flush=True)
 subprocess.run([sys.executable,str(root/"scripts/install_hermes_pilot.py"),str(target)],env={{"PATH":os.defpath,"LANG":"C.UTF-8","PYTHON_DOTENV_DISABLED":"1"}},check=True)
else:
 args={sys.argv[2:]!r}
 env=dict(os.environ,NEWS_OFFICER_AGENT_BACKEND=args[1],NEWS_OFFICER_HERMES_PYTHON=args[0],NEWS_OFFICER_FEEDS_PATH=os.environ.get("NEWS_OFFICER_FEEDS_PATH","/app/feeds.json"))
 script = "smoke_runtime_conversation.py" if "--transport" in args else "smoke_agent_continuity.py"
 boot="import news_officer,runpy,sys; news_officer.__path__.insert(0,"+repr(str(root/"src/news_officer"))+"); sys.argv="+repr([str(root/"scripts"/script)]+args[2:])+"; runpy.run_path(sys.argv[0],run_name='__main__')"
 subprocess.run([sys.executable,"-c",boot],env=env,check=True)
'''
    launcher = (
        "import subprocess,tempfile,json,base64,sys,zlib,hashlib; "
        "code=zlib.decompress(base64.b64decode(sys.stdin.read())); "
        f"assert hashlib.sha256(code).hexdigest()=={hashlib.sha256(code.encode()).hexdigest()!r}, 'incomplete upload'; "
        "log=tempfile.NamedTemporaryFile(prefix='hermes-pilot-',suffix='.log',delete=False); "
        "p=subprocess.Popen(['python','-u','-'],stdin=subprocess.PIPE,stdout=log,stderr=subprocess.STDOUT,start_new_session=True); "
        "p.stdin.write(code); p.stdin.close(); "
        "print(json.dumps({'pid':p.pid,'log':log.name}),flush=True)"
    )
    command = ["npx", "--yes", "@railway/cli@5.49.2", "ssh",
                    "-p", "635628cc-885b-4718-bdb1-ec85799dde0c", "-s", "news-officer", "-e", "production",
                    "--", "python", "-c", launcher]
    # Optional native SSH fallback; obtain the host from `railway ssh config`
    # for THIS service. Never guess a deployment/host or mutate personal config.
    if host := os.environ.get("PODCAST_PILOT_SSH_HOST"):
        if not re.fullmatch(r"[0-9a-f-]{36}@ssh\.railway\.com", host):
            raise ValueError("Expected Railway-generated SSH host")
        command = ["ssh", "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=15",
                   "-o", "ServerAliveCountMax=2", "-T", host,
                   shlex.join(["python", "-c", launcher])]
    try:
        result = subprocess.run(command, cwd=ROOT, check=False, timeout=60,
                                input=base64.b64encode(zlib.compress(code.encode())).decode(), text=True)
    except subprocess.TimeoutExpired:
        print("SSH launch receipt timed out; inspect pilot logs before retrying.", file=sys.stderr)
        sys.exit(2)
    sys.exit(result.returncode)


if __name__ == "__main__":
    main()
