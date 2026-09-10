# 项目全面审计报告

## 1. 项目概览与架构梳理

**项目名称**: 半仓智拼 (HalfCart)  
**项目类型**: 超本地化 C2B 预售互助平台（拼单+支付+AI+地理围栏）  
**技术栈**:
- **后端**: Python 3.11+ / FastAPI / SQLAlchemy 2.0 异步 / aiomysql
- **数据库**: MySQL 8.0（核心交易数据）、Redis 7（缓存/锁/限流）、Elasticsearch 8（LBS地理搜索）
- **消息队列**: RabbitMQ 3.12（AI任务异步/通知/对账）
- **前端**: 原生 HTML + Vue 3 + Vant 4（H5移动端）
- **支付**: 支付宝沙箱（RSA2-SHA256签名）
- **AI**: Dify 工作流（NLP/OCR/纠纷裁判）
- **部署**: Docker Compose + Nginx 反向代理

**核心数据流**:
```
用户 → 前端H5 → Nginx → FastAPI → MySQL (交易)
                    ↓
              Redis (缓存/锁) ← → ES (LBS搜索)
                    ↓
              RabbitMQ → AI消费者 (Dify)
```

---

## 2. 缺陷清单（按优先级排序）

### P0 - 致命/Critical

---

#### P0-1: `.env` 文件包含全部生产密钥并可能被提交至版本控制

**位置**: `C:\Users\Lenovo\Desktop\Half\.env:1-75`

**风险与影响**:  
该文件包含所有基础设施密码、JWT密钥、AES加密密钥、Dify API Key、阿里云SMS AccessKey/SecretKey、微信AppID/AppSecret、支付宝沙箱私钥/公钥等全部敏感凭证。攻击者获取此文件后可：
- 伪造JWT Token，以任意用户身份登录
- 解密所有用户手机号
- 盗用阿里云短信服务（产生费用+发送钓鱼短信）
- 调用Dify AI接口（产生费用+数据泄露）
- 伪造支付宝支付回调、窃取资金
- 接管微信公众号发送恶意消息

**修复方案**:
```bash
# 1. 立即将 .env 加入 .gitignore（如果尚未添加）
echo ".env" >> .gitignore

# 2. 轮换所有已泄露的密钥（紧急）：
#    - 阿里云 RAM AccessKey → 禁用旧Key，创建新Key
#    - 微信 AppSecret → 在微信后台重置
#    - Dify API Key → 在Dify后台重新生成
#    - 支付宝沙箱密钥 → 重新生成密钥对
#    - JWT_SECRET_KEY → 生成新随机密钥
#    - PHONE_ENCRYPTION_KEY → 生成新32字节密钥

# 3. 创建 .env.example 模板文件（不含真实密钥）
```

**自我审查**: `.gitignore` 必须确保在所有环境中生效；使用 `git rm --cached .env` 从Git跟踪中移除（如果已被跟踪）；建议使用 `git-secrets` 或 `truffleHog` 扫描历史commit。

---

#### P0-2: 测试环境余额操作接口无鉴权、无 TEST_MODE 检查，可被直接调用转移资金

**位置**: `backend/app/api/v1/test_balance.py:1-365`

**风险与影响**:  
`/api/v1/test/balance/*` 下的所有接口（创建用户、冻结余额增/减、钱包余额增/减、核销结算、违约金划转、查看余额）**完全没有鉴权依赖**，也**没有任何 `TEST_MODE` 检查**。与 `test_auth.py` 不同（后者至少检查了 `TEST_MODE`），这些接口在任何环境下都可用。

攻击场景：
1. `POST /api/v1/test/balance/create-user` 创建测试用户
2. `POST /api/v1/test/balance/wallet-increase` 给该用户充值任意金额
3. 提现到真实支付宝/微信账户
4. **直接产生资金损失**

即使 `TEST_MODE=false`，这些接口仍然完全可用。

**修复方案**:
```python
# 在每个 test_balance.py 的接口函数开头添加：
from app.config import settings
from app.middleware.exception_handler import AppException
from app.schemas.common import ErrorCode

# 在每个接口函数体开头添加：
if not settings.TEST_MODE:
    raise AppException(
        code=ErrorCode.PERMISSION_DENIED,
        message="TEST_MODE 已关闭, 该接口不可用",
    )
```

**自我审查**: 确保所有 test_*.py 文件都有此检查；更根本的方案是将测试路由注册放在 `if settings.TEST_MODE:` 条件块内。

