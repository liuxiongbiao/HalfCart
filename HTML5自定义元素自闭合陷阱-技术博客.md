# HTML5 自定义元素自闭合陷阱：一个让 Vue 表单"消失"的排障实录

> 摘要：注册页面的 5 个输入框只渲染出第 1 个，其余 4 个加上提交按钮全部"消失"。排查过程从 Vue 响应式、CDN 加载、CSS 遮挡一路追到 HTML 解析器的规范行为，最终定位到一行看似无害的 `/>` 自闭合写法。

---

## 1. 症状

用户反馈：半仓智拼的注册页面**只有一个用户名输入框**，手机号、邮箱、密码、确认密码、注册按钮统统不显示。

但登录页面表现正常——它有同样的 Vue 3 + Vant 4 技术栈，同样的 CDN 依赖，同样的项目结构。

打开注册页，DevTools 显示 DOM 中只有一条 `<input>`：

```
inputs found: 1
  [0] placeholder="4-32位，字母开头..."
buttons found: 0
```

5 个字段丢了 4 个，按钮丢了 1 个。不是 CSS 隐藏——是压根没渲染出来。

## 2. 排查过程

### 2.1 第一轮：怀疑响应式与实例化模式

登录页和注册页的 Vue 实例化写法不同：

```js
// 登录页（正常）
var app = Vue.createApp({
  setup: function () { ... }
});
app.use(vant); app.mount('#app');

// 注册页（异常）
const { createApp, ref, reactive, computed } = Vue;
createApp({
  setup() { ... }
}).use(vant).mount('#app');
```

我将注册页统一为登录页的 `Vue.createApp` + `function()` 模式，**问题依旧**。

### 2.2 第二轮：怀疑 ES6 语法

注册页使用了大量箭头函数、`const`/`let` 声明、模板字面量。虽然现代浏览器都支持，但为了排除一切变量，我逐一将所有 `=>` 改为 `function()`，所有 `const`/`let` 改为 `var`。**问题依旧**。

### 2.3 第三轮：怀疑条件渲染指令

密码字段后的 `v-if="form.password"` 和 `v-for="i in 4"` 会不会触发 Vue 编译错误？全部去掉，只保留纯静态模板。**问题依旧**。

### 2.4 第四轮：Playwright 截图确认

用 Playwright 在 headless Chromium 中对注册页截图，确认了症结：

```
inputs found: 1
buttons found: 0
```

Vue 没有崩溃（没有 console error），但它**只渲染了第一个 `<van-field>` 就停止了**。后续所有元素——包括剩余的 4 个 `<van-field>`、密码强度指示区、提交按钮——全部静默消失。

这不再是 JavaScript 逻辑问题，而更像 DOM 结构层面的问题。

### 2.5 第五轮：对比两个页面的 HTML 源码

把登录页和注册页的模板逐行对比：

```html
<!-- 登录页 —— 正常 -->
<van-field v-model="codePhone" name="phone" label="手机号"
  placeholder="请输入手机号" maxlength="11" type="tel"
  :rules="[{required:true,message:'请输入手机号'}]">
  <template #extra>
    <van-button>获取验证码</van-button>
  </template>
</van-field>   <!-- ← 显式闭合标签 -->

<!-- 注册页 —— 异常 -->
<van-field v-model="form.username" label="用户名"
  placeholder="4-32位，字母开头，字母或数字"
  maxlength="32"
  :error="!!errors.username"
  @blur="check('username')"
  @input="errors.username=''" />   <!-- ← 自闭合！ -->
```

**发现了**：登录页用 `<van-field>...</van-field>` 显式闭合，注册页用 `<van-field ... />` 自闭合。

## 3. 根因分析

### 3.1 HTML5 规范的"自闭合"规则

HTML5 规范明确定义了**哪些标签可以是自闭合（void elements）**：

> area, base, br, col, embed, hr, img, input, link, meta, param, source, track, wbr

只有这 15 个。**除此之外的所有标签，自闭合写法 `/>` 是无效的**。

当浏览器解析器遇到：

```html
<van-field v-model="form.username" label="用户名" />
<div class="field-hint">...</div>
<van-field v-model="form.phone" label="手机号" />
```

它实际的理解是：

```html
<van-field v-model="form.username" label="用户名">
  <div class="field-hint">...</div>
  <van-field v-model="form.phone" label="手机号">
    ...
  </van-field>
</van-field>
```

