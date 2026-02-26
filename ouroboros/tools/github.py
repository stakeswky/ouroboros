"""GitHub tools: issues, comments, reactions via REST API."""

import os
import json
import urllib.parse
from typing import Dict, Any, List, Optional

import requests
from ouroboros.tools.registry import ToolEntry


def _extract_owner_repo() -> tuple[str, str]:
    """Extract owner/repo from git remote origin URL."""
    import subprocess
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            check=True
        )
        url = result.stdout.strip()
        
        # Handle different URL formats
        if url.startswith("git@github.com:"):
            # git@github.com:stakeswky/ouroboros.git
            path = url.split(":", 1)[1].removesuffix(".git")
        elif url.startswith("https://github.com/") or "github.com/" in url:
            # Handle various HTTPS formats:
            # 1. https://github.com/stakeswky/ouroboros.git
            # 2. https://github_pat_...:x-oauth-basic@github.com/stakeswky/ouroboros.git
            # 3. https://token@github.com/stakeswky/ouroboros.git
            
            # Extract everything after github.com/
            parts = url.split("github.com/")
            if len(parts) < 2:
                raise ValueError(f"Could not extract path from URL: {url}")
            path = parts[1].removesuffix(".git")
        else:
            raise ValueError(f"Unsupported remote URL format: {url}")
        
        owner, repo = path.rstrip("/").split("/", 1)
        return owner, repo
    except Exception as e:
        raise RuntimeError(f"Failed to extract owner/repo from git remote: {e}")


def _api_request(method: str, endpoint: str, data: Optional[Dict] = None) -> Dict[str, Any]:
    """Make authenticated GitHub API request."""
    token = os.getenv("GITHUB_TOKEN")
    if not token:
        raise RuntimeError("GITHUB_TOKEN environment variable not set")
    
    base_url = "https://api.github.com"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "Ouroboros"
    }
    
    url = f"{base_url}/{endpoint.lstrip('/')}"
    
    try:
        response = requests.request(
            method=method,
            url=url,
            headers=headers,
            json=data if data else None,
            timeout=30
        )
        response.raise_for_status()
        if response.content:
            return response.json()
        return {}
    except requests.exceptions.RequestException as e:
        if hasattr(e.response, 'json') and e.response.json():
            error = e.response.json()
            raise RuntimeError(f"GitHub API error: {error.get('message', str(e))}")
        raise RuntimeError(f"GitHub API request failed: {e}")


def list_github_issues(
    state: str = "all",
    label: str = ""
) -> str:
    """List GitHub issues for the repository."""
    owner, repo = _extract_owner_repo()
    endpoint = f"repos/{owner}/{repo}/issues"
    
    params = {"state": state}
    if label:
        params["labels"] = label
    
    # Convert params to query string
    param_str = urllib.parse.urlencode(params)
    if param_str:
        endpoint = f"{endpoint}?{param_str}"
    
    issues = _api_request("GET", endpoint)
    
    if isinstance(issues, dict) and issues.get("message"):
        # API error
        return f"Error: {issues['message']}"
    
    if not issues:
        return "No issues found."
    
    result_lines = []
    for issue in issues:
        # Filter out pull requests (they have pull_request field)
        if "pull_request" in issue:
            continue
            
        result_lines.append(f"#{issue['number']}: {issue['title']}")
        result_lines.append(f"  State: {issue['state']}, Author: {issue['user']['login']}")
        result_lines.append(f"  URL: {issue['html_url']}")
        if issue.get("labels"):
            labels = ", ".join(label["name"] for label in issue["labels"])
            result_lines.append(f"  Labels: {labels}")
        result_lines.append("")
    
    if not result_lines:
        return "No issues found (only pull requests)."
    
    return "\n".join(result_lines)


def get_github_issue(issue_number: int) -> str:
    """Get a specific GitHub issue."""
    owner, repo = _extract_owner_repo()
    endpoint = f"repos/{owner}/{repo}/issues/{issue_number}"
    
    issue = _api_request("GET", endpoint)
    
    if isinstance(issue, dict) and issue.get("message"):
        return f"Error: {issue['message']}"
    
    if "pull_request" in issue:
        return f"Error: #{issue_number} is a pull request, not an issue."
    
    lines = [
        f"#{issue['number']}: {issue['title']}",
        f"State: {issue['state']}",
        f"Author: {issue['user']['login']}",
        f"Created: {issue['created_at']}",
        f"URL: {issue['html_url']}",
    ]
    
    if issue.get("labels"):
        labels = ", ".join(label["name"] for label in issue["labels"])
        lines.append(f"Labels: {labels}")
    
    if issue.get("assignee"):
        lines.append(f"Assignee: {issue['assignee']['login']}")
    
    if issue.get("milestone"):
        lines.append(f"Milestone: {issue['milestone']['title']}")
    
    lines.append("")
    lines.append("Body:")
    lines.append(issue.get("body", "(No description)"))
    
    return "\n".join(lines)


