---
type: flowchart
style: whiteboard
palette: whiteboard-yellow-blue
density: sufficient
image_count: 6
language: zh-CN
article: ../../PDF扫描件实现逻辑.md
---

# PDF 扫描件实现逻辑 · 配图大纲

## 配图 1

位置: 第一节“先看全链路”之后

目的: 用一张图建立从扫描件输入到最终可查阅结果的整体心理模型。

视觉内容: PDF 扫描件、Flask、本地临时目录、阿里云 OSS、百度 PaddleOCR-VL、最终 Markdown、可视化 Viewer；主流程为“接收 → 上传 → 识别 → 持久化 → 发布”。

文件名: `01-flowchart-overview.svg`

## 配图 2

位置: 第二节“本地 PDF 如何变成百度可访问的输入”之后

目的: 解释本地文件为什么必须先上传 OSS，以及上传阶段的校验、分片和重试逻辑。

视觉内容: 浏览器选择 PDF、Flask 100 MB 请求上限、`.uploads/` 临时文件、PDF 100 MB 校验、1 MB 分片、3 个线程、最多 3 次重试、public-read 源文件 URL、最终清理临时文件。

文件名: `02-flowchart-upload.svg`

## 配图 3

位置: 第三节“百度异步 OCR 如何运行”之后

目的: 展示 Token、任务提交、task_id、状态轮询和成功/失败分支。

视觉内容: 获取 access_token、提交 PaddleOCR-VL、返回 task_id、每隔 poll_interval 查询、pending/running/processing、success/failed/max_wait；提交阶段最多重试 3 次，查询阶段目前未统一重试。

文件名: `03-flowchart-baidu-async.svg`

## 配图 4

位置: 第四节“临时 Markdown 如何变成持久结果”之后

目的: 解释百度临时资源为何需要转存，以及 Markdown 内图片 URL 的替换过程。

视觉内容: 下载 markdown_url、提取图片 URL、逐张下载、上传 OSS、替换临时链接、上传最终 Markdown、public 与 signed 两种访问模式。

文件名: `04-flowchart-markdown-persist.svg`

## 配图 5

位置: 第五节“原文与 OCR 文字如何联动”之后

目的: 展示 PaddleOCR-VL 坐标数据如何转换成浏览器中的双向高亮 Viewer。

视觉内容: parse_result_url、pages/layouts/span_boxes、坐标归一化、注入 Viewer 模板、PDF.js 渲染、SVG polygon 覆盖、marked + DOMPurify 渲染 Markdown、entry_id 联动高亮。

文件名: `05-flowchart-viewer-link.svg`

## 配图 6

位置: 第六节“任务生命周期、可靠性与边界”之后

目的: 同时说明 Web 任务如何结束，以及当前最值得优先修复的工程风险。

视觉内容: 创建 job_id、后台线程、状态消息、done/error、删除临时文件；P0 输出对象覆盖、P1 内存任务不持久、P1 Office Viewer 不可用、P2 查询缺少重试、P2 requirements 缺 Flask、测试与分片上传不同步。

文件名: `06-flowchart-runtime-risks.svg`
