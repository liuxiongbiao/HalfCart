# 一次完整的全栈调试实录：定位死地址问题

> **项目**：HalfCart 半仓智拼 | **问题**：首页定位始终显示北京市虚假地址 | **耗时**：7 轮迭代 | **日期**：2026-07-24

---

## 一、问题描述

用户反馈：打开首页 `index.html`，左上角定位地址始终显示"北京市海淀区三里屯133号"，而非用户的真实城市（长沙）。进入地图选点后却能正确定位到长沙。

**预期**：首页左上角应显示用户当前所在城市和大致位置。
**实际**：始终显示固定的北京地址，无论用户身在何处。

---

## 二、排查过程

### 第 1 轮 —— 怀疑前端定位逻辑

**思路**：首页定位是否根本没有调用浏览器定位 API？

**检查**：阅读 `frontend/src/index.html` 中的 `getLocation()` 函数，发现硬编码了默认坐标：

```javascript
var userLat = 39.99, userLng = 116.47;  // 北京天安门
```

当 `navigator.geolocation.getCurrentPosition()` 失败时，直接用默认坐标发起雷达查询。

**修复**：给浏览器定位加 8 秒超时保护，超时后标记"定位超时"而非静默使用北京坐标。

**结论**：❌ 未解决。这是代码质量优化，但不是根因。

---

### 第 2 轮 —— 添加 IP 定位降级

**思路**：浏览器 GPS 在桌面端 HTTP 环境下通常不可用，需要一个 IP 定位兜底方案。

**修复**：GPS 失败后调用后端 `/api/v1/lbs/ip-locate` 做城市级定位。后端使用高德 IP 定位 API。

**结论**：❌ 未解决。后端 IP 定位永远返回北京 Mock 数据。

---

### 第 3 轮 —— 使用浏览器端 IP 定位

**思路**：后端在 Docker 容器内，看到的是 Docker 网关 IP（`172.x.x.x`），不是用户真实 IP。改用浏览器端直连第三方 IP 定位服务。

**修复**：前端直接调用 `https://ipapi.co/json/` 获取用户真实 IP 的城市。

**结论**：❌ 未解决。`ipapi.co` 在中国大陆被 GFW 阻断，请求永远超时。

---

### 第 4 轮 —— 使用高德 JSAPI 浏览器端定位

**思路**：高德 JSAPI v2.0 已在项目中加载（用于地图选点），其 `AMap.Geolocation` 插件支持 GPS + WiFi + IP 三通道定位，全程国内网络。

**修复**：首页改用 `AMap.Geolocation` 替代浏览器原生 `navigator.geolocation`。配置参数：
```javascript
new AMapLib.Geolocation({
    enableHighAccuracy: true,
    timeout: 8000,
    GeoLocationFirst: true,
});
```

**关键发现**：此时控制台日志显示坐标已经是长沙：
```
GET /api/v1/lbs/regeo?lat=28.219864&lng=112.903904
```
但前端页面仍然显示北京地址。

**结论**：❌ 未解决。定位引擎返回了正确坐标，但地址显示依然错误——问题出在坐标→地址的反查环节。

---

### 第 5 轮 —— 发现 Mock 模式仍在运行

**思路**：坐标正确（28.22°N，112.90°E 确为长沙），但后端 `regeo` API 返回的是北京地址。可能是后端高德 API Mock 模式仍在运行。

**检查**：`.env` 文件配置 `AMAP_MOCK_ENABLED=false`。但进入容器直接查询：
```bash
docker exec halfcart-backend python -c \
  "from app.config import settings; print(settings.AMAP_MOCK_ENABLED)"
# 输出: True  ← 问题！
```

**根因**：`.env` 改了但容器只用 `docker compose restart` 重启过。Docker 的 `restart` 不会重新读取环境变量，必须 `--force-recreate` 重建容器。

**修复**：
```bash
docker compose up -d --force-recreate backend consumer
```

验证：
```bash
docker exec halfcart-backend python -c "from app.config import settings; print(settings.AMAP_MOCK_ENABLED)"
# 输出: False ✅
```

直接测试 Python 函数：
```python
await amap.regeo(112.904, 28.219)
# 返回: "湖南省长沙市岳麓区麓谷街道..." ✅
```

**结论**：❌ 未解决。后端 API 已返回正确数据，但用户页面仍然显示北京地址。

---

### 第 6 轮 —— 发现高德 Geolocation 的 IP 地址不可信

**思路**：前端的 `AMap.Geolocation` 回调中 `result.formattedAddress` 字段来自高德 IP 库，可能对用户 IP 返回了错误的城市文本。

**代码审查**：
```javascript
amapGeolocation.getCurrentPosition(function(status, result) {
    if (status === 'complete' && result.position) {
        // result.position = {lat: 28.219, lng: 112.904} ← 坐标正确（长沙）
        // result.formattedAddress = "北京市海淀区三里屯133号" ← IP 库错误！
        
        if (result.formattedAddress) {
            currentLocation.value = result.formattedAddress;  // ← 直接用了 IP 地址
        } else {
            reverseGeocode(userLat, userLng);  // ← 始终不会执行
        }
    }
});
```

**修复**：始终用坐标调用后端 `regeo` API，不信任高德 IP 库的文本地址。

**结论**：❌ 仍未解决。代码改为始终走 `reverseGeocode` 后端 API，但页面仍显示北京地址。

