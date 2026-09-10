# 半仓智拼 (HalfCart) 项目指令

## 启动项目
```bash
cd C:\Users\Lenovo\Desktop\Half
docker compose up -d          # 需先启动Docker Desktop
```
健康检查: http://localhost:8000/health | API文档: http://localhost:8000/docs

## 技术栈
Python 3.11 FastAPI + MySQL 8.0 + Redis 7 + Elasticsearch 8 + RabbitMQ 3.12 + Vue 3 + Vant 4

## DeepSeek API (NLP 解析)
- API Key: `<your-deepseek-api-key>` (配置在 `.env` 的 `DEEPSEEK_API_KEY`)
- 模型: `deepseek-chat`
- 管理后台: https://platform.deepseek.com

## 项目当前状态 (2026-07-23)

### 架构总览

```
前端 (22个独立HTML页面, Vue 3 CDN + Vant 4)
  ├── index.html        — 首页/雷达大厅 (附近拼单 + 我的发布)
  ├── discover.html     — 发现页 (热门/高信用/最新推荐Feed)
  ├── publish.html      — 发布页 (普通拼单 + 现货转让OCR + 🎤语音发单)
  ├── order-detail.html — 订单详情 (四步进度条 + 倒计时 + 参团人头像)
  ├── login.html        — 登录页
  ├── register.html     — 注册页
  ├── profile.html      — 个人中心
  ├── wallet-settings.html — 收款账户设置 (新建)
  ├── wallet-board.html — 我的钱包
  ├── wallet-record.html — 资金流水
  ├── withdraw-apply.html — 申请提现
  ├── address-list.html — 收货地址 (高德地图选点)
  └── ... (共22个页面)

后端 (FastAPI + 5个MQ消费者)
  ├── /api/v1/orders/*      — 订单CRUD + 状态流转 + 语音发单
  ├── /api/v1/orders/speech-to-order — 🎤 语音发单 (POST)
  ├── /api/v1/radar/list    — LBS雷达大厅 (ES geo_distance)
  ├── /api/v1/orders/discover — 发现页推荐Feed (hot/trusted/latest)
  ├── /api/v1/categories    — 品类字典 (免鉴权, Redis缓存)
  ├── /api/v1/lbs/*         — 高德LBS (geocode/regeo/POI搜索)
  ├── /api/v1/auth/*        — 登录/注册/Token刷新
  ├── /api/v1/user/*        — 个人中心/余额/流水/地址
  ├── /api/v1/withdraw/*    — 提现申请/记录
  └── /api/v1/dify/*        — Dify AI (OCR/纠纷裁判)
```

### 今天完成的工作 (2026-07-23)

#### 1. 语音发单完整修复 (四轮迭代)
- ✅ 重写语音录音逻辑: MediaRecorder 先启动 → Web Speech 并行当加速器，**点停止绝不会落空**
- ✅ 修复 `_stopRecording` 异步竞态: 加 `_mediaPending` 标记防止 getUserMedia 未就绪时回 idle
- ✅ Whisper 模型路径修复: `download_root="/tmp/whisper"` 解决 `/app/.cache` 权限问题
- ✅ `common.js` `request()` 支持 `options.timeout` 参数，语音发单 3 处 `timeout: 60000`（60s）
- ✅ 错误提示优化: `.catch()` 不再覆盖后端消息；`deepseek_nlp.py` 去掉原文打印
- ✅ Whisper 中文检测: 中文占比不足一半时拒绝，防止输出英文/乱码
- ✅ `orders.py` 补 `from app.config import settings` (修复 NameError 500)
- ✅ `deepseek_nlp.py` 错误消息过滤非中文字符

#### 2. 安全漏洞修复
- ✅ `admin/disputes.py:66` `manual_judge` 补 `Depends(admin_required)` — 未登录可调纠纷判定的致命漏洞

#### 3. 敏感字段加密
- ✅ `t_user_addresses.phone` String(11)→String(256) + encrypt/decrypt
- ✅ `t_user_payment_accounts.account` / `real_name` String→String(512) + encrypt/decrypt
- ✅ `pay_account_service.py` 创建/更新时 `encrypt_phone()`；`pay_account.py` to_dict 时 `decrypt_phone()`
- ✅ `address.py` 地址 CRUD 加密写入/解密读取；存量明文兼容 `_safe_decrypt()`
- ✅ DDL: `deploy/mysql/init/05_encrypt_fields.sql`

