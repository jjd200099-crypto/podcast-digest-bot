"""Configure only the existing bot's supervision; never create/upgrade resources.

Default is a preview. Requires an already authenticated Railway CLI. Uses the
inspected public API schema, without reading or printing authentication tokens.
"""

import json
import subprocess
import sys

PROJECT = '635628cc-885b-4718-bdb1-ec85799dde0c'
SERVICE = 'a4bd38a4-6f26-4a9b-afce-29a75f929401'
ENVIRONMENT = '6a20d0b8-37a8-4745-87dc-3ba8025c31a6'
CLI = ['npx', '--yes', '@railway/cli@5.49.2']
SETTINGS = {'healthcheckPath': '/healthz', 'healthcheckTimeout': 180,
            'restartPolicyType': 'ALWAYS', 'numReplicas': 1, 'sleepApplication': False}


def main():
    if sys.argv[1:] not in ([], ['--apply']):
        raise SystemExit('Usage: configure_cloud_health.py [--apply]')
    result = subprocess.run(CLI + ['status', '--json'], capture_output=True, text=True, check=True, timeout=45)
    current = json.loads(result.stdout)
    assert current['id'] == PROJECT, 'Wrong linked project'
    assert any(e['node']['id'] == SERVICE for e in current['services']['edges']), 'Missing bot service'
    assert any(e['node']['id'] == ENVIRONMENT for e in current['environments']['edges']), 'Missing production environment'
    print(json.dumps({'project': PROJECT, 'service': SERVICE, 'settings': SETTINGS, 'apply': '--apply' in sys.argv}))
    if '--apply' not in sys.argv:
        return
    query = 'mutation($service: String!, $environment: String!, $settings: ServiceInstanceUpdateInput!) { serviceInstanceUpdate(serviceId: $service, environmentId: $environment, input: $settings) }'
    variables = {'service': SERVICE, 'environment': ENVIRONMENT, 'settings': SETTINGS}
    # A plan restriction is a real blocker, not authorization to buy an upgrade.
    subprocess.run(CLI + ['api', query, '--variables', json.dumps(variables), '--compact'], check=True, timeout=45)


if __name__ == '__main__':
    main()
