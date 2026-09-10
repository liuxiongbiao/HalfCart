# HalfCart 项目审计与修复计划

## 背景

项目完成度约 80%，核心主流程未完全跑通，多个细节未经验证。采用混合审计策略，分两轮执行：

- **第一轮**：核心主流程冒烟测试（打通端到端链路）
- **第二轮**：全量功能审计（逐模块深挖，对应方案 B）
- **第三轮**：页面逐页走查（对应方案 A）

## 第一轮：核心主流程冒烟测试

### 目标链路

```
注册 → 登录 → 首页雷达加载 → 发布拼单 → 首页雷达可见 → 参团 → 支付 → 订单状态流转
```

### 检查点

| # | 步骤 | 检查项 | 关键文件 |
|---|------|--------|----------|
| 1 | 注册/登录 | Token 签发、刷新、401 自动重试、注册表单完整性 | `backend/app/api/v1/auth.py`, `backend/app/core/security.py`, `frontend/src/js/common.js`, `login.html`, `register.html` |
| 2 | 首页 LBS | 真实定位获取地址、雷达列表 ES geo_distance 查询、品类筛选 | `frontend/src/index.html`, `backend/app/api/v1/lbs.py`, `backend/app/api/v1/radar.py`, ES 索引 |
| 3 | 发布拼单 | 表单→API→ES 同步→品类/坐标落库、现货转让 OCR、语音发单 | `frontend/src/publish.html`, `backend/app/api/v1/orders.py`, `backend/app/services/order_service.py`, ES 消费者 |
| 4 | 雷达可见 | 新账号登录后能否看到另一账号发布的订单（跨用户可见性） | ES 消费者、`radar/list` 端点、`order_service.py` 列表查询 |
| 5 | 参团 | 拼友视角下单、库存扣减、信用分检查 | `frontend/src/order-detail.html`, `backend/app/services/order_service.py` |
| 6 | 支付 | 支付页面、余额扣减、状态流转、退款逆向流转 | `frontend/src/payment.html`（or 支付组件）, `backend/app/services/verification_service.py` |
| 7 | 订单流转 | 团长/拼友双视角状态展示、四步进度条、倒计时 | `frontend/src/order-detail.html`, 状态机逻辑 |

### 当前已知问题（用户反馈）

1. `index.html` 左上角定位显示死值，无法获取真实位置
2. 高德地图定位精度不足
3. 支付界面不完善
4. 同一位置发布拼单后，换账号登录首页雷达不显示（跨用户可见性 bug）
5. 团长/拼友双视角界面未实现

### 输出

- 每个步骤标注 ✅ 通过 或 🐛 发现问题（附修复方案）
- 主流程跑通后，产出第一轮审计报告
- 阻断性 bug 当场修复，非阻断性 bug 记录至第二轮清单

## 第二轮：全量功能审计（方案 B 分层）

### 分层审计

1. **数据层**：模型字段完整性、索引、DDL 与 ORM 一致性、加密字段
2. **Service 层**：状态机完整性、边界条件、事务、错误处理
3. **API 层**：端点鉴权、参数校验、响应格式、错误码
4. **前端层**：22 个页面 UI 完整性、Vue 组件正确性、API 对接

### 模块清单

- 订单状态机（6 种状态 × 团长/拼友双视角）
- 钱包 & 支付闭环（充值、扣款、退款、提现）
- 语音发单三通道（Web Speech / MediaRecorder / Whisper）
- 地址管理（加密存储、高德地图选点）
- 收款账户管理（加密存储、CRUD）
- 纠纷/售后（Dify 裁判、退款阶梯）
- 聊天/阅后即焚
- 安全（端点鉴权、敏感字段加密、SQL 注入防护）
- 测试覆盖（15 条现有，需扩展）

## 第三轮：页面逐页走查（方案 A 功能驱动）

对 22 个页面逐一验证：
- UI 渲染完整性
- API 对接正确性
- 边界情况（空状态、错误状态、加载状态）
- 响应式/移动端适配

## 执行策略

- 每轮产出审计报告（`AUDIT_ROUND_N.md`）
- 阻断性 bug 当场修复
- 非阻断性 bug 和优化项记录至 backlog
- 每轮结束后用户确认再进入下一轮
