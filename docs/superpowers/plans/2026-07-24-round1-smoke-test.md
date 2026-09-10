# 第一轮：核心主流程冒烟测试 实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 打通"注册→登录→首页LBS定位→发布拼单→雷达可见→参团→支付→订单流转"全链路，修复阻断性bug，确保项目可端到端运行。

**架构：** 冒烟测试方式——每个检查点先审查代码找出问题，再启动Docker验证，修复阻断性bug，非阻断性记录至第二轮清单。按链路顺序执行，因为后续步骤依赖前面步骤的正确性。

**技术栈：** Python 3.11 FastAPI + MySQL 8.0 + Redis 7 + Elasticsearch 8 + Vue 3 CDN + Vant 4

---

## 前置准备：启动项目

### 任务 0：启动 Docker 并验证所有容器健康

**文件：**
- `docker-compose.yml`
- `.env`

- [ ] **步骤 1：启动 Docker 服务**

```bash
docker compose up -d
```

- [ ] **步骤 2：等待所有容器就绪，验证健康检查**

```bash
# 等待约30秒让所有容器启动
sleep 30
# 检查容器状态
docker compose ps
# 验证后端健康
curl -s http://localhost:8000/health
```

预期：所有 7 个容器 Running，健康检查返回 `{"status": "ok"}`

- [ ] **步骤 3：检查各服务连通性**

```bash
# MySQL
docker exec halfcart-mysql mysqladmin ping -u root -p"${MYSQL_ROOT_PASSWORD}" 2>/dev/null
# Redis
docker exec halfcart-redis redis-cli ping
# ES
curl -s http://localhost:9292/_cluster/health | grep status
# RabbitMQ
curl -s -u guest:guest http://localhost:6672/api/overview | grep -o '"status":"ok"'
```

预期：MySQL `mysqld is alive`，Redis `PONG`，ES `"status":"green"`（或 yellow），RabbitMQ 返回 JSON 含 `"status":"ok"`

- [ ] **步骤 4：如果任何容器未就绪，检查日志并修复**

```bash
docker compose logs <service_name> --tail 50
```

---

## 检查点 1：注册/登录流程

### 任务 1.1：审查并修复注册流程

**发现的问题（来自探索）：**
1. `register.html` 注册成功后丢弃了后端返回的 token，强制用户重新登录
2. `register.html` 收集邮箱字段但未发送给后端
3. 注册验证码和登录验证码共用 `"login"` scene（低优先级，记录至第二轮）

**文件：**
- 修改：`frontend/src/register.html`（`onSubmit` 函数）

- [ ] **步骤 1：修复注册成功后自动登录**

在 `frontend/src/register.html` 中找到 `onSubmit` 函数（约第 230-260 行），当前逻辑是注册成功后 `setTimeout(() => { window.location.href = ... }, 1500)` 跳转到登录页。

修改为：注册成功后保存 token 并直接跳转首页。

找到类似以下代码：
```javascript
// 当前代码（约第 245-255 行）
if (res.code === 0) {
    vant.Toast.success('注册成功');
    setTimeout(function() {
        var redirect = getQuery('redirect') || 'login.html';
        window.location.href = redirect;
    }, 1500);
}
```

替换为：
```javascript
if (res.code === 0) {
    // 后端返回 token pair，注册即登录
    if (res.data && res.data.accessToken) {
        window.setToken(res.data.accessToken);
        window.setRefreshToken(res.data.refreshToken);
        window.setUser(res.data.user);
        vant.Toast.success('注册成功');
        setTimeout(function() {
            var redirect = getQuery('redirect') || 'index.html';
            window.location.href = redirect;
        }, 800);
    } else {
        vant.Toast.success('注册成功，请登录');
        setTimeout(function() {
            window.location.href = 'login.html';
        }, 800);
    }
}
```

- [ ] **步骤 2：验证修复 — 检查 `auth_service.py` 中 `register_user` 的返回值**

在 `backend/app/services/auth_service.py` 中搜索 `register_user` 函数，确认返回值包含 `access_token`、`refresh_token`、`user` 字段。预期返回 `token_pair` 字典。

```bash
grep -n "def register_user" backend/app/services/auth_service.py
grep -n "return" backend/app/services/auth_service.py | head -20
```

确认返回值结构与前端解析一致。

- [ ] **步骤 3：测试注册流程**

