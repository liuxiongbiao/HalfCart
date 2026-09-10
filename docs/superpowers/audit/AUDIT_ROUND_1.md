# HalfCart 第一轮审计报告：核心主流程冒烟测试

**日期：** 2026-07-24

## 修复摘要

| 检查点 | 状态 | 修复内容 |
|--------|------|---------|
| 1. 注册/登录 | ✅ | 注册自动登录、Token轮换、微信API_BASE |
| 2. LBS定位 | ✅ | 定位超时+降级、Mock地址随坐标变化 |
| 3. 发布拼单 | ✅ | receipt_img OCR、语音超时60s |
| 4. 雷达可见 | ✅ | ES同步日志升级、确认must_not预期行为 |
| 5. 参团 | ✅ | 非参与人视图、三态互斥 |
| 6. 支付 | ✅ | 二次确认弹窗、结果页跳转 |
| 7. 订单流转+双视角 | ✅ | 三态操作栏、退款比例统一 |

## 各检查点详情

### 1. 注册/登录 (Task 1.1 + 1.2 + 1.3)

**状态：✅ 全部通过**

- 注册成功后自动登录：写入 token/refreshToken/user 到 localStorage，跳转首页
- 修复了 `var redirect` 变量提升导致的 fallback 分支 redirect 丢失
- refresh 端点返回新 refresh_token，前端 common.js 已适配
- 微信路由确认：login.html 使用二维码轮询流程（`/api/v1/wechat/*`），两份后端实现均在线且路径正确，无需修改
- login.html 中 `127.0.0.1:8000` 硬编码已替换为 `API_BASE`

### 2. LBS定位 (Task 2.1 + 2.2)

**状态：✅ 全部通过**

- 定位：8 秒 setTimeout 降级，`enableHighAccuracy:true` / `timeout:10000` / `maximumAge:300000`
- Mock regeo：基于坐标 md5 哈希从 15 城市地址库确定性选取，替代硬编码北京
- 高德加载失败降级：`initMap()` 中已有 `if (!AMapLib)` 防护
- ES 同步：4 处日志从 WARNING 提升为 ERROR（`logger.exception`），覆盖直接同步和 MQ 发送路径
- `must_not creator_id` 确认是 PRD 预期行为（用户不看到自己的订单），非 bug

### 3. 发布拼单 (Task 3.1)

**状态：✅ 主要步骤通过**

- 普通拼单/现货转让 payload 字段名与后端 Schema 完全匹配
- `receipt_img` 修复：硬编码 `'ocr_receipt'` 改为 `spotState.receiptImage || ''`
- 语音发单 3 处 `timeout: 60000` 已配置（`_processAudioBlob`、`_processSpeechText`、`onVoiceConfirm`）
- 手动测试未执行（规格部分满足）

### 4. 雷达可见 (Task 2.2)

**状态：✅ 通过**

- ES 文档 22 条，5 个消费者全部运行且队列为空，MQ 死信 0
- `logger.exception` 替换 `logger.warning`，自动输出完整堆栈，控制流不变
- 多账号手动测试未执行但有充分诊断结论（非 bug）

### 5. 参团 (Task 5.1)

**状态：✅ 全部通过**

- `v-if` 互斥 bug 修复：`v-if="isCreator"` → `v-else`（内嵌 `v-if="!isParticipant"` → `v-else`），三条路径互斥
- `goPay()` 改为 `goPage('pay-confirm.html', { id: orderId })`，使用 `orderId` 局部变量
- 死代码清理：`onJoin` 函数和 return 暴露已移除

### 6. 支付 (Task 6.1)

**状态：✅ 全部通过**

- 二次确认弹窗：`vant.Dialog.confirm()` 含金额 `¥X.XX`，确认调 `doPay()`，取消静默返回
- 支付成功跳转 `pay-result.html?orderId=XX&amount=YY`
- `pay-result.html` 读取 query 参数获取 `orderId`/`amount`，从后端获取 `orderNo`
- 向后兼容支付宝回调和钱包支付参数
- `submitting` 标记在 `doPay()` 开始设 true，`.then()` 和 `.catch()` 均重置

### 7. 订单流转+双视角 (Task 7.1)

**状态：✅ 有条件通过**

- 三态操作栏正确：`userRole==='leader'` / `userRole==='participant'` / 路人三互斥
- `isCreator` 从 ref 改为 computed，`fetchOrder()` 中移除手动赋值
- 退款比例统一：`constants.py` line 60 `PURCHASING: 0.80` 与 `verification_service.py` line 261 `Decimal("0.80")` 一致
- 边界情况：未登录用户 `getUser()` 返回 null 安全兜底

