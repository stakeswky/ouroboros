"""GitHub tools: issues, comments, reactions — via REST API."""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

import requests

from ouroboros.tools.registry import ToolContext, ToolEntry

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _api_request(ctx: ToolContext, method: str, endpoint: str, data: Optional[Dict[str, Any]] = None) -> str:
    """Make GitHub API request and return parsed JSON or error string."""
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        return "⚠️ GITHUB_TOKEN environment variable is not set."

    repo_slug = _get_repo_slug(ctx)
    if repo_slug.startswith("⚠️"):
        return repo_slug

    url = f"https://api.github.com/repos/{repo_slug}/{endpoint}"

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "Ouroboros/6.2.4",
    }

    try:
        if method.upper() == "GET":
            resp = requests.get(url, headers=headers, timeout=30)
        elif method.upper() == "POST":
            resp = requests.post(url, headers=headers, json=data, timeout=30)
        elif method.upper() == "PATCH":
            resp = requests.patch(url, headers=headers, json=data, timeout=30)
        else:
            return f"⚠️ Unsupported HTTP method: {method}"

        if resp.status_code == 404:
            return f"⚠️ Resource not found (404): {endpoint}"
        if resp.status_code == 401:
            return "⚠️ Authentication failed. Check GITHUB_TOKEN."
        if resp.status_code >= 400:
            error_detail = resp.json().get("message", resp.text[:200])
            return f"⚠️ GitHub API error ({resp.status_code}): {error_detail}"

        return resp.json()
    except requests.exceptions.Timeout:
        return "⚠️ GitHub API request timed out (30s)."
    except requests.exceptions.ConnectionError:
        return "⚠️ Connection error to GitHub API."
    except Exception as e:
        return f"⚠️ GitHub API error: {e}"


def _get_repo_slug(ctx: ToolContext) -> str:
    """Get 'owner/repo' from environment or remote URL."""
    user = os.environ.get("GITHUB_USER", "").strip()
    repo = os.environ.get("GITHUB_REPO", "").strip()
    if user and repo:
        return f"{user}/{repo}"

    # Try to extract from git remote origin
    import subprocess
    try:
        out = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=str(ctx.repo_dir),
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
        # Match patterns: git@github.com:owner/repo.git or https://github.com/owner/repo.git
        m = re.search(r"(?<=github.com[:/])([^/]+)/([^/.]+)", out)
        if m:
            return f"{m.group(1)}/{m.group(2)}"
    except Exception as e:
        log.debug(f"Failed to get repo slug from git remote: {e}")

    return "⚠️ Could not determine repository owner/repo."


def _api_list_issues(ctx: ToolContext, state: str = "open", labels: str = "", limit: int = 20) -> str:
    """Fetch issues via pagination."""
    params = {
        "state": state,
        "per_page": min(limit, 100),  # GitHub max per_page
        "page": 1,
    }
    if labels:
        params["labels"] = labels

    result = _api_request(ctx, "GET", "issues", params)
    if isinstance(result, str) and result.startswith("⚠️"):
        return result

    if not isinstance(result, list):
        return f"⚠️ Unexpected API response: {type(result)}"

    return result


# ---------------------------------------------------------------------------
# Tool handlers (same signatures)
# ---------------------------------------------------------------------------

def _list_issues(ctx: ToolContext, state: str = "open", labels: str = "", limit: int = 20) -> str:
    """List GitHub issues with optional filters."""
    issues = _api_list_issues(ctx, state, labels, limit)
    if isinstance(issues, str) and issues.startswith("⚠️"):
        return issues

    if not issues:
        return f"No {state} issues found."

    lines = [f"**{len(issues)} {state} issue(s):**\n"]
    for issue in issues:
        labels_str = ", ".join(l.get("name", "") for l in issue.get("labels", []))
        author = issue.get("user", {}).get("login", "unknown")
        lines.append(
            f"- **#{issue['number']}** {issue['title']}"
            f" (by @{author}{', labels: ' + labels_str if labels_str else ''})"
        )
        body = (issue.get("body") or "").strip()
        if body:
            preview = body[:200] + ("..." if len(body) > 200 else "")
            lines.append(f"  > {preview}")

    return "\n".join(lines)


def _get_issue(ctx: ToolContext, number: int) -> str:
    """Get a single issue with full details and comments."""
    if number <= 0:
        return "⚠️ issue number must be positive"

    issue = _api_request(ctx, "GET", f"issues/{number}")
    if isinstance(issue, str) and issue.startswith("⚠️"):
        return issue

    comments = _api_request(ctx, "GET", f"issues/{number}/comments")
    if isinstance(comments, str) and comments.startswith("⚠️"):
        comments = []

    labels_str = ", ".join(l.get("name", "") for l in issue.get("labels", []))
    author = issue.get("user", {}).get("login", "unknown")

    lines = [
        f"## Issue #{issue['number']}: {issue['title']}",
        f"**State:** {issue['state']}  |  **Author:** @{author}",
    ]
    if labels_str:
        lines.append(f"**Labels:** {labels_str}")

    body = (issue.get("body") or "").strip()
    if body:
        lines.append(f"\n**Body:**\n{body[:3000]}")

    if comments:
        lines.append(f"\n**Comments ({len(comments)}):**")
        for c in comments[:10]:  # limit to 10 most recent
            c_author = c.get("user", {}).get("login", "unknown")
            c_body = (c.get("body") or "").strip()[:500]
            lines.append(f"\n@{c_author}:\n{c_body}")

    return "\n".join(lines)


