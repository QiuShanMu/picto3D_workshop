# 混元生 3D API Spec

对齐日期：2026-08-26。  
权威目录：[API 概览](https://cloud.tencent.com/document/product/1804/120838)。  
提交：[SubmitHunyuanTo3DProJob](https://cloud.tencent.com/document/product/1804/123447)。  
查询：[QueryHunyuanTo3DProJob](https://cloud.tencent.com/document/product/1804/123448)。  
数据结构：[File3D / ViewImage](https://cloud.tencent.com/document/product/1804/120828)。

本文是实现契约。文档打架时以**提交专业版任务页**为准，不以数据结构页的过时枚举为准。

## 1. 接入

| 项 | 值 |
|---|---|
| 主机 | `ai3d.tencentcloudapi.com` |
| 版本 | `2025-05-13` |
| 鉴权 | 腾讯云 API 3.0，TC3-HMAC-SHA256 |
| SDK | `tencentcloud-sdk-python`，产品 `ai3d` |
| 禁止 | `hunyuan.tencentcloudapi.com`、`SubmitHunyuanTo3DJob`（已不在概览中） |

TokenHub（`tokenhub.tencentmaas.com`）是平行网关，参数近似、字段 snake_case。本项目 v1 **只接 1804 云 API**。同事确认的文档就是这一套。

## 2. v1 使用的接口

| 接口 | 何时调用 |
|---|---|
| `SubmitHunyuanTo3DProJob` | 每个生成/重跑任务 |
| `QueryHunyuanTo3DProJob` | 轮询，建议 10–20s |
| `SubmitTextureTo3DJob` / `DescribeTextureTo3DJob` | 几何过、纹理不过时的可选重跑（v1 可后做） |
| `Convert3DFormat` | 只要 FBX 等额外格式时（v1 可后做） |

不用：极速版、组件、UV、动作、绑骨、人物。

## 3. 提交专业版

公共参数：`Action=SubmitHunyuanTo3DProJob`，`Version=2025-05-13`，`Region` 必填。

| 字段 | v1 | 约束 |
|---|---|---|
| `Model` | `3.1` | `3.1` 时 `LowPoly` 不可用 |
| `ImageUrl` | 正面图 URL | 与 `ImageBase64`、`Prompt` 三选一；图文互斥 |
| `MultiViewImages` | 其余视角 | 每视角一张；见下表 |
| `EnablePBR` | `true` | 默认关，我们默认开 |
| `GenerateType` | `Normal` | 还可用 LowPoly / Geometry / Sketch，v1 不用 |
| `FaceCount` | 可选 | 3000–1500000，LowPoly 时无效 |
| `ResultFormat` | 不传 | 文档：默认 OBJ+GLB；可选再加 STL/USDZ/FBX |

返回：`JobId`（24 小时）、`RequestId`。

图片：单边 128–5000；主图 URL ≤8MB / Base64 ≤6MB；多视**所有图合计** URL ≤8MB / Base64 ≤6MB；多视格式 jpg/png。建议纯色底、单主体、占比 >50%、无字。

入图优先 COS（或其它公网/预签名 URL）。不要把 ≥2048 原图直接 Base64。

**本项目实现**：`pipeline/hunyuan/upload.py` 在 `submit` 前把本地 `api/<sku>/` 图上传到 COS 并返回公网 URL（`public` 用桶公网读 URL，`presign` 用短时预签名 URL），再赋给 `ImageUrl` / `MultiViewImages`。上传需配 `TENCENTCLOUD_COS_BUCKET`；否则提交失败。已为 `http(s)://` 值直通不做上传。

## 4. 视角映射

作业规范：`SKU_01` … `SKU_10` = 水平每 45° 共 8 张 + 俯 + 仰。  
约定旋转方向：序号增大为**从正面看向左**（逆时针转台）。若实拍相反，只改映射表，不改混元字段名。

| 文件 | 角度 | 混元字段 | 3.1 |
|---|---|---|---|
| `*_01` | 正面 0° | `ImageUrl`（无 ViewType） | 必传 |
| `*_02` | 左前 45° | `ViewType=left_front` | 传 |
| `*_03` | 左 90° | `left` | 传 |
| `*_04` | 左后 135° | 无槽 | **不传，仅归档** |
| `*_05` | 后 180° | `back` | 传 |
| `*_06` | 右后 225° | 无槽 | **不传，仅归档** |
| `*_07` | 右 270° | `right` | 传 |
| `*_08` | 右前 315° | `right_front` | 传 |
| `*_09` | 俯视 | `top` | 传 |
| `*_10` | 仰视 | `bottom` | 传 |

`ViewImage`：`ViewType` + `ViewImageUrl`（或 `ViewImageBase64`）。  
数据结构页只列了 `back/left/right`，**以本表和提交页为准**。

缺正面则拒绝提交。缺其它槽位：能提交则提交，报告里标记「视角不齐」。**缺省合法**，工位不必凑满 8/10 张。默认拍哪几档等 [网格实验](./spec-hunyuan-grid.md) 结论，不要在代码里写死。

## 5. 查询专业版

入参：`JobId`。

| `Status` | 编排状态 | 动作 |
|---|---|---|
| `WAIT` | 排队中 | 继续轮询 |
| `RUN` | 处理中 | 继续轮询 |
| `DONE` | 待下载 | 立刻下载 `ResultFile3Ds` |
| `FAIL` | 生成失败 | 记 `ErrorCode`/`ErrorMessage`，进重跑；不扣积分 |

`ResultFile3Ds[]`：`Type`、`Url`（24 小时）、`PreviewImageUrl`。  
查询示例有时只给一条 OBJ zip。实现必须**按数组逐个下载**，按 `Type` 分文件名，不能假设一定有 GLB。

**本项目实现**：`TencentHunyuan.download` 用 `requests` 对每个 URL 做 HTTP GET，文件名优先取 URL basename（如 `model.glb`），否则回退到 Type 的规范名（`model.glb`/`model.obj`…），并记录到 `hunyuan.log.json` 与 `work/<batch>/<sku>/vN/`。

积分：`ResultCreditConsumed`、`ResultCreditDetails`（JSON 字符串）写入该次版本日志。参考：Normal 20 + MultiView 10 + PBR 10（+ FaceCount 10）。

## 6. 并发与超时

- 账号默认专业版 **3 个并发**（主子账号共享）。本地 `inflight ≤ 3`。
- `JobId` 超过 24 小时未 `DONE`：视为过期，按生成失败重提，不得再 query 当成功。
- 轮询间隔 10–20s；单任务墙钟可配置，默认 30 分钟告警、不自动杀（等官方 FAIL 或过期）。

## 7. 补纹理（可选后做）

`SubmitTextureTo3DJob`：已有 GLB/OBJ + 参考图或 prompt；3.1 才支持多视图；默认 1 并发。用于「几何过、纹理不过」而不整模重出。
