#!/usr/bin/env python3
"""测试TMail接口"""
import requests
import time

BASE_URL = "https://mail.nnioj.com"
API_KEY = "sk_727Bf97UTf15-m1WGw6G-XlV6ELRsmjQ"
HEADERS = {"Authorization": f"Bearer {API_KEY}"}

def test_tmail():
    # 1. 创建邮箱
    print("[1] 创建邮箱...")
    res = requests.get(f"{BASE_URL}/api/generate", params={"ttl": 120}, headers=HEADERS, timeout=20)
    print(f"    状态码: {res.status_code}")
    data = res.json()
    print(f"    响应: {data}")
    
    email = data.get("email")
    if not email:
        print("[错误] 未获取到邮箱地址")
        return
    
    print(f"    邮箱: {email}")
    
    # 2. 获取邮件列表
    print("\n[2] 获取邮件列表...")
    res = requests.get(f"{BASE_URL}/api/fetch", params={"to": email}, headers=HEADERS, timeout=20)
    print(f"    状态码: {res.status_code}")
    emails = res.json()
    print(f"    响应类型: {type(emails)}")
    print(f"    响应: {emails}")
    
    # 3. 检查邮件格式
    if emails and len(emails) > 0:
        msg = emails[0]
        print(f"\n[3] 第一封邮件格式:")
        print(f"    类型: {type(msg)}")
        print(f"    内容: {msg}")
        print(f"    ID字段: {msg.get('id', '无id字段')}")
        
        # 4. 获取邮件详情
        msg_id = msg.get("id")
        if msg_id:
            print(f"\n[4] 获取邮件详情 (ID: {msg_id})...")
            res = requests.get(f"{BASE_URL}/api/fetch/{msg_id}", headers=HEADERS, timeout=20)
            print(f"    状态码: {res.status_code}")
            detail = res.json()
            print(f"    响应: {detail}")
    else:
        print("\n[3] 暂无邮件（这是正常的，因为刚创建）")
    
    print("\n[完成] 测试结束")

if __name__ == "__main__":
    test_tmail()