---

#### P0-3: TEST_MODE 默认 `True` + `admin_required` 在测试模式下对 user_id=1 无条件放行

**位置**: `backend/app/api/deps.py:11-18`，`backend/app/config.py:225-227`

**风险与影响**:  
- `TEST_MODE` 默认值为 `True`
- `admin_required` 在 `TEST_MODE=True` 时，只要 `user_id == 1` 就返回管理员权限，**完全跳过数据库角色检查**
- 攻击者只需以 user_id=1 的身份登录（可通过 P0-4 的 `/api/v1/test/token` 接口直接获取 token），即可访问所有 `/api/v1/admin/*` 管理接口

**攻击场景**:
1. `POST /api/v1/test/token {"user_id": 1}` → 获取管理员 JWT
2. `GET /api/v1/admin/users` → 查看所有用户手机号
3. `POST /api/v1/admin/users/ban` → 封禁任意用户
4. `POST /api/v1/admin/users/adjust-credit` → 篡改信用分
5. `POST /api/v1/admin/withdraws/*` → 审批提现（资金风险）

**修复方案**:
```python
# config.py - 修改默认值
TEST_MODE: bool = Field(
    default=False,  # 生产安全：默认关闭
    description="测试模式 (开启 /api/v1/test/* 接口; 生产环境必须关闭)",
)

# deps.py - 移除测试模式下的硬编码管理员逻辑
async def admin_required(user_id: int = Depends(get_current_user)) -> int:
    """运营角色权限校验。始终从数据库读取角色。"""
    from app.core.database import async_session_factory
    from app.models.user import User

    async with async_session_factory() as session:
        user = await session.get(User, user_id)
        if not user:
            raise PermissionDeniedException("用户不存在")
        if user.role != 1:
            raise PermissionDeniedException("需要管理员权限 (role=1)")
    return user_id
```

**自我审查**: 删除硬编码的 `user_id == 1` 检查后，必须确保至少有一个管理员用户在数据库中（通过数据库初始化脚本创建）。

---

#### P0-4: 测试 Token 接口可获取任意用户的 JWT（免鉴权）

**位置**: `backend/app/api/v1/test_auth.py:26-59`，`backend/app/api/v1/router.py:47`

**风险与影响**:  
`POST /api/v1/test/token` 接受任意 `user_id` 并返回有效 JWT Token。该接口注册在 `test_api_router`（无全局鉴权依赖）。虽然有 `TEST_MODE` 检查，但 `TEST_MODE` 默认 `True`。攻击者可以：
- 枚举 user_id 获取所有用户的 JWT
- 以管理员身份操作（user_id=1 → admin_required 绕过）

**修复方案**:
```python
# 短期：确保 TEST_MODE 默认 False
# 中期：为 test 接口增加额外防护
@router.post("/token", summary="[TEST] 获取Token")
async def get_test_token(req: TokenRequest, db: AsyncSession = Depends(get_db)):
    if not settings.TEST_MODE:
        raise AppException(code=ErrorCode.PERMISSION_DENIED, message="...")
    
    # 增加额外防护：仅允许本地IP访问
    # from fastapi import Request
    # if request.client.host not in ("127.0.0.1", "::1", "localhost"):
    #     raise AppException(code=ErrorCode.PERMISSION_DENIED, message="仅限本地访问")
    ...
```

---

#### P0-5: 验证码登录创建用户时手机号明文存储，完全绕过 AES 加密方案

**位置**: `backend/app/services/auth_service.py:72-74`

**风险与影响**:  
```python
user = User(
    phone=phone,          # ← 明文存储！
    phone_hash=user_hash,
    ...
)
```

与 `register_user`（正确使用 `encrypt_phone(phone)`）不同，`login_by_code` 直接将明文手机号存入数据库。这导致：
- 通过验证码登录创建的用户，其手机号在数据库中为明文
- 加密方案形同虚设
- 数据库泄露后，这部分用户的手机号直接暴露
- 后续 `decrypt_phone()` 对这些用户的数据解密会失败（因为不是合法密文格式）
- `mask_phone()` 函数中 `if len(phone) > 20` 的启发式判断对这些用户不生效

**修复方案**:
```python
# auth_service.py line 73
user = User(
    phone=encrypt_phone(phone),   # ← 修复：使用加密
    phone_hash=user_hash,
    ...
)
# 确保导入：from app.core.security import encrypt_phone
```

