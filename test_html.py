#!/usr/bin/env python3
"""测试HTML验证码提取"""
from email_register import extract_verification_code, _html_to_text

html_content = '''<!doctype html>
<html>
<body>
<h1>Validate your email</h1>
<p>Hi,</p>
<p>Thank you for creating an xAI account. Please use the code below to validate your email address.</p>
<table>
<tbody>
<tr>
<td style="text-align: center; background: #FAFAFA; padding: 30px 20px; font-size: 26px; font-weight: bold;">1YN-T65</td>
</tr>
</tbody>
</table>
</body>
</html>'''

# 测试从HTML提取验证码
text = _html_to_text(html_content)
print('HTML转文本:')
print(text)
print()

# 测试提取验证码
code = extract_verification_code(html_content)
print(f'从HTML提取验证码: {code}')

code2 = extract_verification_code(text)
print(f'从纯文本提取验证码: {code2}')