手动测试：
1. 打开 `http://localhost:3000/register.html`
2. 填写用户名（test001）、手机号、获取验证码、输入密码
3. 提交注册
4. 确认自动跳转到 `index.html`（而非 `login.html`）
5. 检查 localStorage 中 `halfcart_token`、`halfcart_refresh`、`halfcart_user` 已设置

### 任务 1.2：审查并修复 Token 刷新机制

**发现的问题：**
1. Refresh token 不会被轮换（后端不返回新 refresh_token）
2. 并发刷新有轻微竞态条件（低优先级）

**文件：**
- 审查：`backend/app/api/v1/auth.py`（`refresh_access_token` 端点）
- 审查：`frontend/src/js/common.js`（`trySilentRefresh` 函数）

- [ ] **步骤 1：后端 refresh 端点返回新 refresh_token**

找到 `backend/app/api/v1/auth.py` 中的 `refresh_access_token` 端点，当前只返回 `access_token`。

修改为同时签发新的 refresh_token（实现 token rotation）：

```python
# 在 refresh_access_token 中，替换 return 语句
@router.post("/refresh", response_model=APIResponse)
async def refresh_access_token(req: RefreshTokenRequest):
    # ... 现有验证逻辑 ...
    user_id = payload["sub"]
    phone_hash = payload.get("phash", "")
    
    # 签发新的 token pair（实现 token rotation）
    token_pair = create_token_pair(user_id, phone_hash)
    
    return APIResponse.success(data={
        "access_token": token_pair["access_token"],
        "refresh_token": token_pair["refresh_token"],  # 新增：轮换 refresh_token
        "token_type": token_pair["token_type"],
        "expires_in": settings.ACCESS_TOKEN_EXPIRE_SECONDS,
    })
```

- [ ] **步骤 2：前端保存新的 refresh_token**

确认 `frontend/src/js/common.js` 中 `trySilentRefresh` 已处理 `refreshToken`（第 107 行已有逻辑：`if (data.data.refreshToken) setRefreshToken(data.data.refreshToken);`）。此逻辑已就绪，无需修改。

- [ ] **步骤 3：手动测试 Token 刷新**

```bash
# 1. 先登录获取 token
curl -s -X POST http://localhost:8000/api/v1/auth/login-by-code \
  -H "Content-Type: application/json" \
  -d '{"phone":"13800138000","code":"123456"}' | python -m json.tool

# 2. 用 refresh_token 刷新（替换为实际值）
curl -s -X POST http://localhost:8000/api/v1/auth/refresh \
  -H "Content-Type: application/json" \
  -d '{"refresh_token":"<YOUR_REFRESH_TOKEN>"}' | python -m json.tool
```

预期：响应包含 `access_token` 和 `refresh_token` 两个字段。

### 任务 1.3：修复微信登录路由不匹配（如果有）

**发现的问题：**
1. `login.html` 调用 `/api/v1/wechat/qrcode` 和 `/api/v1/wechat/check`，但后端路由在 `/api/v1/auth/wechat/qr` 和 `/api/v1/auth/wechat/callback`
2. 硬编码 `127.0.0.1:8000` 而非使用 `API_BASE`

**文件：**
- 修改：`frontend/src/login.html`（`wechatLogin`、`loadWechatQR`、`pollWechatState` 函数）

- [ ] **步骤 1：修复微信登录 API 路径**

在 `login.html` 中找到所有 `fetch('http://127.0.0.1:8000/api/v1/wechat/...')` 调用，替换为使用 `API_BASE + '/api/v1/auth/wechat/...'`。

具体修改（约第 380-420 行）：
```javascript
// 旧代码
var apiBase = 'http://127.0.0.1:8000';
// 或硬编码的 URL

// 新代码：从 common.js 获取 API_BASE
var apiBase = window.API_BASE || 'http://localhost:8000';

// wechatLogin 中：
fetch(apiBase + '/api/v1/auth/wechat/qr')
// （原为 /api/v1/wechat/qrcode）

// pollWechatState 中：
fetch(apiBase + '/api/v1/auth/wechat/callback?scene_id=' + sceneId)
// 需要确认后端是否支持 scene_id 参数轮询——如果不支持，标记为第二轮待办
```

- [ ] **步骤 2：标记微信登录为第二轮验证项**

由于微信 OAuth 需要公网域名/内网穿透才能完整测试，将端到端微信登录测试标记为第二轮。当前确保路由不报 404 即可。

```bash
# 验证路由存在
curl -s http://localhost:8000/api/v1/auth/wechat/qr | python -m json.tool
```

预期：返回 JSON 响应（可能是微信配置错误，但不应该是 404）。

---

