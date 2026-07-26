# 业务流程

## 一次完整任务怎么跑

1. 在控制台或手工配置里提供 4 类关键参数：
   - 前置网络出口：`browser_proxy` / `proxy`
   - 临时邮箱：`temp_mail_api_base` / `temp_mail_admin_password` / `temp_mail_domain`
   - 注册次数：`run.count`
   - 结果落池：`api.endpoint` / `api.token`
2. 控制台创建任务后，会生成独立任务目录和独立 `config.json`。
3. 执行器启动浏览器；如果当前是无头 Linux，优先通过 `Xvfb` 提供显示环境。
4. 浏览器进入 `x.ai` 注册页，并切到邮箱注册流程。
5. 执行器调用临时邮箱 API 创建地址。
6. 把这个邮箱地址填进 `x.ai` 注册页提交。
7. 轮询临时邮箱，拿到验证码。
8. 提交验证码，进入资料填写页。
9. 自动填写随机姓名和密码，完成注册。
10. 注册页的 `CreateUserAndSession` 完成后，从同一个浏览器 profile 提取 `sso`。
11. 把 `sso` 写入任务目录下的 `sso/task_<id>.txt`，并立即生成/推送 Grok2API Web Auth。
12. 在浏览器被回收之前，复用当前登录 Session 执行标准 xAI Device Authorization：
    - `POST https://auth.x.ai/oauth2/device/code` 获取 `device_code` / `user_code`
    - 当前注册浏览器打开 `verification_uri_complete`
    - 依次确认 `Continue`、`Allow`
    - `POST https://auth.x.ai/oauth2/token` 换取 `access_token` / `refresh_token`
13. token 成功后一次性扇出三类产物：
    - Grok2API：Web SSO Auth
    - CLIProxyAPI（CPA）：`xai-<email>.json`
    - Sub2API：`SUB2API-grok-<email>.json`
14. DownloadGate 在账号交付时从同一份权威凭据派生四种互不重复消耗库存的下载格式：
    - CPA flat JSON
    - Sub2API DataPayload
    - Cockpit `auth.json`
    - GrokCLI-2API 2.x `auth` map（保留 refresh token、SSO 与注册密码，供下游续期自愈）
15. 控制台持续解析日志，显示当前轮次、成功数、失败数、最近邮箱和错误。

## GrokCLI-2API 取件文件怎么用

- 取件页选择“下载 GrokCLI-2API JSON”。
- 在 grokcli-2api 管理台的账号导入区上传该文件；其 2.x Go 接口
  `/admin/api/accounts/import-file` 与 `/admin/api/accounts/import-files` 都接受这种 `auth` map。
- 批量取件 ZIP 会把该格式放进每张卡密目录的 `grokcli-2api/` 子目录。
- 老取件记录没有保存 SSO/注册密码时仍可导入并依靠 access/refresh token 使用；新取件记录会额外保留自愈字段。

## SSO、Device Flow 与 CPA 的边界

- `CreateUserAndSession`/登录负责生成 Web `sso`；它能用于 Grok Web/Grok2API。
- Device Flow 由 `auth.x.ai` 签发 OAuth access/refresh token，使用公开 Grok CLI client：
  - `client_id=b1a00492-073a-47ea-816f-4c329264a828`
  - `scope=openid profile email offline_access grok-cli:access api:access`
- CPA **不是 OAuth 签发方**。CPA 只读取并续期已经成功签发的 xAI OAuth 凭据。
- 有效 SSO 不代表账号一定具备 Grok CLI/API OAuth 权限。若 token 端点返回
  `invalid_grant: Access denied`，系统记录为 `oauth_entitlement_denied`，停止重复签发，
  但不会把仍可登录的 Web/SSO 账号错误标成失效。

## 现阶段卡点通常在哪

这个业务最常见的失败点不是脚本代码本身，而是外部依赖：

- 出口 IP 被 `x.ai` 风控
- 临时邮箱域名被 `x.ai` 明确拒绝
- 浏览器没走 WARP，但邮箱请求走了，前后链路不一致
- `Xvfb`、Chrome/Chromium、Python 版本不匹配
- sink 地址不对，导致注册成功但未入池

## 什么叫“完全闭环”

在这个项目里，完全闭环指的是：

- 有可用网络出口
- 有能被 `x.ai` 接受的邮箱域名
- 注册脚本能稳定跑
- 成功结果本地留档
- 成功结果自动推送到下游号池
- 控制台能看到实时状态和日志

只满足“能注册”还不算闭环；必须能观测、能落池、能重复跑批才算闭环。

在当前仓库里：

- `warp` 和 `grok2api` 已经可以跟随 `docker compose` 一起启动
- 临时邮箱仍然需要你自己提供，因为不同用户的可用域名和实现方式差异很大
