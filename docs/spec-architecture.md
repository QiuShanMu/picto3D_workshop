# 架构 Spec

对齐日期：2026-09-02。需求见 [requirements.md](./requirements.md)。

## 1. 原则

混元是唯一的生成后端，以适配器形式接入。编排层不绑定 SDK 细节，只依赖：

```
submit(sku_images, params) -> job_id
poll(job_id) -> {status, files?, error?, credits?}
download(files) -> local_paths
```

后处理接口（补纹理、转格式）同样走适配器，v1 可以只实现 `submit/poll/download`。

## 2. 模块

```
D435i 手转 ──► capture      采集包 captures/（见 spec-capture.md）
              │
              ▼
           handoff      投影成 incoming/SKU_01.jpg
              │
              ▼
           preprocess    rembg（可关）+ API 压缩图
              │
              ▼
           queue         本地队列，在飞混元任务 ≤3
              │
              ▼
           hunyuan       专业版 3.1 Submit/Query
              │
              ▼
           store         24h 内拉 GLB/OBJ 到版本目录
              │
              ▼
           validate      trimesh + glTF-Validator + bbox
              │
        ┌─────┴─────┐
        ▼           ▼
     review       rerun
   model-viewer   按问题类型改参或重提
        │
        ▼
     archive     品类/批次/SKU + 元数据
```

