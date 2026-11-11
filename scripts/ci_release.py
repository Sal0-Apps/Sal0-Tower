"""Cancel only the superseded workflow and reject publishing obsolete source."""
import json
import os
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def main():
    repository = os.environ['GITHUB_REPOSITORY']
    revision = os.environ['GITHUB_SHA']
    root = os.environ.get('GITHUB_API_URL', 'https://api.github.com') + '/repos/' + repository
    headers = {'Authorization': 'Bearer ' + os.environ['GH_TOKEN'], 'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28'}

    def api(path, method='GET'):
        request = Request(root + path, headers=headers, method=method)
        with urlopen(request, timeout=30) as response:
            payload = response.read()
            return json.loads(payload) if payload else None

    # The old filename has been retired. Never cancel this new workflow, PRs,
    # another branch, completed runs or any other repository/workflow.
    runs = api('/actions/runs?branch=main&per_page=100')['workflow_runs']
    for run in runs:
        if run.get('path') != '.github/workflows/publish.yml' or run.get('head_branch') != 'main' or run.get('status') == 'completed':
            continue
        for operation in ('cancel', 'force-cancel'):
            try:
                api('/actions/runs/' + str(run['id']) + '/' + operation, 'POST')
                print('Cancellation requested for legacy run', run['id'], operation)
                break
            except HTTPError as exc:
                print('Legacy run', run['id'], operation, 'returned HTTP', exc.code)
                if exc.code not in (409, 500, 502, 503):
                    break
    current = api('/git/ref/heads/main')['object']['sha']
    if current != revision:
        raise SystemExit('Main advanced to another commit; this obsolete source will not be published.')
    print('Source is the current main commit:', revision)


if __name__ == '__main__':
    main()
