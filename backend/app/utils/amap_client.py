"""
高德地图服务异步客户端
======================
企业级封装: 地理编码/逆地理编码/POI搜索/距离计算/IP定位/坐标校验。
全配置化、零硬编码、支持 Mock 模式、熔断器、多级缓存、自动降级。

核心技术:
  - aiohttp 异步请求, 不阻塞事件循环
  - 熔断器: 连续失败N次→冷却期→自动恢复
  - Redis 缓存: 相同参数 24h 内不重复调用
  - Mock 模式: 无Key时可完整联调, 出入参与真实完全一致
  - 坐标系: 全链路 GCJ-02
"""

import asyncio
import hashlib
import json
import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import urlencode

import aiohttp

from app.config import settings
from app.core.redis_client import get_redis

logger = logging.getLogger(__name__)

# ── 高德 API 端点 ──
_AMAP_BASE = "https://restapi.amap.com/v3"
_GEOCODE_URL = f"{_AMAP_BASE}/geocode/geo"
_REGEO_URL = f"{_AMAP_BASE}/geocode/regeo"
_POI_URL = f"{_AMAP_BASE}/place/text"
_IP_URL = f"{_AMAP_BASE}/ip"
_DISTANCE_URL = f"{_AMAP_BASE}/distance"

# ── Redis Key ──
_CACHE_GEO_KEY = "halfcart:amap:geo:{hash}"
_CACHE_REGEO_KEY = "halfcart:amap:regeo:{hash}"
_CACHE_POI_KEY = "halfcart:amap:poi:{hash}"
_CACHE_IP_KEY = "halfcart:amap:ip:{ip}"
_CB_KEY = "halfcart:amap:circuit_breaker"

# ── Mock 地址库（覆盖全国主要城市）──
MOCK_ADDRESSES = [
    {"province": "北京市", "city": "北京市", "district": "朝阳区", "address": "北京市朝阳区望京街道100号"},
    {"province": "上海市", "city": "上海市", "district": "浦东新区", "address": "上海市浦东新区张江高科技园区200号"},
    {"province": "广东省", "city": "广州市", "district": "天河区", "address": "广东省广州市天河区体育西路88号"},
    {"province": "广东省", "city": "深圳市", "district": "南山区", "address": "广东省深圳市南山区科技园南路66号"},
    {"province": "浙江省", "city": "杭州市", "district": "西湖区", "address": "浙江省杭州市西湖区文三路55号"},
    {"province": "四川省", "city": "成都市", "district": "高新区", "address": "四川省成都市高新区天府大道77号"},
    {"province": "湖北省", "city": "武汉市", "district": "洪山区", "address": "湖北省武汉市洪山区光谷大道33号"},
    {"province": "江苏省", "city": "南京市", "district": "鼓楼区", "address": "江苏省南京市鼓楼区汉中路22号"},
    {"province": "陕西省", "city": "西安市", "district": "雁塔区", "address": "陕西省西安市雁塔区高新路11号"},
    {"province": "重庆市", "city": "重庆市", "district": "渝中区", "address": "重庆市渝中区解放碑步行街99号"},
    {"province": "福建省", "city": "厦门市", "district": "思明区", "address": "福建省厦门市思明区环岛南路77号"},
    {"province": "湖南省", "city": "长沙市", "district": "岳麓区", "address": "湖南省长沙市岳麓区梅溪湖路44号"},
    {"province": "山东省", "city": "青岛市", "district": "市南区", "address": "山东省青岛市市南区香港中路55号"},
    {"province": "河南省", "city": "郑州市", "district": "金水区", "address": "河南省郑州市金水区花园路33号"},
    {"province": "天津市", "city": "天津市", "district": "南开区", "address": "天津市南开区鞍山西道88号"},
]


# ══════════════════════════════════════════════
# 熔断器
# ══════════════════════════════════════════════


