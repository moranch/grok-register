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
    deadline = time.time() + timeout
    while time.time() < deadline:
        for text in ["Continue", "Next", "Verify", "Create account", "Sign in", "Submit"]:
            try:
                el = page.locator(f'text="{text}"').last
                if el.is_visible():
                    el.click()
                    return True
            except Exception:
                pass
        try:
            el = page.query_selector('button[type="submit"]:not([disabled])')
            if el and el.is_visible():
                el.click()
                return True
        except Exception:
            pass
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


def _get_kiro_tokens(page, timeout: int = 60) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            result = page.evaluate("""
            () => {
                const out = {};
                for (const k of Object.keys(localStorage)) {
                    out[k] = localStorage.getItem(k);
                }
                for (const k of Object.keys(sessionStorage)) {
                    out['__session__' + k] = sessionStorage.getItem(k);
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
            # cookie fallback
            try:
                cookies = page.context.cookies()
                for c in cookies:
                    cn = c.get("name", "").lower()
                    cv = c.get("value", "")
                    if cn == "__secure-authjs.session-token" and not access:
                        access = cv
                    elif "accesstoken" in cn and not access:
                        access = cv
                    if "refreshtoken" in cn and not refresh:
                        refresh = cv
                    if "idtoken" in cn and not id_token:
                        id_token = cv
                if access:
                    return {"accessToken": access, "refreshToken": refresh, "idToken": id_token}
            except Exception:
                pass
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
        deadline = time.time() + 300

        email_selectors = [
            'input[placeholder*="username@example.com"]',
            'input[type="email"]',
            'input[name="email"]',
            'input[name="username"]',
        ]
        name_selectors = [
            'input[placeholder*="Maria"]',
            'input[placeholder*="name" i]',
            'input[name="name"]',
            'input[name="fullName"]',
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

            if "kiro.dev" in url and "profile.aws" not in url and "signin.aws" not in url:
                return

            if hash_part == prev_hash:
                if hash_stuck_since is None:
                    hash_stuck_since = time.time()
                elif time.time() - hash_stuck_since > 20:
                    step_key = hash_part.split("/")[-1]
                    if step_key in handled_steps:
                        self.log(f"步骤 {step_key} 卡住 20 秒，重试")
                        handled_steps.discard(step_key)
                        hash_stuck_since = None
            else:
                prev_hash = hash_part
                hash_stuck_since = None

            # --- enter-email ---
            if "enter-email" in hash_part and "enter-email" not in handled_steps:
                enter_email_retries += 1
                if enter_email_retries > 5:
                    raise RuntimeError(f"enter-email 重试超 5 次: {page.url}")
                self.log(f"AWS: enter-email (第{enter_email_retries}次)")
                time.sleep(1.5)
                for sel in email_selectors:
                    try:
                        el = page.query_selector(sel)
                        if el and el.is_visible():
                            cur = el.input_value() or ""
                            if not cur:
                                el.fill(email)
                            break
                    except Exception:
                        pass
                # 同页可能有 name 输入框
                name = _random_name()
                name_filled = False
                name_deadline = time.time() + 10
                while time.time() < name_deadline and not name_filled:
                    for sel in name_selectors:
                        try:
                            el = page.query_selector(sel)
                            if el and el.is_visible():
                                el.click()
                                time.sleep(0.2)
                                el.fill(name)
                                if el.input_value():
                                    self.log(f"填写姓名: {name}")
                                    name_filled = True
                                    break
                        except Exception:
                            pass
                    if not name_filled:
                        time.sleep(0.5)
                if not name_filled:
                    self.log("enter-email 页无姓名框，直接提交")
                time.sleep(0.5)
                _click_submit_button(page, timeout=8)
                handled_steps.add("enter-email")
                start_wait = time.time()
                while time.time() - start_wait < 15:
                    time.sleep(0.5)
                    new_hash = page.url.split("#")[-1] if "#" in page.url else ""
                    if new_hash != hash_part:
                        break
                else:
                    self.log("enter-email 提交后 URL 未变化，重试")
                    handled_steps.discard("enter-email")
                    hash_stuck_since = time.time()
                continue

            # --- enter-name ---
            if "enter-name" in hash_part and "enter-name" not in handled_steps:
                self.log("AWS: enter-name")
                time.sleep(1.5)
                name = _random_name()
                name_filled = False
                name_deadline = time.time() + 15
                while time.time() < name_deadline and not name_filled:
                    for sel in name_selectors:
                        try:
                            el = page.query_selector(sel)
                            if el and el.is_visible():
                                el.click()
                                time.sleep(0.2)
                                el.fill(name)
                                if el.input_value():
                                    self.log(f"填写姓名: {name}")
                                    name_filled = True
                                    break
                        except Exception:
                            pass
                    if not name_filled:
                        time.sleep(0.5)
                _click_submit_button(page, timeout=5)
                handled_steps.add("enter-name")
                time.sleep(2)
                continue

            # --- verify-email / verify-otp ---
            if ("verify-email" in hash_part or "verify-otp" in hash_part) and "verify-email" not in handled_steps:
                self.log("AWS: verify-email")
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
                    raise RuntimeError("需要 otp_callback")
                code = self.otp_callback()
                if not code:
                    raise RuntimeError("未获取到验证码")
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

            # --- create-password (hash or DOM) ---
            page_has_pwd = False
            try:
                pwd_check = page.query_selector('input[type="password"]')
                page_has_pwd = bool(pwd_check and pwd_check.is_visible())
            except Exception:
                pass
            if ("create-password" in hash_part or page_has_pwd) and "create-password" not in handled_steps:
                self.log("AWS: create-password")
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

            time.sleep(1)

        raise RuntimeError(f"AWS Builder ID 注册未在规定时间内完成: {page.url}")

    def run(self, email: str, password: str) -> dict:
        if not self.otp_callback:
            raise RuntimeError("需要 otp_callback")

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

            # 2. 点击 AWS Builder ID
            self.log("选择 AWS Builder ID 登录方式")
            builder_clicked = False
            deadline_builder = time.time() + 15
            while time.time() < deadline_builder and not builder_clicked:
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
                    builder_clicked = _js_click_by_text(page, "Builder ID", "AWS Builder ID")
                    if builder_clicked:
                        self.log("点击了 Builder ID (JS)")
                if not builder_clicked:
                    time.sleep(0.5)

            time.sleep(2)

            # 3. 可能有二级 Sign in 按钮
            _click_submit_button(page, timeout=5)
            time.sleep(2)

            # 4. 等待 AWS 域名
            self.log("等待 AWS 登录页...")
            if not _wait_for_url(page, AWS_SIGNIN_DOMAIN, timeout=30):
                if AWS_PROFILE_DOMAIN not in page.url:
                    raise RuntimeError(f"未跳转到 AWS: {page.url}")

            time.sleep(2)

            # 5. signin.aws 填邮箱
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

            # 6. signin.aws 填完邮箱后页面会跳到 profile.aws
            # 某些环境 page.url 不会立刻更新，所以不死等 URL
            # 短暂等待后直接进入 SPA 主循环（它自己会读 page.url / hash）
            time.sleep(5)
            if "kiro.dev" in page.url:
                self.log("已有账号，直接登录成功")
            else:
                self.log("进入注册流程...")
                self._handle_aws_profile_spa(page, email, password)

            # 7. 等待跳回 kiro.dev
            self.log("等待跳回 Kiro...")
            if not _wait_for_url(page, "kiro.dev", timeout=60):
                raise RuntimeError(f"Kiro 注册未跳转回应用: {page.url}")

            time.sleep(3)

            # 8. 提取 tokens
            self.log("提取 Kiro 访问令牌...")
            tokens = _get_kiro_tokens(page, timeout=60)
            if not tokens:
                try:
                    keys = page.evaluate("() => Object.keys(localStorage)")
                    self.log(f"localStorage keys: {keys[:20]}")
                except Exception:
                    pass

            # 尝试查询套餐信息（用 accessToken + sessionToken cookie）
            account_overview = {}
            access_token = tokens.get("accessToken", "")
            authjs_session = ""
            aws_session = ""
            try:
                cookies = page.context.cookies()
                for c in cookies:
                    cn = c.get("name", "")
                    cv = c.get("value", "")
                    if cn == "__Secure-authjs.session-token" and cv:
                        authjs_session = cv
                    elif cn == "SessionToken" and cv:
                        aws_session = cv
            except Exception:
                pass

            # portal API 需要 authjs session（不是 AWS SSO bearer）
            portal_session = authjs_session
            if access_token and portal_session:
                try:
                    from platforms._vendor_aar.kiro.switch import get_kiro_portal_state, summarize_kiro_usage
                    self.log("查询 Kiro 套餐信息...")
                    portal_state = get_kiro_portal_state(access_token, session_token)
                    if portal_state and portal_state.get("available"):
                        summary = summarize_kiro_usage(portal_state)
                        if summary:
                            plan_title = summary.get("plan_title") or summary.get("subscription_type") or ""
                            account_overview = {
                                "plan_name": plan_title or "Free",
                                "plan_state": "free" if "free" in (plan_title or "free").lower() else plan_title.lower(),
                                "user_email": summary.get("user_email", ""),
                                "user_status": summary.get("user_status", ""),
                                "breakdowns": summary.get("breakdowns", []),
                            }
                            self.log(f"套餐: {plan_title or 'Free'}")
                    else:
                        self.log(f"套餐查询返回空: {portal_state}")
                except Exception as exc:
                    self.log(f"查询套餐失败（不影响注册）: {exc}")

            self.log(f"✓ 注册成功: {email}")
            return {
                "email": email,
                "password": password,
                "accessToken": access_token,
                "refreshToken": tokens.get("refreshToken", ""),
                "idToken": tokens.get("idToken", ""),
                "sessionToken": aws_session or authjs_session,
                "authjs_session": authjs_session,
                "clientId": "",
                "clientSecret": "",
                "account_overview": account_overview,
            }