## 检查点 2：首页 LBS 定位 + 雷达列表

### 任务 2.1：修复首页定位"死值"问题

**发现的问题：**
1. 硬编码默认坐标 `var userLat = 39.99, userLng = 116.47;`（北京天安门）
2. 浏览器 geolocation 无超时设置，可能无限挂起
3. AMap Mock 模式返回假地址（即使坐标正确）
4. 地图选点器加载失败时无降级

**文件：**
- 修改：`frontend/src/index.html`（`getLocation` 函数、地图选点器逻辑）
- 修改：`backend/app/config.py`（AMap Mock 配置）

- [ ] **步骤 1：给浏览器 geolocation 加超时和 highAccuracy**

在 `frontend/src/index.html` 中找到 `getLocation()` 函数（约第 307-315 行），修改为：

```javascript
function getLocation() {
    if (navigator.geolocation) {
        // 加超时和超时后的降级处理
        var timeoutId = setTimeout(function() {
            currentLocation.value = '定位超时·使用IP定位';
            fetchOrders(); // 用默认坐标先加载
        }, 8000); // 8秒超时
        
        navigator.geolocation.getCurrentPosition(
            function(pos) {
                clearTimeout(timeoutId);
                userLat = pos.coords.latitude;
                userLng = pos.coords.longitude;
                reverseGeocode(userLat, userLng);
                fetchOrders();
            },
            function(err) {
                clearTimeout(timeoutId);
                console.warn('Geolocation error:', err.message);
                currentLocation.value = '定位失败·使用默认位置';
                fetchOrders(); // 用默认坐标先加载
            },
            {
                enableHighAccuracy: true,
                timeout: 10000,
                maximumAge: 300000  // 5分钟缓存
            }
        );
    } else {
        currentLocation.value = '浏览器不支持定位';
        fetchOrders();
    }
}
```

- [ ] **步骤 2：关闭 AMap Mock 模式（如已配置真实 Key）**

检查 `.env` 中 `AMAP_WEB_KEY` 是否有值：

```bash
grep AMAP_WEB_KEY .env
```

如果 `AMAP_WEB_KEY` 为空，Mock 模式必须保持开启。但仍需改进 Mock 逻辑：Mock 应该至少返回与用户坐标一致的地址，而非始终显示"北京"。

修改 `backend/app/utils/amap_client.py` 中的 `_mock_regeo()`（约第 260-280 行），让它基于传入的坐标生成地址，而非硬编码北京：

```python
def _mock_regeo(self, lng: float, lat: float) -> dict:
    """Mock 逆地理编码 — 基于实际坐标生成地址"""
    # 使用坐标的小数部分生成变体，而非硬编码北京
    import hashlib
    seed = hashlib.md5(f"{lat:.4f},{lng:.4f}".encode()).hexdigest()
    idx = int(seed[:8], 16) % len(MOCK_ADDRESSES)
    addr = MOCK_ADDRESSES[idx]
    return {
        "status": "1",
        "regeocode": {
            "formatted_address": f"{addr}（模拟）",
            "addressComponent": {
                "province": addr.get("province", ""),
                "city": addr.get("city", ""),
                "district": addr.get("district", ""),
            }
        }
    }
```

如果 `.env` 中有 `AMAP_WEB_KEY` 有效值，将 `AMAP_MOCK_ENABLED` 设为 `false`：

```bash
# 编辑 .env
# AMAP_MOCK_ENABLED=false
```

- [ ] **步骤 3：添加高德 JSAPI 加载失败的降级提示**

在 `initMap()` 函数（约第 228 行）中，确认已有 `!AMapLib` 检查和提示。如果高德加载失败，确保用户看到明确的提示而非静默失败。

- [ ] **步骤 4：验证定位流程**

```bash
# 启动后打开浏览器访问 http://localhost:3000
# 1. 浏览器应弹出定位权限请求
# 2. 允许后，左上角应显示地址（非"定位失败·使用默认"）
# 3. 如果拒绝定位，应显示降级提示
```

---

### 任务 2.2：修复雷达列表"换账号看不到订单"问题

**发现的问题：**
1. `must_not` 过滤 `creator_id != user_id` — 自己看不到自己的订单（这是预期行为，但导致测试混淆）
2. ES 同步失败被静默记录 — 订单可能未进入 ES 索引
3. 状态过滤严格 — 只有 status=0 (GATHERING) + order_type=1 (NORMAL) 的订单可见

