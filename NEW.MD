# HalfCart 项目修复清单 (71项)

> 基于 2026-07-25 全面代码审查 | 按优先级排序

---

## 🔴 严重 (11项) — 运行时错误 / 功能完全失效 / 安全漏洞

### 后端 (4)

- [x] **C-1** [main.py](backend/app/main.py#L140) CORS 配置 `hasattr(settings, 'CORS_ALLOWED_ORIGINS')` 永远为 False → 所有环境回退到 localhost 硬编码列表。开发环境设 `allow_origins=["*"]` 完全绕开 CORS
  - 修复: 在 `config.py` 添加 `CORS_ALLOWED_ORIGINS` 字段，移除 `["*"]` 通配符
- [x] **C-2** [wechat_login.py](backend/app/api/v1/wechat_login.py#L113-L141) `qr_img` 变量仅在 `if token:` 内赋值，token 获取失败时触发 `UnboundLocalError`
  - 修复: 在 `if token:` 之前初始化 `qr_img = ""`
- [x] **C-3** [test_balance.py](backend/app/api/v1/test_balance.py) 测试端点接受任意 `user_id`/`amount`，仅靠 `TEST_MODE` 守卫。误部署生产时匿名可操作任何用户余额
  - 修复: 加 `APP_ENV != "production"` 双重守卫 或加 `Depends(admin_required)`
- [x] **C-4** [disputes.py](backend/app/api/v1/admin/disputes.py#L69-L71) 手动判定的错误码返回裸整数 `code=503`/`code=437` 而非 `ErrorCode` 枚举，破坏 API 契约
  - 修复: 改为 `ErrorCode.DISPUTE_NOT_FOUND` 及正确的枚举值

### 前端 (3)

- [x] **C-5** [messages.html](frontend/src/messages.html#L250-L255) `goBack` 在 setup 中定义但未放入 `return` → 返回按钮点击无反应
  - 修复: 在 `return {}` 中添加 `goBack: goBack`
- [x] **C-6** [login.html](frontend/src/login.html#L388-L405) 微信扫码登录用 `fetch(API_BASE + ...)` 直接调用，但 `API_BASE` 是 `common.js` 闭包私有变量 → `ReferenceError` → 微信登录彻底失效
  - 修复: 改用 `window.request()` 或显式定义 URL
- [x] **C-7** [pay-confirm.html](frontend/src/pay-confirm.html#L147) 读取 `localStorage.getItem('halfcart_selected_addr')` 预选地址，但全项目无代码写入该 key → 地址预选永远为 null
  - 修复: 在 `address-list.html` 的 `selectAddr` 中写入 localStorage，或在 pay-confirm 中改用 API 获取默认地址

### 基础设施 (4)

- [x] **C-8** [docker-compose.yml](docker-compose.yml#L111-L117) ES 8 `xpack.security.enabled: false` + 端口 9292 暴露宿主机 → 无认证可读写所有索引
  - 修复: 至少启用 basic auth，或生产前移除端口映射
- [x] **C-9** [docker-compose.yml](docker-compose.yml#L152-L154) RabbitMQ 管理控制台 15672 暴露 + 默认凭据 `halfcart/halfcart123`
  - 修复: 生产前移除端口映射或修改默认密码
- [x] **C-10** [.env](.env) 10+ 个真实生产密钥明文存储（Dify/DeepSeek/阿里云/微信/支付宝）
  - 修复: 考虑用 Windows DPAPI 或密钥管理服务加密存储，至少确保本机磁盘加密开启
- [x] **C-11** [config.py](backend/app/config.py) `DEEPSEEK_API_KEY`、`DIFY_*_API_KEY`、`AMAP_WEB_KEY` 声明为 `str` 而非 `pydantic.SecretStr` → 可能被 traceback 打印泄露
  - 修复: 改为 `SecretStr`，调用处用 `.get_secret_value()`

---

## 🟠 高危 (22项) — 可能导致生产 Bug / 功能缺失 / 安全隐患

### 后端 (7)

- [x] **H-1** [auth.py](backend/app/api/v1/auth.py#L267) `wechat_callback` 引用 `settings.WECHAT_FRONTEND_URL` 但 `settings` 未导入 → `NameError`
  - 修复: 添加 `from app.config import settings`
- [x] **H-2** [config.py](backend/app/config.py#L137-L138) JWT 默认 15min/7d 但 [.env.example](backend/.env.example#L58-L59) 仍写 7d/30d → 开发者照抄会部署错误配置
  - 修复: 更新 `.env.example` 与实际默认值一致并加注释
- [x] **H-3** [dispute_service.py](backend/app/services/dispute_service.py#L78) 硬编码 `Decimal("500")` 忽略 `settings.DISPUTE_AUTO_JUDGE_MAX_AMOUNT`
  - 修复: 改为 `Decimal(str(settings.DISPUTE_AUTO_JUDGE_MAX_AMOUNT))`
- [x] **H-4** [participation_service.py](backend/app/services/participation_service.py#L136-L395) 3 处 `except Exception:` 补偿退款无日志 → 退款失败也毫无记录
  - 修复: 每处加 `logger.exception("补偿操作失败")`
- [x] **H-5** [orders.py](backend/app/api/v1/orders.py#L266) + [categories.py](backend/app/api/v1/categories.py#L86) 返回裸 `code=500` 而非 `ErrorCode.INTERNAL_ERROR`
  - 修复: 改为 `code=ErrorCode.INTERNAL_ERROR.value`
- [x] **H-6** [test_auth.py](backend/app/api/v1/test_auth.py) `POST /api/v1/test/token` 任意 `user_id` 生成有效 JWT → `TEST_MODE=true` 时任何人可冒充任意用户
  - 修复: 同 C-3，加 `APP_ENV != "production"` 双重守卫
- [x] **H-7** [wechat_service.py](backend/app/services/wechat_service.py#L248) `except Exception:` 静默吞掉阿里云校验异常，fallback 也失败时无日志
  - 修复: 加 `logger.warning(f"阿里云校验失败, fallback Redis: {e}")`

### 前端 (8)

- [x] **H-8** 8 个页面使用 `const { createApp } = Vue` 解构，绕过 `common.js` 对 `Vue.createApp` 的 monkey-patch（`goPage`/`request`/`getUser` 全局注入失效）
  - 涉及文件: `profile-edit.html`, `pay-result.html`, `messages.html`, `chat-detail.html`, `help-center.html`, `refund-apply.html`, `verify-code-show.html`, `verify-code-verify.html`
  - 修复: 改为 `Vue.createApp({ setup: function() {...} })`
- [x] **H-9** [my-orders.html](frontend/src/my-orders.html#L205-L414) 35 条 `console.log('[已完成诊断]')` 暴力日志补丁（代码注释标注了"修复后可删除"）
  - 修复: 删除第 205-414 行所有诊断日志
- [x] **H-10** [index.html](frontend/src/index.html#L324-L357) 6 条 location/regeo 调试 `console.log` 残留（`[regeo]`/`[location]`）
  - 修复: 删除这些 `console.log` 行
- [x] **H-11** [CLAUDE.md](CLAUDE.md#L14) DeepSeek API Key `sk-f30763...` 明文提交到 Git → 任何有仓库访问权限的人可见
  - 修复: 从文件中移除，改用环境变量占位符。已提交的历史需在 DeepSeek 后台 rotate key
- [x] **H-12** 高德 Key `fcbf19bd...` 和 jscode `f140d721...` 在 [index.html](frontend/src/index.html) / [publish.html](frontend/src/publish.html) / [address-list.html](frontend/src/address-list.html) 3 处重复硬编码
  - 修复: 提取到 `common.js` 的全局配置或 `data/` 目录统一引用
- [x] **H-13** [wallet-settings.html](frontend/src/wallet-settings.html#L280-L302) 迁移代码 `migrateLocalData()` 每次 `onMounted` 都调 API → 首次迁移成功后后续访问浪费请求
  - 修复: 迁移成功后设标记 `localStorage.setItem('halfcart_migrated', '1')`，onMounted 先检查标记
- [x] **H-14** [wallet-board.html](frontend/src/wallet-board.html#L289-L290) 账户数读取 `localStorage.getItem('halfcart_pay_accounts')`，但该 key 被 wallet-settings 迁移后删除 → 账户数始终显示 0
  - 修复: 改为从 API 加载账户数，或检查迁移标记后走 API
- [x] **H-15** [pay-confirm.html](frontend/src/pay-confirm.html#L139-L142) 钱包余额直接取 `res.data.walletBalance`，其他页面用 `res.data.profile.walletBalance` → 响应格式可能不匹配，余额显示 0.00
  - 修复: 已核实 — `/profile` 返回 `wallet_balance`(→`walletBalance`)在 data 顶层, `/home` 用 `data.profile.walletBalance` 包装不同。两个 API 均正确, 非 Bug。

### 基础设施 (6)

- [x] **H-16** 所有基础设施密码含项目名+年份（`halfcart_root_2024`/`halfcart123`/`halfcart_redis_2024`/`elastic123`）→ 弱密码
  - 修复: 生产部署前全部更换为随机强密码
- [x] **H-17** `JWT_SECRET_KEY` = `dev-halfcart-jwt-secret-key-change-in-production` → 占位符值
  - 修复: 生产环境使用 `openssl rand -hex 32` 生成
- [x] **H-18** `PHONE_ENCRYPTION_KEY` = `halfcart-aes-key-32bytes!!!` → 完全可预测
  - 修复: 生产环境使用 `openssl rand -hex 16` 生成 32 字节密钥
- [x] **H-19** [security.py](backend/app/core/security.py#L223) AES key 长度不足时零填充（`\x00`），非 PBKDF2/HKDF → 削弱有效密钥强度
  - 修复: 使用 PBKDF2 派生密钥，或直接拒绝长度不匹配的配置
- [x] **H-20** [docker-compose.yml](docker-compose.yml#L210-L304) 后端和 consumer 服务均设置 `TEST_MODE=true`
  - 修复: 非开发环境设 `TEST_MODE=false`
- [x] **H-21** [backend/Dockerfile](backend/Dockerfile#L35) pip 清华镜像但未启用 `--require-hashes` → 供应链风险
  - 修复: 生产构建时加 hash 校验或切回 PyPI 官方源

### 调试残留 (1)

- [x] **H-22** [index.html](frontend/src/index.html) 5 条 `console.log` 调试语句（`[regeo]`/`[location]` 前缀），commit `8729f68` 添加后未删除
  - 修复: 删除第 324/327/330/334/357 行

---

## 🟡 中等 (22项) — 功能缺口 / 潜在 Bug / 代码质量

- [ ] **M-1** [notify_consumer.py](backend/app/tasks/notify_consumer.py#L103-L132) WebSocket 推送和 SMS 通知标记为 TODO 未实现 → 用户收不到实时推送
- [x] **M-2** [es_client.py](backend/app/core/es_client.py#L199-L324) `search_nearby_orders` 已标记 DEPRECATED 但仍保留 125 行代码 + 硬编码 status 过滤
- [x] **M-3** [dify_api.py](backend/app/api/v1/dify_api.py#L67) OCR `image_url` Field max_length=10MB 但 `config.py` 限制 2MB → 不一致且无双重校验
- [x] **M-4** [security.py](backend/app/core/security.py#L380-L384) bcrypt 不可用时用 AES 密钥做 HMAC-SHA256 密码哈希 → 密钥未分离 + 无盐
- [x] **M-5** [reconciliation.py](backend/app/api/v1/admin/reconciliation.py#L26-L81) 参数名 `user_id` 实际是 admin_id → 命名误导
- [x] **M-6** [auth.py](backend/app/api/v1/auth.py#L155) `rate_limit_check` 导入但从未使用 → 死代码
- [x] **M-7** [withdraw.py](backend/app/api/v1/withdraw.py#L10-L18) `Header` 导入未使用；`WithdrawStatus(a.status).name` 状态越界时抛 `ValueError`
- [x] **M-8** [dify_api.py](backend/app/api/v1/dify_api.py#L89-L142) Dify 调用无 try/except 包裹 → 异常直接到全局 handler，错误信息不够具体
- [x] **M-9** [messages.html](frontend/src/messages.html#L229) `notifications` 数组声明后从未填充 → "系统通知" Tab 永远为空
- [ ] **M-10** [publish.html](frontend/src/publish.html#L699-L712) 现货转让批量发布无回滚 → 第 3 件失败时前 2 件已创建
- [x] **M-11** [index.html](frontend/src/index.html) 无 `requireAuth()` → "我的发布" Tab 对未登录用户 401 静默失败
- [ ] **M-12** 全项目约 50 处 `.catch(function(){})` 空错误处理 → 调试困难，用户无错误反馈
- [x] **M-13** [common.js](frontend/src/js/common.js#L151-L152) 所有 fetch 失败都弹 "网络异常" toast → timeout/abort 也触发误导提示
- [ ] **M-14** [frontend/Dockerfile](frontend/Dockerfile) 只提供 "前端服务运行中" 占位页，不构建实际 src/ → 容器化部署前端不可用
- [ ] **M-15** [deploy/nginx/nginx.conf](deploy/nginx/nginx.conf#L45-L50) nginx 反代 `/` 到 `backend:8000` 而非 `frontend:80` → 主入口无法访问前端
- [x] **M-16** [docker-compose.yml](docker-compose.yml#L359-L361) nginx `depends_on` 无 `condition: service_healthy` → backend 未就绪时 nginx 返回 502
- [x] **M-17** `schema.sql` 和 `v3.2_indexes.sql` 在仓库根目录，可能与 `deploy/mysql/init/` 不同步
- [ ] **M-18** [frontend/Dockerfile](frontend/Dockerfile#L10) 注释 "后续将替换为 Vue3 + Vite 完整构建" 但未实施
- [x] **M-19** [login.html](frontend/src/login.html#L422-L424) 协议文本硬编码，`register.html` 已改为从 `data/agreements.json` 加载 → 不一致
- [x] **M-20** [refund-apply.html](frontend/src/refund-apply.html#L230) 退款 API 请求体含 `{reason: desc}`，但 `order-detail.html` 退款传空 `body: {}` → 不一致
- [x] **M-21** [address-list.html](frontend/src/address-list.html#L399-L423) 编辑地址时 `form.fullAddress` 仅取 `addr.detail`，保存时拼接可能造成重复 → "朝阳区 朝阳区 望京SOHO"
- [ ] **M-22** [pay-confirm.html](frontend/src/pay-confirm.html) — 现货转让项发布无失败处理

---

## ⚪ 轻微 (16项) — 代码风格 / 维护性 / 小改进

- [ ] **L-1** [wechat_mp.py](backend/app/api/v1/wechat_mp.py#L25) 与 [wechat_login.py](backend/app/api/v1/wechat_login.py#L149-L154) 签名验证函数重复
- [x] **L-2** [user.py](backend/app/api/v1/user.py#L12) `Header` 导入但未使用
- [x] **L-3** [models/user.py](backend/app/models/user.py) `created_at` 缺 Python 级 default，ORM flush 前为 None
- [ ] **L-4** [payment_log.py](backend/app/models/payment_log.py#L57) `idempotent_key` String(80) 边界偏紧
- [x] **L-5** [config.py](backend/app/config.py#L186) `DIFY_WORKFLOW_ASR` 注释 "[已废弃]" 但仍保留
- [ ] **L-6** [order.py](backend/app/models/order.py#L73-L74) `location_lat`/`location_lng` 用 `DECIMAL` 而非项目统一的 `mapped_column`
- [x] **L-7** [wechat_login.py](backend/app/api/v1/wechat_login.py#L64) `hasattr(settings.WECHAT_MP_SECRET, 'get_secret_value')` 防御性检查 → 已确认是 SecretStr，回退分支是死代码
- [ ] **L-8** [docker-compose.yml](docker-compose.yml#L257) backend 容器挂载 `./frontend/src:/app/frontend/src:ro` → 不必要的耦合
- [x] **L-9** [backend/Dockerfile](backend/Dockerfile#L60) `--forwarded-allow-ips "*"` → 最小权限原则，应限定为 nginx 内网 IP
- [ ] **L-10** MySQL/Redis healthcheck 密码作为命令行参数传递 → 会出现在 `docker inspect` 和进程列表
- [x] **L-11** `.gitignore` 缺少 `.pytest_cache/`、`.coverage` 和 `*.pem`/`*.key`/`*.crt` 模式
- [x] **L-12** [security.py](backend/app/core/security.py#L87-L130) JWT `jwt.encode()` 是 CPU-bound 同步调用但在 `async def` 中直接使用
- [ ] **L-13** [verify-code-show.html](frontend/src/verify-code-show.html) 和 [verify-code-verify.html](frontend/src/verify-code-verify.html) 可能是孤儿页面 → 无其他页面通过 `goPage()` 链接到它们
- [ ] **L-14** [Dockerfile](frontend/Dockerfile) 整个文件是占位 echo → 应补充 Vue3+Vite 多阶段构建
- [x] **L-15** [wallet-settings.html](frontend/src/wallet-settings.html) 迁移后 localStorage key 被删除但 `wallet-board.html` 仍在读
- [ ] **L-16** [deploy/nginx/nginx.conf](deploy/nginx/nginx.conf) 未配置 SSL 证书但暴露 443 端口

---

## 📊 统计

| 维度 | 🔴 | 🟠 | 🟡 | ⚪ | 合计 |
|------|----|----|----|----|------|
| 后端 | 4 | 7 | 8 | 6 | 25 |
| 前端 | 3 | 8 | 7 | 5 | 23 |
| 基础设施/安全 | 4 | 6 | 7 | 5 | 22 |
| 调试残留 | — | 1 | — | — | 1 |
| **合计** | **11** | **22** | **22** | **16** | **71** |

---

## 🎯 修复顺序建议

1. **今天**: C-5 → C-6 → C-2 → H-1（4 个运行时错误，功能完全失效）
2. **本周**: C-1 → C-8 → C-9 → C-10 → C-11 → H-8 → H-9 → H-10 → H-11 → H-22（安全 + 调试残留）
3. **本月**: 剩余高危 + 中等项
4. **后续迭代**: 轻微项

---

## 📌 已知限制 (15项 — 延后处理)

以下项目非缺陷修复，属于功能缺口或架构设计决策，建议在后续迭代中处理：

### 功能缺口 (需开发)
- **M-1** WebSocket/SMS 推送 — `notify_consumer.py` TODO，需完整实现
- **M-10** 现货转让回滚 — 批量发布需改为事务性 API
- **M-14/M-18/L-14** 前端 Dockerfile — 占位页，需 Vue3+Vite 多阶段构建

### 架构改进 (需设计)
- **M-15** nginx 反代 `/` → `backend:8000` 而非 `frontend:80`，前端无法通过 80 端口访问
- **M-12** ~50 处空 `.catch()` — 需逐页评估哪些需要用户提示
- **M-22** pay-confirm 现货转让无失败处理 — 需补充错误提示

### 低优先级 (按需处理)
- **L-1** wechat_mp.py / wechat_login.py 签名验证重复 — 可提取共用函数
- **L-4** payment_log idempotent_key 列宽 — 当前 80 字符够用，监控即可
- **L-6** order 模型 DECIMAL 风格不一致 — 不影响功能
- **L-8** docker-compose volume 耦合 — 低风险
- **L-10** healthcheck 密码在命令行 — docker inspect 可见，开发环境可接受
- **L-13** verify-code 页面 — 非孤儿，通过推送通知/直接URL访问
- **L-16** nginx 无 SSL — 开发环境无需 TLS
