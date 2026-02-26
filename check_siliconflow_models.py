#!/usr/bin/env python3
import requests
import json

print("检查siliconflow.cn上的可用模型...")

try:
    url = 'https://api.siliconflow.cn/v1/models'
    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    
    data = resp.json()
    models = data.get('data', [])
    
    print(f"总共 {len(models)} 个模型")
    print()
    
    # 检查MiniMax模型
    minimax_models = [m for m in models if 'minimax' in m.get('id', '').lower()]
    print(f"=== MiniMax模型: {len(minimax_models)} ===")
    for m in minimax_models:
        model_id = m.get('id', '')
        model_name = m.get('name', 'N/A')
        pricing = m.get('pricing', {})
        prompt_price = pricing.get('prompt', 'N/A')
        completion_price = pricing.get('completion', 'N/A')
        print(f"  {model_id}")
        print(f"    名称: {model_name}")
        print(f"    输入价格: {prompt_price} $/token")
        print(f"    输出价格: {completion_price} $/token")
        print()
    
    # 检查DeepSeek模型
    deepseek_models = [m for m in models if 'deepseek' in m.get('id', '').lower()]
    print(f"=== DeepSeek模型: {len(deepseek_models)} ===")
    for m in deepseek_models:
        model_id = m.get('id', '')
        model_name = m.get('name', 'N/A')
        print(f"  {model_id} - {model_name}")
    
    print()
    print("=== 前20个可用模型示例 ===")
    for m in models[:20]:
        model_id = m.get('id', '')
        model_name = m.get('name', 'N/A')
        pricing = m.get('pricing', {})
        has_pricing = '✓' if pricing.get('prompt') else '✗'
        print(f"  [{has_pricing}] {model_id} - {model_name}")
    
except Exception as e:
    print(f"错误: {e}")