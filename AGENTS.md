# Agent 说明

零售多 SKU 图生 3D 流水线。人读文档从 [README.md](./README.md) 进。

## 红线

- 生成后端只接腾讯云混元生 3D（产品 1804，`ai3d.tencentcloudapi.com`，版本 `2025-05-13`）。
- 不要调用已下线的 `SubmitHunyuanTo3DJob` / `hunyuan.tencentcloudapi.com`。
- 不要自托管 Hunyuan3D / TRELLIS，不要把 ComfyUI 当产线。
- 密钥只走环境变量，禁止写入仓库。
- 混元结果 URL 和 JobId 只有 24 小时，必须落盘。
- 多视图合计体积按官方限制压图；135° / 225° 不要塞进 `MultiViewImages`。
- 同时在飞的专业版任务默认 ≤3。

## 文档

| 要改什么 | 先读 |
|---|---|
| 范围、验收 | `docs/requirements.md` |
| 模块、目录 | `docs/spec-architecture.md` |
| 混元字段、视角 | `docs/spec-hunyuan-api.md` |
| 状态、校验、重跑 | `docs/spec-orchestration.md` |
| 拍摄作业 | `零售多SKU图生3D全流程方案.md` |
| D435i 采集/入库 | `docs/spec-capture.md` |
| 多设备采集适配 | `docs/spec-multi-camera.md` |
| 混元视角实验 | `docs/spec-hunyuan-grid.md` |

作业规范是业务源；与 API 冲突时以 `docs/` 里「对齐后的行为」为准。

## 实现策略

先做 5 个可单独跑的闭环（预处理 / 混元落盘 / 校验 / 检查 / 归档），最后才串编排。不要一上来建完整状态机和全局大配置。相邻只合并：入站+压图、submit+poll+download。

## 现状

2026-09-02：采集（含平场校正 shading、**SKU 条码前置录入**）→ 交接 → 批次装配 → 预处理 → 生成队列（mock 跑通）→ 校验 → 归档全链路可闭环。生产 WebUI 已重构为**带侧边导航的多工作台应用**，单进程整合 7 个工作台：批次看板、采集台、**SKU 详情·图审（图片浏览器）**、3D 生成台、3D 校验·尺寸矫正台、**3D 查看·尺寸调整台（Three.js，r160 本地托管）**、归档目录；页面模板在 `pipeline/webui/templates/`（Jinja 继承 base + app.css），子类可执行动作在 `pipeline/webui/actions.py`（build_skus / start_generate_task / get_generate_status / size_correct / archive_sku_web / rerun_sku / list_archives）。**SKU 详情页 `/sku/<id>`**：独立图片浏览器（8 格缩略图点击切大图），集中显示采集状态 + 生成版本 + 校验结论 + 归档，并提供「⚙️ 提交生成」（填目标 `size_mm`）——**生成在后台异步跑**（`start_generate_task` 起单进程后台线程：提交→轮询→自动下载→自动校验），页面轮询 `/gen/status` 实时显示排队中/生成中/完成/失败；`rerun_sku`（`/gen/rerun`）与 `archive_sku_web`（`/archive/run`）也走 WebUI 入口。3D 查看工作台（`/viewer3d`）用**本地托管 three.js r160**（`static/vendor/three/`，离线可用）加载 `work/<batch>/<sku>/vN/model.glb`，支持轨道查看（旋转/缩放/线框/截图）、实测包围盒 mm、前端预览缩放、后端导出下一版本（trimesh 按目标尺寸 + 重校验）。**尺寸矫正已与查看器联动**：校验台每行「尺寸矫正」跳到 `/viewer3d?ver=vN&size=W,H,D&mode=size`，自动进入调整模式（高亮面板、预填目标、自动等比预览缩放），预览确认后导出下一版本，一键「回校验台」刷新；预览用最大边对齐等比缩放，与后端导出结果一致。混元真实适配器已实现完整 `submit/poll/download`（`pipeline/hunyuan/tencent.py`），`--provider mock|auto` 切换；本地 `api/<sku>/` 图提交前经 COS 上传取公网 URL（`pipeline/hunyuan/upload.py`），`download` 用 HTTP GET 逐条拉 `ResultFile3Ds` 并按 Type 分名落盘。`make_adapter` / `TencentHunyuan` / `upload` 用 `python-dotenv` 加载 `.env`（含 `TENCENTCLOUD_COS_BUCKET`、`TENCENTCLOUD_UPLOAD_URL_MODE`）。朝向元数据**唯一来源是 `pipeline/views.py` 的 `ViewSlot.pose_name`（如 正面/45°左前/90°左…）**，前端档位小字与详情页角度标签统一从它取。数据契约：`capture.json` 含 `target_views/session_metrics/attempt` 与顶层 `barcode`，`barcode.json` 含 `value/type/source/image/captured_at/manual`，`_batch_manifest.json` 含 `ready/incomplete/missing_required`，`work/<batch>/<sku>/vN/` 每档落盘，`work/<batch>/<sku>/meta.json` 存 `size_mm`（详情页提交时写入，供自动校验）。

