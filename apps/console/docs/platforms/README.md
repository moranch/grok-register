# 平台插件开发指南

> 对应 Requirement 14 AC4：第一批 Platform_Plugin 作为"平台插件开发范本"。

## 目录结构

```
platforms/{platform_name}/
├── __init__.py          # 空文件，标记为 Python 包
├── plugin.py            # 平台适配层（必须）
├── protocol_mailbox.py  # 协议模式注册 Worker（按需）
├── browser_register.py  # 浏览器注册 Worker（按需）
├── browser_oauth.py     # OAuth 浏览器流程（按需）
├── switch.py            # 账号切换逻辑（按需）
└── core.py              # 平台协议核心逻辑（按需）
```

## 必填字段

在 `plugin.py` 中，你的平台类必须声明以下类变量：

| 字段 | 类型 | 说明 |
|---|---|---|
| `name` | `str` | 平台标识（小写，如 `grok`） |
| `display_name` | `str` | 显示名称（如 `Grok`） |
| `supported_executors` | `List[str]` | 支持的执行器列表 |

## 可选字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `version` | `str` | 版本号，默认 `1.0.0` |
| `capabilities` | `Capabilities` | 能力位声明 |
| `register_engines` | `List[EngineSpec]` | 注册引擎列表 |
| `preferred_captcha_strategies` | `List[str]` | 验证码策略优先级 |
| `supported_exporters` | `List[str]` | 支持的导出器白名单 |
| `default_extra_schema` | `Dict` | JSON Schema，供前端渲染配置表单 |

## 最小示例

```python
from core.base_platform import BasePlatform, Account, AccountStatus, Capabilities, EngineSpec
from core.registry import register_platform


@register_platform
class MyPlatform(BasePlatform):
    name = "myplatform"
    display_name = "My Platform"
    supported_executors = ["protocol"]
    capabilities = Capabilities(supports_validity_check=True)

    def build_register_flow(self, engine):
        from core.registration.protocol_mailbox_flow import ProtocolMailboxFlow

        def worker_builder(ctx, otp_callback):
            return MyWorker(proxy=ctx.proxy, log_fn=ctx.log, otp_callback=otp_callback)

        def result_mapper(ctx, result):
            return Account(
                platform="myplatform",
                email=result["email"],
                token=result["token"],
                status=AccountStatus.REGISTERED,
                extra=result,
            )

        flow = ProtocolMailboxFlow(
            worker_builder=worker_builder,
            result_mapper=result_mapper,
        )
        flow.set_context(None)
        return flow

    def check_validity(self, account) -> bool:
        # 实现有效性检测逻辑
        return bool(account.get("token") or account.get("sso"))
```

## 注册引擎（EngineSpec）

如果你的平台有多种注册路径，声明 `register_engines`：

```python
register_engines = [
    EngineSpec(
        id="refresh_token",
        display_name="Refresh Token 模式",
        description="产出 Access + Refresh Token",
        is_recommended=True,
    ),
    EngineSpec(
        id="access_token_only",
        display_name="Access Token 模式",
        description="仅产出 Access Token",
    ),
]
```

前端会自动渲染为单选控件。

## 回调时序

```
TaskRuntime.schedule(task_id)
  → PlatformPlugin.register(email, password)
    → get_engine(engine_id)
    → build_register_flow(engine) / build_browser_flow(engine)
    → Flow.run(email, password)
      → worker_builder(ctx, otp_callback)
      → worker.run(email, password)
        → 创建邮箱 → 调用目标 API → 等待 OTP → 提交验证 → 提取 token
      → result_mapper(ctx, result)
    → Account
  → ExporterDispatcher.push_all(account)
  → SSE emit("success")
```

## 向后兼容

如果你有旧的注册脚本，可以通过子进程方式调用：

```python
def build_browser_flow(self, engine):
    if engine.id == "legacy_script":
        # 以子进程方式调用旧脚本
        ...
```

参考 `platforms/grok/plugin.py` 中的 `GrokLegacyWorker` 实现。