---

### 第 7 轮 —— 添加调试日志，发现真凶

**思路**：在 `reverseGeocode` 的 `.then()` 回调中添加 `console.log`，直接看后端返回了什么数据。

**修复**：添加调试日志后，用户控制台输出：
```
[regeo] response: {code: 0, message: '查询成功', data: {…}}
[regeo] setting address to: 北京市海淀区三里屯133号
```

**关键发现**：后端 API 返回的 `formattedAddress` 就是"北京市海淀区三里屯133号"！但刚才直接测 Python 函数返回的是长沙地址——同一个函数，同一个参数，结果不同！

**推断**：一定存在缓存层。阅读 `amap_client.py` 中的 `regeo` 方法：

```python
async def regeo(self, lng: float, lat: float) -> Optional[dict]:
    coord_hash = self._hash(f"{lng:.6f}", f"{lat:.6f}")
    cache_key = _CACHE_REGEO_KEY.format(hash=coord_hash)
    cached = await self._cache_get(cache_key)
    if cached:
        return cached  # ← 缓存命中，直接返回！
    # ... 调用真实 API ...
```

**验证**：
```bash
docker exec halfcart-redis redis-cli -a xxx KEYS "*regeo*"
# 输出: 5 条缓存记录

docker exec halfcart-redis redis-cli -a xxx GET "halfcart:amap:regeo:779672fa893c1bf4"
# 输出: {"formatted_address": "北京市海淀区三里屯133号", ...}
```

**真凶确认**：Mock 模式期间，这组坐标被反向地理编码为虚假的北京地址，结果写入了 Redis 缓存（TTL 未设置或极长）。关闭 Mock 后，同坐标的请求命中缓存，直接返回了旧数据。

---

## 三、最终修复

```bash
# 清除所有 regeo 缓存
docker exec halfcart-redis redis-cli -a $REDIS_PASS \
  DEL halfcart:amap:regeo:779672fa893c1bf4 \
      halfcart:amap:regeo:0d540fa12b9b7bdc \
      halfcart:amap:regeo:acb29486a6d132e0 \
      halfcart:amap:regeo:b8aff51d8c3be775 \
      halfcart:amap:regeo:d96b34ecb7f33073
```

刷新页面后，首次请求缓存未命中 → 调用真实高德 API → 返回正确长沙地址 → 写入新缓存 → 显示正确。

---

## 四、技术总结

### 完整问题链路

```
① 浏览器 GPS 失败 (HTTP 环境)
     ↓
② ipapi.co 被 GFW 阻断 (境外服务)
     ↓
③ 高德 IP 定位返回假地址 (IP 库偏差)
     ↓  但坐标正确 (28.22, 112.90)
     ↓
④ 后端 regeo Mock 模式开启 (容器 env 未更新)
     ↓  假数据写入 Redis 缓存
     ↓
⑤ Mock 关闭 (重建容器)
     ↓  但缓存仍返回假的北京地址
     ↓
⑥ Redis 缓存命中 → 始终返回 "北京市海淀区三里屯133号"
```

### 教训

| # | 教训 | 具体表现 |
|---|------|---------|
| 1 | **`docker restart` 不更新 env** | 改了 `.env` 必须 `--force-recreate` 重建容器 |
| 2 | **缓存是调试盲区** | 直接测 Python 函数绕过缓存 → 误判为"API 正常" |
| 3 | **调试日志至关重要** | 加了 `console.log` 才确认后端 API 确实返回了假数据 |
| 4 | **不要信任 IP 定位的文本** | IP 库的 `formattedAddress` 可能与坐标不一致 |
| 5 | **境外服务在国内不可靠** | `ipapi.co` 方案在本地可行，但用户在国内 |
| 6 | **Mock 数据应有明确标识** | 如果假数据带 `(Mock)` 后缀，排查会快很多 |

### 涉及的技术层

```
前端 (Vue 3 + Vant 4)
  ├── navigator.geolocation (浏览器 GPS)
  ├── AMap.Geolocation   (高德 JSAPI 定位)
  ├── ipapi.co           (第三方 IP 定位, 被墙)
  └── reverseGeocode()   (坐标反查)
         ↓
Nginx 反向代理
         ↓
后端 (FastAPI)
  ├── /api/v1/lbs/regeo  (反向地理编码)
  └── /api/v1/lbs/ip-locate (IP 定位)
         ↓
服务层
  ├── amap_service.regeo_location()
  └── amap_client.regeo()
         ↓
缓存层 (Redis)
  └── halfcart:amap:regeo:* (坐标→地址缓存)
         ↓
外部 API (高德 Web Service)
  └── 逆地理编码 API
```

---

## 五、后续改进建议

1. **给 Mock 数据加统一后缀**（如 `（Mock - 请检查配置）`），让开发者在页面上能一眼识别
2. **缓存 TTL 设置**：`regeo` 缓存应设置合理过期时间（如 24 小时），避免 Mock 数据永久驻留
3. **环境变量变更检测**：在 Docker entrypoint 中打印关键配置，便于确认容器实际使用的值
4. **健康检查增强**：`/health` 端点可返回 Mock 状态，让前端在开发模式下显示警告横幅
5. **HTTPS 部署**：生产环境启用 HTTPS 后，浏览器 GPS 精度会大幅提升，减少对 IP 定位的依赖
