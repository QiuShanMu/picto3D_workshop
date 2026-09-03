# 零售多 SKU 图生 3D 流水线

把转台多角度商品图批量变成可归档的 3D 资产。生成走腾讯云混元生 3D API，其余编排自建。

**当前阶段：全链路段已可闭环。采集（含平场校正、SKU 条码前置录入）→ 交接 → 批次装配 → 预处理 → 生成队列（mock 跑通）→ 校验 → 归档；生产 WebUI 已重构为带侧边导航的多工作台应用（批次看板 / 采集台 / SKU 详情·图审 / 3D 生成台 / 3D 校验·尺寸矫正台 / 3D 查看·尺寸调整台 / 归档目录），单进程直接启用，`--provider mock|auto` 切换混元引擎。3D 查看台用本地 three.js r160 加载 GLB 并支持查看与尺寸调整。混元真实适配器（submit/poll/download + COS 上传）已完整实现；生成动作可在 SKU 详情页**一键提交并在后台异步跑**（提交→轮询→自动下载→自动校验），无需手动拉取。**数据契约与状态以 `docs/spec-orchestration.md`、`docs/spec-capture.md` 为准。**

## 文档

| 文档 | 内容 |
|---|---|
| [零售多SKU图生3D全流程方案.md](./零售多SKU图生3D全流程方案.md) | 拍摄 / 质检作业规范 |
| [docs/requirements.md](./docs/requirements.md) | 软件需求 |
| [docs/spec-architecture.md](./docs/spec-architecture.md) | 模块、目录、开发策略 |
| [docs/spec-hunyuan-api.md](./docs/spec-hunyuan-api.md) | 混元 API 契约 |
| [docs/spec-orchestration.md](./docs/spec-orchestration.md) | 状态机、校验、重跑 |
| [docs/spec-capture.md](./docs/spec-capture.md) | D435i 定机位、手转入库 |
| [docs/spec-production-capture.md](./docs/spec-production-capture.md) | 生产化采集：双人协同、防漏拍、批次装配、降级 |
| [docs/spec-hunyuan-grid.md](./docs/spec-hunyuan-grid.md) | 混元视角缺省网格实验 |

## 已实现的闭环

不依赖混元密钥。每个单元：目录进 → 目录出 → `report.json`。

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
# 方式一：一键安装全部依赖（核心 + 相机 + 扫码 + 混元 + 测试，等价于下面各 .[..] extra 加总）
pip install -r requirements.txt
# 方式二：按需分组安装（更精简，按你的工位/链路勾选）
pip install -e ".[dev]"
pip install -e ".[capture]"        # 拍摄工位才需要 pyrealsense2 / opencv
pip install -e ".[capture-web]"    # 采集页面才需要 flask（含 pyrealsense2）
pip install -e ".[capture-barcode]"  # SKU 条码识别才需要 pyzbar（可选；缺失时降级 OpenCV + 人工键入）
pip install -e ".[scan]"             # 独立扫码服务才需要（flask + pyzbar + opencv + cryptography）

# 校验：任意 GLB/OBJ
python -m pipeline.validate path\to\model.glb --size-mm 120,80,40 --out report.json

# 预处理：SKU 文件夹（APP-0812-001_01.jpg … _10.jpg）
python -m pipeline.preprocess path\to\incoming\APP-0812-001 --out path\to\api\APP-0812-001 --sku APP-0812-001

# D435i 工位采集：列设备
python -m pipeline.capture --list-devices
# 拍摄一个 SKU（按档位手转，空格拍 / s 跳过 / q 结束）
python -m pipeline.capture --batch 0812 --sku APP-0812-001 --tilt 25

# 平场校正标定（移开商品、让画面被均匀中性面填满再执行）
python -m pipeline.capture --calibrate-shading --wb 5500

# T4 交接：capture 包 → incoming/
python -m pipeline.capture.handoff_main data\captures\0812\APP-0812-001

# 采集页面（实时预览 + 点击一下采集一张）
python -m pipeline.capture.webapp --batch 0812 --sku APP-0812-001 --port 5000

# 批次装配：把整批采集包盘点成 ready/incomplete，并导出 ready SKU 到 incoming/
python -m pipeline.capture.batch_main 0812

