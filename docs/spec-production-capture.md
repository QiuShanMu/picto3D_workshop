# 生产化采集规划（双人协同 · 提效 · 不漏拍）

对齐日期：2026-09-01。
本文是 `spec-capture.md` 的**生产化篇**：在采集闭环已跑通（T1+T3 采集、T3b 平场、T4 交接、S1 采集页）的前提下，从**产品经理 + 生产落地**视角回答三件事：
1. 怎样**更快更高效**地采集；
2. 怎样**确保每个面都不遗漏**（尤其双人：一人拍摄、一人操作电脑）；
3. 怎样**输出稳健的数据契约**，让下游 `preprocess → queue → hunyuan → validate → review → archive` 直接用起来，并在**真实生产环境**下可降级、可追溯。

技术字段/接口口径仍以 `spec-capture.md`、`spec-hunyuan-api.md`、`spec-orchestration.md` 为准，本文不重复、不冲突。

---

## 0. 一句话结论（给决策者）

**保持"人转台 + 软件确认"不变，把单人拍摄升级为"指挥台 + 执行位"的双人流水线**：执行位只负责摆正、转台、放稳，指挥台（电脑）负责识别人工档位、实时门闩、记录、检漏、打码。软件从"被动记快门"变成"主动防漏 + 可追责 + 可重跑"的数据源。

> 现有 `capture` 闭环的**采集数据结构已经能支撑生产**，无需推倒重来；真正要补的是『**防漏拍**』『**双人同步**』『**SKU 级批次装配**』和『**生产环境下的故障降级**』四件事。本文把这四件事定清楚，落地时逐项实现。

> **进度标注**：本文是**规划 + 已落地**。文中**已实现**的有：采集数据契约增强（`capture.json` 的 `target_views`/`session_metrics`/`attempt`）、**SKU 条码前置录入**（`capture.json` 顶层 `barcode` + `color/barcode.jpg` + `barcode.json`，条码图不入 `frames`、不进图生3D；`pyzbar` 主 + OpenCV 兜底 + 人工键入兜底，WebUI 采集台「读条码 → 自动填 SKU → 锁定后拍视角」，CLI `--with-barcode`）、**手持扫码枪 Qp2100 键盘仿真接入**（USB 即插即用，扫入字符打进焦点输入框，采集台页面内 `keydown` 捕获聚合自动填 SKU 并锁定；勿走后台全局钩子 pynput/串口/msvcrt —— 扫码枪是键盘模式，字符进前台焦点窗口，RDP 远程键鼠会占用输入通道）、批次装配 `pipeline.capture.batch_main`（产出 `data/incoming/<batch>/_batch_manifest.json`，含 `ready/incomplete/missing_required`）、生产 WebUI（`pipeline.webui` 批次看板 + 采集台 + 管线状态；采集台**已拍图片回看**：缩略图网格 + lightbox 放大左右翻动，点「回采集·重拍这档」把该档设为当前档直接补拍，拍摄与检查同页完成）、**SKU 详情·图审页 `/sku/<id>`**（图片浏览器，集中显示采集/生成/校验/归档，提供「⚙️ 提交生成」后台异步生成 + 归档 + 重新生成入口），以及生成队列（`pipeline.queue` + `pipeline.hunyuan`，`--provider mock` 跑通整链；**混元真实适配器已实现 submit/poll/download + COS 上传**，`--provider auto` 有 key 自动切真）。**仍属规划/待办**：人工检查页（`review`，当前由 `viewer3d` + 校验台 verdict 承担）、真并发队列与断点续跑、rembg 去背景。文档用「规划」「建议」区分已实现与未实现。

---

## 1. 目标与约束

### 1.1 生产目标

