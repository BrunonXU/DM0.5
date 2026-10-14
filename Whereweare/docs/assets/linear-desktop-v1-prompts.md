# Linear 方向桌面效果图生成记录

2026-10-13 · 使用内置 imagegen；生成后修正今天日期对齐与空白未来节点。此图为待评审概念，不是运行截图。

最终图：[linear-desktop-v1.png](linear-desktop-v1.png)

## 生成提示词

```text
Use case: ui-mockup.
Create ONE highly polished desktop website screenshot for the personal robotics project tracker "Whereweare". Landscape 1600x1000 composition. This is a new design preview inspired specifically by Linear's real desktop project timeline: exceptionally disciplined alignment, quiet navigation, fine separators, compact precise controls, shared chronological grid, restrained typography. Make this look like a working professional desktop application, not a marketing landing page or generic AI dashboard.

Visual direction: warm near-white ivory #F6F4EF main background, slightly darker warm-gray sidebar, charcoal text, restrained brick-red #B4493E main project, muted desaturated green for teaching and slate-blue for data. Flat surfaces, crisp thin strokes, subtle 4–6px corner radii only on actual controls, no floating white cards, no glows, no gradients, no soft pastel icon tiles, no glass, no giant bubbles, no shadows around every item, no decorative illustrations. All text is carefully typeset Simplified Chinese, neutral modern sans-serif with confident title weight and readable 18–20px equivalent milestone labels. Dates smaller and tabular. Dense enough to feel like a serious desktop tool yet easy to scan.

Layout:
A quiet narrow left sidebar around 176px wide. At top plain wordmark "Whereweare". Under it only three small text navigation entries "现在" (selected with a flat faint highlight), "项目库", "回顾". Near the bottom a subtle text link "Projects ↗". Do not add placeholder menus or irrelevant statistics.
Main workspace has generous but controlled margins. Top small breadcrumb "F1 / π0.5" on left, a quiet outlined "更新进度" control on right. A large dark 36–40px heading "积木分拣". Under it a SINGLE short line "采集已有记录，专项数据待验收". An understated "查看架构 ↗" text action sits on the same header area.
Below, slim view toolbar with "时间线" selected and "架构" as adjacent view; right end "近两周" dropdown, small back/forward chevrons, "今天". No bright call-to-action dominating the page.
The dominant main workspace is a precise full-width timeline, NOT placed inside a big rounded card. Three horizontal parallel rows of equal height, with fixed concise labels on the left: "积木任务", "示教", "数据". Tiny colored marks and typography give identity, no large pictograms. A shared date axis above the rows shows "10.06", "10.09", "10.12", "10.13". The current-date vertical hairline is brick red with the small text "今天" at its top. The today position is around 70% across the dated timeline area. The dated axis ends at today; to the right a distinct undated continuation area, separated subtly and labeled ONCE "接下来 · 未排期". This future area must NOT pretend suggested tasks have calendar dates.
Straight thin rails connect milestone diamonds and circles. Main line red and visually strongest; supports quieter green and slate. Use clear rectangular row geometry like Linear, NO decorative curved bends. Same-day milestones align exactly vertically across rows. Row whitespace and fine separators structure information. Milestone labels sit directly adjacent to their markers, not in repeated floating cards. Current states appear beside today's markers, clearly readable. Suggestions sit along dashed continuations to the right. Only one next action is emphasized, the others may be omitted for clarity.

Exact project content, carefully respecting uncertainty:
Main row "积木任务": at the far left, outside the recent date scale, a small prior-history annotation "上次 · 09.28" and "姿态现场通过"; use a tiny axis break so this historical event is NOT shown at a recent date. Current marker at today: strong label "专项数据待验收", smaller line "训练与分拣尚未确认".
Teaching row "示教": event at 10.12 "START 分条部署"; event at 10.13 "848 组示教已录制", smaller qualifier "质量待审核". These are milestones on one same rail, not duration bars or completion percentages.
Data row "数据": at 10.12 "旧候选 1834 帧", qualifier "回读通过 · 待审核"; at 10.13 "新候选 695 帧", qualifier "48 段 · 待导出验证". Distinguish the two batches, do not merge counts.
The single emphasized next action extends rightward from the data row: small "建议" label and bold short action "验证新候选回读". Keep this within the undated area.
At the very bottom, one very quiet footnote "已暂停：输入延迟测试" and a text action at far right "全部记录 ↗". No need for extra explanatory body copy.

The screenshot must show the full desktop viewport with all three rows and next action visible. Main visual attention goes to current milestone column and the primary project, not sidebar or toolbar. No mobile layouts, no phones, no tablet devices, no browser hardware frame, no perspective, no watermark, no invented achievements, no fake progress percentages, no repeated badges. Use the specified Chinese wording accurately. The result should feel carefully composed and human-designed, with Linear-like precision and the user's warm paper palette.
```

## 定点修正提示词

```text
Edit this Whereweare desktop design preview with exactly two small timeline corrections. Preserve all styling, layout, colors, typography, text, controls, row heights, and project content.
1. The date "10.13" currently sits on its own gridline to the left of the red "今天" line. Remove that misplaced date and its redundant gridline. Place "10.13" immediately beneath the red "今天" label, centered on the same red vertical line and three current circular markers. Thus today and October 13 are exactly one shared x coordinate. Keep 10.06, 10.09, 10.12 and all historical markers unchanged.
2. In the undated future area, the main-task and teaching rows have empty unlabeled diamond endpoints. Remove only these two empty diamond shapes. Their quiet dashed lines may simply fade/end. Preserve the data row's labeled future diamond and the action "建议 / 验证新候选回读" exactly.
Do not add any new elements or explanatory text. This stays one full desktop screenshot, no device frame.
```

