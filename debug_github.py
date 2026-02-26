import os
import requests

def test_github_api():
    token = os.getenv('GITHUB_TOKEN')
    if not token:
        print("GITHUB_TOKEN not found")
        return
        
    print(f"Token preview: {token[:10]}...")
    print(f"Token length: {len(token)}")
    
    headers = {
        'Authorization': f'Bearer {token}',
        'Accept': 'application/vnd.github.v3+json',
        'User-Agent': 'Test'
    }
    
    # Test basic repo access
    url = 'https://api.github.com/repos/stakeswky/ouroboros'
    print(f"\nTesting repo access: {url}")
    
    try:
        response = requests.get(url, headers=headers, timeout=10)
        print(f"Response status: {response.status_code}")
        if response.status_code == 200:
            data = response.json()
            print(f"Repo: {data.get('full_name')}")
            print(f"Description: {data.get('description')}")
            print(f"Private: {data.get('private')}")
        else:
            print(f"Error: {response.text[:200]}")
    except Exception as e:
        print(f"Error during repo access: {e}")
    
    # Test issues endpoint like the tool does
    url = 'https://api.github.com/repos/stakeswky/ouroboros/issues'
    params = {'state': 'all', 'per_page': 5}
    print(f"\nTesting issues endpoint: {url}")
    print(f"Params: {params}")
    
    try:
        response = requests.get(url, headers=headers, params=params, timeout=10)
        print(f"Response status: {response.status_code}")
        print(f"Response headers:")
        for key, value in response.headers.items():
            if 'rate' in key.lower() or 'link' in key.lower():
                print(f"  {key}: {value}")
        
        if response.status_code != 200:
            print(f"Error details: {response.text}")
        else:
            issues = response.json()
            print(f"Found {len(issues)} issues/pull requests")
            
            # Filter out pull requests
            actual_issues = [i for i in issues if 'pull_request' not in i]
            print(f"Actual issues (excl. PRs): {len(actual_issues)}")
            
            for issue in actual_issues[:3]:
                print(f"  #{issue['number']}: {issue['title']}")
    except Exception as e:
        print(f"Error during issues access: {e}")

if __name__ == '__main__':
    test_github_api()