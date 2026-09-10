# Dify OCR 集成排坑指南——从"控制台正常、API 挂掉"到全链路打通

> 摘要：记录半仓智拼项目集成 Dify OCR 工作流的完整排坑过程。困扰两天的问题最终定位到 4 个毫不起眼的配置/格式细节。本文按排查方法论展开，每个坑附诊断日志、根因分析、修复代码。

---

## 背景

半仓智拼是一个 C2B 本地拼单平台。我们将"小票 OCR 识别→现货转让"功能对接到 Dify 多模态工作流。用户在 H5 页面拍摄购物小票，后端调用 Dify 工作流识别商品名/价格/数量，前端渲染商品列表供用户选择转让。

**核心矛盾**：完全相同的小票图片、完全相同的工作流，**Dify 控制台调试一切正常，集成到项目中后持续失败**——返回空、报错、超时轮番出现。

---

## 问题全景

| 序号 | 现象 | Dify 返回 |
|:--|:--|:--|
| 1 | "405 Method Not Allowed" | 路由未注册 |
| 2 | "user_input_text is required" | 跑到了错误的工作流 |
| 3 | "receipt_image is required in input form" | 输入变量名不匹配 |
| 4 | "File validation failed" | 文件引用格式不完整 |
| 5 | "unsupported_media_type" (415) | 误用 multipart |
| 6 | HTTP 200 但识别结果为空 | 前端字段名被驼峰转换 |

每一个错误对应一个坑。下面按排查顺序逐个展开。

---

## 坑 1：Docker 代码未更新——405 Method Not Allowed

### 现象
```
POST /api/v1/dify/ocr-parser → 405 Method Not Allowed
```

### 诊断
后端代码打包在 Docker 镜像里，不是 volume 挂载。`docker compose restart` 只重启容器，**不会重新构建镜像**。

### 修复
```bash
docker compose up -d --build backend consumer
```

### 教训
**每次改后端 Python 代码都必须 rebuild**。前端 HTML 是静态文件，通过 Nginx 的 volume 挂载可以直接生效。

---

## 坑 2：API Key 错配——"user_input_text is required"

### 现象
```json
{"code": "invalid_param", "message": "user_input_text is required in input form", "status": 400}
```

### 诊断
Dify 报了 `user_input_text` 缺失。但 OCR 工作流的输入变量是 `receipt_image`，不是 `user_input_text`。`user_input_text` 是**文本解析工作流**的输入变量。

`get_dify_client()` 使用了通用的 `DIFY_API_KEY`，这个 Key 指向的是文本解析 App，不是 OCR App。

### 根因
我们有一个通用的 `DIFY_API_KEY` 和一个 OCR 专属的 `DIFY_OCR_API_KEY`。调用不同工作流必须用对应的 API Key。

### 修复
```python
# dify_service.py — 错误写法
client = get_dify_client()  # 用通用 Key

# 正确写法
ocr_api_key = settings.DIFY_OCR_API_KEY  # 用 OCR 专属 Key
client = DifyClient(api_key=ocr_api_key)
```

DifyClient 增加了 `__init__(api_key=...)` 参数支持：
```python
def __init__(self, api_key: str = "") -> None:
    key = api_key or settings.DIFY_API_KEY.get_secret_value()
    self._api_key = key
```

### 教训
**Dify 的 API Key 按 App 隔离**，每个 App 有独立的 Key。OCR App 和 Text Parser App 是不同的 App，需要各自的 Key。

---

## 坑 3：输入变量名不匹配——Dify 告诉你答案

### 现象
```json
{"message": "receipt_image is required in input form"}
```

### 诊断
我们在 `.env` 里设的默认值是 `DIFY_OCR_INPUT_VAR=image_url`，但工作流的实际输入变量名叫 `receipt_image`。

### 根因
不要凭猜测设置变量名。**Dify 的报错信息直接告诉了正确答案**：它说 `receipt_image is required`，意味着输入变量名就是 `receipt_image`。

### 修复
```ini
# .env — 改前
DIFY_OCR_INPUT_VAR=image_url

# 改后
DIFY_OCR_INPUT_VAR=receipt_image
```

同步修改 `config.py` 和 `docker-compose.yml` 的默认值。

### 教训
**Dify 的 400 错误会明确告诉你缺了什么参数**。仔细读错误信息比反复试错更高效。

---

## 坑 4：API 路径双重 `/v1`——拼 URL 的低级错误

### 现象
```
POST https://api.dify.ai/v1/files/upload → 405 / 404
```

### 诊断
`DIFY_API_BASE=https://api.dify.ai/v1`，代码里又拼接了 `/v1/files/upload`，最终变成 `https://api.dify.ai/v1/v1/files/upload`。

### 根因
```python
# 常量定义
_WORKFLOW_RUN_PATH = "/v1/workflows/run"  # ❌

# 拼接
url = f"{self._api_base}{_WORKFLOW_RUN_PATH}"
# → https://api.dify.ai/v1/v1/workflows/run
```

因为 `DIFY_API_BASE` 已经以 `/v1` 结尾。

### 修复
```python
_WORKFLOW_RUN_PATH = "/workflows/run"  # ✅
# → https://api.dify.ai/v1/workflows/run
```

同理修复 `BaseDifyWorkflow._call_dify()` 中的同款问题，以及文件上传路径：
```python
# dify_client.py
url = f"{self._api_base}/files/upload"  # ✅ (修复后)

# dify_base.py
url = f"{self.api_base}/workflows/run"  # ✅ (修复后)
```

