# 前端动效接入与边界

本模块完整使用已安装的 GSAP 和 `@gsap/react`；以 `useGSAP` 作用域、`gsap.matchMedia`、显式元素引用及 cleanup 管理生命周期。没有改动编辑器、详情、分页/分段逻辑或已有 CSS。

## 主 agent 接入

在 `main.jsx` 的现有样式导入后导入 `./motion.css`。此文件由主 agent 修改；动效 agent 没有修改入口。

现有 `Motion` 与 `Modal` 调用可保持原样。`Motion` 从 `ui.tsx` 继续导出，按现有 class 自动选择页面、概览、批量操作条方案。

| 目标         | 可见反馈                                                                                 |
| ------------ | ---------------------------------------------------------------------------------------- |
| 页面         | 0.36 秒渐入与 12px 上移；标题/页签轻错峰；新载入表格首屏至多 6 行，错峰总时长 0.40 秒    |
| 文档概览切换 | 0.34 秒淡入；至多 6 个直接子元素 12px 横向进入，总时长 0.38 秒                           |
| 批量操作条   | 0.32 秒上移淡入、0.97→1 缩放；卸载时 0.28 秒不可交互退出影像                             |
| Modal        | 0.36 秒进入；0.30 秒退出完成后调用真实关闭；背景遮罩使用新增 motion.css 的同步透明度动画 |
| Toast        | 0.32 秒出现，消息文本更新时 0.28 秒透明度反馈，卸载时 0.28 秒退出影像                    |

批量操作条和 Toast 的父层条件卸载无法由子组件延迟，因此退出影像仅保留画面；真实按钮和业务状态立即移除。影像设置 inert、aria-hidden，移除 ID/朗读属性，不能点击或聚焦，正常 0.28 秒删除，兜底 600ms 删除。减少动效偏好及 pagehide 会清除影像。

## 连续文稿

**不扫描、不动画全部段落；不创建 100 段错峰，也不添加分页。** 页面观察器只看表格元素插入，不观察文字/属性变化。连续文稿可用主 agent 自己的轻量 DOM、content-visibility 及单编辑器方案。

按需从 `./ui` 或 `./motion` 导入：

```tsx
<MotionItem
  active={isNewlyInserted || isNewlySelected}
  identity={blockId}
  className="your-existing-paragraph-class"
>
  {paragraph}
</MotionItem>
```

`active` 默认 false：不创建 GSAP 动画。只对新插入或选中的一个段落设 true，不要把 100 段全部设 true。`identity` 必须是稳定内容块 ID 或独立插入事件标识，不使用正文或每次输入递增的计数；后续输入不重启过渡。

## Modal 的关闭契约

- 关闭图标、Esc、遮罩、普通 footer 的直接 `onClick={close}` 及 `FormModal` 的取消/成功保存都会等待退出，再调用父层 close。
- 表单提交立即执行 API，不等待入场/退出动画；只有成功后的关闭等待退出。
- busy 拒绝关闭，重复 Esc/重复关闭合并为一次回调。退出阶段设 inert，防止二次提交。
- 父层 close 如被未保存确认阻止，下一任务恢复显示、交互和原焦点，不保留透明遮罩。
- 减少動效偏好初始启用时立即关闭；动画途中启用也立即完成关闭。
- **父层强制卸载、切换资源 key、外部直接 setState 关闭，或复合回调内部自行 close 后导航，无法由子组件延迟。** 这类路径会立即清理动画、关闭原生 dialog、恢复可用原焦点。没有拦截 React 卸载或延迟业务数据变更。
- 可选 render-child 接口让新组件显式使用关闭控制，而不操作调用者现有流程：

```tsx
<Modal title="操作" close={close}>
  {({ requestClose, complete }) => (
    <div>
      <button onClick={requestClose}>取消</button>
      <button
        onClick={async () => {
          await save();
          complete();
        }}
      >
        保存并关闭
      </button>
    </div>
  )}
</Modal>
```

`complete()` 专供已成功提交后的关闭，允许越过该次保存仍在回落的 busy 状态。

## 验证

```sh
npm run typecheck
npm exec --yes --package=jsdom -- node --test src/motion.test.mjs
```

独立测试使用真实 React / GSAP 和 JSDOM。dialog 的原生 showModal/close、布局测量采用明确的测试替身：验证生命周期、作用域、目标数量、回调时机、焦点调用与清理，不代表真实浏览器视觉/原生焦点圈验收。

浏览器验收由主 agent 进行，本动效任务不访问任何浏览器。