#### 4. 占位页面消除
- ✅ `address-edit.html` — 已删除（孤立页面，零引用，`address-list.html` 已有完整 CRUD）
- ✅ `chat-detail.html` — 移除 60 行硬编码演示对话，替换为空状态提示
- ✅ `register.html` — 用户协议/隐私政策提取到 `data/agreements.json` via `fetch()`
- ✅ `help-center.html` — 15 条 FAQ 提取到 `data/faq.json` via `fetch()`

#### 5. 收款账户后端 (对接 wallet-settings)
- ✅ `backend/app/models/pay_account.py` — `t_user_payment_accounts` 模型
- ✅ `backend/app/schemas/pay_account.py` — 请求/响应 Schema
- ✅ `backend/app/services/pay_account_service.py` — 最多 5 个 / 同类型唯一 / 删默认自动切换
- ✅ `backend/app/api/v1/pay_account.py` — CRUD + 设默认 5 端点
- ✅ `wallet-settings.html` — localStorage → API CRUD + 一次性迁移
- ✅ `withdraw-apply.html` — localStorage → API 加载已保存账户
- ✅ SQL: `deploy/mysql/init/04_payment_accounts.sql`

#### 6. 测试基础设施
- ✅ `requirements-dev.txt` — pytest / pytest-asyncio / pytest-cov / pytest-env
- ✅ `pyproject.toml` — pytest 配置 (asyncio_mode=auto, coverage)
- ✅ `tests/conftest.py` — Mock Redis/ES/MQ/SMS + FakeRedis + FakeES + 测试 App fixture
- ✅ 15 条测试: `test_auth_service.py`(5) + `test_order_service.py`(5) + `test_balance_service.py`(2) + `test_user_service.py`(3)

#### 7. 其他修复
- ✅ `common.js` API_BASE `127.0.0.1` → `localhost` (修复 CORS)
- ✅ Whisper 模型安装: `openai-whisper==20250625` 已安装到容器，`/tmp/whisper/tiny.pt` 缓存
- ✅ Redis 端口冲突: 移除宿主机 6379 映射（后端通过 Docker 内网连接）

### 之前完成的工作 (2026-07-22)

#### 1. 钱包模块完整重构 (方案C)
- ✅ 新建 `wallet-settings.html` — 收款账户管理 (支付宝/微信 CRUD, localStorage)
- ✅ 重写 `wallet-board.html` — 修复 "收款设置"→地址页 Bug, 骨架屏加载, 数字动画
- ✅ 重写 `withdraw-apply.html` — 收款人输入框, 已保存账户选择, 快捷金额, 周二打款日
- ✅ 重构 `wallet-record.html` — 月度统计摘要, 类型筛选, 时间范围选择, 分页
- ✅ `common.js` 新增 `window.getFlowIcon(type)` 流水图标映射

#### 2. 订单删除功能 (PRD 补充)
- ✅ `backend/app/models/order.py` — 新增 `is_deleted` 软删除字段
- ✅ `backend/app/services/order_service.py` — 新增 `delete_order()` + 列表查询过滤
- ✅ `backend/app/api/v1/orders.py` — 新增 `DELETE /{order_id}` 端点
- ✅ `frontend/src/my-orders.html` — 删除按钮 (待交接不可删除)
- ✅ 规则: GATHERING→解散退款, DELIVERING→禁止删除, FINISHED/DISBANDED→软删除

#### 3. PRD 差距修复 (7项中修6项)
- ✅ #1 PURCHASING→GATHERING 逆向流转 (退款后不满员自动回退)
- ✅ #2 退款扣信用分 (GATHERING退出-1, PURCHASING退出-3)
- ✅ #3 拼友视角退款/退出/售后按钮 (`order-detail.html`)
- ✅ #4 定时解散完整退款 (scheduler.py 增加 wallet_balance_increase)
- ✅ #5 聊天清理消费者 (`chat_cleanup_consumer.py` 新建, 物理删除过期消息)
- ✅ #6 纠纷≥500元拦截 (直接进入人工审核)
- ⚠️ #7 语音发单 — 本次完成三通道架构 (见下方)

#### 4. 🎤 语音发单 — 三通道架构 (新建)
```
用户点击 🎤
  ├─ Chrome/Safari → Web Speech API (浏览器实时识别, <1s, 免费)
  │                    └─→ 文本 → DeepSeek NLP → 订单
  └─ 微信/其他 → MediaRecorder 录音上传
                   ├─ 阿里云 NLS (已配置AppKey, 需补AccessKey)
                   └─ Whisper tiny (可选本地兜底)
```
- ✅ `backend/app/services/speech_service.py` (~300行) — 三通道路由 + 阿里云NLS REST API + Whisper
- ✅ `backend/app/services/deepseek_nlp.py` (~230行) — DeepSeek NLP (替代Dify), 含模糊日期解析
- ✅ `backend/app/api/v1/orders.py` — 新增 `POST /speech-to-order` 端点
- ✅ `frontend/src/publish.html` — 真实录音UI (Web Speech + MediaRecorder双通道)
- ✅ 清理旧 ASR MQ 存根代码 (~80行移除)
- ✅ Dockerfile 增加 ffmpeg