**SKU 条码前置录入（两条独立路线）**：开拍视角前可选填 SKU。① **图像识别**（`pipeline/capture/barcode.py`，`pyzbar` 主 + OpenCV `BarcodeDetector` 兜底，覆盖 Code128/39、EAN/UPC、QR），识别后自动填 SKU，识别失败/无 pyzbar 允许人工键入，条码图落 `color/barcode.jpg` + `barcode.json`。② **手持扫码枪 Qp2100（推荐，键盘仿真 Keyboard Wedge）**：USB 直连后即插即用，扫入的条码字符以快速击键打进**当前焦点输入框**，结尾带回车。采集台页面内用前端 `keydown` **捕获阶段**监听，按「相邻字符间隔 <60ms 判为同一梭、回车/Tab 终结」聚合，自动填 SKU 并 `doLock` 锁定；捕获后强制聚焦 SKU 框、把累积字符实时回填 SKU 框，锁后再扫**忽略**（防止后续扫码误入其它输入框）。WebUI 采集台「读条码 / 扫码枪扫码 → 自动填并锁定 → ①视角采集」；CLI 加 `--with-barcode`。`pyzbar` 是可选依赖 `.[capture-barcode]`，缺失时优雅降级。

> 扫码枪 Qp2100 **不要走后台全局钩子（如 pynput 全局键盘监听）、不要走串口（需配置条码，Qp2100 无）、不要用 `msvcrt` 控制台读键**——这些在被否决的探索中均不可靠。根因：扫码枪是键盘模式，字符进了**前台焦点窗口**（如 Cursor 对话输入框 WebView），后台钩子抓不到；且 **RDP 远程键鼠会占用输入通道**（远程时扫不进/设备消失）。关掉 RDP 本地直连即插即用。手机当摄像头那条路（`pipeline.scan.app` / `p3d-scan --https`）照旧有效，与扫码枪并存。

