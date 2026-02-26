import os
print("Testing GitHub token...")
token = os.environ.get("GITHUB_TOKEN")
if token:
    print(f"Token exists, first 10 chars: {token[:10]}")
else:
    print("No token")
    
# Try curl via shell
import subprocess
r = subprocess.run(["curl", "-s", "-H", f"Authorization: token {token}", "https://api.github.com/repos/stakeswky/ouroboros/issues?state=open&per_page=1"], capture_output=True, text=True)
print(f"Curl status: {r.returncode}")
print(f"Response preview: {r.text[:200]}")