| 目标 | 量化口径 | 现状 |
|---|---|---|
| 单 SKU 采集节拍 | ≤3–5 min/SKU（P0 斜视 8 档） | 单人视角接近，双人可再压 |
| 不漏拍（每面覆盖） | 运行中**任一面未拍即阻断、库内必留残缺记录** | 目前仅靠操作员自觉，**无强制检漏** |
| 数据可用率 | `当前/交接` 目录只在「齐了该批要求的档」才标 `handed_off` | 目前只保证 `01` 即可导出 |
| 可追溯 | 任一帧可定位到「谁、何时、哪台相机、哪个档位、是否配色校正」 | 已有 `capture.json`，缺操作员/会话维度 |
| 批次吞吐 | 一批 20–50 SKU 流水起来 | 采集是瓶颈，需有批次级装配脚本 |

### 1.2 硬约束（来自 v1 范围，不要突破）

- **单 D435i 锁死机位**；不接电动转台、机械臂、双机位同步。
- **P0 主路径只有 8 张 yaw**；`09` 俯视 / `10` 仰视本工位**拍不到真值**，v1 不假装补齐（见 spec-capture §2.2）。
- 生成后端只认 **`ai3d` 专业版 3.1**；混元不认深度/IMU/角度次数。
- 密钥只走环境变量，不入库、不入文档示例。
- **缺省是一等公民**：除 `01` 外各档可不拍、可跳过；但「跳过」与「漏拍」必须在数据里区分开。

---

## 2. 双人协同：指挥台 vs 执行位

### 2.1 为什么双人更快、更稳

单人 = 同时做三件事：摆正商品 → 转台到角度 → 回电脑按快门。三者相互争抢手眼，且**人离开电脑看台面时，软件无法知道当前是几度**，容易漏拍或拍重。

双人分工后，**软件（指挥台）成为节拍源**，把"人"的动作降为两条简单指令：

| 角色 | 只做这几件事 | 不做 |
|---|---|---|
| **执行位（拍摄员）** | ①放正商品定义正面 ②转台到台面标识 ③确认姿态到位 | 不碰电脑、不看 UI，只管摆与转 |
| **指挥台（电脑 / 操作员）** | ①播报"下一档 45°" ②看到即认档、校验 ③记录/标跳过 ④结束时检漏 | 不碰商品、不转台 |

> 关键变化：**转台是"人"的物理动作，但"下一档是多少度"由软件说了算**。执行位听到/看到提示后转动，指挥台确认，形成 `提示 → 动作 → 确认` 的闭环。这样"每个面都拍到"从**靠记忆**变成**靠流程**。

### 2.2 需要新增的最小机制（防漏拍核心）

- **档位清单驱动**：会话一开始由该批次要求（默认 8 档 `01–08`，可配置）生成"待拍队列"，软件**按序播报**，而不是让操作员自由挑角度。
- **每档双状态**：`shot`（已拍且过闩）/ `skipped`(主动跳过，写明原因)/ `missing`(未处理)。`missing` 在结束时**必须显式检漏**，不允许静默漏。
- **进度可视化**：指挥台显示 `01✓ 02✓ 03⋯ 04△ 05✓ …`，一眼看出哪档缺。
- **档位防重**：同一 `index` 重拍时**覆盖但计数 **→ 保留 `attempt` 次数，用于追溯"这面是否重拍过"。

### 2.3 双人语音/画面同步的替代方案（低成本）

不必上语音识别。指挥台用**大字号 + 颜色 + 声音三通道**提示：
- 屏幕中央大字「**下一档 03 · 90° 左**」，配合高亮色带；
- 可选蜂鸣/语音 TTS 播报档位号（省硬件）；
- 台面贴**固定标识**（0°/45°…315°），执行位只对标识，不用读角度。

> 这样即使执行位背对屏幕，也能靠"台面标识 + 提示音"完成转动，**无需低头看电脑**，是双人提速的关键。

---

## 3. 数据契约（生产级采集包）

### 3.1 采集包目录（保持 spec-capture §4，仅**增强字段**）

