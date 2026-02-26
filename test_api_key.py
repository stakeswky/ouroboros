#!/usr/bin/env python3
import os
import requests

api_key = os.getenv('OPENROUTER_API_KEY', '')
print(f'API Key: {api_key[:10]}...' if api_key else 'API Key: NOT SET')

if api_key:
    # 测试siliconflow.cn的模型列表
    url = 'https://api.siliconflow.cn/v1/models'
    headers = {'Authorization': f'Bearer {api_key}'}
    
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        print(f'Status Code: {resp.status_code}')
        
        if resp.status_code == 200:
            data = resp.json()
            models = data.get('data', [])
            print(f'总模型数: {len(models)}')
            
            # 检查MiniMax
            minimax_models = [m for m in models if 'minimax' in m.get('id', '').lower()]
            print(f'MiniMax 模型数: {len(minimax_models)}')
            for m in minimax_models:
                model_id = m.get('id', '')
                model_name = m.get('name', 'N/A')
                pricing = m.get('pricing', {})
                prompt_price = pricing.get('prompt', 'N/A')
                print(f'  {model_id} - {model_name} (输入: ${prompt_price}/token)')
            
            # 检查DeepSeek
            deepseek_models = [m for m in models if 'deepseek' in m.get('id', '').lower()]
            print(f'DeepSeek 模型数: {len(deepseek_models)}')
            for m in deepseek_models[:5]:
                print(f'  {m.get("id", "")} - {m.get("name", "N/A")}')
            
        else:
            try:
                error_data = resp.json()
                print(f'错误响应: {error_data}')
            except:
                print(f'响应文本: {resp.text[:200]}')
    
    except Exception as e:
        print(f'请求错误: {e}')
else:
    print('没有API key，无法进行测试')