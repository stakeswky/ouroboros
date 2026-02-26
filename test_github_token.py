#!/usr/bin/env python3
import os
import requests
import subprocess
import json

print("Testing GitHub token access...")

# Method 1: Check env variable
token = os.getenv("GITHUB_TOKEN")
if not token:
    print("ERROR: GITHUB_TOKEN not set in environment")
    exit(1)

print(f"Token found, length: {len(token)}")
print(f"Token preview: {token[:10]}...")

# Method 2: Try to extract repo info
try:
    result = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
        check=True
    )
    url = result.stdout.strip()
    print(f"\nGit remote origin: {url}")
    
    # Parse URL
    if url.startswith("git@github.com:"):
        path = url.split(":", 1)[1].removesuffix(".git")
    elif url.startswith("https://github.com/") or "github.com/" in url:
        parts = url.split("github.com/")
        if len(parts) < 2:
            print("ERROR: Could not extract path from HTTPS URL")
            exit(1)
        path = parts[1].removesuffix(".git")
    else:
        print(f"ERROR: Unsupported URL format: {url}")
        exit(1)
    
    owner, repo = path.rstrip("/").split("/", 1)
    print(f"Extracted: owner={owner}, repo={repo}")
    
    # Method 3: Test API access
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "TestScript"
    }
    
    # Test basic API access
    response = requests.get("https://api.github.com/user", headers=headers, timeout=10)
    print(f"\nAPI User test status: {response.status_code}")
    if response.status_code == 200:
        user_data = response.json()
        print(f"Authenticated as: {user_data.get('login')}")
        print(f"User ID: {user_data.get('id')}")
    else:
        print(f"API Error: {response.text}")
        
    # Test repo access
    response = requests.get(
        f"https://api.github.com/repos/{owner}/{repo}",
        headers=headers,
        timeout=10
    )
    print(f"\nRepo access test status: {response.status_code}")
    if response.status_code == 200:
        repo_data = response.json()
        print(f"Repo: {repo_data.get('full_name')}")
        print(f"Private: {repo_data.get('private')}")
        
        # Test issues endpoint
        response = requests.get(
            f"https://api.github.com/repos/{owner}/{repo}/issues",
            headers=headers,
            params={"state": "open", "per_page": 5},
            timeout=10
        )
        print(f"\nIssues API test status: {response.status_code}")
        if response.status_code == 200:
            issues = response.json()
            print(f"Found {len(issues)} issues (including PRs)")
            if issues:
                for issue in issues[:3]:
                    if "pull_request" in issue:
                        continue
                    print(f"  #{issue['number']}: {issue['title'][:50]}...")
            else:
                print("No issues found or repo is empty")
        else:
            print(f"Issues API Error: {response.text[:200]}")
    else:
        print(f"Repo API Error: {response.text[:200]}")
        
except Exception as e:
    print(f"\nERROR: {type(e).__name__}: {str(e)}")
    import traceback
    traceback.print_exc()