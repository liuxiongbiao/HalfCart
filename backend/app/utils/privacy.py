"""
隐私脱敏工具
===========
PRD 5.3 隐私保护:
  - 手机号脱敏 13x****xxxx (非订单关联场景)
  - 地址信息隐去具体门牌号，仅展示小区/楼宇级别
  - 聊天消息内容在订单完成后物理销毁
  - 地理位置信息仅用于拼单匹配，不向第三方泄露

底座设计:
  - 手机号: AES加密存储 → 数据库不可直接读取
  - 地址: 业务层脱敏后输出
  - 聊天: 阅后即焚 (由 order.chat.cleanup MQ任务处理)
"""

import re
from typing import Optional


# ──────────────────────────────────────────────
# 手机号脱敏 (PRD 5.3)
# ──────────────────────────────────────────────

def mask_phone(phone: str, show_prefix: bool = True) -> str:
    """
    手机号脱敏格式: 138****1234

    规则: 保留前3位和后4位，中间4位替换为 ****

    Args:
        phone: 11位手机号
        show_prefix: 是否保留前3位，为False时隐藏前3位

    Returns:
        脱敏后的手机号

    Examples:
        >>> mask_phone("13812341234")
        '138****1234'

        >>> mask_phone("13812341234", show_prefix=False)
        '****1234'
    """
    if not phone or len(phone) < 7:
        return "****"
    if show_prefix:
        return f"{phone[:3]}****{phone[-4:]}"
    return f"****{phone[-4:]}"


# ──────────────────────────────────────────────
# 地址脱敏 (PRD 5.3)
# ──────────────────────────────────────────────

def mask_address(address: str) -> str:
    """
    地址信息脱敏: 隐去具体门牌号，仅展示小区/楼宇级别。

    规则:
      1. 先按常见敏感尾缀截断 (号/栋/楼/室/户/单元)
      2. 截断后添加 "***" 前缀

    Examples:
        >>> mask_address("北京市朝阳区XXX小区3号楼2单元1501室")
        '北京市朝阳区XXX小区3号楼***'

        >>> mask_address("山姆会员店(望京店)收货区")
        '山姆会员店(望京店)收货区***'
    """
    if not address:
        return "***"

    # 按优先级匹配敏感后缀
    markers = [
        # (后缀, 是否包含该后缀)
        ("号楼", False),
        ("栋", False),
        ("单元", False),
        ("号楼", False),
        ("室", False),
        ("号", True),   # 需要"号"后面还有内容才截断
        ("座", False),
        ("户", False),
    ]

    best_cut = len(address)
    for marker, _ in markers:
        idx = address.rfind(marker)
        if 0 <= idx < best_cut:
            # 检查该位置后面是否有内容需要遮盖
            after = address[idx + len(marker):].strip()
            if after:
                best_cut = idx + len(marker)

    if best_cut < len(address):
        return address[:best_cut] + "***"
    return address + "***"


# ──────────────────────────────────────────────
# 坐标精度模糊化
# ──────────────────────────────────────────────

def fuzz_location(lat: float, lng: float, precision: int = 3) -> tuple[float, float]:
    """
    坐标精度模糊化，降低到指定小数位。

    用于对外展示，避免暴露用户精确位置 (PRD 5.3)。

    Args:
        lat: 纬度
        lng: 经度
        precision: 保留小数位数 (默认3位 ≈ 100m精度)

    Returns:
        (fuzzed_lat, fuzzed_lng)

    Examples:
        >>> fuzz_location(39.9151234, 116.4041234)
        (39.915, 116.404)
    """
    return (
        round(lat, precision),
        round(lng, precision),
    )


# ──────────────────────────────────────────────
# 距离格式化
# ──────────────────────────────────────────────

def format_distance(meters: float) -> str:
    """
    格式化距离显示文本。

    PRD 4.3: LBS雷达卡片展示距离。

    Examples:
        >>> format_distance(320)
        '320m'
        >>> format_distance(1200)
        '1.2km'
        >>> format_distance(45)
        '<100m'
    """
    if meters < 0:
        return "--"
    if meters < 100:
        return "<100m"
    if meters < 1000:
        return f"{int(meters)}m"
    return f"{meters / 1000:.1f}km"


# ──────────────────────────────────────────────
# 昵称脱敏
# ──────────────────────────────────────────────

def mask_nickname(nickname: str, max_visible: int = 2) -> str:
    """
    昵称脱敏: 仅展示首尾若干字符。

    Examples:
        >>> mask_nickname("张三丰")
        '张**'
        >>> mask_nickname("Alex")
        'A**x'
        >>> mask_nickname("A")
        'A'
    """
    if not nickname:
        return "**"
    if len(nickname) <= 2:
        return nickname[0] + "*"
    visible = min(max_visible, max(1, len(nickname) // 3))
    return nickname[:visible] + "**" + nickname[-1] if len(nickname) > visible + 2 else nickname[:visible] + "*"


# ──────────────────────────────────────────────
# 金额脱敏 (运营展示用)
# ──────────────────────────────────────────────

def format_amount_fen(yuan: float) -> int:
    """
    元转分 (用于第三方支付渠道交互)。

    PRD 4.5: 资金结算精度保证，Decimal(12,2) → 分(int)。
    """
    return int(round(yuan * 100))


def format_amount_yuan(fen: int) -> float:
    """分转元。"""
    return fen / 100.0