**文件：**
- 审查：`backend/app/services/radar_service.py`（`_build_es_dsl`）
- 审查：`backend/app/services/order_service.py`（ES 同步逻辑）
- 审查：`backend/app/tasks/es_sync_consumer.py`（MQ 消费者）

- [ ] **步骤 1：排查 ES 同步是否正常**

```bash
# 检查 ES 索引中是否有订单数据
curl -s http://localhost:9292/order_index/_count | python -m json.tool

# 查看 ES 同步消费者日志
docker compose logs halfcart-consumer --tail 30 | grep -i "es\|elastic\|sync"

# 检查 RabbitMQ 队列状态
curl -s -u guest:guest http://localhost:6672/api/queues | python -m json.tool | grep -A5 "order.status.sync"
```

预期：ES 索引中有文档，消费者无错误日志，MQ 队列消息被消费。

- [ ] **步骤 2：如果 ES 同步有问题，检查 MQ 消费者是否运行**

```bash
# 查看消费者容器状态
docker compose ps halfcart-consumer

# 查看消费者启动日志
docker compose logs halfcart-consumer --tail 50
```

如果消费者未启动，检查 `docker-compose.yml` 中 `halfcart-consumer` 的定义和启动命令。

- [ ] **步骤 3：确认订单创建时 ES 直接同步未失败**

在 `backend/app/services/order_service.py` 中找到 `create_order` 函数，确认 `upsert_order_doc(order.id, es_doc, refresh=True)` 调用未被静默吞掉异常。

搜索：
```bash
grep -n "upsert_order_doc\|except" backend/app/services/order_service.py | head -20
```

如果 `upsert_order_doc` 被包在 `try/except` 中且只 `logger.error`，添加更明显的告警。

- [ ] **步骤 4：多账号测试雷达可见性**

手动测试：
1. 账号 A 登录，在北京天安门附近（39.99, 116.47）发布一个拼单
2. 退出登录，注册账号 B
3. 账号 B 登录，允许定位（或手动选点到天安门附近）
4. 确认雷达列表中能看到账号 A 发布的拼单

---

## 检查点 3：发布拼单

### 任务 3.1：验证发布表单完整性和 API 对接

**发现的问题：**
1. 现货发布 `receipt_img` 传的是硬编码字符串 `'ocr_receipt'`，而非图片数据
2. 语音发单三通道需要 60s 超时

**文件：**
- 审查：`frontend/src/publish.html`
- 审查：`backend/app/api/v1/orders.py`（create_order, create_spot_order, speech-to-order）

- [ ] **步骤 1：审查发布表单的请求 payload**

在 `publish.html` 中找到 `submitOrder()` 函数（约第 650-700 行），检查请求体是否与后端 `CreateOrderRequest` Schema 匹配。

关键检查项：
- `goods_name` → `goodsName`（注意 convertKeys 做 snake→camel 转换，前端应发 camelCase）
- `category_id` → `categoryId`
- `total_people` → `totalPeople`
- `price_per_person` → `pricePerPerson`
- `lat`, `lng`, `radius_km` → `lat`, `lng`, `radiusKm`

```bash
# 对照后端 Schema 检查
grep -A30 "class CreateOrderRequest" backend/app/schemas/order.py
```

- [ ] **步骤 2：修复现货 receipt_img 硬编码字符串**

在 `publish.html` 中找到现货发布逻辑，确认 `receipt_img` 字段的值。如果是 `'ocr_receipt'`，改为实际 OCR 识别结果或空字符串。

- [ ] **步骤 3：测试发布流程**

手动测试：
1. 登录后打开 `http://localhost:3000/publish.html`
2. 填写商品名称、选择品类、设置价格、人数、截止时间
3. 选择交接地点
4. 点击发布
5. 确认成功提示，跳转到订单详情

---

## 检查点 4：跨账号雷达可见（已在 2.2 覆盖）

此检查点与 2.2 合并。验证标准：账号 A 发布的订单，账号 B 在雷达列表中可见。

---

## 检查点 5：参团流程

### 任务 5.1：验证普通拼单参团

**文件：**
- 审查：`frontend/src/order-detail.html`（参团按钮、非参与人视图）
- 审查：`frontend/src/pay-confirm.html`（支付确认页面）
- 审查：`backend/app/services/participation_service.py`（join_order）

- [ ] **步骤 1：审查非参与人视图**

在 `order-detail.html` 中找到 `isCreator` 的判断逻辑。当前非参与人看到的是 `!isCreator` 分支，该分支假设用户已是拼友。需要增加 `isParticipant` 判断。