**安卓 USB 相机（隔离实验，未入主流程）**：`experiments/android_usb_cam/` 是**实验阶段0**，2026-09-03 已跑通「手机→USB(ADB 隧道)→Python 解码→进入 gate/handoff/preprocess」闭环；**主项目 `pipeline/`、`docs/`、`webui/` 零改动**（`experiments/` 被根 `.gitignore` 排除）。移动端选 **`kafkasl/spyglass`**（开源 DroidCam 替代品，MIT、`GET /snap` 按需抓 JPEG、端口 4747），`build_spyglass.ps1` 自动下载 JDK17+Android SDK 并自建 APK 后 `adb install`（本机无 Android 环境也 OK）。**2026-09-03 又修复了 spyglass 的 `POST /config` 500**：根因是 `applyConfig`(→`bindCamera`→`bindToLifecycle`) 跑在 NanoHTTPD 后台线程，而 CameraX `bindToLifecycle` 强制主线程；已把切相机动作 `post` 到主线程。同次改动让 `bindCamera` 按 `availableCameraInfos` 索引选镜头（原来非 1 一律 `DEFAULT_BACK_CAMERA` 选不到别的镜头），`GET /status` 增加 `cameras` 报告各相机 `facing`。**网格实验结论**：该华为机 `availableCameraInfos` 枚举出 6 个逻辑相机，但**只有 `cam0` 真正清晰**（lapvar≈216 vs 其余 ~6；cam1/cam3 全黑、cam2/4/5 糊）；分辨率被 CameraX 就近映射成**方形**（请求 3264x2448 → 实际 **3072x3072**，q95 时 sharp≈130~216）；推荐配置 `camera_id=0, resolution=3264x2448, jpeg_quality=95, fps=15`。**snap 延迟≈0.03~0.08s**（读最近已编码帧，旧 README 记的 5s 是未预热/慢速情形）。关键结论：**现有 `gate_frame` 阈值对手机源偏高**（手机帧 sharp≈132、obj≈36.6%），需下调 `min_sharpness`(60→30)、`min_object_ratio`(0.40→0.08)，即**新输入源必须按实测重标定 gate 阈值**。真 4K/1080p 需自研 App 走 Camera2 TakePicture（已确认不并入主流程前不做）。