第一个 `<van-field>` **从未闭合**，它把后面的 `<div>`、第二个 `<van-field>`、第三个、第四个……全部吞为自己的子节点。

### 3.2 为什么第一个字段照常渲染？

Vant 4 的 `van-field` 组件期望的内部 DOM 结构是：

```
van-field > van-cell > div.van-cell__value > input
```

当第一个 `<van-field>` 被 Vant 正确实例化后，它的模板渲染出一个 `<input>`。但因为它"吞噬"了后续所有 DOM 节点，Vue 在编译第二个 `<van-field>` 时发现它的位置已经在第一个 `<van-field>` 的内部了——这破坏了 Vant 的组件边界假设。

Vue 3 的模板编译器在这种情况下不会报错（它默默跳过不合预期的子树），但也不会渲染出你期望的结果。

### 3.3 为什么登录页不受影响？

登录页的 `van-field` 全部使用**显式闭合标签**：

```html
<van-field ...>
  <template #extra>...</template>
</van-field>
```

这恰好也是 Vant 官方文档示例的写法——因为 `van-field` 经常需要 `<template #extra>` 插槽，所以自然写成了显式标签。但注册页是我手写的，为了"简洁"使用了 `/>` 自闭合——正是这个"简洁"导致了 Bug。

### 3.4 Vue 的 SFC 编译器 vs 浏览器 HTML 解析器

这里有一个容易被忽视的陷阱：

| 场景 | `<van-field />` 行为 |
|------|---------------------|
| Vue SFC（`.vue` 文件，编译时处理） | 编译器将其转换为正确的 AST，正常工作 |
| 浏览器内模板（`<script>` 中的 DOM 模板） | 浏览器 HTML 解析器先处理，`/>` 被忽略 |

这也是为什么很多开发者在 `.vue` 文件中用 `/>` 没问题，但把同样的模板写在 HTML 文件中就炸了——两道不同的解析路径，行为完全不同。

## 4. 解决方案

将所有 `<van-field ... />` 改为 `<van-field ...></van-field>`：

```html
<!-- ❌ 错误：HTML5 中自定义元素不能自闭合 -->
<van-field v-model="form.phone" label="手机号" />

<!-- ✅ 正确：显式闭合 -->
<van-field v-model="form.phone" label="手机号"></van-field>
```

修复后验证：

```
inputs found: 5
  [0] placeholder="4-32位，字母开头..."
  [1] placeholder="请输入11位手机号"
  [2] placeholder="example@domain.com"
  [3] placeholder="至少8位，包含字母和数字"
  [4] placeholder="请再次输入密码"
buttons found: 1
  BTN: 注册
```

5 个字段 + 注册按钮全部正常渲染。

## 5. 经验总结

### 5.1 规则：浏览器内模板永远不要用 `/>` 闭合自定义元素

如果你的 Vue/React/Angular 模板直接写在 HTML 文件中的 `<script>` 标签或 `<template>` 标签里（即由浏览器 HTML 解析器先在 DOM 中实例化，再交给框架编译），务必遵循这一条：

> **只有 HTML 规范的 15 个 void 元素可以用 `/>` 自闭合，其余所有元素——包括框架组件——必须写显式闭合标签。**

### 5.2 调试技巧

遇到"部分元素不渲染"的问题时，优先检查：

1. **浏览器 DevTools → Elements 面板**：DOM 树是否和预期的模板结构一致？子节点是否被错误嵌套？
2. **右键 → 查看网页源代码**（不是 DevTools 的 Elements 面板）：这是浏览器解析器**修改前**的原始 HTML。对比两者差异可以暴露解析问题。
3. **Playwright/ Puppeteer 截图**：当本地环境难以复现时，headless 浏览器可以精确诊断渲染结果。

### 5.3 脚手架建议

在项目中加入 lint 规则，检查 `.html` 文件中的自定义元素自闭合：

```bash
# 快速检查（grep）
grep -Pn '<(van|el|my|app)-[^>]*\s/>' *.html
```

或者配置 `eslint-plugin-vue` 的 `vue/html-self-closing` 规则，在浏览器内模板场景下设为 `"any"` 以外的值。

---

*排查时间：2026-07-18 · 技术栈：Vue 3.4 + Vant 4 · 浏览器：Chromium (Playwright)*
