"""
桥接层：把我们的邮箱池（api/_shared 的 mailbox_pick_best + TMail API）
包装成上游 vendor BaseMailbox 接口，让 vendor 的 register() 能用我们的邮箱。
"""
from __future__ import annotations

import time
import logging
from typing import Set

from core._vendor_aar.base_mailbox import BaseMailbox, MailboxAccount
from api._shared import fetch_all, fetch_one, execute_no_return, now_iso

logger = logging.getLogger(__name__)


def _pick_mailbox_provider() -> dict | None:
    """从我们的 mailbox_providers 表里加权挑一个启用的 provider。"""
    rows = fetch_all("SELECT * FROM mailbox_providers WHERE enabled = 1")
    if not rows:
        return None
    import random
    weights = []
    for r in rows:
        s = int(r["success_count"])
        f = int(r["failure_count"])
        weights.append((s + 1) / (s + f + 2))
    chosen = random.choices(rows, weights=weights, k=1)[0]
    return dict(chosen)


class BridgeMailbox(BaseMailbox):
    """用我们系统的邮箱 provider 实现上游 BaseMailbox 接口。"""

    def __init__(self, proxy: str = ""):
        self.proxy = proxy
        self._provider: dict | None = None

    def _get_provider(self) -> dict:
        if self._provider is None:
            self._provider = _pick_mailbox_provider()
            if self._provider is None:
                raise RuntimeError(
                    "无可用邮箱 Provider，请在邮箱 Provider 页添加并启用至少一个"
                )
        return self._provider

    def _session(self):
        from curl_cffi.requests import Session
        s = Session(impersonate="chrome131")
        if self.proxy:
            s.proxies = {"http": self.proxy, "https": self.proxy}
        return s

    def get_email(self) -> MailboxAccount:
        """创建临时邮箱。"""
        provider = self._get_provider()
        api_base = provider["api_base"].rstrip("/")
        provider_type = provider.get("provider_type", "tmail")

        session = self._session()

        if provider_type in ("tmail", "moemail"):
            # TMail v3 (mail.nnioj.com 这类 Astro 前端的临时邮箱服务):
            #   GET /api/generate  → 生成邮箱
            #   GET /api/fetch?to=<email>  → 拉邮件列表（返回 JSON 数组）
            # 认证：Authorization: Bearer <api_key>（没有 key 会提示 "no available domains"）
            admin_password = provider.get("admin_password", "") or ""
            headers = {}
            if admin_password:
                headers["Authorization"] = f"Bearer {admin_password}"

            resp = session.get(
                f"{api_base}/api/generate",
                headers=headers,
                timeout=20,
            )
            if resp.status_code != 200:
                raise RuntimeError(
                    f"TMail 邮箱创建失败: {resp.status_code} - {resp.text[:200]}"
                )
            data = resp.json()
            email = data.get("email", "")
            if not email:
                raise RuntimeError(f"TMail 创建邮箱未返回 email: {data}")
            return MailboxAccount(email=email, account_id=admin_password, extra={
                "provider_id": provider["id"],
                "api_base": api_base,
                "provider_type": provider_type,
                "admin_password": admin_password,
            })

        elif provider_type == "duckmail":
            # DuckMail: POST /accounts
            site_password = provider.get("site_password", "") or ""
            domain = provider.get("domain", "") or ""
            if not domain:
                # 先拉 domains
                dr = session.get(f"{api_base}/domains", timeout=10)
                dr.raise_for_status()
                domains = dr.json()
                if isinstance(domains, list) and domains:
                    domain = domains[0] if isinstance(domains[0], str) else domains[0].get("domain", "")

            import random, string
            username = "".join(random.choices(string.ascii_lowercase + string.digits, k=10))
            password = "".join(random.choices(string.ascii_letters + string.digits, k=12))
            payload = {"address": f"{username}@{domain}", "password": password}
            headers = {}
            if site_password:
                headers["x-site-password"] = site_password

            resp = session.post(
                f"{api_base}/accounts",
                json=payload,
                headers=headers,
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            email = data.get("address", "") or f"{username}@{domain}"
            token = data.get("token", "") or ""
            return MailboxAccount(email=email, account_id=token, extra={
                "provider_id": provider["id"],
                "api_base": api_base,
                "provider_type": provider_type,
                "token": token,
                "password": password,
            })
        else:
            raise RuntimeError(
                f"BridgeMailbox 暂不支持 provider_type='{provider_type}'，"
                f"请使用 tmail/moemail/duckmail"
            )

    def get_current_ids(self, account: MailboxAccount) -> Set:
        """获取当前邮件 ID 列表（TMail /api/fetch?to=<email>）。"""
        extra = account.extra or {}
        api_base = extra.get("api_base", "")
        if not api_base:
            return set()
        session = self._session()
        admin_pw = extra.get("admin_password", "")
        headers = {}
        if admin_pw:
            headers["Authorization"] = f"Bearer {admin_pw}"
        try:
            resp = session.get(
                f"{api_base}/api/fetch",
                params={"to": account.email},
                headers=headers,
                timeout=10,
            )
            resp.raise_for_status()
            items = resp.json() if isinstance(resp.json(), list) else resp.json().get("data", [])
            return {str(item.get("id", "")) for item in items if item.get("id")}
        except Exception:
            return set()

    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: Set = None,
        code_pattern: str = None,
    ) -> str:
        """轮询 TMail /api/fetch 拉邮件，提取验证码。

        TMail v3 返回 JSON 数组，每条至少有 subject + 某个 body 字段。
        若服务端有预提取的 verification_code 字段优先使用，否则正则抓 6 位数字。
        """
        import re

        extra = account.extra or {}
        api_base = extra.get("api_base", "")
        if not api_base:
            raise RuntimeError("邮箱 account 缺少 api_base")

        pattern = re.compile(code_pattern or r"(?<!\d)(\d{6})(?!\d)")
        seen = set(before_ids or [])
        session = self._session()
        admin_pw = extra.get("admin_password", "")
        headers = {}
        if admin_pw:
            headers["Authorization"] = f"Bearer {admin_pw}"
        deadline = time.time() + timeout

        while time.time() < deadline:
            try:
                resp = session.get(
                    f"{api_base}/api/fetch",
                    params={"to": account.email},
                    headers=headers,
                    timeout=10,
                )
                if resp.status_code == 200:
                    raw = resp.json()
                    items = raw if isinstance(raw, list) else raw.get("data", [])
                    for item in items:
                        mail_id = str(item.get("id", ""))
                        if not mail_id or mail_id in seen:
                            continue
                        seen.add(mail_id)
                        subject = str(item.get("subject", ""))
                        # keyword 只用于 subject/preview，避免 body 碎片误匹
                        if keyword and keyword.lower() not in (
                            subject + " " + str(item.get("preview", ""))
                        ).lower():
                            continue
                        # 优先：服务端已提取的 verification_code
                        code = str(item.get("verification_code") or "").strip()
                        if code and code.lower() != "none" and pattern.fullmatch(code):
                            return code
                        # 兜底：从各种 body 字段里抓 6 位数字
                        text = " ".join(
                            str(item.get(k, "")) for k in (
                                "subject", "preview", "text", "body", "html", "raw", "content"
                            )
                        )
                        m = pattern.search(text)
                        if m:
                            return m.group(1) if m.groups() else m.group(0)
            except Exception as e:
                logger.debug(f"轮询 TMail 邮件失败: {e}")

            time.sleep(3)

        raise TimeoutError(f"等待验证码超时 ({timeout}s)，邮箱: {account.email}")