@dataclass
class CircuitBreaker:
    failures: int = 0
    last_failure_time: float = 0.0
    is_open: bool = False

    def record_failure(self) -> None:
        self.failures += 1
        self.last_failure_time = time.monotonic()
        if self.failures >= settings.AMAP_CIRCUIT_BREAKER_THRESHOLD:
            self.is_open = True
            logger.error(f"高德熔断器触发: 连续失败{self.failures}次, 冷却{settings.AMAP_CIRCUIT_BREAKER_COOLDOWN}s")

    def record_success(self) -> None:
        self.failures = 0
        self.is_open = False

    def check(self) -> bool:
        """返回 True 表示熔断器关闭(可请求), False 表示熔断中。"""
        if not self.is_open:
            return True
        elapsed = time.monotonic() - self.last_failure_time
        if elapsed >= settings.AMAP_CIRCUIT_BREAKER_COOLDOWN:
            self.failures = 0
            self.is_open = False
            logger.info("高德熔断器冷却完成, 恢复请求")
            return True
        return False


_circuit_breaker = CircuitBreaker()


# ══════════════════════════════════════════════
# 核心工具类
# ══════════════════════════════════════════════


class AmapClient:
    """高德地图异步客户端 (单例模式)。"""

    def __init__(self):
        self._key = settings.AMAP_WEB_KEY.get_secret_value()
        self._mock = settings.AMAP_MOCK_ENABLED
        self._timeout = settings.AMAP_TIMEOUT
        self._retry = settings.AMAP_RETRY_TIMES

    # ─── HTTP 请求基础方法 ──────────────────────

    async def _call(self, endpoint: str, params: dict) -> Optional[dict]:
        """通用高德 API 调用, 含重试/熔断/异常处理。"""
        if not _circuit_breaker.check():
            logger.warning(f"高德熔断中, 降级到 Mock: {endpoint}")
            return None

        if not self._key and not self._mock:
            logger.warning("高德 Key 未配置且 Mock 关闭")
            return None

        params["key"] = self._key
        params["output"] = "JSON"

        for attempt in range(self._retry + 1):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        endpoint,
                        params=params,
                        timeout=aiohttp.ClientTimeout(total=self._timeout),
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            if data.get("status") == "1":
                                _circuit_breaker.record_success()
                                return data
                            else:
                                logger.warning(
                                    f"高德返回业务错误: endpoint={endpoint.split('/')[-1]} "
                                    f"info={data.get('info','')}"
                                )
                                _circuit_breaker.record_failure()
                                return None
                        else:
                            logger.warning(f"高德 HTTP {resp.status}: {endpoint}")
                            _circuit_breaker.record_failure()
            except asyncio.TimeoutError:
                logger.warning(f"高德超时 (attempt {attempt+1}): {endpoint}")
                _circuit_breaker.record_failure()
                if attempt < self._retry:
                    await asyncio.sleep(0.5 * (attempt + 1))
            except aiohttp.ClientError as e:
                logger.warning(f"高德网络异常 (attempt {attempt+1}): {e}")
                _circuit_breaker.record_failure()
                if attempt < self._retry:
                    await asyncio.sleep(0.5 * (attempt + 1))

        return None

    # ─── 缓存辅助 ────────────────────────────────

    async def _cache_get(self, key: str) -> Optional[dict]:
        try:
            r = get_redis()
            raw = await r.get(key)
            return json.loads(raw) if raw else None
        except Exception:
            return None

    async def _cache_set(self, key: str, data: dict) -> None:
        try:
            r = get_redis()
            await r.setex(key, settings.AMAP_CACHE_TTL, json.dumps(data, ensure_ascii=False))
        except Exception:
            pass

    def _hash(self, *args: str) -> str:
        raw = ":".join(str(a) for a in args)
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    # ══════════════════════════════════════════
    # 1. 地理编码: 地址 → 坐标
    # ══════════════════════════════════════════

    async def geocode(self, address: str, city: str = "") -> Optional[dict]:
        """
        地址文本 → 经纬度 (GCJ-02)。

        Returns:
            {lng, lat, formatted_address, province, city, district, adcode, poi_name}
            失败/mock返回 None 时业务层自行降级。
        """
        if not address or not address.strip():
            return None

        addr_hash = self._hash(address, city or "")
        cache_key = _CACHE_GEO_KEY.format(hash=addr_hash)
        cached = await self._cache_get(cache_key)
        if cached:
            logger.debug(f"地理编码缓存命中: {address[:20]}...")
            return cached

        if self._mock or not self._key:
            result = self._mock_geocode(address, city)
        else:
            params = {"address": address}
            if city:
                params["city"] = city
            raw = await self._call(_GEOCODE_URL, params)
            result = self._parse_geocode(raw) if raw else None

        if result:
            await self._cache_set(cache_key, result)
        return result

    def _parse_geocode(self, raw: dict) -> Optional[dict]:
        geocodes = raw.get("geocodes", [])
        if not geocodes:
            return None
        g = geocodes[0]
        location = g.get("location", "0,0")
        lng_str, lat_str = location.split(",")
        try:
            lng, lat = float(lng_str), float(lat_str)
        except (ValueError, TypeError):
            return None
        return {
            "lng": lng,
            "lat": lat,
            "formatted_address": g.get("formatted_address", ""),
            "province": g.get("province", ""),
            "city": g.get("city", ""),
            "district": g.get("district", ""),
            "adcode": g.get("adcode", ""),
            "poi_name": g.get("building", {}).get("name", "") if isinstance(g.get("building"), dict) else "",
        }

    def _mock_geocode(self, address: str, city: str = "") -> dict:
        """Mock 地理编码 — 生成合理偏移的坐标（与前端默认定位对齐）。"""
        base_hash = int(hashlib.md5(address.encode()).hexdigest()[:8], 16)
        # 基准与前端 index.html 默认定位 (39.99, 116.47) 对齐
        # 偏移 ±0.005° ≈ 500m，保证 Mock 订单都在 1.5km 雷达半径内
        lat = 39.99 + (base_hash % 1000) / 100000.0
        lng = 116.47 + ((base_hash >> 16) % 1000) / 100000.0
        return {
            "lng": round(lng, 6),
            "lat": round(lat, 6),
            "formatted_address": f"北京市朝阳区{address}",
            "province": "北京市",
            "city": "北京市",
            "district": "朝阳区",
            "adcode": "110105",
            "poi_name": address,
        }

    # ══════════════════════════════════════════
    # 2. 逆地理编码: 坐标 → 地址
    # ══════════════════════════════════════════

    async def regeo(self, lng: float, lat: float) -> Optional[dict]:
        """
        经纬度 → 格式化地址 (GCJ-02)。

        Returns:
            {formatted_address, province, city, district, township, street,
             pois: [{name, address, distance_m}], adcode}
        """
        coord_hash = self._hash(f"{lng:.6f}", f"{lat:.6f}")
        cache_key = _CACHE_REGEO_KEY.format(hash=coord_hash)
        cached = await self._cache_get(cache_key)
        if cached:
            return cached

        if self._mock or not self._key:
            result = self._mock_regeo(lng, lat)
        else:
            params = {"location": f"{lng},{lat}", "extensions": "base"}
            raw = await self._call(_REGEO_URL, params)
            result = self._parse_regeo(raw) if raw else None

        if result:
            await self._cache_set(cache_key, result)
        return result

    def _parse_regeo(self, raw: dict) -> Optional[dict]:
        regeo = raw.get("regeocode", {})
        if not regeo:
            return None
        comp = regeo.get("addressComponent", {})
        pois_raw = regeo.get("pois", []) or []
        pois = [
            {"name": p.get("name", ""), "address": p.get("address", ""),
             "distance_m": int(p.get("distance", 0))}
            for p in pois_raw[:5]
        ]
        return {
            "formatted_address": regeo.get("formatted_address", ""),
            "province": comp.get("province", ""),
            "city": comp.get("city", ""),
            "district": comp.get("district", ""),
            "township": comp.get("township", ""),
            "street": comp.get("streetNumber", {}).get("street", "") if isinstance(comp.get("streetNumber"), dict) else "",
            "adcode": comp.get("adcode", ""),
            "pois": pois,
        }

    def _mock_regeo(self, lng: float, lat: float) -> dict:
        """Mock 逆地理编码 — 基于实际坐标生成地址（覆盖全国多城市）。

        Returns:
            扁平格式, 与 _parse_regeo() 输出一致, 调用方无需额外转换。
        """
        seed = hashlib.md5(f"{lat:.4f},{lng:.4f}".encode()).hexdigest()
        idx = int(seed[:8], 16) % len(MOCK_ADDRESSES)
        addr = MOCK_ADDRESSES[idx]
        return {
            "formatted_address": f"{addr['address']}（模拟）",
            "province": addr.get("province", ""),
            "city": addr.get("city", ""),
            "district": addr.get("district", ""),
            "township": "",
            "street": "",
            "adcode": "",
            "pois": [],
        }

    # ══════════════════════════════════════════
    # 3. POI 关键字搜索
    # ══════════════════════════════════════════

    async def poi_search(
        self,
        keyword: str,
        city: str = "",
        lng: Optional[float] = None,
        lat: Optional[float] = None,
    ) -> list[dict]:
        """
        关键字搜索周边 POI。

        Returns:
            [{name, address, lng, lat, distance_m, adcode, poi_type}]
        """
        if not keyword or not keyword.strip():
            return []

        coord_part = f"{lng:.4f},{lat:.4f}" if lng is not None and lat is not None else ""
        poi_hash = self._hash(keyword, city or "", coord_part)
        cache_key = _CACHE_POI_KEY.format(hash=poi_hash)
        cached = await self._cache_get(cache_key)
        if cached:
            return cached

        if self._mock or not self._key:
            result = self._mock_poi_search(keyword, lng, lat)
        else:
            params = {"keywords": keyword, "offset": 10}
            if city:
                params["city"] = city
            if lng is not None and lat is not None:
                params["location"] = f"{lng},{lat}"
            raw = await self._call(_POI_URL, params)
            result = self._parse_poi(raw) if raw else []

        if result:
            await self._cache_set(cache_key, result)
        return result

    def _parse_poi(self, raw: dict) -> list[dict]:
        pois = raw.get("pois", []) or []
        return [
            {
                "name": p.get("name", ""),
                "address": p.get("address", ""),
                "lng": float(p.get("location", "0,0").split(",")[0] or 0),
                "lat": float(p.get("location", "0,0").split(",")[1] or 0),
                "distance_m": int(p.get("distance", 0)),
                "adcode": p.get("adcode", ""),
                "poi_type": p.get("type", ""),
            }
            for p in pois
        ]

    def _mock_poi_search(self, keyword: str, lng=None, lat=None) -> list[dict]:
        base = [
            {"name": f"{keyword}便利店", "address": "北京市朝阳区望京街道100号", "lng": 116.470 + (hash(keyword) % 100) / 10000, "lat": 39.995 + (hash(keyword) % 100) / 10000, "distance_m": 300, "adcode": "110105", "poi_type": "购物"},
            {"name": f"{keyword}超市", "address": "北京市朝阳区广顺北大街200号", "lng": 116.480, "lat": 40.000, "distance_m": 600, "adcode": "110105", "poi_type": "购物"},
            {"name": f"{keyword}快递站", "address": "北京市朝阳区阜通东大街50号", "lng": 116.475, "lat": 39.990, "distance_m": 400, "adcode": "110105", "poi_type": "生活服务"},
        ]
        return base

    # ══════════════════════════════════════════
    # 4. 批量距离计算 (Haversine 优先)
    # ══════════════════════════════════════════

    async def batch_distance(
        self,
        origin: tuple[float, float],
        destinations: list[tuple[float, float]],
    ) -> list[float]:
        """
        批量计算起点到多个终点的直线距离 (米)。

        优先使用本地 Haversine 公式 (毫秒级)，不使用高德 API。
        """
        o_lng, o_lat = origin
        results = []
        for d_lng, d_lat in destinations:
            results.append(self._haversine(o_lat, o_lng, d_lat, d_lng))
        return results

    def _haversine(self, lat1: float, lng1: float, lat2: float, lng2: float) -> float:
        """Haversine 公式计算两点球面距离 (米)。"""
        r = 6371000.0  # 地球半径(米)
        phi1 = math.radians(lat1)
        phi2 = math.radians(lat2)
        delta_phi = math.radians(lat2 - lat1)
        delta_lambda = math.radians(lng2 - lng1)
        a = math.sin(delta_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
        return round(r * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a)), 1)

    # ══════════════════════════════════════════
    # 5. IP 定位
    # ══════════════════════════════════════════

    async def ip_locate(self, ip: str) -> Optional[dict]:
        """
        IP 粗略定位 — 无 GPS 时的兜底方案。

        Returns:
            {lng, lat, city, province, rectangle, adcode}
        """
        if not ip or ip in ("127.0.0.1", "localhost", "::1"):
            return self._mock_ip_locate()

        cache_key = _CACHE_IP_KEY.format(ip=ip)
        cached = await self._cache_get(cache_key)
        if cached:
            return cached

        if self._mock or not self._key:
            result = self._mock_ip_locate()
        else:
            params = {"ip": ip}
            raw = await self._call(_IP_URL, params)
            result = self._parse_ip(raw) if raw else self._mock_ip_locate()

        if result:
            await self._cache_set(cache_key, result)
        return result

    def _parse_ip(self, raw: dict) -> Optional[dict]:
        province = raw.get("province", "")
        city = raw.get("city", "")
        rect = raw.get("rectangle", "")
        if not rect or rect == "[]":
            return None
        parts = rect.replace("[", "").replace("]", "").split(";")[0].split(",")
        try:
            lng = float(parts[0]) if len(parts) > 0 else 116.40
            lat = float(parts[1]) if len(parts) > 1 else 39.90
        except (ValueError, IndexError):
            return None
        return {
            "lng": lng, "lat": lat, "city": f"{province}{city}",
            "province": province, "adcode": raw.get("adcode", ""),
        }

    def _mock_ip_locate(self) -> dict:
        # 从 MOCK_ADDRESSES 中随机取一个城市，而非写死北京
        import hashlib
        import time as _time
        idx = int(hashlib.md5(str(_time.time()).encode()).hexdigest()[:8], 16) % len(self.MOCK_ADDRESSES)
        addr = self.MOCK_ADDRESSES[idx]
        # 生成该城市的大致经纬度
        base_lat = {"北京市": 39.90, "上海市": 31.23, "广州市": 23.13, "深圳市": 22.54,
                     "杭州市": 30.29, "成都市": 30.57, "武汉市": 30.59, "南京市": 32.06,
                     "重庆市": 29.53, "西安市": 34.26, "长沙市": 28.23, "天津市": 39.13,
                     "苏州市": 31.30, "东莞市": 23.02, "佛山市": 23.02}
        base_lng = {"北京市": 116.41, "上海市": 121.47, "广州市": 113.26, "深圳市": 114.06,
                     "杭州市": 120.16, "成都市": 104.07, "武汉市": 114.30, "南京市": 118.80,
                     "重庆市": 106.55, "西安市": 108.94, "长沙市": 112.94, "天津市": 117.20,
                     "苏州市": 120.62, "东莞市": 113.75, "佛山市": 113.12}
        return {
            "lng": base_lng.get(addr["city"], 116.41),
            "lat": base_lat.get(addr["city"], 39.90),
            "city": addr["city"], "province": addr["province"], "adcode": addr.get("adcode", ""),
        }

    # ══════════════════════════════════════════
    # 6. 坐标合法性校验
    # ══════════════════════════════════════════

    async def check_coord_valid(self, lng: float, lat: float) -> tuple[bool, str]:
        """
        校验坐标合法性。

        规则:
          - 数值合法 (lat -90~90, lng -180~180)
          - 在中国境内 (lat 18~54, lng 73~135)
          - 非原点 (0,0)
          - 非虚假固定坐标 (例如 39.9/116.4 高频出现的虚拟定位)

        Returns:
            (is_valid: bool, reason: str)
        """
        if lat == 0 and lng == 0:
            return False, "定位为原点 (0,0), 请检查GPS权限"
        if not (-90 <= lat <= 90):
            return False, f"纬度非法: {lat}"
        if not (-180 <= lng <= 180):
            return False, f"经度非法: {lng}"

        # 中国境内粗校验
        if not (18 <= lat <= 54):
            return False, "坐标不在中国境内 (纬度超限)"
        if not (73 <= lng <= 135):
            return False, "坐标不在中国境内 (经度超限)"

        # 常见虚拟定位坐标检测
        fake_spots = [
            (39.90923, 116.397428),  # 北京天安门虚拟定位高频坐标
            (31.230416, 121.473701),  # 上海人民广场虚拟定位
            (22.543099, 114.057868),  # 深圳市民中心虚拟定位
        ]
        for f_lat, f_lng in fake_spots:
            if abs(lat - f_lat) < 0.0001 and abs(lng - f_lng) < 0.0001:
                return False, "检测到虚假定位固定坐标, 请使用真实定位"

        return True, ""


# ══════════════════════════════════════════════
# 模块级单例
# ══════════════════════════════════════════════

_amap_client: Optional[AmapClient] = None


def get_amap() -> AmapClient:
    global _amap_client
    if _amap_client is None:
        _amap_client = AmapClient()
    return _amap_client