### 教训
**凡是拼接 URL 的地方都要检查是否有重复路径段**。让 `DIFY_API_BASE` 以 `/v1` 结尾是最常见的配置方式，内部路径就不要再加 `/v1` 前缀了。

---

## 坑 5：文件引用格式——Workflow API ≠ Chat API

### 现象
```json
// 单对象
"File validation failed for file: receipt.jpeg"

// 数组
"receipt_image in input form must be a file"

// multipart
"unsupported_media_type" (415)
```

### 诊断
Dify 控制台测试工作流时，文件是通过 Web UI 上传的，Dify 前端处理了文件上传逻辑。我们通过 API 调用时，需要先上传文件再引用。

正确流程：
1. `POST /files/upload`（multipart）→ 获取 `file_id`
2. `POST /workflows/run`（JSON）→ `inputs` 中引用 `file_id`

经过多轮试验，Dify Workflow API 接受的文件引用格式为**单对象（非数组）**，放在 `inputs[变量名]` 中：

### 修复
```python
# ① 上传文件
upload_result = await client.upload_file(
    file_content=raw_bytes,
    filename="receipt.jpg",
    mime_type="image/jpeg",
)
file_id = upload_result.get("id")

# ② 在 inputs 中引用
workflow_inputs[input_key] = {
    "transfer_method": "local_file",
    "upload_file_id": file_id,
    "type": "image",          # ← 必须有！
}
```

注意 `"type": "image"` 字段是关键——没有它 Dify 会报 "File validation failed"。

### 教训
**Dify Chat API 和 Workflow API 的传参格式不同**。Chat API 的 `files` 放在请求体顶层，Workflow API 放在 `inputs` 里。不要混用两套 API 的格式。

---

## 坑 6（最隐蔽）：`convertKeys` 驼峰转换——Dify 返回了数据，前端却显示空

### 现象
后端日志：`解析后商品数: 9`（Dify 正确识别了 9 件商品）
前端页面：`未识别到商品`

### 诊断
打开前端 Console，`[OCR DEBUG]` 显示 Dify 完整返回了 9 件商品。后端 API 返回 `items` 数组也是对的。

问题出在 `common.js` 的一个函数：

```javascript
// common.js
window.convertKeys = function (obj) {
    // 递归把 snake_case → camelCase
    function toCamel(s) { return s.replace(/_([a-z])/g, function (_, c) { return c.toUpperCase(); }); }
    ...
};
```

所有 API 响应通过 `convertKeys` 后：
- `item_name` → `itemName`
- `unit_price` → `unitPrice`
- `total_price` → `totalPrice`
- `_debug` → `Debug`

但前端代码还在用旧字段名取：
```javascript
var name = raw.item_name || '';  // undefined!
```

然后 `_isValidProduct` 过滤函数检查 `name` 为空 → 全部过滤 → 0 件商品。

### 修复
前端取字段时同时兼容两种命名：
```javascript
var name = (raw.itemName || raw.item_name || '').trim();
var price = raw.totalPrice || raw.total_price || 0;
```

### 教训
**前后端字段命名不一致是集成中最高频的隐蔽 Bug**。排查步骤：打印 API 实际返回的数据结构 `console.log(JSON.stringify(res.data))`，对比前端代码里取字段的方式。

---

## 完整数据流（修复后）

```
前端 H5
  │ 用户拍照 → FileReader → base64 data URL
  │ POST /api/v1/dify/ocr-parser { image_url: "data:image/jpeg;base64,..." }
  ▼
后端 FastAPI (dify_api.py)
  │ 调用 parse_receipt_ocr(image_url)
  ▼
Dify Service (dify_service.py)
  │ ① base64 解码 → bytes
  │ ② POST Dify /files/upload (multipart) → file_id
  │ ③ POST Dify /workflows/run (JSON) → inputs[receipt_image]={transfer_method:local_file, upload_file_id, type:image}
  │ ④ 解析 outputs.parsed_result (JSON字符串 → 数组)
  │ ⑤ 返回 OCROutput { items: [...], debugInfo: {...} }
  ▼
响应 → common.js convertKeys → camelCase
  │ items: [{itemName, unitPrice, totalPrice, quantity}, ...]
  ▼
前端 publish.html
  │ ① _isValidProduct() 过滤无效条目
  │ ② 渲染商品列表（自定义勾选圆 + 整行可点）
  │ ③ 选中 → 编辑表单联动（单一数据源 spotState.ocrItems）
  │ ④ 提交 → 逐件创建现货订单
```

---

## 排查方法论总结

面对"某平台控制台正常、API 集成失败"这类问题时：

1. **不要凭猜测改代码**。每个节点加日志，用数据驱动排查。
2. **仔细读 Dify 的错误信息**。`receipt_image is required` 直接告诉你变量名，`user_input_text is required` 告诉你跑错了工作流。
3. **检查所有拼 URL 的地方**——双重前缀是最常见的低级错误。
4. **不要混用 Chat API 和 Workflow API 的文档**。两套 API 参数格式不同。
5. **打印 API 实际返回的数据结构**。字段名被中间件转换后，前端取到的和你以为的完全不同。
6. **每次改后端代码都要 rebuild Docker 镜像**。`restart` 不会更新代码。

---

> 博客日期：2026-07-18 | 项目：半仓智拼 (HalfCart) | 技术栈：FastAPI + Dify Workflow API + Vue 3