def _comment_on_issue(ctx: ToolContext, number: int, body: str) -> str:
    """Add a comment to an issue."""
    if number <= 0:
        return "⚠️ issue number must be positive"

    if not body or not body.strip():
        return "⚠️ Comment body cannot be empty."

    result = _api_request(ctx, "POST", f"issues/{number}/comments", {"body": body})
    if isinstance(result, str) and result.startswith("⚠️"):
        return result
    return f"✅ Comment added to issue #{number}."


def _close_issue(ctx: ToolContext, number: int, comment: str = "") -> str:
    """Close an issue with optional closing comment."""
    if number <= 0:
        return "⚠️ issue number must be positive"

    if comment and comment.strip():
        result = _comment_on_issue(ctx, number, comment)
        if result.startswith("⚠️"):
            return result

    result = _api_request(ctx, "PATCH", f"issues/{number}", {"state": "closed"})
    if isinstance(result, str) and result.startswith("⚠️"):
        return result
    return f"✅ Issue #{number} closed."


def _create_issue(ctx: ToolContext, title: str, body: str = "", labels: str = "") -> str:
    """Create a new GitHub issue."""
    if not title or not title.strip():
        return "⚠️ Issue title cannot be empty."

    data = {"title": title}
    if body:
        data["body"] = body

    result = _api_request(ctx, "POST", "issues", data)
    if isinstance(result, str) and result.startswith("⚠️"):
        return result

    issue_number = result.get("number")
    if not issue_number:
        return "⚠️ Issue created but could not parse response."

    if labels:
        # Add labels separately
        label_list = [l.strip() for l in labels.split(",") if l.strip()]
        if label_list:
            label_result = _api_request(ctx, "PATCH", f"issues/{issue_number}", {"labels": label_list})
            if isinstance(label_result, str) and label_result.startswith("⚠️"):
                # Issue created but labels failed — not fatal
                return f"✅ Issue #{issue_number} created (labels failed: {label_result})"

    return f"✅ Issue #{issue_number} created: {result.get('html_url')}"


# ---------------------------------------------------------------------------
# Tool registration (identical signatures)
# ---------------------------------------------------------------------------

def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry("list_github_issues", {
            "name": "list_github_issues",
            "description": "List GitHub issues. Use to check for new tasks, bug reports, or feature requests from the creator or contributors.",
            "parameters": {"type": "object", "properties": {
                "state": {"type": "string", "default": "open", "enum": ["open", "closed", "all"], "description": "Filter by state"},
                "labels": {"type": "string", "default": "", "description": "Filter by label (comma-separated)"},
                "limit": {"type": "integer", "default": 20, "description": "Max issues to return (max 50)"},
            }, "required": []},
        }, _list_issues),

        ToolEntry("get_github_issue", {
            "name": "get_github_issue",
            "description": "Get full details of a GitHub issue including body and comments.",
            "parameters": {"type": "object", "properties": {
                "number": {"type": "integer", "description": "Issue number"},
            }, "required": ["number"]},
        }, _get_issue),

        ToolEntry("comment_on_issue", {
            "name": "comment_on_issue",
            "description": "Add a comment to a GitHub issue. Use to respond to issues, share progress, or ask clarifying questions.",
            "parameters": {"type": "object", "properties": {
                "number": {"type": "integer", "description": "Issue number"},
                "body": {"type": "string", "description": "Comment text (markdown)"},
            }, "required": ["number", "body"]},
        }, _comment_on_issue),

        ToolEntry("close_github_issue", {
            "name": "close_github_issue",
            "description": "Close a GitHub issue with optional closing comment.",
            "parameters": {"type": "object", "properties": {
                "number": {"type": "integer", "description": "Issue number"},
                "comment": {"type": "string", "default": "", "description": "Optional closing comment"},
            }, "required": ["number"]},
        }, _close_issue),

        ToolEntry("create_github_issue", {
            "name": "create_github_issue",
            "description": "Create a new GitHub issue. Use for tracking tasks, documenting bugs, or planning features.",
            "parameters": {"type": "object", "properties": {
                "title": {"type": "string", "description": "Issue title"},
                "body": {"type": "string", "default": "", "description": "Issue body (markdown)"},
                "labels": {"type": "string", "default": "", "description": "Labels (comma-separated)"},
            }, "required": ["title"]},
        }, _create_issue),
    ]
