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

    def __init__(self, proxy: str = "", stop_event=None):
        self.proxy = proxy
        self._provider: dict | None = None
        # stop_event (threading.Event) 由 supervisor 注入，让 wait_for_code
        # 能在用户手动停止任务时立刻醒过来，而不是等满 120 秒。
        self._stop_event = stop_event

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
        """轮询 TMail v3 两步拉邮件 → 提验证码。

        TMail v3 (mail.nnioj.com 这类 Astro UI 的临时邮箱) 的收信 API 是两步的：
          1. GET /api/fetch?to=<email>       → 返回 [{id, to, from, subject, created_at, ...}]
                                                 只有 meta 字段，**没有 body**
          2. GET /api/fetch/<id>             → 返回 {content: "<html>...</html>",
                                                     attachments: [],
                                                     code: "below"/"above",
                                                     code_found: bool}
             其中 content 是 HTML 正文，验证码一般在 content 里需要正则提。

        所以不能只靠 list 接口——必须 follow up 拉详情。
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
            # 用户手动停止：立刻抛异常让 worker 退出，不再等 120s
            if self._stop_event is not None and self._stop_event.is_set():
                raise RuntimeError("Task stopped by user")
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

                        # list 响应里的 meta 用于 keyword 快速过滤
                        subject = str(item.get("subject", ""))
                        sender = str(item.get("from", ""))
                        if keyword:
                            meta_text = f"{subject} {sender}".lower()
                            if keyword.lower() not in meta_text:
                                continue

                        # Step 2: 拉详情（content 是 HTML 正文）
                        try:
                            detail = session.get(
                                f"{api_base}/api/fetch/{mail_id}",
                                headers=headers,
                                timeout=10,
                            )
                            if detail.status_code != 200:
                                logger.debug(f"拉邮件详情失败 id={mail_id}: {detail.status_code}")
                                continue
                            d = detail.json() if isinstance(detail.json(), dict) else {}
                        except Exception as e:
                            logger.debug(f"拉邮件详情异常 id={mail_id}: {e}")
                            continue

                        content = str(d.get("content", ""))
                        # 先从 content 正则抓 6 位数字
                        m = pattern.search(content)
                        if m:
                            code = m.group(1) if m.groups() else m.group(0)
                            if code and code.lower() != "none":
                                return code

                        # 兜底：subject 里有时也带验证码（比如 "Verify ... - 368905"）
                        full_text = f"{subject} {content}"
                        m2 = pattern.search(full_text)
                        if m2:
                            code = m2.group(1) if m2.groups() else m2.group(0)
                            if code and code.lower() != "none":
                                return code
            except Exception as e:
                logger.debug(f"轮询 TMail 邮件失败: {e}")

            # 用 event.wait 代替 time.sleep —— stop_event 被 set 时立刻醒
            if self._stop_event is not None:
                if self._stop_event.wait(timeout=3):
                    raise RuntimeError("Task stopped by user")
            else:
                time.sleep(3)

        raise TimeoutError(f"等待验证码超时 ({timeout}s)，邮箱: {account.email}")
