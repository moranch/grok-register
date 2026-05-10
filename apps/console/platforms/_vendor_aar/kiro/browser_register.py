"""Kiro (AWS Builder ID) 浏览器注册流程（Camoufox）。

注册流程：
  1. 打开 app.kiro.dev/signin
  2. 点击 "AWS Builder ID" 选项
  3. 跳转到 us-east-1.signin.aws → profile.aws.amazon.com
  4. AWS Builder ID 注册 SPA：
     a. enter-email 步：确认/填写邮箱 → Continue
     b. enter-name 步：填写姓名 → Continue
     c. verify-email 步：填写 OTP → Continue
     d. create-password 步：设置密码 → Continue
  5. 跳回 app.kiro.dev，从 localStorage 提取 Cognito tokens
"""
import random
import string
import time
from typing import Callable, Optional
from urllib.parse import urlparse

from camoufox.sync_api import Camoufox

KIRO_URL = "https://app.kiro.dev"
AWS_SIGNIN_DOMAIN = "signin.aws"
AWS_PROFILE_DOMAIN = "profile.aws.amazon.com"


def _build_proxy_config(proxy: Optional[str]) -> Optional[dict]:
    if not proxy:
        return None
    parsed = urlparse(proxy)
    if not parsed.scheme or not parsed.hostname or not parsed.port:
        return {"server": proxy}
    config = {"server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"}
    if parsed.username:
        config["username"] = parsed.username
    if parsed.password:
        config["password"] = parsed.password
    return config


def _wait_for_url(page, substring: str, timeout: int = 60) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if substring in page.url:
            return True
        time.sleep(1)
    return False


def _js_click_by_text(page, *texts) -> bool:
    """用 JS 找到 textContent 精确匹配的最小叶节点并点击。"""
    for text in texts:
        try:
            clicked = page.evaluate(f"""
            () => {{
                const walker = document.createTreeWalker(
                    document.body, NodeFilter.SHOW_TEXT, null
                );
                let node;
                while (node = walker.nextNode()) {{
                    if (node.textContent.trim() === {repr(text)}) {{
                        const el = node.parentElement;
                        if (el) {{ el.click(); return true; }}
                    }}
                }}
                return false;
            }}
            """)
            if clicked:
                return True
        except Exception:
            pass
    return False


def _click_submit_button(page, timeout: int = 8) -> bool:
    """点击 submit 按钮（AWS 页面用 button[type=submit]）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        # 1. 优先 Playwright locator text 精确匹配
        for text in ["Continue", "Next", "Verify", "Create account", "Sign in", "Submit"]:
            try:
                el = page.locator(f'text="{text}"').last
                if el.is_visible():
                    el.click()
                    return True
            except Exception:
                pass
        # 2. button[type=submit]
        try:
            el = page.query_selector('button[type="submit"]:not([disabled])')
            if el and el.is_visible():
                el.click()
                return True
        except Exception:
            pass
        # 3. JS text walker
        if _js_click_by_text(page, "Continue", "Next", "Verify", "Create account"):
            return True
        time.sleep(0.5)
    return False


def _fill_input_wait(page, selectors: list, value: str, timeout: int = 20) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        for sel in selectors:
            try:
                el = page.query_selector(sel)
                if el and el.is_visible():
                    el.click()
                    el.fill(value)
                    return True
            except Exception:
                pass
        time.sleep(0.5)
    return False


def _get_kiro_tokens(page, timeout: int = 30) -> dict:
    """从 localStorage 提取 Cognito accessToken / refreshToken。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            result = page.evaluate("""
            () => {
                const out = {};
                for (const k of Object.keys(localStorage)) {
                    out[k] = localStorage.getItem(k);
                }
                return out;
            }
            """)
            access = refresh = id_token = ""
            for k, v in result.items():
                kl = k.lower()
                if "accesstoken" in kl and not access:
                    access = v
                if "refreshtoken" in kl and not refresh:
                    refresh = v
                if "idtoken" in kl and not id_token:
                    id_token = v
            if access:
                return {"accessToken": access, "refreshToken": refresh, "idToken": id_token}
        except Exception:
            pass
        time.sleep(2)
    return {}


def _random_name() -> str:
    first = ''.join(random.choices(string.ascii_lowercase, k=random.randint(4, 7))).capitalize()
    last = ''.join(random.choices(string.ascii_lowercase, k=random.randint(4, 7))).capitalize()
    return f"{first} {last}"


