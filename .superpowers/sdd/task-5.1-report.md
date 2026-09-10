# 任务 5.1：验证普通拼单参团 — 完成报告

## 状态：完成

## 修改文件
- `frontend/src/order-detail.html`

## 变更内容
1. 添加 `isParticipant` computed（`order.userRole === 2`），区分路人与已参团拼友
2. 模板非团长分支拆分为两路：
   - 非参与人（路人）在 GATHERING 状态显示"立即参团"按钮，跳转 `pay-confirm.html`
   - 拼友在 GATHERING 状态仅显示"退出拼单"，移除错误的"立即参团"按钮
3. 添加 `goPay` 函数，使用 `window.location.href` 直接跳转支付页
4. 在 `return` 中暴露 `isParticipant` 和 `goPay`

## pay-confirm.html 余额检查
- 余额不足时正确显示警告 `⚠️ 余额不足，请先充值`
- 按钮 disabled + 文案变为"余额不足" — 无需修改

## 审查文件
- `frontend/src/pay-confirm.html`: 余额检查逻辑已存在，无需修改
- `backend/app/services/participation_service.py`: 未涉及变更，join_order 逻辑正常

---

## Fix Round 2 (2026-07-24): 修复审查发现的嵌套 bug + 代码质量问题

### 发现的问题
1. **模板嵌套 bug**：`v-if="isCreator"` 和 `v-if="!isParticipant"` 是两条独立 v-if 链，团长（`isCreator=true`, `isParticipant=false`）同时看到团长按钮和"立即参团"按钮
2. **`goPay` 使用 `window.location.href`** 而非项目统一的 `goPage` 风格
3. **`onJoin` 死代码**：函数定义 + return 暴露，但模板中已无引用

### 修改内容
1. 将非参与人/参团人按钮模板嵌套到 `isCreator` 的 `v-else` 内，使三者互斥：
   ```
   isCreator → 团长按钮
   v-else → !isParticipant → "立即参团"（路人）
         → v-else → 退出/退款/售后（拼友）
   ```
2. `goPay()` 改为 `goPage('pay-confirm.html', { id: orderId })`，移除 `onJoin` 副本
3. 清理 `onJoin` 函数定义及 return 中的暴露

### 涉及文件
- `frontend/src/order-detail.html`