**多设备采集适配（方案筹备→已对齐生产基线，2026-09-04）**：规划「**保留 D435i、同时接入手机（安卓 USB）相机、支持多类采集设备**」。契约见 `docs/spec-multi-camera.md`；衔接材料见 `experiments/android_usb_cam/BRIDGE_TO_MAIN.md`。核心思路：① 抽象 `CaptureDevice` 协议（`open/grab/close` + `DeviceCapabilities`，含 has_depth/has_imu/supports_shading/**supports_exposure_control**/gate_defaults），工厂 `make_capture_device(kind, ...)` 按 `d435i|android_usb|both` 选择；② 复用现有 `CameraInfo`/`FrameBundle`；③ **下游零改动**——`handoff/preprocess/hunyuan/validate/archive` 只认 `frames[].color` + `incoming/SKU_0X.jpg`，手机源只需产出**同构 `capture.json`**；④ 只改 3 处衔接：`run.py::capture_sku`、`webapp.py::CameraWorker`（生产 WebUI 采集台复用它）、`webui/__main__.py`（加 `--camera` 参数）；⑤ 设备差异落 `capture.json.camera`（新增 `kind`/`transit`/`camera_id`/`resource`，向后兼容）。手机设备：无 depth/IMU/shading/**exposure**（`gate_defaults={sharp:30,obj:0.08~0.97}`，D435i 保持 `{sharp:60,obj:0.40~0.92}`）。**已对齐生产 c161ecf**：生产给 `D435iCamera` 新增实时曝光控制（EV/增益/自动曝光），`D435iDevice` 需透传 `exposure_controls`/`set_exposure_controls`；手机 `supports_exposure_control=False` 时 `CameraWorker.status()` 返回 `supports_exposure_control=false`，前端 `capture.html` 的 `setExposureSupported()` **置灰曝光控件（EV/增益/自动曝光/应用曝光）并保留曝光+亮度提醒**（`exposureActual` 显示"设备不支持曝光控制"，`brightnessHint` 照常按亮度评估更新）——优雅降级而非整块隐藏。**2026-09-04 已真机验证**：手机（`NABDU20512011233`）经 spyglass 拉帧跑通——`AndroidUsbDevice.open()`（POST config ≤/snap）+ `grab()` 得 `3072x3072`（请求 3264x2448 被 CameraX 映射方形），gate 手机阈值 `sharp=33.6/exp=0.0007/obj=35%` 通过；手机源产出**同构 `capture.json`** 后 `handoff`（下游关键模块）**零改动正常导出**。实现注意：`AndroidUsbDevice.open()` 在 `POST /config` 后需 `settle≈0.8s`（CameraX rebind 前 `/snap` 读到旧低清帧如 1456x1456→sharp<30 被拒）；`_adb_forward()` 需把 `tcp:4747:4747` 解析成 `adb forward tcp:4747 tcp:4747`（勿 `split(":")` 成 4 段）。**预览比例约定（2026-09-04）**：采集台实时预览容器 `.preview` **不要用固定 `aspect-ratio:16/9` + `object-fit:cover`**（会把 4:3 画面裁成 16:9），`<img id="live">` 用 `width:100%;height:auto;object-fit:contain` **让预览跟随服务端实际画面比例动态变化**（服务端 `_long_edge_resize` 后手机源为 4:3）；分辨率文本 `$('res')` 取自 `/info` 的 `d.color`（相机真实分辨率，如 `3072x4096`），非前端硬编码。**scrcpy 相机能力（2026-09-04 探索，非采集后端）**：`scrcpy --list-cameras` / `--list-camera-sizes` 能**一次性列出各镜头 id/facing/最大尺寸/fps/变焦范围/合法尺寸清单**，是「新机型按实测重标定 gate 阈值」的高效探测工具（比逐档试 spyglass config 快）；但**无「按需抓单帧」接口**（只有 `--record` 整段录像 + 镜像窗口），故**暂不作为采集后端**，仍用 spyglass `GET /snap`。camera mirroring 需 **Android 12+**。**注意 scrcpy 与 spyglass 的相机 id 映射不同**（scrcpy 报 `0/1/2/3/4/6`，spyglass 报 `0..5`），逻辑 id 不能跨工具复用。完整记录见 `docs/spec-multi-camera.md` 附录 A。

- `python -m pipeline.capture [--with-barcode]`：多档位手转采集（空格拍/s 跳过/q 结束；`--with-barcode` 会话开头拍条码）。
- `python -m pipeline.capture --calibrate-shading`：标定平场 LUT（换机/换布光才重跑）。
- `python -m pipeline.capture.handoff_main <capture_dir>`：T4 导出到 incoming。
- `python -m pipeline.capture.webapp`：本地采集页面（实时预览 + 点击采集一张）。
- `python -m pipeline.capture.batch_main <batch>`：批次盘点 + 导出 ready 到 incoming。
- `python -m pipeline.queue <batch> --provider mock`：生成队列（mock 跑通）。
- `python -m pipeline.validate <model.glb> --size-mm W,H,D`：模型校验。
- `python -m pipeline.archive <sku> <batch> --category <cat>`：归档到目录。
- `python -m pipeline.webui --batch <b> [--no-camera] [--camera d435i|android_usb] --provider mock|auto`：生产 WebUI（多工作台，含 SKU 详情·图审 / 后台异步生成；`--camera` 选采集设备，默认 `d435i`，`android_usb` 走手机 spyglass）。
- WebUI 内：看板 `/`、采集台 `/capture`（含「① SKU 条码采集」→ 读条码/锁定）、SKU 详情·图审 `/sku/<id>`（图审→填 `size_mm`→「⚙️ 提交生成」后台异步）、3D 生成 `/generate`（勾选→提交→版本）、校验·矫正 `/validate`（verdict/尺寸偏差/一键矫正/归档/重新生成）、查看·尺寸调整 `/viewer3d`、归档 `/archive`。
- `python -m pipeline.scan --host 0.0.0.0 --port 5070 --https`：独立 SKU 扫码服务（安卓手机当摄像头）。**手机相机需 HTTPS**：用 `https://<电脑IP>:5070/scan`，不要 `http://`。自签名证书自动生成于 `.p3d/cert/`，含局域网 IP SAN。`scripts/start_scan.ps1 -Https` 一键启动。
- `scripts/start_webui.ps1` / `scripts/demo_e2e.ps1`：一键启动 / 一键全链路演示。
- `pytest`：单元测试（validate/preprocess/shading/queue/batch/webui-state/barcode/scan）。
