#!/usr/bin/env python3
import os
import json
import urllib.request
import traceback

print("测试MiniMax M2.5模型...")

api_key = os.getenv('OPENROUTER_API_KEY', '')
if not api_key:
    print('错误: OPENROUTER_API_KEY未设置')
    exit(1)

print(f"使用API Key: {api_key[:10]}...")

# 测试调用MiniMax模型
headers = {
    'Authorization': f'Bearer {api_key}',
    'Content-Type': 'application/json',
    'HTTP-Referer': 'https://ouroboros.ai',
    'X-Title': 'Ouroboros'
}

data = {
    'model': 'minimax/minimax-m2.5',
    'messages': [{'role': 'user', 'content': '简单测试：这是一条测试消息。请回复"收到测试"。'}],
    'max_tokens': 50
}

print(f"请求数据: {json.dumps(data, ensure_ascii=False)}")

try:
    req = urllib.request.Request(
        'https://openrouter.ai/api/v1/chat/completions',
        headers=headers,
        data=json.dumps(data).encode('utf-8'),
        method='POST'
    )
    response = urllib.request.urlopen(req)
    result = json.load(response)
    print('✓ MiniMax模型测试成功')
    response_content = result.get('choices', [{}])[0].get('message', {}).get('content', 'N/A')
    print(f'响应: {response_content}')
except Exception as e:
    print(f'✗ 测试失败: {type(e).__name__}')
    error_detail = str(e)
    print(f'错误详情: {error_detail}')
    
    # 尝试获取更多错误信息
    if hasattr(e, 'read'):
        try:
            error_body = e.read().decode('utf-8')
            print(f'错误响应体: {error_body[:200]}')
        except:
            pass