#!/usr/bin/env python3
"""Fail-closed PR gate for completed Codex code review on the current head.

The Codex GitHub app writes a versioned review state in an issue comment.
A GitHub Actions test success is *not* a Codex review approval. This gate
waits for the summary from the exact PR head and rejects unresolved findings.
The Codex summary tracks resolution state; old inline comments do not.
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

MARKER = re.compile(r'<!-- codex-review-state:v1 (\{[^\r\n]+\}) -->')
BOT_NAMES = ('chatgpt-codex-connector', 'chatgpt-codex-connector[bot]')


def parse_review_states(issue_comments):
    states = []
    for comment in issue_comments:
        if comment.get('user', {}).get('login') not in BOT_NAMES:
            continue
        match = MARKER.search(comment.get('body') or '')
        if not match:
            continue
        try:
            states.append(json.loads(match.group(1)).get('code_review') or {})
        except (ValueError, TypeError):
            continue
    return states


def verdict(issue_comments, head_sha):
    matches = [
        x for x in parse_review_states(issue_comments)
        if x.get('commit_sha') == head_sha
    ]
    if not matches:
        return 'wait', 'Codex has not reported a review for this PR head'
    review = matches[-1]
    status = review.get('status')
    if status in ('failed', 'error', 'cancelled', 'canceled', 'timed_out'):
        return 'fail', f'Codex review terminated with status {status}'
    if status != 'completed':
        return 'wait', 'Codex review for this head has not completed'
    if review.get('has_unresolved_findings') is not False:
        return 'fail', 'Codex review reports outstanding findings'
    return 'pass', 'Codex review complete for head; no outstanding findings'


def github_list(url, token):
    """Fetch all pages rather than missing later comments."""
    results = []
    while url:
        req = urllib.request.Request(
            url,
            headers={
                'Authorization': f'Bearer {token}',
                'Accept': 'application/vnd.github+json',
                'X-GitHub-Api-Version': '2022-11-28',
            },
        )
        with urllib.request.urlopen(req, timeout=25) as resp:
            results.extend(json.load(resp))
            link = resp.headers.get('Link', '')
            match = re.search(r'<([^>]+)>;\s*rel="next"', link)
            url = match.group(1) if match else None
    return results


def self_test():
    sha = 'a' * 40
    def summary(sha_value, completed=True, unresolved=False):
        state = {'code_review': {
            'commit_sha': sha_value,
            'status': 'completed' if completed else 'in_progress',
            'has_unresolved_findings': unresolved,
        }}
        return {'user': {'login': BOT_NAMES[1]},
                'body': '<!-- codex-review-state:v1 ' + json.dumps(state) + ' -->'}
    assert verdict([], sha)[0] == 'wait'
    assert verdict([summary('b'*40)], sha)[0] == 'wait'
    assert verdict([summary(sha, False)], sha)[0] == 'wait'
    assert verdict([summary(sha, unresolved=True)], sha)[0] == 'fail'
    assert verdict([summary(sha)], sha)[0] == 'pass'
    for terminal in ('failed', 'error', 'cancelled', 'canceled', 'timed_out'):
        item = summary(sha)
        payload = {'code_review': {'commit_sha': sha, 'status': terminal}}
        item['body'] = '<!-- codex-review-state:v1 ' + json.dumps(payload) + ' -->'
        assert verdict([item], sha)[0] == 'fail'
    # A previous inline comment can remain visible even after resolution.
    # The review summary (not raw comment presence) supplies that state.
    assert verdict([summary(sha, unresolved=False)], sha)[0] == 'pass'
    print('Codex review gate self-tests: OK')


def main():
    if '--self-test' in sys.argv:
        self_test()
        return 0
    repo = os.environ['GITHUB_REPOSITORY']
    number = int(os.environ['PR_NUMBER'])
    sha = os.environ['PR_HEAD_SHA']
    token = os.environ['GH_TOKEN']
    api = f'https://api.github.com/repos/{repo}'
    deadline = time.monotonic() + 14 * 60
    previous_message = ''
    while time.monotonic() < deadline:
        try:
            issues = github_list(
                f'{api}/issues/{number}/comments?per_page=100', token
            )
            state, reason = verdict(issues, sha)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            # Transport/API faults should never be interpreted as approved.
            state, reason = 'wait', f'GitHub API unavailable: {exc}'
        if reason != previous_message:
            print(f'{state.upper()}: {reason}', flush=True)
            previous_message = reason
        if state == 'pass':
            return 0
        if state == 'fail':
            return 1
        time.sleep(20)
    print('FAIL: timed out waiting for completed Codex review of current head', flush=True)
    return 1


if __name__ == '__main__':
    sys.exit(main())