### 之前完成的工作

#### 2026-07-21
- ES 端口 9200→9292 (Windows 端口排除)
- 品类系统重构 (categories表 + Redis缓存 + 动态筛选)
- 发现页 (方案B推荐Feed)
- 订单详情页重构 (四步进度条 + 倒计时 + 头像堆叠)
- 高德地图 JSAPI v2.0 集成
- "返回即退出登录"致命Bug修复 (Token静默刷新)
- 多个422错误修复

#### 2026-07-18
- Dify AI 三工作流打通 (OCR/ASR/纠纷裁判)
- 现货转让 OCR 完整流程
- P0-P2 安全漏洞修复
- 前端设计系统 V2.0
- 登录/注册/个人中心页面重构

### 关键配置 (.env)

```env
# DeepSeek NLP (语音发单)
DEEPSEEK_API_KEY=<your-deepseek-api-key>
DEEPSEEK_MODEL=deepseek-chat

# 阿里云 NLS (语音识别, 微信环境)
ALIYUN_NLS_APP_KEY=<your-aliyun-nls-app-key>
ALIYUN_NLS_ACCESS_KEY_ID=        # 待填写
ALIYUN_NLS_ACCESS_KEY_SECRET=    # 待填写

# 高德地图
AMAP_WEB_KEY=<your-amap-web-key>
AMAP_MOCK_ENABLED=true

# Dify AI (OCR + 纠纷裁判, 语音发单已切至DeepSeek)
DIFY_API_KEY=<your-dify-api-key>
DIFY_OCR_API_KEY=<your-dify-ocr-api-key>
```

### 容器 & 端口

| 容器 | 端口 | 说明 |
|---|---|---|
| halfcart-mysql | 3306(内) | MySQL 8.0 |
| halfcart-redis | 6379 | Redis 7 |
| halfcart-elasticsearch | 9292→9200 | ES 8 (避开Windows保留端口) |
| halfcart-rabbitmq | 6672→5672 | MQ (避开Windows保留端口 5600-5699) |
| halfcart-backend | 8000 | FastAPI |
| halfcart-consumer | — | MQ消费者 (5个: ES同步+通知+聊天清理+OCR+纠纷) |
| halfcart-frontend | 3000→80 | Vue3 Nginx |
| halfcart-nginx | 80, 443 | 反向代理 |

### 重要文件

**后端核心**:
- 配置: `backend/app/config.py`
- 入口: `backend/app/main.py`
- 安全: `backend/app/core/security.py` (AES-256-CBC encrypt_phone/decrypt_phone)
- 订单API: `backend/app/api/v1/orders.py` (含删除+语音端点)
- 订单服务: `backend/app/services/order_service.py` (状态机+删除+逆向流转)
- 退款核销: `backend/app/services/verification_service.py` (阶梯退款+信用分)
- 语音识别: `backend/app/services/speech_service.py` (三通道: NLS→Whisper, 中文检测)
- NLP解析: `backend/app/services/deepseek_nlp.py` (DeepSeek替代Dify)
- 收款账户: `backend/app/services/pay_account_service.py` (CRUD+加密, 新建)
- 收款API: `backend/app/api/v1/pay_account.py` (新建)
- 小票OCR: `backend/app/services/dify_service.py` (仍用Dify)
- 聊天清理: `backend/app/tasks/chat_cleanup_consumer.py` (阅后即焚)

**前端核心**:
- 公共JS: `frontend/src/js/common.js` (含 request() timeout支持、getFlowIcon)
- 首页: `frontend/src/index.html`
- 发现页: `frontend/src/discover.html`
- 发布页: `frontend/src/publish.html` (含重写的三通道语音录音UI, MediaRecorder优先)
- 订单详情: `frontend/src/order-detail.html` (含拼友退出/退款/售后按钮)
- 我的订单: `frontend/src/my-orders.html` (含删除按钮)
- 钱包: `frontend/src/wallet-board.html`
- 收款设置: `frontend/src/wallet-settings.html` (已对接后端API CRUD)
- 收货地址: `frontend/src/address-list.html` (高德地图选点, phone加密存储)
- 提现: `frontend/src/withdraw-apply.html` (已从API加载已保存账户)
- 帮助中心: `frontend/src/help-center.html` (FAQ从data/faq.json加载)
- 数据文件: `frontend/src/data/faq.json`, `frontend/src/data/agreements.json`