# 生成队列（mock，无密钥跑通）：api/<batch>/<sku>/ -> work/<batch>/<sku>/v1/model.glb
python -m pipeline.queue 0812 --provider mock --fixture-dir experiments\fixtures

# 归档：work/<batch>/<sku>/ -> data/archive/<category>/<batch>/<sku>/
python -m pipeline.archive APP-0812-001 0812 --category appliance

# 生产 WebUI（批次看板 + 采集台 + 3D 生成 + 校验·矫正 + 归档）
python -m pipeline.webui --batch 0812 --port 5010 --provider mock   # 带相机 / mock 引擎
python -m pipeline.webui --batch 0812 --no-camera --port 5010       # 仅看板（无相机可浏览/重建）

pytest
```

预处理会生成最多 8 张 API 图（不含 135°/225°），并尽量压到合计 8MB 以内。

### D435i 工位采集

对应 `docs/spec-capture.md` 的 T1+T2+T3：预览 → 按台面标识手转 → 当场门闩 → 落盘 `data/captures/<batch>/<sku>/`（`capture.json` + `camera.json` + `color/` + `depth/`），随后可导出到 `incoming/`。

**色彩校正（两层）**：

1. **白平衡**：D435i 默认自动白平衡在 5500K 暖色工位下会漂移。本模块默认**关闭自动 WB/AE 并把白平衡冻结到 5500K**（曝光/增益取当前自动值固化），保证每帧一致中性。可用 `--wb` 覆盖；用 `--exposure/--gain` 指定曝光/增益，用 `--auto-color` 保留传感器自动曝光/白平衡。
2. **平场校正（shading）**：D435i 镜头有径向色彩阴影，导致画面中心圆内偏青、边缘偏品红（与白平衡无关）。本模块用一张均匀中性面参考帧标定逐像素增益 LUT（`data/captures/shadings/<serial>_shading.json`），采集时自动应用，消除中心/边缘色偏。标定：移开商品执行 `python -m pipeline.capture --calibrate-shading`；换布光或换机需重新标定。可用 `--no-shading` 关闭，`--shading-lut <path>` 指定 LUT。

**采集页面**：`python -m pipeline.capture.webapp` 启动本地页面（`http://127.0.0.1:5000`），中间是实时预览，点「采集一张」即抓一帧并按档位落盘到 `data/captures/<batch>/<sku>/color/`（含当场清晰度/过曝/主体占比门闩）。适合快速抽查帧；完整多档位手转流程仍用上面的 `python -m pipeline.capture`。

**SKU 条码前置录入（s01）**：开拍视角前可选先填 SKU，两条独立路线：① **图像识别**——拍一张 SKU 条码图，用 `pyzbar`（覆盖 Code128/39、EAN/UPC、QR）识别，自动填入 SKU；识别失败可人工键入核对。条码图落盘 `color/barcode.jpg` + `barcode.json`，写入 `capture.json` 顶层 `barcode`（可选字段），**永不进 `frames`**，因此不会流入 `handoff → incoming → preprocess → 混元`。② **手持扫码枪 Qp2100（键盘仿真 Keyboard Wedge，推荐）**——USB 直连即插即用，扫入字符以快速击键打进**当前焦点输入框**（结尾回车）；采集台页面内用前端 `keydown` 捕获阶段监听，按「相邻字符间隔 <60ms 判为同一梭、回车/Tab 终结」聚合，自动填 SKU 并锁定。**勿走后台全局钩子（pynput / 串口 / msvcrt）**——扫码枪是键盘模式，字符进前台焦点窗口，后台钩子抓不到；RDP 远程键鼠会占用输入通道。WebUI 采集台点「读条码」或扫码枪扫码 → 核对 → 「锁定」后才开始视角采集；CLI 加 `--with-barcode` 在会话开头询问。

**独立 SKU 扫码服务（`p3d-scan`，手机当摄像头）**：用一台安卓手机专门扫码，手机只当摄像头推帧，识别在电脑端做。独立进程，不与采集台耦合，便于单独闭环测试后再接入。

> ⚠️ **HTTPS 是硬前提**：手机浏览器要调用 `getUserMedia` 开摄像头，必须走 **HTTPS 或 localhost**。用局域网 IP + `http://` 会被浏览器判为不安全上下文，`navigator.mediaDevices` 为 `undefined`，相机权限根本申请不到（表现为画面黑/无预览框）。**务必用 `--https` 启动，并用 `https://<电脑IP>:5070/scan` 访问。**

