#!/usr/bin/env python3
"""Test calling GitHub tools directly."""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Import the tool directly
import importlib
try:
    github_module = importlib.import_module("ouroboros.tools.github")
    print("✅ Imported github module")
    
    # Test calling list_github_issues directly
    print("\n🔍 Testing list_github_issues:")
    
    # Test 1: With no arguments (should use defaults)
    print("Test 1: No arguments")
    try:
        result = github_module.list_github_issues(None, {})  # ctx, args
        print(f"Result: {result}")
    except Exception as e:
        print(f"Error: {e}")
    
    # Test 2: With state argument
    print("\nTest 2: state='open'")
    try:
        result = github_module.list_github_issues(None, {"state": "open"})
        print(f"Result: {result}")
    except Exception as e:
        print(f"Error: {e}")
        
except Exception as e:
    print(f"❌ Error: {e}")
    import traceback
    traceback.print_exc()