查找约第 400-500 行的模板部分，确认按钮渲染逻辑：

```javascript
// 需要在 setup 中增加 isParticipant 计算
var isParticipant = Vue.computed(function() {
    return order.value.userRole === 2; // role=2 是拼友
});
```

- [ ] **步骤 2：确保非参与人能看到"立即参团"按钮**

在订单详情模板中，GATHERING 状态的非参与人应该看到"立即参团"按钮（跳转到 `pay-confirm.html`）。

如果当前模板只有 `isCreator` 和 `!isCreator` 两个分支，增加第三个分支或修改 `!isCreator` 分支逻辑：

```html
<!-- 非参与人（GATHERING 状态） -->
<van-button v-if="!isCreator && !isParticipant && order.status === 0"
    type="primary" block @click="goPay">立即参团</van-button>

<!-- 拼友已参团 -->
<van-button v-if="isParticipant && order.status === 0"
    type="danger" plain block @click="quitOrder">退出拼单</van-button>
```

- [ ] **步骤 3：验证 pay-confirm.html 余额检查逻辑**

确认 `pay-confirm.html` 在余额不足时正确禁用按钮并显示提示。检查 `canPay` computed 属性。

- [ ] **步骤 4：测试参团流程**

手动测试：
1. 账号 B 在雷达列表点击账号 A 的订单
2. 进入订单详情，看到"立即参团"按钮
3. 点击进入支付确认页
4. 确认支付
5. 返回订单详情，状态变为"已参团"

---

## 检查点 6：支付闭环

### 任务 6.1：审查并完善支付界面

**发现的问题：**
1. `pay-confirm.html` 只支持钱包余额支付，无第三方支付
2. `pay-result.html` 有支付宝回调处理但未连接
3. 无支付安全确认（一键支付，无二次确认）

**文件：**
- 修改：`frontend/src/pay-confirm.html`
- 审查：`frontend/src/pay-result.html`

- [ ] **步骤 1：在 pay-confirm.html 增加支付确认弹窗**

在"确认支付"按钮的点击事件中，先弹出确认对话框：

```javascript
function onPay() {
    if (submitting.value) return;
    if (!canPay.value) {
        vant.Toast.fail('余额不足，请先充值');
        return;
    }
    
    // 二次确认弹窗
    vant.Dialog.confirm({
        title: '确认支付',
        message: '确认使用钱包余额支付 ¥' + order.value.pricePerPerson.toFixed(2) + ' ？',
        confirmButtonText: '确认支付',
        cancelButtonText: '再想想',
    }).then(function() {
        doPay();
    }).catch(function() {
        // 用户取消
    });
}
```

- [ ] **步骤 2：支付成功后跳转到 pay-result.html**

修改 `doPay()` 函数，支付成功后跳转到 `pay-result.html` 而非订单详情：

```javascript
function doPay() {
    submitting.value = true;
    var endpoint = isSpot.value
        ? '/api/v1/orders/' + orderId + '/buy-spot'
        : '/api/v1/orders/' + orderId + '/join';
    
    var body = {};
    if (!isSpot.value && selectedAddress.value) {
        body.addressId = selectedAddress.value.id;
    }
    
    request(endpoint, { method: 'POST', body: JSON.stringify(body) })
        .then(function(res) {
            // 跳转到支付结果页
            var params = 'orderId=' + orderId + '&amount=' + order.value.pricePerPerson;
            window.location.href = 'pay-result.html?' + params;
        })
        .catch(function(err) {
            vant.Toast.fail(err.message || '支付失败');
        })
        .finally(function() {
            submitting.value = false;
        });
}
```

- [ ] **步骤 3：完善 pay-result.html 支付成功展示**

确认 `pay-result.html` 能正确展示支付金额、订单编号，并提供"查看订单"按钮跳转到 `order-detail.html`。

- [ ] **步骤 4：测试支付闭环**

手动测试：
1. 从订单详情点击"立即参团" → 确认支付
2. 弹出确认对话框
3. 确认后完成支付
4. 跳转到支付结果页（成功状态）
5. 点击"查看订单"回到订单详情

---

## 检查点 7：订单状态流转 + 双视角

### 任务 7.1：实现团长/拼友双视角 UI

**发现的问题：**
1. 订单详情页缺少完整的三态视图（非参与人 / 团长 / 拼友）
2. 非参与人的 GATHERING 状态无"参团"按钮（已在 5.1 覆盖）
3. 评价功能 stub（"功能开发中"）

**文件：**
- 修改：`frontend/src/order-detail.html`

