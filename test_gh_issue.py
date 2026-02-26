#!/usr/bin/env python3
import sys
import os
sys.path.insert(0, '.')
os.chdir('/content/ouroboros_repo')

# Direct test of the tool function
from ouroboros.tools.github import list_github_issues as tool_fn
import requests

# Test with simplest call
print("Testing list_github_issues...")
try:
    result = tool_fn(state='all', label='')
    print('Result:', result)
except requests.exceptions.HTTPError as e:
    print('HTTPError:', e)
    print('Response:', e.response.text[:500])
except Exception as e:
    print('Error:', e)
    import traceback
    traceback.print_exc()