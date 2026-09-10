# ===========================================================
# 半仓智拼 (HalfCart) 前端开发占位镜像
# ===========================================================
# 后续将替换为 Vue3 + Vite 完整构建
# 当前阶段: 提供可启动的 Nginx 占位服务
# ===========================================================
FROM nginx:1.25-alpine

# 创建占位首页
RUN echo '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0,maximum-scale=1.0,user-scalable=no"><title>HalfCart 半仓智拼</title><style>body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;background:#f5f5f5}main{text-align:center;padding:2rem}h1{color:#333;font-size:1.5rem}p{color:#999;font-size:.875rem}</style></head><body><main><h1>🛒 HalfCart 半仓智拼</h1><p>前端服务运行中 — 即将上线</p></main></body></html>' > /usr/share/nginx/html/index.html

EXPOSE 80

HEALTHCHECK --interval=15s --timeout=5s --retries=3 \
    CMD wget -q --spider http://localhost:80/ || exit 1