- [ ] **步骤 1：重构订单详情操作栏 — 三态视图**

在 `order-detail.html` 的 `setup()` 中添加 `userRole` 和 `isCreator`、`isParticipant` 的 computed：

```javascript
var isCreator = Vue.computed(function() {
    return order.value.creatorId === user.value.userId;
});
var isParticipant = Vue.computed(function() {
    return order.value.userRole === 2;
});
var userRole = Vue.computed(function() {
    if (isCreator.value) return 'leader';
    if (isParticipant.value) return 'participant';
    return 'stranger';
});
```

在模板中根据 `userRole` 渲染不同的操作栏：

```html
<!-- 团长操作栏 -->
<template v-if="userRole === 'leader'">
    <!-- GATHERING: 取消拼单 + 分享 -->
    <!-- PURCHASING: 已买到 -->
    <!-- DELIVERING: 验证核销码 + 取消(仅现货无人) -->
    <!-- FINISHED: 查看钱包 -->
    <!-- DISBANDED: 返回 -->
</template>

<!-- 拼友操作栏 -->
<template v-else-if="userRole === 'participant'">
    <!-- GATHERING: 退出拼单 + 分享 -->
    <!-- PURCHASING: 申请退款 + 联系团长 -->
    <!-- DELIVERING: 查看核销码 + 申请售后 -->
    <!-- FINISHED: 去评价 -->
</template>

<!-- 路人操作栏 -->
<template v-else>
    <!-- GATHERING: 立即参团 -->
    <!-- 其他状态: 显示"订单已结束"或隐藏操作栏 -->
</template>
```

- [ ] **步骤 2：修复退款比例常量不一致**

`constants.py` 中 PURCHASING 退 75%，`verification_service.py` 实际退 80%。统一为 80%（以实际逻辑为准）。

```bash
grep -n "REFUND_RATE\|0\.75\|0\.80" backend/app/utils/constants.py backend/app/services/verification_service.py
```

修改 `constants.py`：
```python
# 修改前
("PURCHASING", "采购中", 0.75),
# 修改后
("PURCHASING", "采购中", 0.80),
```

- [ ] **步骤 3：端到端测试订单全生命周期**

手动测试（双账号）：
1. 账号 A 发布拼单，状态 GATHERING
2. 账号 B 参团，满员后状态 → PURCHASING
3. 账号 A 标记"已买到"，状态 → DELIVERING
4. 账号 A 输入核销码验证，状态 → FINISHED

每一步验证：
- 团长视角的按钮是否正确
- 拼友视角的按钮是否正确
- 进度条步骤是否正确

---

## 收尾

### 任务 8：产出第一轮审计报告

- [ ] **步骤 1：汇总所有检查点的结果**

记录格式：
```markdown
| 检查点 | 状态 | 发现 | 修复 |
|--------|------|------|------|
| 1. 注册/登录 | ✅/🐛 | ... | ... |
| 2. LBS定位 | ... | ... | ... |
| ... | ... | ... | ... |
```

- [ ] **步骤 2：列出第二轮待办项**

将非阻断性问题、优化建议、未覆盖的模块整理为第二轮清单。

- [ ] **步骤 3：保存审计报告**

保存至 `docs/superpowers/audit/AUDIT_ROUND_1.md`

---

## 文件变更总览

| 文件 | 操作 | 所属检查点 |
|------|------|-----------|
| `frontend/src/register.html` | 修改 — 注册成功后自动登录 | 1.1 |
| `backend/app/api/v1/auth.py` | 修改 — refresh 返回新 refresh_token | 1.2 |
| `frontend/src/login.html` | 修改 — 微信路由对齐 | 1.3 |
| `frontend/src/index.html` | 修改 — 定位超时+降级 | 2.1 |
| `backend/app/utils/amap_client.py` | 修改 — Mock 地址随坐标变化 | 2.1 |
| `backend/app/services/order_service.py` | 审查 — ES 同步错误处理 | 2.2 |
| `frontend/src/publish.html` | 审查 — 表单 payload 对齐 | 3.1 |
| `frontend/src/order-detail.html` | 修改 — 三态双视角操作栏 | 5.1, 7.1 |
| `frontend/src/pay-confirm.html` | 修改 — 支付确认弹窗+结果页跳转 | 6.1 |
| `frontend/src/pay-result.html` | 修改 — 完善结果展示 | 6.1 |
| `backend/app/utils/constants.py` | 修改 — 退款比例统一为 0.80 | 7.1 |
