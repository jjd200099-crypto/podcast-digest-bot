"""Package current main only after both CI checks pass; dry-run by default."""

import argparse
import json
import subprocess
import tempfile
from pathlib import Path


def run(*args, cwd=None):
    return subprocess.check_output(args, cwd=cwd, text=True).strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', required=True)
    parser.add_argument('--service', default='news-officer')
    parser.add_argument('--environment', default='production')
    parser.add_argument('--deploy', action='store_true')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    repo = json.loads(run('gh', 'repo', 'view', '--json', 'nameWithOwner', cwd=root))['nameWithOwner']
    commit = json.loads(run('gh', 'api', f'repos/{repo}/commits/main', cwd=root))['sha']
    checks = json.loads(run('gh', 'api', f'repos/{repo}/commits/{commit}/check-runs', cwd=root))['check_runs']
    for name in ('test', 'hermes-contract'):
        matches = sorted((c for c in checks if c['name'] == name), key=lambda c: c['id'], reverse=True)
        if not matches or matches[0]['conclusion'] != 'success':
            raise SystemExit(f'Release blocked: {name} must pass on main {commit}')
    run('git', 'fetch', 'origin', 'main', cwd=root)
    print(json.dumps({'commit': commit, 'branch': 'main', 'checks': 'passed',
                      'deploy': args.deploy, 'dirty_worktree_included': False}), flush=True)
    if not args.deploy:
        return
    with tempfile.TemporaryDirectory(prefix='news-officer-release-') as directory:
        stage = Path(directory)
        # Extract an archive of the verified commit, never upload the worktree.
        archive = subprocess.Popen(['git', 'archive', commit], cwd=root, stdout=subprocess.PIPE)
        unpack = subprocess.run(['tar', '-x', '-C', directory], stdin=archive.stdout, check=False)
        archive.stdout.close()
        if archive.wait() != 0 or unpack.returncode:
            raise SystemExit('Archive failed')
        (stage / 'release.json').write_text(json.dumps({'commit': commit, 'branch': 'main'}))
        subprocess.run(['npx', '--yes', '@railway/cli@5.49.2', 'up', directory, '--path-as-root',
                        '--service', args.service, '--environment', args.environment,
                        '--project', args.project, '--detach', '--message', f'main {commit}'], check=True)
    print('Uploaded; verify Railway SUCCESS and /healthz before calling this release live.', flush=True)


if __name__ == '__main__':
    main()