```
data/captures/<batch_id>/<sku_id>/
  capture.json            # 增强：加 operator、target_views、session_metrics、barcode
  camera.json             # 内参、串号、分辨率、shading LUT 引用
  color/01_yaw000.jpg     # 已应用 WB + shading
  color/02_yaw045.jpg
  ...
  color/barcode.jpg       # SKU 条码图（前置录入；不入 frames、不进图生3D）
  barcode.json            # {value,type,source,image,captured_at,manual}
  depth/01_yaw000.png     # 16-bit；深度失败可空，不挡入库
  shadings/<serial>_shading.json   # 可选，建议放会话级共享目录
```

不新增顶层「数据文件类型」之外的结构；`handoff` 仍按 spec-capture §5 投影。

### 3.2 `capture.json` 增强字段（**向后兼容**，只加字段不破坏现有 key）

```json
{
  "schema": "capture.v1",               // 不变
  "sku_id": "APP-0812-001",
  "batch_id": "0812",
  "station_id": "d435i-desk-1",
  "operator": "zhang-san",              // 新增：本次会话操作员（可空=未登记）
  "started_at": "2026-09-01T08:00:00+08:00",
  "finished_at": "...",                 // 新增：会话结束（补检漏后）
  "pose_mode": "yaw_manual_marks",
  "target_views": {                     // 新增：本批要求拍的档 + 每档是否必拍
    "01": {"required": true},
    "02": {"required": true},
    "03": {"required": true},
    "04": {"required": false},          // 135° 仅归档，不送混元
    "05": {"required": true},
    "06": {"required": false},
    "07": {"required": true},
    "08": {"required": true},
    "09": {"required": false},          // 俯视：P0 不拍
    "10": {"required": false}
  },
  "rotation": {"method": "manual_marks", "step_deg": 45, "direction": "ccw"},
  "barcode": {                          // 新增（可选）：SKU 条码前置录入
    "value": "APP-0812-001",
    "image": "color/barcode.jpg",
    "captured_at": "...",
    "auto": true                        // true=扫码识别, false=人工键入
  },
  "camera": {
    "model": "d435i", "serial": "", "color": "1920x1080", "tilt_deg": 25,
    "color_controls": {...}, "shading_lut": "shadings/<serial>_shading.json"
  },
  "session_metrics": {                  // 新增：一次会话汇总
    "total_attempts": 10,               // 总抓帧数（含重拍）
    "ok_frames": 8,
    "skipped": ["04", "06"],
    "missing": [],                       // 必拍但没拍到的档 → 检漏依据
    "recapture": ["03"]                  // 重拍过的档（首次失败后补拍）
  },
  "frames": [
    {
      "index": "01", "yaw_deg": 0, "pose": "yaw", "hunyuan": "ImageUrl",
      "color": "color/01_yaw000.jpg", "depth": "depth/01_yaw000.png",
      "ok": true,
      "attempt": 1,                      // 新增：第几次抓这档（1=一次过）
      "gate": {"sharpness": ..., "exposure": ..., "object_ratio": ...},
      "captured_at": "..."
    }
  ],
  "status": "captured"                  // captured | needs_review | handed_off
}
```

**规则（生产必守）**：
- `frames` 只列**实拍且过闩**的档；`skipped`/`missing` 不进 `frames`，写在 `session_metrics`。
- `missing` = `target_views.required == true` 但 `frames` 里没有 → 结束检漏必查。
- `attempt` 用于追溯重拍；同一 `index` 出现多次时保留最后一次,但在 `session_metrics.recapture` 里登记。
- **深度失败不挡交接**；`depth` 可空。
- 文件先 `*.tmp` 再 `os.replace`，防半截图被交接扫到。
- **条码不混片**：`barcode.jpg` / `barcode.json` 只归档，**永不写进 `frames`**；`barcode` 字段可选，缺省兼容旧数据。**识别值与人工键入不一致时以人工为准**，条码值仅存档+提示核对。

---

## 4. 交接层：从"有01就交"到"齐该批要求才判完工"

