# 模型减面 / 轻量化

对齐日期：2026-09-03。混元生3D 接口**没有"降面省钱"档位**（`FaceCount`/`LowPoly` 均加价），因此产线不靠接口参数降面；需要低面数/轻量化资产时，对**已下载的 GLB 做后处理减面**。本文档记录技术选型与一手验证结果。

## 1. 为什么不在接口降面

官方 1804 计费表（专业版）：`FaceCount` 与 `LowPoly` 均为**加价项**，且 `FaceCount` 是固定附加值 +10，不随面数多少变化。想"低面数 = 低成本"在本接口不成立。

- 低成本档是 `Geometry`(15, 白模) 或 `Normal`(20) 不开 PBR。
- 主路径固定 Model=3.1（与 3.0 同价、几何纹理更好）。
- 面数/轻量化 → 后处理。

## 2. 目标模型结构（实测）

用 `data/experiments/grid_run_1/V1/model/model_glb.glb` 实测：

| 属性 | 值 |
|---|---|
| 面数 | ~50 万（499,954） |
| 顶点 | ~26.6 万 |
| 视觉 | `TextureVisuals`（带 UV，266,052 × 2） |
| 材质 | `PBRMaterial`，`baseColorTexture` 4096×4096 |
| 单文件 | 40+ MB |

即：混元输出**带一张 4K 漫反射贴图 + UV**。减面必须保住 UV，否则贴图错乱，零售资产不可用。

## 3. 减面技术选型（一手验证）

> 触发场景：想把 50 万面降到 30 万 / 10 万 / 3 万面，且**不破坏贴图**。

### 3.1 已实测的四条路（全部会丢 UV）

对同一个 V1 模型做 `simplify_quadric_decimation` / `simplify_mesh`：

| 方法 | 接口 | 结果 | 保 UV? |
|---|---|---|---|
| `open3d.TriangleMesh.simplify_quadric_decimation` | `target_number_of_triangles` | 精确命中目标面数 | ❌ 丢（读回变 `ColorVisuals`） |
| `trimesh.Trimesh.simplify_quadric_decimation` | `face_count` | 精确命中 | ❌ 丢（`uv=None`） |
| `fast_simplification.simplify_mesh` | 基于 pyvista | 命中 | ❌ 不暴露 UV |
| `fast_simplification.simplify`（底层） | verts/faces | 命中 | ❌ 不接受 tcoords |

**结论**：当前 Python 生态里，这几条**纯 quadric 减面**默认都不传/不保留 UV。需要额外手段保住贴图。

### 3.2 可靠路线（推荐，按序）

**A. gltfpack / meshoptimizer（首选）**
- 专业的 glTF/OBJ 优化工具，**原生处理 UV**，能兼顾减面 + 保纹理 + 压缩。
- 命令：`gltfpack -i model.glb -o out.glb -si 0.1`（`-si` 简化比例），或 `-cc` 压缩，`-tc` 纹理。
- 参考：https://github.com/zeux/meshoptimizer
- 验证待做：确认它对带 4K 贴图的混元 GLB 的 UV 保留表现。

**B. 减面后重新 UV 展开 / 纹理重映射**
- 用 `open3d`/`trimesh` 减面（丢 UV 可接受），再用 `xatlas`（`xatlas-python`）做重展 UV + 把原贴图重新采样到新 UV。
- 较重，但几何落地方可控。

**C. 纹理烘焙（高模→低模）**
- 减面后从原高模烘焙漫反射/法线到低模（`xatlas` + 烘焙工具）。
- 最重，适合对贴图质量极其敏感的高价值资产。

### 3.3 本项目建议

v1 先用 **A（gltfpack）** 作为后处理开关：`pipeline.optimize`（或并入 `preprocess` 下游），对指定 SKU 通过 `--target-reduction` 减面，输出 `work/.../vN/` 的轻量版。加入 `validate` 复检（减面后流形/体积/bbox 需仍在阈值内）。

## 4. 减面指标与验收

减面后需满足（否则该次减面作废，回退原版）：

- 流形：`is_watertight` 仍为真（减面不应引入裂缝）。
- 体积偏差：△volume 在阈值内（如 ±5%，依 `validate` 现有体积上下限）。
- 贴图：导出的 GLB 仍能被 `trimesh` 读到 `PBRMaterial` + UV，且 `bbox` 尺寸与原版一致。
- 文件体积：目标面数对应的尺寸显著小于原版（如降到 1/3 以上）。

## 5. 待办

- [ ] 实测 gltfpack 对混元 GLB 的减面 + 保 UV 表现。
- [ ] 若 gltfpack 不满足，评估 `xatlas` 重展 UV 路线。
- [ ] 在 `validate` 增加"减面后贴图仍存在"检查项（选做）。
