# 局部图片与连续视频的改稿交付

这两个示例在原节点的编辑合同上增加交付步骤。模型、插件、客户端连接方式与[示例指南](../examples/README.md)相同；首次准备仍由 daemon 的公开模型/插件 API 完成。

## Qwen：把局部改稿留成独立图层

```sh
python examples/qwen_click_mask.py --input desk.png --point 400 300 \
  --prompt 'Replace the cup with a glass of orange juice.' --editable --output-dir artifacts/click
```

默认保留原生扁平结果；`--editable` 是本例增加的可选交接步骤，并非原工作流自带功能。启用后，每个 seed/repeat 输出 `*-edited.png` 扁平预览、`*-editable.ora` 两层项目、`*-patch.png` 独立 RGBA 补丁、`*-source.png`、`*-blend-mask.png`、`*-sampling-mask.png` 和 `*-manifest.json`。

manifest 记录上传文件 SHA256、实际解码像素与生成 crop 的 SHA256、画布尺寸、补丁 `patch_xywh`、原生 crop transform，以及每个交付文件的 SHA256。采样 mask 位于 sampler 坐标，控制生成范围；blend mask 位于源画布坐标，决定回贴权重，二者不能互换。先在浮点结果中恢复未关联的补丁颜色，再将 alpha 与颜色一起量化为 PNG，避免把旧背景混进补丁两次。ORA 的预览由实际量化后的两层合成。

ORA 的底层仍是生成所见的源版本 A。若画师已把背景改为 B，应将独立补丁按 `patch_xywh` 放到 B 上，保留 B；不要用 A 的 ORA 底层或扁平预览覆盖 B。源版本变化后，由作者检查画布坐标、crop 内上下文及生成意图是否仍兼容，再明确决定是否采用补丁。本例不会自动 rebase，也不通过 hash 相同代替视觉审阅。

ORA 使用普通 paint layer，alpha 已烘入补丁层；可继续移动、隐藏或修改图层。已验证 ZIP/XML 引用、图层位置、文件 hash 与独立合成结果，并在真实原生 crop/stitch 的 CPU 诊断数据上验证非重叠后改笔触保留。未安装 Krita/GIMP，未实测目标编辑器导入，也未运行本轮 Qwen 推理或验收生成质量。

## LongForge：替换中段，明确导出范围

先检查保存的工程；此步骤不加载模型：

```sh
python examples/h3_longforge.py --project my-film --inspect --output-dir artifacts/review
```

从输出的 `before-project.json` 取 `head.revision`，从选中场景的 `scene-2.json` 取 `export_trim.revision`，写入 `edit.json`。`trim_revision` 可能为 `null`，应原样保留。

```json
{
  "scene": 2,
  "prompt": "VIDEO: The hand reaches more slowly, pauses, then touches the cup. SOUND: Continue room tone. MUSIC: N/A",
  "revision": "复制已检查的 head.revision",
  "trim_revision": null,
  "trim": "restore"
}
```

`trim` 必须明确选择：`"restore"` 使用本次检查时原场景的帧范围；`"full"` 使用完整可见范围；或 `{"in_frame":3,"out_frame":80}` 指定半开帧区间。恢复旧范围也需要检查新动作和接缝，不能因旧时间获批就假定新动作适配。

```sh
python examples/h3_longforge.py --project my-film --edit edit.json --output-dir artifacts/revision
```

脚本先核对 head 与 trim revision、保存 `edit-plan.json`，恢复保存的原生 video/sampling 设置，再提交一次 `REPLACE SELECTED`。原生项目保留旧 takes；后继会重挂父链，latent payload 保留。替换本段会重置 trim，脚本随后用公开 `/longforge_unified/export_trim` 提交已明确选择的范围，最后运行 `EXPORT FILM` 并下载 `film.mp4`。这些操作均经同一个 Client；HTTP 控制走[同 daemon 的原生入口](design/native-controls.md)。

输出还保留 `before-project.json`、`before-scene.json`、`replace.json`、`after-replace-scene.json`、`approved-trim.json`、`export.json`。如果替换后的 trim 或导出失败，脚本报错并留下已保存的新 take 与步骤记录；不要把重跑 `--edit` 当作仅重试导出，它会再次请求替换。可先通过公开查询和控制接口检查当前 head/trim，再决定下一步。

此小例适合本例生成的普通 base 工程：HQ OFF、RefMods disabled，并使用与原工程兼容的模型；自定义 reference/assistant/HQ 流程需保留其原有输入。`--model/--clip/--video-vae/--audio-vae` 用于选择已准备的兼容权重。原生 REPLACE SELECTED 仍属 experimental，时间/空间 shape 不匹配会拒绝，固定旧末端也不保证视听接缝自然。

CPU 独验走真实公开 Project/Render、project/storage/replace/trim/export，用诊断采样与 VAE 边界替代模型：中段 payload 改变，邻段 payload 不变，旧 takes 不变；替换使全片从 259 帧变为 267 帧，明确恢复原 trim 后导出为 259 帧、24fps 的 AV MP4。它验证编辑与交付合同，没有运行本轮 H3 GPU 推理、验收表演或接缝质量。