现有 `handoff` 只要 `01` 存在就导出。生产上需要**两档判定**：

| 模式 | 交给下游什么 | 何时用 |
|---|---|---|
| **快速交**（现状） | `01` 在即导出，`missing` 允许 | 网格实验 / 试跑 / 紧急排查 |
| **标准交**（生产默认） | **该批全部 `required` 档都拍到**才导出；否则标 `needs_review` 并列出缺失档 | 正式放量 |

`handoff.json` 增加两个字段支持下游决策：

```json
{
  "schema": "handoff.v1",
  "sku_id": "...", "batch_id": "...", "source": "capture.v1",
  "source_capture": "captures/.../capture.json",
  "exported": ["01","02","03","05","07","08"],
  "skipped": ["04","06"],
  "missing_required": [],           // 新增：必拍但缺 → 标准交此时不判完工
  "mode": "standard",               // 新增：standard | fast
  "status": "handed_off"            // handed_off | needs_review | incomplete
}
```

这样 `preprocess`、`queue`、`hunyuan` 能**直接读 `handoff.json`** 决定：缺非必拍档 → 照走；缺必拍档 → 标 `incomplete` 让人补拍或改 `mode=fast`，而不是被静默吞掉。

---

## 5. 批次级装配（提效关键，从单SKU到整批）

生产是**一批**不是一个个。采集侧补一个**批次装配脚本**（对应 T4 之上、T5 之下），把整批的采集包引到 `incoming/` 并产出批次清单：

```
p3d-batch-assemble --batch 0812 --capture-root data/captures --incoming-root data/incoming
    → incoming/<batch>/<sku>/… + incoming/<batch>/_batch_manifest.json
```

`_batch_manifest.json` 内容（给下游 queue/orchestration 用）：

```json
{
  "schema": "batch_manifest.v1",
  "batch_id": "0812",
  "sku_count": 30,
  "ready": 28,             // 满足 standard 交的 SKU 数
  "incomplete": 2,         // 缺必拍档
  "skus": [
    {"sku_id": "APP-0812-001", "status": "ready", "missing_required": []},
    {"sku_id": "APP-0812-002", "status": "incomplete", "missing_required": ["03","08"]}
  ]
}
```

**价值**：下游 `queue`/看板不再自己数目录判断谁齐了，直接读批次清单；拍摄组也能一眼看到"整批还有哪几个 SKU 缺了几档"。

---

## 6. 生产环境风险与降级策略

| # | 风险场景 | 现象 | 缓解 / 降级 |
|---|---|---|---|
| 1 | 相机掉了/断连 | `list_devices` 空、`grab` 抛异常 | 启动先探测，连不上明确报错退出；**不静默重试**；web 页顶部显示 `serial=—` |
| 2 | 平场 LUT 缺失 | 中心/边缘色偏（已修过） | 采集时若发现 `serial_shading.json` 缺失：**warn 并降级不校正**，同时在 `capture.json` 标 `shading_lut:null`，提示先跑 `--calibrate-shading` |
| 3 | 灯光色温漂移 | 整体偏色 | 依赖冻结 WB；换灯后需重新跑 `--calibrate-shading` 更新默认 WB/LUT |
| 4 | 占台商品糊 / 过曝 | 门闩拒收 | 门闩当场拒 → 改 `missing`，允许重拍；**清晰度阈值可配置**，不同品类（织物vs金属）不同 |
| 5 | 单帧偶发重影/花屏 | 门闩 sharp 低 | 抓多帧取最佳(或用最新帧)，bad frame 丢弃；`attempt`+1 |
| 6 | 深度流失败 | `depth` 空 | 不挡交接（沿用规则）；`camera.json` 标注 depth=off |
| 7 | 批次中断 | 进度丢失 | **采集是增量的**：每次落盘即持久化到 `capture.json`；重开会话续拍，不丢已拍帧 |
| 8 | 密钥/云依赖 | submit 失败 | 采集侧**完全不依赖云**，离线可跑；云问题只影响生成侧 |
| 9 | 交接命名错 | preprocess 报缺档 | 由 `views.py` 统一出 index→文件名，禁止手工起名；`handoff` 只拷贝，改名逻辑单一 |
| 10 | 操作员记错档 | 拍重/漏拍 | `target_views` 队列 + 检漏 + `attempt` 追溯；档位由软件播报，不靠记忆 |
| 11 | 大批量吞吐瓶颈 | 采集是单机串行 | `p3d-batch-assemble` 批量装配，采集端与生成端解耦；生成端 ≤3 并发是官方限制，不是采集瓶颈 |

