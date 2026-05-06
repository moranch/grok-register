#!/usr/bin/env python3
"""测试完整的注册流程"""
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from email_register import (
    get_email_and_token,
    get_oai_code,
    fetch_emails,
    fetch_email_detail,
    _extract_mail_content,
    extract_verification_code
)

def test_flow():
    print("=" * 60)
    print("测试TMail完整流程")
    print("=" * 60)
    
    # 1. 创建邮箱
    print("\n[1] 创建临时邮箱...")
    email, mail_token = get_email_and_token()
    if not email:
        print("[错误] 创建邮箱失败")
        return
    print(f"    邮箱: {email}")
    print(f"    Token: {mail_token[:20]}...")
    
    # 2. 获取邮件列表
    print("\n[2] 获取邮件列表...")
    messages = fetch_emails(mail_token, email)
    print(f"    邮件数量: {len(messages)}")
    if messages:
        print(f"    第一封邮件: {messages[0]}")
    
    # 3. 检查邮件格式
    if messages:
        msg = messages[0]
        print(f"\n[3] 邮件格式检查:")
        print(f"    类型: {type(msg)}")
        print(f"    字段: {list(msg.keys()) if isinstance(msg, dict) else 'N/A'}")
        print(f"    ID: {msg.get('id', '无id字段')}")
        
        # 4. 获取邮件详情
        msg_id = msg.get("id")
        if msg_id:
            print(f"\n[4] 获取邮件详情 (ID: {msg_id})...")
            detail = fetch_email_detail(mail_token, str(msg_id))
            print(f"    详情: {detail}")
            
            if detail:
                content = _extract_mail_content(detail)
                print(f"\n[5] 提取邮件内容:")
                print(f"    内容: {content[:200]}...")
                
                code = extract_verification_code(content)
                print(f"\n[6] 提取验证码:")
                print(f"    验证码: {code}")
    
    print("\n" + "=" * 60)
    print("测试完成")
    print("=" * 60)

if __name__ == "__main__":
    test_flow()