class KiroBrowserRegister:
    def __init__(
        self,
        *,
        headless: bool = True,
        proxy: Optional[str] = None,
        otp_callback: Optional[Callable[[], str]] = None,
        log_fn: Callable[[str], None] = print,
    ):
        self.headless = headless
        self.proxy = proxy
        self.otp_callback = otp_callback
        self.log = log_fn

    def _handle_aws_profile_spa(self, page, email: str, password: str) -> None:
        """处理 profile.aws.amazon.com 上的多步注册 SPA。
        
        步骤对应 URL hash：
          #/signup/enter-email  → 填/确认邮箱 → Continue
          #/signup/enter-name   → 填姓名 → Continue
          #/signup/verify-email → 填 OTP → Continue
          #/signup/create-password → 填密码 → Continue (可选)
        """
        deadline = time.time() + 300  # 最多等 5 分钟完成整个流程

        email_selectors = [
            'input[placeholder*="username@example.com"]',
            'input[type="email"]',
            'input[name="email"]',
            'input[name="username"]',
        ]
        name_selectors = [
            'input[placeholder*="Maria"]',
            'input[placeholder*="Your name" i]',
            'input[placeholder*="Full name" i]',
            'input[placeholder*="Full Name"]',
            'input[name="name"]',
            'input[name="fullName"]',
            'input[name="full_name"]',
            'input[id*="name" i]:not([id*="user" i]):not([id*="email" i]):not([id*="first" i]):not([id*="last" i])',
        ]
        otp_selectors = [
            'input[placeholder*="6"]',
            'input[name="otp"]',
            'input[name="code"]',
            'input[autocomplete="one-time-code"]',
            'input[inputmode="numeric"]',
            'input[maxlength="6"]',
        ]
        pwd_selectors = [
            'input[type="password"]',
            'input[name="password"]',
        ]

        handled_steps = set()
        enter_email_retries = 0
        prev_hash = None
        hash_stuck_since = None

        while time.time() < deadline:
            url = page.url
            hash_part = url.split("#")[-1] if "#" in url else ""

            # 跳回了 kiro.dev -> 完成
            if "kiro.dev" in url and "profile.aws" not in url and "signin.aws" not in url:
                return

            # 检测 hash 是否卡住（同一 hash 停留过久且已处理过）→ 允许重试
            if hash_part == prev_hash:
                if hash_stuck_since is None:
                    hash_stuck_since = time.time()
                elif time.time() - hash_stuck_since > 20:
                    step_key = hash_part.split("/")[-1]
                    if step_key in handled_steps:
                        self.log(f"⚠️ 步骤 {step_key} 卡住 20 秒，移除标记以重试")
                        handled_steps.discard(step_key)
                        hash_stuck_since = None
            else:
                prev_hash = hash_part
                hash_stuck_since = None

            # --- enter-email 步（邮箱+姓名在同一页）---
            if "enter-email" in hash_part and "enter-email" not in handled_steps:
                enter_email_retries += 1
                if enter_email_retries > 5:
                    raise RuntimeError(
                        f"AWS enter-email 步骤重试超 5 次仍无法前进 — "
                        f"邮箱域名可能被 AWS 拒绝 (url={page.url})"
                    )
                self.log(f"AWS 步骤: 确认邮箱 (第{enter_email_retries}次)")
                time.sleep(1.5)  # 给 SPA 渲染时间
                # 填邮箱（若为空）—— profile.aws 的 enter-email 页面可能：
                #   a) 有 email input 且预填了（从 signin.aws 带过来）→ 不用填
                #   b) 有 email input 但空 → 填入
                #   c) 根本没有 email input（只显示文本 + Continue）→ 直接 submit
                email_ok = False
                for sel in email_selectors:
                    try:
                        el = page.query_selector(sel)
                        if el and el.is_visible():
                            cur = el.input_value() or ""
                            if not cur:
                                el.click()
                                el.fill(email)
                                time.sleep(0.2)
                            email_ok = bool(el.input_value())
                            break
                    except Exception:
                        pass
                # 即使没找到 email input 也继续（情况 c），直接点 Continue
                if not email_ok:
                    self.log("enter-email 页无邮箱输入框（可能已预填），直接提交")

                # 注意：新版 AWS enter-email 页面只有邮箱，姓名在下一步 enter-name
                # 不要在这里填姓名——之前误匹配到 username/其它字段导致 AWS 拒绝提交
                time.sleep(0.5)
                _click_submit_button(page, timeout=8)
                handled_steps.add("enter-email")
                # 等待 hash 变化（最多 20 秒）。AWS 有时响应慢。
                start_wait = time.time()
                advanced = False
                while time.time() - start_wait < 20:
                    time.sleep(0.5)
                    new_url = page.url
                    new_hash = new_url.split("#")[-1] if "#" in new_url else ""
                    if new_hash != hash_part:
                        advanced = True
                        break  # hash 变了，进入下一步
                if not advanced:
                    # 20 秒后 hash 未变，提交可能失败，允许重试
                    self.log("⚠️ enter-email 提交后 URL 未变化，将重试")
                    handled_steps.discard("enter-email")
                    hash_stuck_since = time.time()
                continue

            # --- enter-name 步 ---
            # 新版 AWS 把 First name / Last name 拆成两个独立输入框；
            # 老版本是单个 Full name。两种都处理。
            if "enter-name" in hash_part and "enter-name" not in handled_steps:
                self.log("AWS 步骤: 填写姓名")
                time.sleep(1.5)
                name = _random_name()
                parts = name.split(" ", 1)
                first_name = parts[0] if parts else "User"
                last_name = parts[1] if len(parts) > 1 else "User"

                # 先尝试两个独立框
                first_sels = [
                    'input[placeholder*="First" i]',
                    'input[name*="first" i]',
                    'input[id*="first" i]',
                    'input[autocomplete="given-name"]',
                ]
                last_sels = [
                    'input[placeholder*="Last" i]',
                    'input[name*="last" i]',
                    'input[id*="last" i]',
                    'input[autocomplete="family-name"]',
                ]

                def _try_fill(sels, val):
                    for sel in sels:
                        try:
                            el = page.query_selector(sel)
                            if el and el.is_visible():
                                el.click()
                                time.sleep(0.1)
                                el.fill(val)
                                time.sleep(0.1)
                                if el.input_value():
                                    return True
                        except Exception:
                            pass
                    return False

                first_ok = False
                last_ok = False
                name_deadline = time.time() + 15
                while time.time() < name_deadline and not (first_ok and last_ok):
                    if not first_ok:
                        first_ok = _try_fill(first_sels, first_name)
                    if not last_ok:
                        last_ok = _try_fill(last_sels, last_name)
                    if first_ok and last_ok:
                        break
                    time.sleep(0.3)

                if first_ok and last_ok:
                    self.log(f"填写 First/Last name: {first_name} / {last_name}")
                else:
                    # Fallback：老版单 Full name
                    name_filled = False
                    fallback_deadline = time.time() + 10
                    while time.time() < fallback_deadline and not name_filled:
                        for sel in name_selectors:
                            try:
                                el = page.query_selector(sel)
                                if el and el.is_visible():
                                    el.click()
                                    time.sleep(0.2)
                                    el.fill(name)
                                    if el.input_value():
                                        self.log(f"填写 Full name: {name}")
                                        name_filled = True
                                        break
                            except Exception:
                                pass
                        if not name_filled:
                            time.sleep(0.5)
                    if not name_filled:
                        self.log("⚠️ enter-name 未找到姓名输入框")

                _click_submit_button(page, timeout=5)
                handled_steps.add("enter-name")
                time.sleep(2)
                continue

            # --- verify-email 步 ---
            # AWS 可能把这个步骤命名为 verify-email / verify-otp 两种 hash 之一（不同版本）
            if ("verify-email" in hash_part or "verify-otp" in hash_part) and "verify-email" not in handled_steps:
                self.log("AWS 步骤: 填写验证码")
                # 等待 OTP 输入框出现
                otp_el = None
                otp_deadline = time.time() + 30
                while time.time() < otp_deadline:
                    for sel in otp_selectors:
                        try:
                            el = page.query_selector(sel)
                            if el and el.is_visible():
                                otp_el = el
                                break
                        except Exception:
                            pass
                    if otp_el:
                        break
                    time.sleep(1)

                if not otp_el:
                    raise RuntimeError(f"未出现验证码输入框: {page.url}")

                if not self.otp_callback:
                    raise RuntimeError("Kiro 注册需要邮箱验证码但未提供 otp_callback")

                code = self.otp_callback()
                if not code:
                    raise RuntimeError("未获取到邮箱验证码")
                self.log(f"填写验证码: {code}")
                otp_el.click()
                for digit in str(code).strip():
                    page.keyboard.press(digit)
                    time.sleep(0.1)
                time.sleep(0.5)
                _click_submit_button(page, timeout=5)
                handled_steps.add("verify-email")
                time.sleep(2)
                continue

            # --- create-password 步 ---
            # AWS 有时会在 verify-otp 成功后直接替换组件为密码页但不改 hash，
            # 所以这里既匹配 hash 也主动查 DOM 上有没有可见的密码输入框。
            page_has_pwd = False
            try:
                pwd_el_check = page.query_selector('input[type="password"]')
                page_has_pwd = bool(pwd_el_check and pwd_el_check.is_visible())
            except Exception:
                page_has_pwd = False
            if ("create-password" in hash_part or page_has_pwd) and "create-password" not in handled_steps:
                self.log("AWS 步骤: 设置密码")
                time.sleep(1)
                pwd_fields = []
                for sel in pwd_selectors:
                    try:
                        els = page.query_selector_all(sel)
                        pwd_fields.extend([e for e in els if e.is_visible()])
                    except Exception:
                        pass
                if pwd_fields:
                    for f in pwd_fields:
                        try:
                            f.click()
                            f.fill(password)
                            time.sleep(0.2)
                        except Exception:
                            pass
                    time.sleep(0.5)
                    _click_submit_button(page, timeout=5)
                handled_steps.add("create-password")
                time.sleep(2)
                continue

            # 没有 hash 的情况：可能在中间跳转页，等待
            time.sleep(1)

        raise RuntimeError(f"AWS Builder ID 注册未在规定时间内完成: {page.url}")

    def run(self, email: str, password: str) -> dict:
        if not self.otp_callback:
            raise RuntimeError("Kiro 注册需要邮箱验证码但未提供 otp_callback")

        if not password:
            password = (
                ''.join(random.choices(string.ascii_uppercase, k=2))
                + ''.join(random.choices(string.digits, k=3))
                + ''.join(random.choices(string.ascii_lowercase, k=5))
                + '!'
            )

        proxy = _build_proxy_config(self.proxy)
        launch_opts = {"headless": self.headless}
        if proxy:
            launch_opts["proxy"] = proxy

        with Camoufox(**launch_opts) as browser:
            page = browser.new_page()

            # 1. 打开 Kiro 登录页
            self.log("打开 Kiro 登录页")
            page.goto(f"{KIRO_URL}/signin", wait_until="domcontentloaded", timeout=30000)
            time.sleep(2)

            # 2. 点击 AWS Builder ID 选项
            self.log("选择 AWS Builder ID 登录方式")
            builder_clicked = False
            deadline_builder = time.time() + 15
            while time.time() < deadline_builder and not builder_clicked:
                # Playwright locator 文本精确匹配
                for text in ["Builder ID", "AWS Builder ID"]:
                    try:
                        el = page.locator(f'text="{text}"').last
                        if el.is_visible():
                            el.click()
                            builder_clicked = True
                            self.log(f"点击了 {text}")
                            break
                    except Exception:
                        pass
                if not builder_clicked:
                    # JS walker fallback
                    builder_clicked = _js_click_by_text(page, "Builder ID", "AWS Builder ID")
                    if builder_clicked:
                        self.log("点击了 Builder ID (JS)")
                if not builder_clicked:
                    time.sleep(0.5)

            time.sleep(2)

            # 3. 可能有二级 "Sign in" 箭头（Kiro 的选项卡 UI）
            _click_submit_button(page, timeout=5)
            time.sleep(2)

            # 4. 等待进入 AWS 域名
            self.log("等待 AWS 登录页...")
            if not _wait_for_url(page, AWS_SIGNIN_DOMAIN, timeout=30):
                if AWS_PROFILE_DOMAIN not in page.url:
                    raise RuntimeError(f"未跳转到 AWS 登录页: {page.url}")

            time.sleep(2)

            # 5. 如果落在 signin.aws（已有账号登录页），先填邮箱提交
            if AWS_SIGNIN_DOMAIN in page.url:
                self.log(f"填写邮箱: {email}")
                email_selectors = [
                    'input[placeholder*="username@example.com"]',
                    'input[type="email"]',
                    'input[name="email"]',
                    'input[name="username"]',
                ]
                if not _fill_input_wait(page, email_selectors, email, timeout=15):
                    raise RuntimeError(f"未找到邮箱输入框: {page.url}")
                time.sleep(0.5)
                _click_submit_button(page, timeout=8)
                time.sleep(3)

            # 6. 填完邮箱 submit 后直接等跳转到 profile.aws（新账号注册流程）
            # signin.aws 上没有密码框，密码在 profile.aws 最后一步才填
            self.log("等待跳转到注册流程...")
            if not _wait_for_url(page, AWS_PROFILE_DOMAIN, timeout=90):
                if "kiro.dev" in page.url:
                    self.log("已有账号，直接登录成功")
                else:
                    raise RuntimeError(f"AWS 注册流程未跳转到 profile.aws: {page.url}")

            if AWS_PROFILE_DOMAIN in page.url:
                self.log("进入 AWS Builder ID 注册流程...")
                self._handle_aws_profile_spa(page, email, password)

            # 7. 等待跳回 kiro.dev
            self.log("等待跳回 Kiro...")
            if not _wait_for_url(page, "kiro.dev", timeout=60):
                raise RuntimeError(f"Kiro 注册未跳转回应用: {page.url}")

            time.sleep(3)

            # 8. 提取 Cognito tokens
            self.log("提取 Kiro 访问令牌...")
            tokens = _get_kiro_tokens(page, timeout=20)

            self.log(f"✓ 注册成功: {email}")
            return {
                "email": email,
                "password": password,
                "accessToken": tokens.get("accessToken", ""),
                "refreshToken": tokens.get("refreshToken", ""),
                "idToken": tokens.get("idToken", ""),
                "sessionToken": "",
                "clientId": "",
                "clientSecret": "",
            }