```powershell
# 启动（Windows PowerShell，HTTPS，自动生成自签名证书 .p3d/cert/scan.crt）
python -m pipeline.scan --host 0.0.0.0 --port 5070 --https
# 或一键：powershell -ExecutionPolicy Bypass -File scripts\start_scan.ps1 -Https

# 不带 HTTPS（仅本机/内网调试用，手机相机不可用）
python -m pipeline.scan --host 0.0.0.0 --port 5070
```

- 手机浏览器访问 `https://<电脑IP>:5070/scan`（首次点「高级 → 继续前往」，信任自签名证书，然后点「开始扫码」授权相机，画面出现后即开始"时刻检测"）。
- 桌面浏览器访问 `https://<电脑IP>:5070/`（或 `http://127.0.0.1:5070/`）查看实时识别结果与手机画面回显。
- 证书会自动包含本机局域网 IP（SAN），换网段后建议重启服务重新生成。

## 接入真实混元（COS 上传 + 异步下载）

`pipeline/hunyuan/tencent.py` 已实现完整的 **submit / poll / download**（ai3d 2025-05-13）：提交时把本地 `api/<sku>/` 图**先上传到 COS 拿公网 URL** 再填 `ImageUrl` / `MultiViewImages`；轮询到 `DONE` 后**逐条 HTTP GET 下载** `ResultFile3Ds` 的 URL 并按 Type 分名落盘到 `work/<batch>/<sku>/vN/`。设好环境变量即可从 mock 切为真（`--provider auto` 有 key 自动切真，无 key 回退 mock）。

> ⚠️ 提交的图必须公网可访问。默认走 **COS 上传**（`TENCENTCLOUD_COS_BUCKET` 必填，桶/对象需允许读或用预签名）；否则提交会因腾讯云拿不到本地路径而失败。

准备（把 `data/.env.example` 复制为 `.env` 并填入）：
```powershell
pip install -e ".[hunyuan]"          # tencentcloud-sdk-python + cos-python-sdk-v5 + dotenv + requests
# .env 里填（程序用 python-dotenv 自动加载，无需手动 export）：
#   TENCENTCLOUD_SECRET_ID / TENCENTCLOUD_SECRET_KEY / TENCENTCLOUD_REGION
#   TENCENTCLOUD_COS_BUCKET / TENCENTCLOUD_COS_REGION
#   TENCENTCLOUD_UPLOAD_URL_MODE=public|presign
```
- `TENCENTCLOUD_COS_BUCKET`：COS 桶名（含 APPID 后缀，如 `my-bucket-1250000000`）。
- `TENCENTCLOUD_UPLOAD_URL_MODE`：`public`（桶默认公网读 URL，需桶可读）或 `presign`（短时预签名 GET，桶私有用这个）。
- 提交后 `process_sku` 会轮询（10–20s）直至 `done`，随后把 `model.glb` 等下载到版本目录并写 `hunyuan.log.json`；可在工作台看版本与校验结果。

## 尚未做

- **人工检查页**：`review` 页签未实现（可单独在 WebUI 上加；当前靠 `viewer3d` 查看 + 校验台 verdict 承担检查角色）。
- **真并发队列**：生成在 WebUI 内是**后台异步线程**（单进程内存任务表），真实并发由适配器侧 inflight 限制（账号默认 ≤3）承担；跨 SKU 的断点续跑/持久化任务表未做（WebUI 重启后内存任务状态丢失，但已落盘的 `vN/model.glb` + `report.json` 仍在）。
- **rembg 去背景**：预处理里预留，未接线。（WebUI 里的一键尺寸矫正已实现：见「校验·矫正」工作台，用 trimesh 按目标 mm 缩放并生成新版本后重校验。）

## 生产工具链（开箱即用）

