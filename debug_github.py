#!/usr/bin/env python3
"""Debug GitHub API issues."""

import os
import json
import requests
import subprocess

def extract_owner_repo():
    """Extract owner/repo from git remote origin URL."""
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

def test_api():
    """Test GitHub API directly."""
    token = os.getenv("GITHUB_TOKEN")
    if not token:
        print("❌ GITHUB_TOKEN not set")
        return False
    
    print(f"✅ GITHUB_TOKEN exists (length: {len(token)}, starts with: {token[:5]}...)")
    
    owner, repo = extract_owner_repo()
    print(f"✅ Owner/Repo: {owner}/{repo}")
    
    # Test simple GET request
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "Ouroboros-Debug"
    }
    
    # Test 1: Simple repo info
    url = f"https://api.github.com/repos/{owner}/{repo}"
    print(f"\n🔍 Testing: GET {url}")
    try:
        response = requests.get(url, headers=headers, timeout=10)
        print(f"   Status: {response.status_code}")
        if response.status_code == 200:
            data = response.json()
            print(f"   ✅ Success: {data.get('full_name', 'Unknown')}")
            return True
        else:
            print(f"   ❌ Error: {response.text}")
            return False
    except Exception as e:
        print(f"   ❌ Exception: {e}")
        return False
    
def test_issues_api():
    """Test issues endpoint specifically."""
    token = os.getenv("GITHUB_TOKEN")
    if not token:
        return False
    
    owner, repo = extract_owner_repo()
    
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "Ouroboros-Debug"
    }
    
    # Test with parameters like the actual tool
    url = f"https://api.github.com/repos/{owner}/{repo}/issues"
    params = {"state": "all"}
    
    print(f"\n🔍 Testing issues endpoint: GET {url}")
    print(f"   Params: {params}")
    
    try:
        response = requests.get(url, headers=headers, params=params, timeout=10)
        print(f"   Status: {response.status_code}")
        print(f"   Response headers: {dict(response.headers)}")
        
        if response.status_code == 200:
            data = response.json()
            print(f"   ✅ Success, got {len(data) if isinstance(data, list) else 'unknown'} items")
            if data and isinstance(data, list):
                print(f"   First issue: #{data[0].get('number', 'N/A')} - {data[0].get('title', 'N/A')}")
            return True
        else:
            print(f"   ❌ Error: {response.text}")
            return False
    except Exception as e:
        print(f"   ❌ Exception: {e}")
        return False

if __name__ == "__main__":
    print("=== GitHub API Debug ===")
    
    if test_api():
        print("\n✅ Basic API test passed")
    else:
        print("\n❌ Basic API test failed")
    
    if test_issues_api():
        print("\n✅ Issues API test passed")
    else:
        print("\n❌ Issues API test failed")