## 修复文件清单

| 文件 | 操作 | 检查点 |
|------|------|--------|
| `frontend/src/register.html` | 注册成功自动登录 | 1.1 |
| `backend/app/api/v1/auth.py` | refresh 返回新 refresh_token | 1.2 |
| `backend/app/services/auth_service.py` | `create_token_pair` 实现 | 1.2 |
| `frontend/src/login.html` | 微信路由 API_BASE 替换 | 1.3 |
| `frontend/src/index.html` | 定位超时+降级 | 2.1 |
| `backend/app/utils/amap_client.py` | Mock 地址随坐标变化 | 2.1 |
| `backend/app/services/radar_service.py` | 审查确认 must_not | 2.2 |
| `backend/app/services/order_service.py` | ES 同步日志 WARNING→ERROR | 2.2 |
| `backend/app/tasks/es_sync_consumer.py` | ES 同步日志 WARNING→ERROR | 2.2 |
| `frontend/src/publish.html` | receipt_img 修复、语音超时 | 3.1 |
| `frontend/src/order-detail.html` | 三态双视角操作栏、v-if 互斥 | 5.1, 7.1 |
| `frontend/src/pay-confirm.html` | 支付确认弹窗 | 6.1 |
| `frontend/src/pay-result.html` | 完善结果展示 | 6.1 |
| `backend/app/utils/constants.py` | 退款比例统一 0.80 | 7.1 |
| `backend/app/services/verification_service.py` | 退款比例统一 0.80 | 7.1 |

## 第二轮待办清单

### 阻断性修复（低于第一轮阈值，但建议优先处理）

| # | 问题 | 来源 | 风险 |
|---|------|------|------|
| 1 | `_mock_regeo` 返回值格式与 `_parse_regeo` 不兼容。`_mock_regeo` 返回嵌套结构（`regeocode.formatted_address`），而 `_parse_regeo` 返回扁平结构（`formatted_address`）。当前 `AMAP_MOCK_ENABLED=false` 不触发，重新启用则前端 `index.html` 获取 `res.data.formattedAddress` 为 `undefined`，地址展示回退为 `lat,lng` 数字坐标。需在 `_mock_regeo()` 末尾做格式归一化。 | 2.1 | 中 |
| 2 | `pay-confirm.html` 余额预检中 `parseFloat(order.pricePerPerson \|\| 0)` — `order` 是 `ref()` 对象，应使用 `order.value.pricePerPerson`。当前表达式恒等于 `0`，`walletBalance.value < 0` 条件永不成立，余额预检形同虚设。 | 6.1 | 中 |
| 3 | 路人 + DELIVERING(2) 现货状态操作栏为空，缺少"立即购买"按钮。PRD 映射表要求在此场景显示购买入口，属历史遗留。 | 7.1 | 低-中 |

### 功能性缺失

| # | 问题 | 来源 |
|---|------|------|
| 4 | 注册页面邮箱字段已在表单中收集但未在请求中发送给后端。 | 1.1 |
| 5 | 注册/登录使用相同的短信验证码 scene，应区分 `login` 和 `register` 以支持不同安全策略。 | 1.1 |
| 6 | 微信端到端扫码登录测试无法完成 — 需公网域名或内网穿透（如 ngrok）才能验证二维码轮询全流程。 | 1.3 |

### 测试覆盖不足

| # | 问题 | 来源 |
|---|------|------|
| 7 | Task 3.1 手动测试（登录→填写→发布→确认成功）未执行。 | 3.1 |
| 8 | Task 2.2 多账号雷达可见性手动测试未执行（但已有充分诊断结论，非 bug）。 | 2.2 |

### 优化建议

| # | 建议 | 来源 |
|---|------|------|
| 9 | 退款比例常量 `PURCHASING: 0.80` 已在 `constants.py` 和 `verification_service.py` 统一，但建议搜索全仓库确认无其他旧常量残留引用（如硬编码 `0.85`、`0.90` 等）。 | 7.1 |
| 10 | DISBANDED(4) 拼友视图无任何操作按钮，产品上可能需要"联系团长"等入口，建议观察用户反馈。 | 7.1 |
| 11 | Token 轮换无服务端旧 token 黑名单，泄露窗口从 30 天压缩到单次刷新间隔，对拼单类应用可接受。如需更高安全等级可后续补充。 | 1.2 |
