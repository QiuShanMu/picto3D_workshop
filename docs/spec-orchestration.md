# 编排 Spec

对齐日期：2026-09-02。配合 [requirements.md](./requirements.md)、[spec-architecture.md](./spec-architecture.md)。

## 1. SKU 清单字段

与作业规范 2.1 一致，软件再加运行时字段。

| 字段 | 必填 | 说明 |
|---|---|---|
| `sku_id` | 是 | 如 `APP-0812-001` |
| `name` | 是 | 品类+型号+规格 |
| `category` | 是 | 归档一级目录 |
| `material` | 是 | 影响是否去背景/布光备注，不进混元 |
| `size_mm` | 是 | `{l,w,h}`，校验 bbox |
| `color` | 是 | 人工检查用 |
| `batch_id` | 是 | 拍摄批次 |
| `notes` | 否 | 透明/反光/软体 |
| `stage` | 系统 | 见状态机 |
| `status` | 系统 | `running` / `pending` / `done` / `error` |
| `rerun_counts` | 系统 | 按问题类型分别计数 |
| `current_version` | 系统 | `v1`… |
| `job_id` | 系统 | 当前混元任务，24h 内有效 |

v1 存储：`size_mm` 作为 SKU 级元数据存 `work/<batch>/<sku>/meta.json`（WebUI 详情页「⚙️ 提交生成」时写入，供自动校验使用）；其余 SKU 清单暂并入 `manifests/skus.json`（或同结构 CSV）。飞书后做。

## 2. 状态机

```
ingested → preprocessed → queued → submitting → generating
                                                ↓
                                         downloading
                                                ↓
                                          validating
                                           /        \
                                    review_needed    rerun_pending
                                           \        /
                                            archived
                                                ↓
                                         exception      （次数用尽或需重拍）
```

| stage | 含义 |
|---|---|
| `ingested` | 原图入站，命名/张数检查通过或待修 |
| `preprocessed` | API 图已生成 |
| `queued` | 等并发名额 |
| `submitting` | 已调 Submit，尚未确认 JobId |
| `generating` | 有 JobId，WAIT/RUN |
| `downloading` | DONE，正在拉文件 |
| `validating` | 自动校验中 |
| `review_needed` | 人看（校验未过或边界） |
| `rerun_pending` | 将按策略再 submit |
| `archived` | 终稿入库 |
| `exception` | 超限或需重拍，等人决 |

作业规范的「拍摄/上传/生成/校验/检查/重跑/归档」映射到上表；拍摄本身不进状态机。

## 3. 入队检查

通过才进入 `preprocessed`：

- 文件名 ` {sku_id}_{01-10}.jpg|png `（允许 jpeg）。
- 至少有 `01`（正面）；建议 10 张齐。缺 135°/225° 以外的槽，标记警告仍可生成。
- 原图单边 ≥2048（可配置）；不足则拒绝或降级警告（配置项，默认拒绝）。
- API 图：长边 1280–1600，多视打包后体积预估 ≤8MB，超则继续降长边，下限 1024。

去背景：默认开 rembg，输出 PNG；纯白底批次可配置跳过。

## 4. 自动校验（v1）

每份 `data/work/.../vN/report.json`。

| 维度 | 检查 | 合格 | 失败标签 |
|---|---|---|---|
| 文件 | 存在、大小 1–20MB（GLB 参考，可配）、能被 trimesh/validator 打开 | 是 | `download_error` |
| 几何 | 破面=0；流形面占比 ≥95%；法线可定向 | 是 | `geometry_error` |
| 纹理 | 若有贴图，最大边 ≥1024 | 是 | `texture_error` |
| 尺寸 | bbox 三边与 `size_mm` 的比例，最大相对偏差 ≤10%（先按最长边对齐尺度） | 是 | `scale_review` |
| 外观分 | v1 **不做** | — | 不自动打 |

- `geometry_error` / `texture_error` / `download_error` → `rerun_pending`（或下载失败先重下一次）。
- `scale_review` → `review_needed`，不自动重跑。
- 全过 → 仍可抽检进 `review_needed`；v1 默认全过也进一次人工（首批校准），配置项 `review_policy=all|failed_only`。

glTF-Validator 报错视为 `download_error` 或 `geometry_error`（按能否打开区分）。

## 5. 人工检查

输入：`current` 的 GLB + 报告 + 原图表。  
界面：静态页或本地小服务，嵌入 model-viewer，360° 旋转。

> **当前实现**：`review` 独立页签尚未单独建；检查与查看由 WebUI 的 **3D 查看·尺寸调整台 `/viewer3d`**（本地 three.js r160，轨道查看 + 包围盒 mm）与 **校验·尺寸矫正台 `/validate`**（verdict / 面数 / 流形率 / 尺寸偏差 + 查看3D/详情/尺寸矫正/归档/重新生成）共同承担。判定与管理语义同下表。

判定（与规范一致）：

| 结论 | 下一状态 |
|---|---|
| 合格 | `archived` |
| 微调后合格 | `rerun_pending`（改参） |
| 不合格 | `exception`（需重拍）或 `rerun_pending` |

记录：检查人、时间、结论、问题描述、建议；不合格附截图路径。

## 6. 重跑

按规范分类型计数，互不合并到一个总数里（「总重跑 ≤3」只约束「生成失败」那一类）。

| 问题 | 动作 | 上限 |
|---|---|---|
| 生成失败 / 服务异常 / Job 过期 | 同参再 Submit | 3 |
| 几何 / 纹理异常 | 改参再 Submit（如关/开 PBR、FaceCount）；纹理可改走 Texture 接口 | 2 |
| 比例异常 | 人确认后再改参 Submit | 2 |
| 还原不足 / 严重失真 | 不自动生成，标需重拍 | 1（重拍后整流程当新任务） |

每次重跑新建 `v<n>`，更新 `current`。超限 → `exception`。历史版本保留。

> **当前实现**：WebUI 的「重新生成」`/gen/rerun`（`actions.rerun_sku`）与「⚙️ 提交生成」共用一个后台异步入口（`actions.start_generate_task`，单进程后台线程：提交→轮询→下载→自动校验），页面轮询 `/gen/status` 跟进；任务状态仅存内存（WebUI 重启丢失，但已落盘 `vN/model.glb` + `report.json` 仍在）。跨 SKU 的持久化断点续跑未做。

## 7. 归档

`data/archive/<category>/<batch_id>/<sku_id>/` 至少包含：

- `model.glb`、`model` 的 OBJ/zip（若有）
- `images/` 拍摄原图
- `report.json`、`review.json`
- `meta.json`（路径、大小、版本、积分、JobId 历史）

`current` 为正式资产；`v*` 留在 `data/work` 或一并拷入 `archive/history/`。

## 8. 批次

- 一批 20–50 SKU，按品类划分（配置与拍摄一致，不是混元模板）。
- 批次之间可以：A 在 generating 时 B 已 ingested。
- v1 质量汇总：跑完一批写出 `manifests/<batch_id>_qa.json`（总数、一次通过、重跑分布、异常清单）。抽检 10% 人工执行，软件只提供随机 SKU 列表。
