# 架构说明

这个仓库现在按“一个项目，对外统一；内部模块解耦”的方式组织。

## 模块

### 1. console

位置：[apps/console](../apps/console)

职责：

- Web 控制台
- 系统默认配置管理
- 新建任务
- 任务状态轮询
- 实时日志查看
- 停止和删除任务

### 2. register-runner

当前位置的实际执行器是根目录脚本：

- [DrissionPage_example.py](../DrissionPage_example.py)
- [email_register.py](../email_register.py)

职责：

- 访问 `x.ai`
- 创建邮箱
- 获取验证码
- 提交注册资料
- 抽取 `sso`
- 在注册浏览器销毁前完成 xAI device-code OAuth
- 写本地结果
- 原子写入 Grok2API / CPA / Sub2API Auth，并推送到 sink
- 为 DownloadGate 保留可派生 GrokCLI-2API `auth` map 的 SSO/OAuth 恢复字段

### 3. network-gateway

位置：[apps/network-gateway](../apps/network-gateway)

职责：

- 托管 WARP / 代理桥接
- 为浏览器和邮箱 API 提供出口
- 在业务开始前确认网络出口可用

当前一体化部署里，它由根目录 [docker-compose.yml](../docker-compose.yml) 中的 `warp` 服务提供。

### 4. token-sink

位置：[apps/token-sink](../apps/token-sink)

职责：

- 接收注册成功后的 token
- 与 `grok2api` 这类消费端对接
- 做去重、落池和结果校验

当前一体化部署里，它由根目录 [docker-compose.yml](../docker-compose.yml) 中的 `grok2api` 服务提供。

### 5. worker-runtime

位置：[apps/worker-runtime](../apps/worker-runtime)

职责：

- 固化 `Xvfb + Chrome/Chromium + Python` 运行依赖
- 让不同机器上的执行环境更一致

## 设计原则

- WARP 不和注册脚本写死耦合
- sink 不直接侵入注册逻辑
- console 只做编排和观测，不直接篡改现有生产任务目录
- 每个任务都复制到自己的运行目录里执行，避免互相污染

## 当前闭环

当前仓库已经能完成下面的完整链路：

1. `warp` 提供默认网络出口
2. `console` 创建任务并写入任务级 `config.json`
3. `register-runner` 独立执行注册流程
4. 成功后将 `sso` 追加写入本地文件并生成 Grok2API Web Auth
5. 复用刚完成 `CreateUserAndSession` 的浏览器执行 device-flow
6. OAuth 成功后生成 CPA 与 Sub2API Auth；无 OAuth 权限时只隔离 OAuth 交付资格
7. DownloadGate 首次取件时原子绑定账号，并派生 CPA / Sub2API / Cockpit / GrokCLI-2API 四种格式
8. 同时把 `sso` 推送到内置 `grok2api`
9. `console` 持续从日志解析实时状态并展示