| 模块 | 实现策略 | 复用 |
|---|---|---|
| ingest / queue / rerun / archive | 自建 | 无可用产线仓库 |
| preprocess | 自建薄封装 | [danielgatis/rembg](https://github.com/danielgatis/rembg) |
| hunyuan | 自建适配器 | 腾讯云 SDK，产品 `ai3d`，版本 `2025-05-13` |
| store | 自建 | 标准 HTTP 下载；图床用 COS（推荐） |
| validate | 自建报告 | [mikedh/trimesh](https://github.com/mikedh/trimesh)、[KhronosGroup/glTF-Validator](https://github.com/KhronosGroup/glTF-Validator) |
| review | 自建极简页 | [google/model-viewer](https://github.com/google/model-viewer) |

不要整仓 fork：InstantMesh、ComfyUI-3D-Pack、Hunyuan3D-2-batch、snapprint、3DScanner。

## 3. 工作目录（开工后按此建）

```
data/incoming/<batch_id>/<sku_id>/     拍摄原图（10 张）
data/api/<batch_id>/<sku_id>/          送给混元的压缩图
data/work/<batch_id>/<sku_id>/v<n>/    该次生成：模型、预览、混元日志、report.json
data/work/<batch_id>/<sku_id>/meta.json  SKU 级元数据（size_mm，供自动校验）
data/work/<batch_id>/<sku_id>/current  指向当前版本的链接或指针文件
data/archive/<category>/<batch_id>/<sku_id>/
manifests/skus.json                    SKU 清单与状态
```

`data/` 不进 git。作业规范里的「工作目录 `批次/SKU`」对应这里的 `data/work`；「归档三级目录」对应 `data/archive`。

## 4. 默认生成档

| 参数 | v1 默认 | 说明 |
|---|---|---|
| 接口 | 专业版 `SubmitHunyuanTo3DProJob` | 不用极速版做主路径 |
| Model | `3.1` | 才能吃俯仰和 45° |
| GenerateType | `Normal` | 带纹理 |
| EnablePBR | `true` | 零售材质 |
| FaceCount | 不传（默认 50 万） | 要控面数再开，会加积分 |
| ResultFormat | 不传 | 收默认 GLB+OBJ |
| 并发 | 3 | 与账号默认一致，可配置 |

品类差异（布光、是否去背景、是否强制 PBR）放配置文件，不写死在适配器里。

## 5. 配置与密钥

| 变量 | 用途 |
|---|---|
| `TENCENTCLOUD_SECRET_ID` | 云 API 密钥（ai3d 鉴权，也复用给 COS 上传） |
| `TENCENTCLOUD_SECRET_KEY` | 云 API 密钥 |
| `TENCENTCLOUD_REGION` | 地域，如 `ap-guangzhou`，开工时按控制台填写 |
| `TENCENTCLOUD_COS_BUCKET` | COS 桶名（含 APPID 后缀）；submit 入图先上传到此桶拿公网 URL |
| `TENCENTCLOUD_COS_REGION` | COS 地域（一般与 `TENCENTCLOUD_REGION` 同） |
| `TENCENTCLOUD_UPLOAD_URL_MODE` | `public`（桶默认公网读）或 `presign`（短时预签名 GET） |
| `HUNYUAN_MAX_INFLIGHT` | 默认 3 |
| `PIPELINE_DATA_ROOT` | 默认 `./data` |

> 程序用 `python-dotenv` 自动加载 `.env`（含上述变量），无需手动 export；密钥只走环境变量 `.env`，禁止写入仓库、文档示例或清单 JSON。

## 6. 开发策略：分模块闭环（不做一步到位 E2E）

每个可交付物是一条 **CLI（或小脚本）+ 目录入 + 目录出 + `report.json`**。上游没就绪时，用夹具文件夹就能单独跑、单独调。整批状态机、并发队列、重跑策略是**最后一层薄编排**，不在第一天出现。

统一契约（各模块自己认，不要先做中央配置中心）：

```
input_dir / 若干文件
        →  module
        → output_dir + report.json   # ok | warn | fail，原因可机读
```

### 6.1 闭环单元（相邻已合并的只留 5 个）

| 单元 | 绑在一起的原因 | 入 | 出 | 本单元参数（少） |
|---|---|---|---|---|
| A. 入站预处理 | 命名检查和压图看的是同一批图，拆开会对两次目录 | `incoming/<sku>/` 10 张 | `api/<sku>/` + 视角清单 | 是否 rembg、长边、原图最小边 |
| B. 混元生成落盘 | JobId / 结果 URL 只有 24h，submit 与 poll/download 不能分开调 | `api/` 图（或 URL） | `work/.../vN/` 模型与混元日志 | 地域、3.1、PBR、轮询间隔 |
| C. 自动校验 | 打开文件和几何/贴图是同一次 trimesh 加载 | 一个 GLB/OBJ + 可选 `size_mm` | `report.json` | 体积上下限、流形阈值、是否做尺度 |
| D. 人工检查 | 只依赖模型，不依赖混元 | GLB + 报告 | `review.json` | 几乎无 |
| E. 归档 | 拷文件，不依赖生成 | work 终稿 + 原图 + 两份 json | `archive/<品类>/<批>/<sku>/` | 目标根路径 |

**不要**和别的单元绑死：预处理 ↔ 混元（省积分调 rembg）、混元 ↔ 校验（用夹具 GLB）、校验 ↔ 检查、检查 ↔ 归档。

**推迟到编排层才出现的东西**（先 E2E 会提前冒出来）：`queue`、`HUNYUAN_MAX_INFLIGHT`、完整 `stage` 状态机、`rerun_counts`、`review_policy`、飞书/批次看板。

### 6.2 建议实现顺序

1. **C 校验**：`python -m pipeline.validate <model.glb> --size-mm L,W,H`（已实现）
2. **A 预处理**：`python -m pipeline.preprocess <incoming/sku> --out <api/sku>`（已实现；rembg 未接线）
3. **B 混元落盘**：单 SKU，密钥齐了再跑。
4. **D 检查页**：指到任意 GLB。
5. **E 归档**：指到任意已通过的 work 目录。
6. **编排**：把 A→B→C→D→E 串起来，这时才加队列、重跑、清单状态。

### 6.3 参数所有权

参数跟单元走，禁止「全局 config 先堆 30 个键」。编排层只传路径和 SKU 标识，不把 A 的长边、C 的流形阈值、B 的 PBR 提升为同一张表的必填项。需要覆盖时用该单元的 CLI 旗标或该单元自己的小 yaml。