**自我审查**: 同样需要检查 `wechat_service.py` 中创建用户的路径（`complete_wechat_register` 已正确使用 `encrypt_phone`，确认无同类问题）。需要数据迁移脚本处理已存在的明文手机号数据。

---

#### P0-6: 订单解散（disband）时无自动退款，用户资金永久丢失

**位置**: `backend/app/services/order_service.py:496-498`

**风险与影响**:  
```python
# ── 预留: 批量退款入口 ──
# TODO: 对接 refund_service.batch_refund_for_order(order_id)
# 全额退款所有已支付拼友, 扣1分信用分, 发送退款通知
```

这是一个 `TODO` 注释，意味着当订单被解散时：
- 已支付的拼友资金不会自动退回
- 团长冻结余额不会解冻
- 用户资金永久锁定在系统中
- 退款只能通过人工介入处理

**修复方案**:
```python
async def disband_order(session, order_id, operator_id, trace_id=None):
    ...
    order.status = OrderStatus.DISBANDED
    
    # 批量退款所有已支付拼友
    participants = await session.execute(
        select(OrderParticipant).where(
            OrderParticipant.order_id == order_id,
            OrderParticipant.pay_status == 1,  # 已支付
        )
    )
    for p in participants.scalars().all():
        # 调用支付宝退款 + 扣减团长冻结余额
        await refund_service.refund_for_disband(session, p, order)
    
    await _commit_and_notify(session, order, old_status, trace_id)
    ...
```

---

### P1 - 高危/High

---

#### P1-1: JWT Access Token 有效期长达 7 天，且无刷新令牌轮换机制

**位置**: `backend/app/config.py:136-141`，`backend/app/core/security.py:87-110`

**风险与影响**:  
- Access Token 7天有效，一旦泄露，攻击者有长达7天的窗口期
- Refresh Token 30天有效，且刷新时不撤销旧 Refresh Token
- 行业最佳实践：Access Token 15分钟-1小时，Refresh Token 配合轮换（旋转刷新）

**修复方案**:
```python
ACCESS_TOKEN_EXPIRE_SECONDS: int = Field(
    default=15 * 60,  # 15分钟
)
REFRESH_TOKEN_EXPIRE_SECONDS: int = Field(
    default=7 * 24 * 3600,  # 7天
)
# 需要在 refresh 接口中实现 Refresh Token Rotation：
# 每次刷新时撤销旧 refresh_token，签发新的 refresh_token
```

---

#### P1-2: 密码使用 HMAC-SHA256 哈希，应使用 bcrypt/argon2

**位置**: `backend/app/core/security.py:363-374`

**风险与影响**:  
HMAC-SHA256 不是密码哈希算法，它：
- 无盐值（salt），相同密码产生相同哈希
- 计算速度极快，易受GPU暴力破解
- 不符合 OWASP 密码存储标准

**修复方案**:
```python
import bcrypt

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

def verify_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))
```

---

#### P1-3: `get_current_user` 中设备版本校验静默跳过（fail-open）

**位置**: `backend/app/core/security.py:70-78`

**风险与影响**:  
当 Redis 不可用时，token 版本校验被静默跳过：
```python
except Exception:
    _auth_logger.warning("Token版本校验跳过: Redis不可用")
```
这意味着如果攻击者能够使 Redis 不可用（如 DDoS），所有旧设备 Token 将重新生效，多设备登录互踢机制完全失效。

**修复方案**: 根据安全需求，要么改为 fail-closed（Redis不可用则拒绝），要么使用数据库作为 fallback。

---

#### P1-4: CORS 允许所有来源 + ElasticSearch 安全完全禁用

**位置**: `backend/app/main.py:137-144`，`docker-compose.yml:112-115`

**风险与影响**:  
- ES: `xpack.security.enabled=false` 且端口 `9200:9200` 暴露在宿主机上，任何人可无认证访问ES集群，读取/修改/删除所有索引数据
- Redis: 端口 `6379:6379` 暴露在宿主机，虽有密码但端口暴露增加了攻击面
- RabbitMQ: 管理端口 `15672:15672` 暴露在宿主机

**修复方案**: 生产环境中移除端口映射或绑定 `127.0.0.1`；ES启用安全功能；使用防火墙限制访问。

---

#### P1-5: 支付回调签名验证函数会修改入参（副作用）

**位置**: `backend/app/utils/alipay_client.py:258-262`

