# Whereweare 第二版视觉生成记录

使用内置 imagegen 生成概念视觉。图片用于评审，不是运行截图；文字和数据边界以方案正文为准。

桌面精修：移除生成的机器人缩略图和无依据的日期节点，将副标题改为“示教已有记录，建议先审核这批数据。”；保留三条轨道与整体构图。

## 手机稿

```text
Use case: ui-mockup.
Create a HIGH-FIDELITY MOBILE WEB APP screen adapting the attached Whereweare desktop design. The attached image is a style and content reference, not a screen to crop or squeeze. Portrait aspect ratio about 390:844. Full flat mobile UI screenshot, no device bezel, no phone photographed, no presentation frame.
Maintain the same warm-white palette, subtle warm-gray background, white rounded timeline canvas, calm Apple-like app typography, original branching-dot brand mark, pale coral/red main project, sage teaching, blue data, tactile pill navigation and extremely restrained shadows. Make the mobile design feel native and carefully composed, not a shrunken desktop.
Top: tiny branching logo + "Whereweare" left, simple refresh icon right with accessible visible text "更新". Under that, a beautiful large title "积木分拣", with a small adjacent warm coral badge "F1". One short sentence "示教已有记录，数据待审核。" No row of stage chips, no backend information.
A compact tasteful segmented control "近期" "现在" "接下来"; "现在" selected. Three parallel horizontal rails inside one rounded white panel. Only the CURRENT region of the timeline is visible on phone; don't show historical dates or squish history into this screen. Leftmost short lane names with small tinted icons: "积木", "示教", "数据". At right of each lane, connected physically to its rail, a compact current node:
- coral main: "专项数据待验收" and small badge "待审核".
- sage: "新示教已录制" and "848 组 · 待质量审核".
- blue: "新候选已筛选" and "695 帧 / 48 段 · 待审核".
These are three rows with perfect padding and consistent alignment, clearly connected thin colored horizontal tracks behind them. One subtle shared vertical now line through the aligned node group. Preserve a strong focal point on the coral current node. All three current cards need to fit naturally without tiny text. Metrics are secondary text, never oversized.
From the BLUE data node, let a short curved dotted continuation lead into ONE compact next suggestion chip inside the same canvas below: "建议" small blue badge + "审核这批数据" + circular arrow. It is attached to the data rail, not a separate huge bottom CTA. Do not add more buttons, no giant black button.
Below canvas, quiet "已暂停 · 输入延迟测试" with a disclosure chevron if enough room.
At the bottom a softly floating, lightly translucent white tab bar with simple outline icons and text "现在", "项目库", "回顾". 现在 active, warm coral accent. Wide touch targets, readable Chinese, precise optical alignment, beautiful restrained materials and comfortable spacing. No iPhone status bar, no fake camera imagery, no long paragraphs, no chart grid, no decorations or giant empty areas. Do not print draft labels. Only use given Chinese text; no invented progress/claims.
```

## 桌面稿

```text
Use case: ui-mockup.
Create a HIGH-FIDELITY DESKTOP WEB APP screen for "Whereweare", a personal project memory app for robotics research. The user rejected a primitive SVG chart of straight thin lines and dozens of scattered text labels. This must be a substantially more beautiful, sophisticated and SIMPLE product design, with the visual craft of Apple's iPad apps scaled to a desktop, extremely precise typography, quiet confident spacing, friendly organic roundness, warm-white surfaces, subtle realistic elevation. This is a full flat UI screenshot, not a photo, not a laptop mockup, no device frame or perspective. Landscape 16:10.

STYLE: warm white background #F6F5F1, white main workspace #FFFFFF, deep charcoal type. Small coral red accent #D9574A for the main project, subdued sage for teaching and misty blue for data. Crisp SF-like sans with properly rendered simplified Chinese. Very clean and legible. No large decorative artwork, no overdone glass, no glow, no gradients behind text, no rainbow, no sidebar. The timeline must be the unmistakable central focal point, not a conventional dashboard of metrics.

COMPOSITION:
1) Slim spacious top nav: a small original minimal branching-line logo and "Whereweare" left. A floating segmented navigation in the center with "现在", "项目库", "回顾"; 现在 selected on white. Quiet circular refresh icon with label "更新" on the right. No dark filled giant call-to-action at top.
2) A generous but compact project heading at upper left, title "F1 · 积木分拣" in large beautiful semibold typography. Small tasteful coral pill "主线" adjacent. One short subline "示教已有记录，下一步是数据审核。" A quiet icon+text button "查看全貌" at the right of this heading. No row of unexplained phase symbols.
3) Below: one generous white rounded rectangular canvas, about 70% of available height, with soft hairline border and very restrained shadow. Inside top left "项目轨迹" small semibold. Small range selector "近两周" with chevron and forward/back controls at top right. The content is THREE clearly parallel horizontal journey rails with gentle round endpoints and a few smooth meaningful curves, thick enough to feel designed (3px). Left short labels "积木任务", "示教", "数据", each with a small beautifully designed outline icon in a pale tinted rounded square. Shared tiny date axis at top: "10.10", "10.12", "10.13". A single vertical pale NOW strip around 64% of the timeline with floating pill "现在". Dates align vertically across the three rails. No dense grid, no giant axes.
4) IMPORTANT: The rail nodes are tactile, beautifully composed small information objects attached to the rail, not long paragraphs scattered above and below. At most ONE past chip + ONE current card per lane. Current cards form one deliberate aligned cluster around the NOW strip. Prior history is light simple tick marks with small readable labels.
   Red top rail: historical chip "姿态已验证" with a subordinate tiny "09.28" outside the recent-date axis as last known result. Current warm pale coral card, largest emphasis: "专项数据待验收" with small badge "待审核". Do not imply training is finished.
   Sage middle rail: small history chip "START 分条"; current white card "新示教已录制", one subordinate line "848 组 · 待质量审核". Do not make 848 a dashboard metric. Use a small record/check icon and a minimal three-frame outline illustration ONLY INSIDE this small card, integrated rather than visual clutter.
   Misty-blue bottom rail: history chip "旧候选" with tiny secondary "1834 帧"; current white card "新候选已筛选", subordinate "695 帧 / 48 段 · 待审核".
5) Rails continue to the right as thin dotted suggestion paths. ONE visually appealing next-action node in the right region connected to the data rail: small pill "建议" then "审核这批数据" and a clear small circular arrow. This should feel like a destination, easy to click. Other future rails end as quiet dots, not three paragraphs of repetitive next steps. There is no date over the undated suggestion. A quiet tiny label "接下来" above this right region.
6) A very quiet footnote row below the canvas, "已暂停  输入延迟测试" left, "查看全部记录  ›" right. No model status, hashes, machine IDs, percentage gauges, pending packets, paragraphs or technical audit notes.

DESIGN QUALITY: exceptionally balanced composition with wide breathing room around important objects, strong hierarchy, consistent padding, smooth restrained corners (canvas 28px, small nodes 16px), clean beautiful Chinese typography. The main coral card and now marker should create one clear optical center. Titles should be visually much larger than secondary metadata. Preserve enough empty area to breathe without meaningless giant empty rails. No fake data, no new accomplishment claims; it is explicitly a concept screen. Do not print labels "mockup", "draft", "设计稿" on the UI itself. No Apple logo. All text must be accurately readable, no gibberish.
```
