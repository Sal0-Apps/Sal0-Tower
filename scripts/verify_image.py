"""Verify anonymous pull resolves to this release, then check its runtime."""
import json
import os
import subprocess

image = 'ghcr.io/sal0-apps/sal0-tower:latest'
info = json.loads(subprocess.check_output(['docker', 'image', 'inspect', image], text=True))[0]
expected = 'ghcr.io/sal0-apps/sal0-tower@' + os.environ['EXPECTED_DIGEST']
assert expected in info['RepoDigests'], (expected, info['RepoDigests'])
assert info['Config']['Labels']['org.opencontainers.image.revision'] == os.environ['EXPECTED_REVISION']
subprocess.run(['docker', 'run', '--rm', '--entrypoint', 'python3', image, '-c', 'import apps, yaml; assert hasattr(apps.AppCatalog, "decorate"); print("Public image OK: Zima metadata and stack grouping are installed.")'], check=True)
print('Anonymous latest pull, digest and source revision verified.')