**风险与影响**:  
```python
def verify_notify_sign(params: dict) -> bool:
    sign = params.pop("sign", "")          # ← 修改原字典
    sign_type = params.pop("sign_type", ...) # ← 修改原字典
```
`dict.pop()` 会修改传入的字典。虽然 `handle_notify` 中做了 `params_copy = dict(params)` 的浅拷贝，但这是一个极易在未来重构中引入bug的陷阱。

**修复方案**: 在函数内部创建副本而非修改入参。

---

#### P1-6: 限流中间件在 Redis 不可用时完全放行（fail-open）

**位置**: `backend/app/middleware/rate_limit.py:60-63`

**风险与影响**:  
攻击者可以先DDoS Redis使其不可用，然后无限制地暴力破解登录接口或短信接口。

**修复方案**: 考虑在 Redis 不可用时使用内存限流作为 fallback，或在安全关键端点（登录/支付）使用 fail-closed 策略。

---

#### P1-7: 前端 `requireAuth` 完全禁用

**位置**: `frontend/src/js/common.js:49-51`

**风险与影响**:  
```javascript
window.requireAuth = function () {
    return;  // 直接返回，不做任何检查
};
```
注释标明"录视频期间已关闭"，但代码已部署。任何前端页面的鉴权检查完全失效。

**修复方案**: 恢复 requireAuth 逻辑，检查 token 存在性和有效性。

---

### P2 - 中危/Medium

---

#### P2-1: 用户地址表手机号明文存储

**位置**: `schema.sql:336`（`phone VARCHAR(11) NOT NULL`）

**风险与影响**: 与用户表的加密方案不一致。地址表中的手机号明文存储，数据泄露后直接暴露收货人手机号。

**修复方案**: 统一使用加密存储，或至少对地址手机号做脱敏处理。

---

#### P2-2: 管理员面板手机号脱敏逻辑错误——直接对密文切片

**位置**: `backend/app/api/v1/admin/users.py:57`

```python
"phone_masked": u.phone[:3] + "****" + u.phone[-4:]
```

**风险与影响**: `u.phone` 存储的是 AES 加密后的 base64 字符串（如 `abc123XYZ...`），而不是11位数字。因此 `u.phone[:3]` 会显示 base64 字符而非数字前缀，脱敏完全失效且显示乱码。

**修复方案**: 先解密再脱敏：
```python
from app.core.security import decrypt_phone, mask_phone
"phone_masked": mask_phone(decrypt_phone(u.phone)) if u.phone else "****"
```

---

#### P2-3: `create_access_token` 和 `create_refresh_token` 中调用 `_get_token_version` 涉及阻塞 Redis 操作

**位置**: `backend/app/core/security.py:87-129`

**风险与影响**: `_get_token_version` 使用同步 Redis 客户端（`r.get(key)`），而这是在 async 上下文中调用的。如果 Redis 响应慢，会阻塞整个事件循环。

**修复方案**: 使用异步 Redis 客户端。

---

#### P2-4: `_insert_log_and_commit` 提交整个 session 而非仅当前操作

**位置**: `backend/app/services/balance_service.py:77-135`

**风险与影响**: 调用 `await session.commit()` 会提交 session 中所有待处理的变更。如果调用方在 session 中有其他未准备好提交的变更，这些变更也会被意外提交。

**修复方案**: 使用嵌套事务（savepoint）或将 commit 职责交还给调用方。

---

#### P2-5: 订单列表查询使用 `len(scalars().all())` 做计数，潜在性能问题

**位置**: `backend/app/services/order_service.py:335-336`

**风险与影响**: 使用 `len(result.scalars().all())` 会从数据库拉取所有符合条件的行到内存中，仅为了计数。数据量大时将导致严重的内存压力和慢查询。

**修复方案**: 使用 `func.count()`：
```python
from sqlalchemy import func
count_stmt = select(func.count()).select_from(Order).where(...)
total = (await session.execute(count_stmt)).scalar() or 0
```

---

#### P2-6: 前端从 unpkg CDN 加载 Vue 和 Vant，存在供应链攻击风险

**位置**: `frontend/src/index.html:7-9`

**风险与影响**: 如果 unpkg CDN 被攻破或网络被劫持，加载的 JS 可被替换为恶意代码，窃取用户 Token、支付密码等。

**修复方案**: 使用 SRI (Subresource Integrity) 或自托管依赖。

---

#### P2-7: 支付金额比对使用 Decimal 字符串比较，但未处理科学计数法

**位置**: `backend/app/services/alipay_service.py:260`