**Infra**:
- 编排: `docker-compose.yml`
- 迁移: `deploy/mysql/init/03_order_is_deleted.sql`
- 迁移: `deploy/mysql/init/04_payment_accounts.sql` (收款账户表, 新建)
- 迁移: `deploy/mysql/init/05_encrypt_fields.sql` (加密字段扩展, 新建)
- 测试: `backend/tests/` (15条, conftest+fixtures, 新建)
- 审计: `AUDIT_REPORT.md`
- PRD: `半仓智拼 (HalfCart) 企业级产品需求文档 (PRD).md`

### Vue 3 页面开发注意事项（重要！）
1. **必须使用 `Vue.createApp({ setup: function() {...} })` 模式**，不要用 `const { createApp } = Vue` 解构
2. **模板中使用 `window.goPage` 时必须先在 setup 里声明 `var goPage = window.goPage` 并放入 return
3. **所有HTML自定义元素（如 `<van-field>`）必须用显式闭合标签 `></van-field>`**，不能用 `/>` 自闭合
4. **底部Tab栏必须在 `<div id="app">` 内部**，否则 Vue 不管理其事件
5. **注意 `common.js` 的 `convertKeys` 会自动把后端返回的 `snake_case` 转为 `camelCase`**，前端取字段时必须用 camelCase
6. **倒计时 setInterval 必须在 `Vue.onUnmounted` 中 `clearInterval`**，防止内存泄漏
7. **401 处理已统一在 common.js 的 `request()` 中**（静默刷新 + `location.replace`），页面无需自行处理
8. **弹窗优先使用 `van-overlay` + 居中卡片**，避免 `van-popup position="bottom"` 在桌面端偏左下角

### 高德地图集成注意事项
- 前端 Key: `fcbf19bd914f78c16ec6fe4c15bd571a`（Web端 JS API）
- 安全密钥: `f140d721cc9f7a1b9bd7cac1ffb57809`（jscode）
- 加载方式: CDN `loader.js` + `_AMapSecurityConfig` + `AMapLoader.load()`
- 必须在 `AMapLoader.load()` 之前设置 `window._AMapSecurityConfig`
- 使用 `AMapLib` 变量存储加载后的 AMap 实例（不要依赖全局 AMap）
- 地图实例用完必须 `destroy()` + 置 null（防止 WebGL 内存泄漏）

### 语音发单注意事项
- **三通道**: MediaRecorder (主力，始终启动) + Web Speech API (Chrome/Safari 加速器，并行跑) + Whisper tiny (后端兜底)
- **NLP**: DeepSeek (`deepseek-chat`) 替代 Dify，配置在 `.env` 的 `DEEPSEEK_API_KEY`
- **端点**: `POST /api/v1/orders/speech-to-order` (支持 text 直传 + audio base64)
- **前端**: `publish.html` 中 `onVoiceBtn()` 控制录制/停止/处理 三态切换
- **超时**: `common.js` `request()` 支持 `options.timeout`，语音发单用 60s
- **Whisper**: 模型已安装，缓存在 `/tmp/whisper/tiny.pt`（72MB），中文不足一半自动拦截
- **阿里云NLS**: 不填也能用（自动降级 Whisper），填了准确率更高（95% vs 85%）

### 订单删除规则
- GATHERING(0)/PURCHASING(1): 删除→解散退款 (仅团长)
- DELIVERING(2): **禁止删除** (需先完成交接)
- FINISHED(3)/DISBANDED(4): 软删除 (is_deleted=1)
- 删除按钮仅在"我发起的"且非待交接状态显示

### 已知待处理
- AI 消费者 (OCR/纠纷) 部分依赖 Dify 工作流配置
- ES 安全在开发环境禁用 (生产部署前需开启)
- 订单解散退款支付宝侧需人工操作
- 微信扫码回调需要公网域名或内网穿透
- RabbitMQ 队列参数冲突 (x-message-ttl 不一致)
- 阿里云 NLS AccessKey 待填写 (不填也能用: Whisper tiny 兜底)
- 测试在 Docker 内运行有 event loop 冲突 (原生环境正常)，15 条中有部分跨模块超时失败
- `Dockerfile` 未包含 `tests/` 和 `pyproject.toml` (需重建时手动 `docker cp`)
- 收款账户 `account`/`real_name` 存量明文数据需迁移脚本加密 (新数据已加密)