def comment_on_issue(issue_number: int, body: str) -> str:
    """Add a comment to a GitHub issue."""
    owner, repo = _extract_owner_repo()
    endpoint = f"repos/{owner}/{repo}/issues/{issue_number}/comments"
    
    data = {"body": body}
    result = _api_request("POST", endpoint, data)
    
    if isinstance(result, dict) and result.get("message"):
        return f"Error: {result['message']}"
    
    return f"Comment created: {result['html_url']}"


def close_github_issue(issue_number: int) -> str:
    """Close a GitHub issue."""
    owner, repo = _extract_owner_repo()
    endpoint = f"repos/{owner}/{repo}/issues/{issue_number}"
    
    data = {"state": "closed"}
    result = _api_request("PATCH", endpoint, data)
    
    if isinstance(result, dict) and result.get("message"):
        return f"Error: {result['message']}"
    
    return f"Issue #{issue_number} closed."


def create_github_issue(title: str, body: str = "", labels: str = "") -> str:
    """Create a new GitHub issue."""
    owner, repo = _extract_owner_repo()
    endpoint = f"repos/{owner}/{repo}/issues"
    
    data = {"title": title}
    if body:
        data["body"] = body
    if labels:
        data["labels"] = [label.strip() for label in labels.split(",") if label.strip()]
    
    result = _api_request("POST", endpoint, data)
    
    if isinstance(result, dict) and result.get("message"):
        return f"Error: {result['message']}"
    
    return f"Issue created: #{result['number']} - {result['title']}\nURL: {result['html_url']}"


def get_tools() -> List[ToolEntry]:
    """Register GitHub tools."""
    return [
        ToolEntry(
            name="list_github_issues",
            schema={
                "name": "list_github_issues",
                "description": "List GitHub issues for the repository",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "state": {
                            "type": "string",
                            "enum": ["open", "closed", "all"],
                            "default": "all",
                            "description": "Issue state: open, closed, or all"
                        },
                        "label": {
                            "type": "string",
                            "default": "",
                            "description": "Filter by label (comma-separated for multiple)"
                        }
                    },
                    "required": []
                }
            },
            handler=list_github_issues,
            timeout_sec=30
        ),
        ToolEntry(
            name="get_github_issue",
            schema={
                "name": "get_github_issue",
                "description": "Get a specific GitHub issue",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "issue_number": {
                            "type": "integer",
                            "description": "Issue number"
                        }
                    },
                    "required": ["issue_number"]
                }
            },
            handler=get_github_issue,
            timeout_sec=30
        ),
        ToolEntry(
            name="comment_on_issue",
            schema={
                "name": "comment_on_issue",
                "description": "Add a comment to a GitHub issue",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "issue_number": {
                            "type": "integer",
                            "description": "Issue number"
                        },
                        "body": {
                            "type": "string",
                            "description": "Comment text"
                        }
                    },
                    "required": ["issue_number", "body"]
                }
            },
            handler=comment_on_issue,
            timeout_sec=30
        ),
        ToolEntry(
            name="close_github_issue",
            schema={
                "name": "close_github_issue",
                "description": "Close a GitHub issue",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "issue_number": {
                            "type": "integer",
                            "description": "Issue number"
                        }
                    },
                    "required": ["issue_number"]
                }
            },
            handler=close_github_issue,
            timeout_sec=30
        ),
        ToolEntry(
            name="create_github_issue",
            schema={
                "name": "create_github_issue",
                "description": "Create a new GitHub issue",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {
                            "type": "string",
                            "description": "Issue title",
                        },
                        "body": {
                            "type": "string",
                            "default": "",
                            "description": "Issue description"
                        },
                        "labels": {
                            "type": "string",
                            "default": "",
                            "description": "Comma-separated labels"
                        }
                    },
                    "required": ["title"]
                }
            },
            handler=create_github_issue,
            timeout_sec=30
        )
    ]