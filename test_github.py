import os, subprocess
print("Testing GitHub token...")
token = os.environ.get("GITHUB_TOKEN")
if token:
    print(f"Token exists, first 10 chars: {token[:10]}")
else:
    print("No token")
    
cmd = ["curl", "-s", "-H", f"Authorization: token {token}", "https://api.github.com/repos/stakeswky/ouroboros/issues?state=open&per_page=1"]
try:
    r = subprocess.run(cmd, capture_output=True)
    print(f"Curl status: {r.returncode}")
    output = r.stdout.decode()[:200]
    print(f"Response preview: {output}")
    if "401" in output:
        print("⚠️ 401 Unauthorized — token invalid/expired")
    elif "200" in output or "[" in output:
        print("✅ Token works")
except Exception as e:
    print(f"Error: {e}")