### 6.1 故障排查如何快速定位

- 每个模块输出 **`report.json`**（`ok/warn/fail` + 原因），`handoff`/`batch-assemble` 同理。
- `capture.json` 记 `operator`、`started_at`、`session_metrics`，能回答"这 SKU 第几次、哪档拍过、谁拍的"。
- **日志按批次可检索**：采集、交接、装配各自写日志，满足 requirements §7「能回答这个 SKU 第几次、用了哪些视角」。

---

## 7. 从"能跑"到"生产可用"的分步落地

按 spec-architecture §6**单模块闭环**，不一次上大编排。建议顺序：

1. **补数据契约**：`capture.json` 增加 `operator`/`target_views`/`session_metrics`/`attempt`；`handoff.json` 增加 `missing_required`/`mode`。**向后兼容**，不破坏现有 key。
2. **防漏拍检漏**：会话结束输出 `session_metrics.missing`，比对 `target_views.required`；必拍缺失则 `status=needs_review` 并醒目提示。
3. **双人指挥台 UI**：在 `webapp.py` 上把"自由选档"改为"**播报待拍队列** + 大字号提示 + 进度点阵"，加 `operator` 输入、`skipped` 登记。
4. **批次装配**：`p3d-batch-assemble` 产出 `_batch_manifest.json`。
5. **标准交切换**：`handoff` 增加 `--mode standard|fast`，默认 `standard`。
6. **故障注入演练**：拔深度、拔相机、删 LUT、断灯，验证降级路径都在 `report.json` 有明确反映。

---

## 8. 验收（生产采集，不含混元）

1. **不漏拍**：会话模拟跳拍 `03`，结束时报 `missing=["03"]`，`status=needs_review`，不允许静默成功。
2. **双人协同**：指挥台按队列播报 `01→08`，执行位仅靠台面标识+提示音完成转动，**无需看电脑**。
3. **数据契约**：`capture.json`/`handoff.json` 含新字段且向后兼容（旧包仍可交），`handoff.json` 的 `missing_required` 与 `exported` 一致。
4. **批次装配**：一批 30 SKU，`_batch_manifest.json` 正确报告 `ready/incomplete` 与各自 `missing_required`。
5. **降级**：拔深度流仍能交接；删 `serial_shading.json` 降级并告警；丢相机时报错退出不静默。
6. **可追溯**：任一帧能答出 operator、attempt、时刻、shading 是否应用。

---

## 9. 明确不做（v1）

- 不接电动转台/步进/机械臂（保持"人手转"）。
- 不做单人 = 双人的自动识别（不做跨机位同步、不做自动摆件）。
- 不做真俯视/真底面（单机位物理限制，P0 只拍 yaw 8 张）。
- 不做语音 NER / 复杂语义；提示音 + 台面标识即可。
- 不做飞书/云看板；v1 用 `_batch_manifest.json`。

---

## 附：与现有文档的对应

| 本文条目 | 对应既有文档 |
|---|---|
| 采集包目录 | spec-capture §4（增强字段，不强改） |
| 视角映射 | spec-capture §1 / spec-hunyuan-api §4 / views.py |
| 交接层 | spec-capture §5（增强 mode） |
| 单模块闭环 / 开发顺序 | spec-architecture §6 |
| 下游 queue / 状态机 | spec-orchestration |
