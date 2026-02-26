#!/usr/bin/env python3
import urllib.request
import json
import sys

print("获取OpenRouter模型列表...")
try:
    url = "https://openrouter.ai/api/v1/models"
    response = urllib.request.urlopen(url)
    data = json.load(response)
    
    models = data.get("data", [])
    print(f"总共找到 {len(models)} 个模型")
    
    # 查找MiniMax相关模型
    minimax_models = []
    for model in models:
        model_id = model.get("id", "").lower()
        if "minimax" in model_id:
            minimax_models.append(model)
    
    print(f"\n=== MiniMax模型 ({len(minimax_models)}个) ===")
    if minimax_models:
        for model in minimax_models:
            print(f"ID: {model.get('id')}")
            print(f"名称: {model.get('name', 'N/A')}")
            desc = model.get("description", "无描述")
            if len(desc) > 150:
                desc = desc[:150] + "..."
            print(f"描述: {desc}")
            pricing = model.get("pricing", {})
            if pricing:
                print(f"价格: prompt=${pricing.get('prompt', '?')}/1K tokens, completion=${pricing.get('completion', '?')}/1K tokens")
            print("-" * 50)
    else:
        print("未找到MiniMax模型")
        print("\n搜索其他可能相关的模型...")
        related = []
        for model in models:
            model_id = model.get("id", "").lower()
            model_name = model.get("name", "").lower()
            if "pro" in model_id or "mini" in model_id or "max" in model_id:
                related.append(model)
        
        for model in related[:10]:  # 只显示前10个
            print(f"相关: {model.get('id')} - {model.get('name', 'N/A')}")
    
    # 显示热门模型
    print("\n=== 热门可用模型 ===")
    hot_ids = ["deepseek-ai/DeepSeek-V3.2", "openai/gpt-5.3-codex", "anthropic/claude-sonnet-4.6", "google/gemini-3.1-pro-preview", "qwen/qwen3.5-flash-02-23"]
    for model in models:
        if model.get("id") in hot_ids:
            print(f"\n{model.get('id')}")
            print(f"  名称: {model.get('name', 'N/A')}")
            print(f"  上下文长度: {model.get('context_length', 'N/A')}")
            pricing = model.get("pricing", {})
            if pricing:
                print(f"  价格: prompt=${pricing.get('prompt', '?')}/1K tokens")
                print(f"         completion=${pricing.get('completion', '?')}/1K tokens")
    
    # 检查当前模型是否存在
    print(f"\n=== 当前模型检查 ===")
    current_model = "deepseek-ai/DeepSeek-V3.2"
    found = False
    for model in models:
        if model.get("id") == current_model:
            found = True
            print(f"✓ 当前模型 '{current_model}' 在OpenRouter中可用")
            break
    
    if not found:
        print(f"⚠️ 当前模型 '{current_model}' 未在OpenRouter中找到")
        print("可能的原因:")
        print("1. 模型ID可能需要更新")
        print("2. DeepSeek可能通过不同的渠道提供")
        print("3. OpenRouter的列表可能已过时")
    
except Exception as e:
    print(f"错误: {e}")
    sys.exit(1)