# Superpowers-ZH 中文增强版

本项目已安装 superpowers-zh 技能框架（20 个 skills）。

## 核心规则

1. **收到任务时，先检查是否有匹配的 skill** — 哪怕只有 1% 的可能性也要检查
2. **设计先于编码** — 收到功能需求时，先用 brainstorming skill 做需求分析
3. **测试先于实现** — 写代码前先写测试（TDD）
4. **验证先于完成** — 声称完成前必须运行验证命令

## 可用 Skills

Skills 位于 `.claude/skills/` 目录，每个 skill 有独立的 `SKILL.md` 文件。

- **brainstorming**: 在任何创造性工作之前必须使用此技能——创建功能、构建组件、添加功能或修改行为。在实现之前先探索用户意图、需求和设计。
- **chinese-code-review**: 中文 review 沟通参考——话术模板、分级标注（必须修复/建议修改/仅供参考）、国内团队常见反模式应对。仅在用户显式 /chinese-code-review 时调用，不要根据上下文自动触发。
- **chinese-commit-conventions**: 中文 commit 与 changelog 配置参考——Conventional Commits 中文适配、commitlint/husky/commitizen 中文模板、conventional-changelog 中文配置。仅在用户显式 /chinese-commit-conventions 时调用，不要根据上下文自动触发。
- **chinese-documentation**: 中文文档排版参考——中英文空格、全半角标点、术语保留、链接格式、中文文案排版指北约定。仅在用户显式 /chinese-documentation 时调用，不要根据上下文自动触发。
- **chinese-git-workflow**: 国内 Git 平台配置参考——Gitee、Coding.net、极狐 GitLab、CNB 的 SSH/HTTPS/凭据/CI 接入差异与镜像同步配置。仅在用户显式 /chinese-git-workflow 时调用，不要根据上下文自动触发。
- **dispatching-parallel-agents**: 当面对 2 个以上可以独立进行、无共享状态或顺序依赖的任务时使用
- **executing-plans**: 当你有一份书面实现计划需要在单独的会话中执行，并设有审查检查点时使用
- **finishing-a-development-branch**: 当实现完成、所有测试通过、需要决定如何集成工作时使用——通过提供合并、PR 或清理等结构化选项来引导开发工作的收尾
- **mcp-builder**: MCP 服务器构建方法论 — 系统化构建生产级 MCP 工具，让 AI 助手连接外部能力
- **receiving-code-review**: 收到代码审查反馈后、实施建议之前使用，尤其当反馈不明确或技术上有疑问时——需要技术严谨性和验证，而非敷衍附和或盲目执行
- **requesting-code-review**: 完成任务、实现重要功能或合并前使用，用于验证工作成果是否符合要求
- **subagent-driven-development**: 当在当前会话中执行包含独立任务的实现计划时使用
- **systematic-debugging**: 遇到任何 bug、测试失败或异常行为时使用，在提出修复方案之前执行
- **test-driven-development**: 在实现任何功能或修复 bug 时使用，在编写实现代码之前
- **using-git-worktrees**: 当需要开始与当前工作区隔离的功能开发，或在执行实现计划之前使用——通过原生工具或 git worktree 回退机制确保隔离工作区存在
- **using-superpowers**: 在开始任何对话时使用——确立如何查找和使用技能，要求在任何响应（包括澄清性问题）之前调用 Skill 工具
- **verification-before-completion**: 在宣称工作完成、已修复或测试通过之前使用，在提交或创建 PR 之前——必须运行验证命令并确认输出后才能声称成功；始终用证据支撑断言
- **workflow-runner**: 在 Claude Code / OpenClaw / Cursor 中直接运行 agency-orchestrator YAML 工作流——无需 API key，使用当前会话的 LLM 作为执行引擎。当用户提供 .yaml 工作流文件或要求多角色协作完成任务时触发。
- **writing-plans**: 当你有规格说明或需求用于多步骤任务时使用，在动手写代码之前
- **writing-skills**: 当创建新技能、编辑现有技能或在部署前验证技能是否有效时使用

## 如何使用

当任务匹配某个 skill 时，使用 `Skill` 工具加载对应 skill 并严格遵循其流程。绝不要用 Read 工具读取 SKILL.md 文件。

如果你认为哪怕只有 1% 的可能性某个 skill 适用于你正在做的事情，你必须调用该 skill 检查。
<!-- superpowers-zh:end -->