**风险与影响**: `Decimal(notify_amount) != record.amount` 比较是安全的，但支付宝回调中的 `total_amount` 可能以科学计数法传递（极少数情况），需确保解析正确。

---

#### P2-8: `get_db` 不自动 commit，依赖调用方显式提交

**位置**: `backend/app/core/database.py:97-115`

**风险与影响**: 注释中说"由调用方显式commit"，但如果调用方忘记 commit，数据会静默丢失。没有机制检测未提交的变更。

**修复方案**: 考虑在 `finally` 块中检测是否有未提交的变更并记录警告。

---

### P3 - 低危/Low

---

#### P3-1: 异常处理通过字符串匹配判断错误类型，脆弱不可靠

**位置**: `backend/app/api/v1/pay.py:59-71`

```python
if "已支付" in msg or "已有待支付" in msg:
    code = ErrorCode.PAYMENT_DUPLICATE
```

**风险与影响**: 如果商品名称或描述中包含"已支付"字样，可能触发错误的错误码映射。

**修复方案**: 使用异常类型而非字符串匹配：
```python
except alipay_service.PaymentDuplicateException:
    code = ErrorCode.PAYMENT_DUPLICATE
```

---

#### P3-2: `_AES_KEY_BYTES` 不足32字节时补零，削弱密钥强度

**位置**: `backend/app/core/security.py:223`

**风险与影响**: 如果配置的密钥不足32字节，代码自动补零。但当前的密钥恰好32字节（`halfcart-aes-key-32bytes!!!`），风险较低。

**修复方案**: 增加密钥长度校验，不足32字节时抛出启动错误。

---

#### P3-3: 日志中可能包含敏感信息

**位置**: 多处，如 `backend/app/services/auth_service.py:29`

```python
logger.info(f"登录验证码已发送: phone={phone}")
```

**风险与影响**: 手机号明文出现在日志中，违反隐私保护要求。

**修复方案**: 日志中脱敏处理：
```python
logger.info(f"登录验证码已发送: phone={phone[:3]}****{phone[-4:]}")
```

---

#### P3-4: 前端 API_BASE 硬编码为 localhost:8000

**位置**: `frontend/src/js/common.js:72`

**风险与影响**: 部署时需要手动修改。

**修复方案**: 从页面 `data-api-base` 属性或环境变量读取。

---

#### P3-5: Docker 容器中 ES 使用单节点模式，无高可用

**位置**: `docker-compose.yml:110`（`discovery.type=single-node`）

**风险与影响**: 单点故障，但在 MVP 阶段可接受。

---

## 3. 项目健康度评估总结

### 整体评价

该项目在架构层面展现了良好的设计意识（双资金字段、幂等键、分布式锁+MySQL行锁双重防护、状态机模式），但在**安全基线**方面存在严重问题。代码处于早期开发阶段，存在大量"先跑通再加固"的妥协痕迹。

### 健康度评分: **32/100**（严重不健康）

各维度评分：
| 维度 | 得分 | 说明 |
|------|------|------|
| 安全基线 | 8/100 | 密钥泄露、接口无鉴权、密码哈希不安全 |
| 资金安全 | 35/100 | 核心账务逻辑设计合理，但测试接口暴露+订单解散无退款 |
| 代码质量 | 40/100 | 结构清晰但存在TODO、不一致（手机号加密/明文混用） |
| 测试覆盖 | 0/100 | 零自动化测试 |
| 运维安全 | 25/100 | Docker端口暴露、ES无认证、CORS全开 |

### 最迫切需要解决的 Top 3 风险点（立即修复）

1. **P0-1: `.env` 密钥泄露** — 轮换所有已泄露的凭证，确保 `.env` 不在版本控制中
2. **P0-2: 测试余额接口无鉴权** — 为 `test_balance.py` 所有端点增加 `TEST_MODE` 检查
3. **P0-5: 手机号明文存储** — 修复 `login_by_code` 中的 `phone=phone` → `phone=encrypt_phone(phone)`

### 建议的修复顺序

1. **立即（24小时内）**: P0-1（密钥轮换）、P0-2（测试接口加锁）、P0-3（TEST_MODE默认False）、P0-4（测试token接口加固）
2. **本周内**: P0-5（手机号加密）、P0-6（解散订单退款）、P1-1（Token过期时间缩短）、P1-2（密码哈希升级）
3. **本月内**: P1-3至P1-7、P2-1至P2-8
4. **下个迭代**: P3-1至P3-5、补充自动化测试、配置CI/CD安全检查（SAST/SCA）
