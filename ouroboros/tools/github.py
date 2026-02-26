"""GitHub tools: issues, comments, reactions."""

import os
import json
import re
from typing import Dict, Any, List, Optional

import requests

from ouroboros.tools.registry import ToolEntry


def _get_repo() -> str:
    """
    Extract owner/repo from git remote URL.
    Returns 'owner/repo' without .git suffix.
    """
    result = os.popen('git remote get-url origin 2>/dev/null').read().strip()
    if not result:
        raise RuntimeError('Cannot determine remote origin')
    # Handle both HTTPS and SSH URLs
    patterns = [
        r'github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?$',  # SSH or HTTPS
    ]
    for pattern in patterns:
        match = re.search(pattern, result)
        if match:
            owner = match.group(1)
            repo = match.group(2)
            return f'{owner}/{repo}'
    raise RuntimeError(f'Cannot parse Git remote: {result}')


def _github_api_request(method: str, path: str, **kwargs) -> Dict[str, Any]:
    """Make a request to GitHub REST API."""
    token = os.environ.get('GITHUB_TOKEN')
    if not token:
        raise RuntimeError('GITHUB_TOKEN environment variable is not set')
    
    url = f'https://api.github.com{path}'
    headers = {
        'Authorization': f'token {token}',
        'Accept': 'application/vnd.github.v3+json',
        'User-Agent': 'Ouroboros-Agent'
    }
    
    # Merge headers
    if 'headers' in kwargs:
        headers.update(kwargs.pop('headers'))
    
    resp = requests.request(method, url, headers=headers, **kwargs)
    resp.raise_for_status()
    return resp.json()


def list_github_issues(state: str = 'open', labels: str = '') -> Dict[str, Any]:
    """
    List issues from the repository.
    
    Args:
        state: 'open', 'closed', or 'all'
        labels: comma-separated label names (optional)
    
    Returns:
        List of issues with basic info.
    """
    repo = _get_repo()
    params = {'state': state}
    if labels:
        params['labels'] = labels
    
    data = _github_api_request('GET', f'/repos/{repo}/issues', params=params)
    
    issues = []
    for issue in data:
        issues.append({
            'number': issue['number'],
            'title': issue['title'],
            'state': issue['state'],
            'user': issue['user']['login'],
            'created_at': issue['created_at'],
            'updated_at': issue['updated_at'],
            'labels': [l['name'] for l in issue['labels']],
            'html_url': issue['html_url']
        })
    
    return {
        'repo': repo,
        'count': len(issues),
        'issues': issues
    }


def get_github_issue(number: int) -> Dict[str, Any]:
    """
    Get details of a specific issue.
    
    Args:
        number: issue number
    
    Returns:
        Full issue data with comments.
    """
    repo = _get_repo()
    
    # Get issue
    issue = _github_api_request('GET', f'/repos/{repo}/issues/{number}')
    
    # Get comments
    comments = _github_api_request('GET', f'/repos/{repo}/issues/{number}/comments')
    
    return {
        'repo': repo,
        'number': issue['number'],
        'title': issue['title'],
        'state': issue['state'],
        'body': issue['body'] or '',
        'user': issue['user']['login'],
        'created_at': issue['created_at'],
        'updated_at': issue['updated_at'],
        'closed_at': issue.get('closed_at'),
        'labels': [l['name'] for l in issue['labels']],
        'comments': [
            {
                'id': c['id'],
                'user': c['user']['login'],
                'body': c['body'],
                'created_at': c['created_at']
            }
            for c in comments
        ],
        'html_url': issue['html_url']
    }


def comment_on_issue(number: int, body: str) -> Dict[str, Any]:
    """
    Add a comment to an issue.
    
    Args:
        number: issue number
        body: comment text (markdown)
    
    Returns:
        The created comment.
    """
    repo = _get_repo()
    
    data = {'body': body}
    comment = _github_api_request('POST', f'/repos/{repo}/issues/{number}/comments', json=data)
    
    return {
        'repo': repo,
        'issue': number,
        'comment_id': comment['id'],
        'html_url': comment['html_url']
    }


def close_github_issue(number: int, comment: Optional[str] = None) -> Dict[str, Any]:
    """
    Close an issue, optionally with a comment.
    
    Args:
        number: issue number
        comment: optional closing comment (if provided, posts before closing)
    
    Returns:
        Result with issue state and comment URL (if any).
    """
    repo = _get_repo()
    
    comment_url = None
    if comment:
        comment_result = comment_on_issue(number, comment)
        comment_url = comment_result['html_url']
    
    # Close issue
    data = {'state': 'closed'}
    issue = _github_api_request('PATCH', f'/repos/{repo}/issues/{number}', json=data)
    
    return {
        'repo': repo,
        'issue': number,
        'state': issue['state'],
        'closed_at': issue.get('closed_at'),
        'comment_url': comment_url,
        'html_url': issue['html_url']
    }


def create_github_issue(title: str, body: str = '', labels: List[str] = None) -> Dict[str, Any]:
    """
    Create a new issue.
    
    Args:
        title: issue title
        body: optional description (markdown)
        labels: optional list of labels
    
    Returns:
        The created issue.
    """
    repo = _get_repo()
    
    data = {
        'title': title,
        'body': body
    }
    if labels:
        data['labels'] = labels
    
    issue = _github_api_request('POST', f'/repos/{repo}/issues', json=data)
    
    return {
        'repo': repo,
        'number': issue['number'],
        'title': issue['title'],
        'state': issue['state'],
        'html_url': issue['html_url']
    }


def get_tools():
    """Return all GitHub tools."""
    return [
        ToolEntry(
            name="list_github_issues",
            schema={
                "name": "list_github_issues",
                "description": "List issues from the repository.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "state": {"type": "string", "default": "open", "description": "'open', 'closed', or 'all'"},
                        "labels": {"type": "string", "default": "", "description": "comma-separated label names (optional)"},
                    },
                    "required": [],
                },
            },
            handler=list_github_issues,
            timeout_sec=30,
        ),
        ToolEntry(
            name="get_github_issue",
            schema={
                "name": "get_github_issue",
                "description": "Get details of a specific issue.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "number": {"type": "integer", "description": "issue number"},
                    },
                    "required": ["number"],
                },
            },
            handler=get_github_issue,
            timeout_sec=30,
        ),
        ToolEntry(
            name="comment_on_issue",
            schema={
                "name": "comment_on_issue",
                "description": "Add a comment to an issue.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "number": {"type": "integer", "description": "issue number"},
                        "body": {"type": "string", "description": "comment text (markdown)"},
                    },
                    "required": ["number", "body"],
                },
            },
            handler=comment_on_issue,
            timeout_sec=30,
        ),
        ToolEntry(
            name="close_github_issue",
            schema={
                "name": "close_github_issue",
                "description": "Close an issue, optionally with a comment.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "number": {"type": "integer", "description": "issue number"},
                        "comment": {"type": "string", "default": None, "description": "optional closing comment (if provided, posts before closing)"},
                    },
                    "required": ["number"],
                },
            },
            handler=close_github_issue,
            timeout_sec=30,
        ),
        ToolEntry(
            name="create_github_issue",
            schema={
                "name": "create_github_issue",
                "description": "Create a new issue.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "issue title"},
                        "body": {"type": "string", "default": "", "description": "optional description (markdown)"},
                        "labels": {"type": "array", "items": {"type": "string"}, "default": None, "description": "optional list of labels"},
                    },
                    "required": ["title"],
                },
            },
            handler=create_github_issue,
            timeout_sec=30,
        ),
    ]