**启动**（Windows，PowerShell）：
```powershell
pip install -e ".[dev,capture-web]"
python -m pipeline.webui --batch 0812 --port 5010
```
浏览器打开 `http://127.0.0.1:5010`（左侧导航切换工作台）：
- **批次看板 `/`**：一屏看到整批 SKU，每张卡是 01-08 档位点阵（绿=已拍、黄=缺档、灰=非必拍），顶部汇总 ready/incomplete/未采集/已归档计数，卡片内管线 rail 追踪 图→模型→归档（生成中显示转圈）。按编号过滤；主按钮按状态给出 去生成/去校验/拍摄。
- **SKU 详情·图审 `/sku/<id>`**：**独立图片浏览器**，下方 8 格缩略图（点击切大图，大图带"档位·朝向·角度"标签），上方显示已拍档位/采集状态/生成版本/校验结论/归档五张指标卡，中部视角清单表（档位/朝向/角度/混元槽/已拍·缺·非必拍），底部生成与校验表 + 操作区。**核心动作**：人图审通过后在此填目标 `size_mm` 并点「⚙️ 提交生成」——生成在**后台异步**跑（提交→轮询→自动下载→自动校验），页面实时轮询显示「排队中/生成中/✅完成/❌失败」；模型完成后可「去校验·矫正」「归档」「重新生成」。看板的「详情」入口跳到这里。
- **采集台 `/capture`**：实时预览 + 档位点阵 + 单点采集（含平场校正与当场门闩：清晰度/过曝/占比），采集结果实时刷新到看板点阵；**已拍档位在点阵中高亮绿色**，避免漏拍/重拍；拍摄档位网格放在采集按钮正上方，鼠标移动更近。**已拍图片回看**：采集台下方「已拍图片」缩略图网格实时列出当前 SKU 已拍各档，点击放大进 lightbox（可左右翻、Esc 关闭），核对角度/构图，点「回采集·重拍这档」即可把该档设为当前档直接补拍，拍摄与检查不用来回切页面。采集台顶部有「① SKU 条码采集」：点「读条码」拍一帧实时识别并自动填 SKU，或**扫码枪扫码直接打进 SKU 框并自动锁定**，核对后点「锁定」，「② 视角采集」才能采集；识别失败/无相机/无扫码枪时允许手动输入 SKU。档位小字显示角度与朝向（来自 `pipeline/views.py` 的 `pose_name`）。
- **3D 生成台 `/generate`**：列出「已齐」可生成 SKU，勾选后一键提交混元生 3D（mock/真），每个 SKU 显示版本历史（done/大小/校验 verdict）与后台任务状态 chip（生成中/排队中）。
- **校验·尺寸矫正台 `/validate`**：列出所有已生成模型，逐条显示 verdict、面数、流形率、包围盒(mm)、尺寸偏差；偏差超标可一键按目标 mm 尺寸矫正（trimesh 缩放 → 生成下一个版本 → 自动重校验）。每行有「查看3D」「详情」「尺寸矫正」「归档」「重新生成」按钮（归档/重新生成走 `/archive/run`、`/gen/rerun`）。
- **3D 查看·尺寸调整台 `/viewer3d`**：用**本地 three.js r160** 加载 `work/<batch>/<sku>/vN/model.glb`，轨道查看（旋转/缩放/线框/截图）、实测包围盒(mm)、当前/目标尺寸对比、输入目标尺寸做前端预览缩放（等比，与后端一致）、一键导出为下一版本（trimesh 按目标尺寸 + 自动重校验）。**与校验台联动**：校验台每行「尺寸矫正」→ 跳到查看器并自动进入调整模式，预览确认后导出新版本，可一键回校验台刷新。
- **归档目录 `/archive`**：展示 `data/archive/` 下已归档资产（只读展示；归档动作由校验台/详情页的「归档」按钮或 CLI 触发）。

**一条链走到底**（mock 示例，全部离线可跑）：
```powershell
python -m pipeline.capture --batch 0821 --sku APP-0821-001        # 或直接用工夹具
python -m pipeline.capture.handoff_main data\captures\0821\APP-0821-001
python -m pipeline.capture.batch_main 0821                        # 盘点 + 导出 ready
python -m pipeline.preprocess data\incoming\0821\APP-0821-001 --out data\api\0821\APP-0821-001 --sku APP-0821-001
python -m pipeline.queue 0821 --provider mock --fixture-dir experiments\fixtures
python -m pipeline.validate data\work\0821\APP-0821-001\v1\model.glb --size-mm 120,80,40
python -m pipeline.archive APP-0821-001 0821 --category appliance
```
