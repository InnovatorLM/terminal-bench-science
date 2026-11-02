"""Public author retrospectives on the original proposal/review thread.

The Markdown comment is the record. No private file or author-written receipt
can substitute for publication, and no additional reviewer approval is needed.
"""
import hashlib
import re
from urllib.parse import urlparse

MARKER = '<!-- gewu-post-acceptance-lessons-v1 -->'
FIELDS = {'Accepted key': 'key', 'Accepted commit': 'commit',
          'Task': 'task', 'Snapshot': 'snapshot_sha256'}
SECTIONS = ('来源与设计', '有效与失败的迭代', '评测结果与局限', '迁移思路与下一题')


def accepted(row):
    return (row.get('state') == 'completed'
            and row.get('scientific_acceptance') is True
            and row.get('formal_decision') == 'accepted')


def template(row):
    lines = [MARKER, '# 出题经验复盘', '']
    lines += [f'{label}: `{row.get(field, "")}`' for label, field in FIELDS.items()]
    for heading in SECTIONS:
        lines += ['', '## ' + heading, '', '<填写已核实的具体经验；未知或缺失证据如实说明>']
    return '\n'.join(lines) + '\n'


def public_lessons_comment(comments, row, author, thread):
    """Comments must come from this thread's paginated Forgejo API response."""
    if not accepted(row) or thread != row.get('proposal_thread'):
        return None
    matches = []
    for comment in comments:
        body = comment.get('body', '')
        if not isinstance(body, str) or MARKER not in body:
            continue
        if (comment.get('user') or {}).get('login') != 'gewu-' + author:
            continue
        url = comment.get('html_url') or comment.get('issue_url')
        if url and not re.fullmatch(
                r'(?:/api/v1/repos)?/team/terminal-bench-science/(?:issues|pulls)/' + str(thread),
                urlparse(url).path):
            continue
        if any(not value or re.findall(r'^' + re.escape(label) + r':\s*`([^`]+)`\s*$', body, re.M) != [value]
               for label, field in FIELDS.items() for value in [row.get(field)]):
            continue
        sections = dict(re.findall(r'^## ([^\n]+)\n(.*?)(?=^## |\Z)', body, re.M | re.S))
        if any(not sections.get(name, '').strip()
               or '<填写' in sections[name]
               or sections[name].strip().casefold() in ('todo', 'tbd', '待填写')
               for name in SECTIONS):
            continue
        matches.append(comment)
    return max(matches, key=lambda c: c.get('id', 0)) if matches else None


def comment_summary(comment, thread):
    return dict(id=comment['id'], thread=thread,
                url=comment.get('html_url') or comment.get('url'),
                sha256=hashlib.sha256(comment['body'].encode()).hexdigest(),
                created_at=comment.get('created_at'))


def blocks_new_idea(feedback):
    return (accepted(feedback) and feedback.get('post_acceptance_required') is True
            and feedback.get('post_acceptance_state') != 'completed')
