# 多设备采集适配 Spec（D435i + 安卓 USB 相机）

对齐日期：2026-09-04（**已基于生产基线 c161ecf 修订**，未动实现）。  
上游：[spec-capture.md](./spec-capture.md)（D435i 定机位 + 手转）、[零售多SKU图生3D全流程方案.md](../零售多SKU图生3D全流程方案.md)。  
实验依据：`experiments/android_usb_cam/`（阶段0，实测 spyglass 拉帧 + 修复 `POST /config` 500）。  
生产基线：`c161ecf`（`feat: 生产适配：增强相机曝光控制与图像删除功能`，**生产已部署**）。  
下游：`pipeline/preprocess`、`pipeline/hunyuan`、`pipeline/validate`、`pipeline/archive`（**对采集设备无感**）。

> 本文件**只定契约与方案，不含实现**。目标：**保留 D435i，同时接入手机（安卓 USB）相机，并可扩展到多类采集设备**。实现按「单模块闭环」推进，先抽象设备协议，再改两个衔接点。
> **（2026-09-04 修订说明）**：生产代码 c161ecf 已给 `D435iCamera`/`CameraWorker` 新增**实时曝光控制**（EV/增益/自动曝光，见 `exposure_controls`/`set_exposure_controls`）与图像删除/平场重标定。本方案据此新增 `supports_exposure_control` 到 `DeviceCapabilities`（D435i=true，手机=false），`D435iDevice` 透传曝光方法。手机源（`supports_exposure_control=false`）的优雅降级**已落地为「置灰曝光控件并保留曝光/亮度提醒」**：`CameraWorker.status()` 返回 `supports_exposure_control=false`，前端 `capture.html` 的 `setExposureSupported()` 置灰 EV/增益/自动曝光/应用曝光，同时 `exposureActual` 显示"设备不支持曝光控制"、`brightnessHint` 照常按亮度评估更新（而非整块隐藏面板）。

## 0. 目标与边界

**要做的**：
1. 抽象出「采集设备」统一接口，让 `capture` 模块不绑定 D435i。
2. 接入手机（spyglass via USB/ADB 隧道），作为一个新设备实现。
3. 生产 WebUI / CLI 能按参数选择设备（`d435i | android_usb`；`both` 规划中）。
4. **保留 D435i 现有能力**（深度、IMU、平场校正、WB 冻结）不动。

**不做的（本阶段）**：
- 不改 `handoff` / `preprocess` / `hunyuan` / `validate` / `archive` —— 它们已对设备无感（见 §5 论证）。
- 不做相机热切换 / 多机同拍拼接 / 深度对齐到手机。
- 不为手机源引入 Bayer / 裸流（保留厂商 ISP 校色）。
- 不把手机当**主路径**，只作为 D435i 高分辨率 RGB 的补充进采集链路。

## 1. 关键事实（为什么可以在设备层解耦而不动下游）

现有下游对所有设备特性**无感**，因为交接契约只依赖这几样：

| 事实 | 出处 |
|---|---|
| `handoff` 只读 `capture.json` 的 `frames[].color` + `ok`，不关心 `camera` 字段 | `pipeline/capture/handoff.py::handoff_sku` |
| `preprocess` 只认 `incoming/` 里的 `SKU_0X.jpg`，不关心来源 | `pipeline/preprocess/run.py` |
| `capture.json` 的 `camera` 字段是自由 dict | `capture/run.py` + `capture_and_validate.py`（实验已复刻） |
| spec-capture 已预留「**再加一台高分辨率 RGB，采集包加 `camera=rgb_dslr`，交接格式不变**」 | `docs/spec-capture.md` §2.1 |

**结论**：只要新设备产出**同构的** `capture.json`（含 `frames[].color`），整条下游零改动。设备特有的元数据（深度/IMU/平场/朝向/镜头信息）都塞进 `capture.json.camera` 与 `camera.json`，不对外暴露。

## 2. 统一设备协议（CaptureDevice）

这是本次方案的核心抽象。**目标接口形态**（对齐现有 `D435iCamera` 的 `open/grab/close` + `list_devices`）：

### 2.1 设备能力描述（dataclass，纯数据类型，不散落 SDK 类型）

```python
# pipeline/capture/device_base.py（新建，仅类型 + 协议，无 SDK）
@dataclass
class DeviceCapabilities:
    kind: str                 # "d435i" | "android_usb" | ...
    model: str                # 展示名：RealSense D435I / Android Front Main
    has_depth: bool           # 是否产 depth（手机为 False）
    has_imu: bool             # D435i 有；手机无
    supports_shading: bool    # 平场校正是否支持（D435i 有 LUT；手机无，用厂商 ISP）
    supports_exposure_control: bool  # 是否支持实时曝光控制（EV/增益/自动曝光）。D435i 有（生产 c161ecf 新增）；手机 spyglass 无 → false
    max_resolution: tuple[int, int]  # (w, h) 推荐（D435i 1920x1080；手机 3072x3072）
    color_controls: dict      # 该设备能控制的色彩项（WB/AE/增益/曝光）
    gate_defaults: dict       # 该设备的 gate 阈值默认（sharp/exp/obj 上下限）
```

> ⚠️ **基于生产基线 c161ecf 的修正**：生产代码（`feat: 生产适配：增强相机曝光控制与图像删除功能`）给 `D435iCamera` 新增了 `exposure_controls()` / `set_exposure_controls()`（实时读/设曝光、增益、自动曝光，带 EV 逻辑）。因此 `DeviceCapabilities` 增加 `supports_exposure_control`：D435i=`true`，手机=`false`。`CameraWorker` 检测到 `false` 时前端 `setExposureSupported(false)` **置灰曝光控件并保留曝光/亮度提醒**，优雅降级。

### 2.2 协议（ABC）

```python
class CaptureDevice(ABC):
    capabilities: DeviceCapabilities

    @abstractmethod
    def open(self) -> CameraInfo: ...
    @abstractmethod
    def grab(self, *, index: str, yaw_deg: int) -> FrameBundle: ...
    @abstractmethod
    def close(self) -> None: ...
    def __enter__(self): ...   # 见现有 D435iCamera
    def __exit__(self, *exc): ...

    # 选配（D435i 有，手机无 → 默认 null-safe）
    def shading_lut_path(self) -> Path | None: return None
    def read_imu(self) -> dict: return {}
    def exposure_controls(self) -> dict | None: return None   # D435i 有；手机无
    def set_exposure_controls(self, *, auto_exposure, exposure=None, gain=None) -> dict | None: return None
```

> `CameraInfo` / `FrameBundle` **复用现有定义**（`pipeline/capture/camera.py`）：`CameraInfo`（model/serial/firmware/color/depth/intrinsics/depth_scale/imu）、`FrameBundle`（color/yaw_deg/index/depth/ts_ns）。手机设备的 `CameraInfo.depth` 记 `"disabled"`、`depth_scale=1.0`、`imu={}`。

### 2.3 工厂（消除 `run.py`/`webapp.py` 里的硬编码）

```python
# pipeline/capture/device_factory.py（新建）
def make_capture_device(kind: str, *, device_id: str | None = None, **opts) -> CaptureDevice:
    """kind: 'd435i' | 'android_usb'。返回对应设备实例。"""
    if kind == "d435i":
        return D435iDevice(serial=device_id, **opts)          # 薄包现有 D435iCamera
    if kind == "android_usb":
        return AndroidUsbDevice(device_id=device_id, **opts)  # 新实现，走 spyglass
    raise ValueError(f"unknown capture device kind: {kind}")

def list_capture_devices(kind: str | None = None) -> list[dict]:
    """按 kind 枚举已连接设备（d435i 走 RealSense context；android_usb 走 adb devices）。"""
```

### 2.4 设备选择优先级（用户决策的默认）

| 参数 | 含义 | 默认 |
|---|---|---|
| `--camera d435i` | 只认 RealSense（现状） | 当前 |
| `--camera android_usb` | 只认手机 USB（spyglass），kind=`android_usb` | 已实现 |
| `--camera both` | 优先 D435i，缺 D435i 时回退手机 | 规划中（未实现） |

> `both` 的排序规则：先 D435i（有 depth+IMU，是主工位相机），D435i 未连接时用手机。明确**不自动混用**——一次采集会话只绑定一个设备，避免 depth 与手机 RGB 流不对齐。（注：kind 枚举值实际为 `d435i | android_usb`，见 `pipeline/capture/device.py::VALID_KINDS`，`both` 尚未实现。）

## 3. 两个实现

### 3.1 `D435iDevice`（改造最小，包装现有 `D435iCamera`）

```python
class D435iDevice(CaptureDevice):
    def __init__(self, *, serial=None, color_res=(1920,1080), depth_res=(1280,720),
                 fps=30, enable_depth=True, tilt_deg=0, color_controls=None, **kw):
        self._cam = D435iCamera(serial=serial, color_res=color_res, depth_res=depth_res,
                                fps=fps, enable_depth=enable_depth, tilt_deg=tilt_deg,
                                color_controls=color_controls)
        self.capabilities = DeviceCapabilities(
            kind="d435i", model="RealSense D435I", has_depth=True, has_imu=True,
            supports_shading=True, supports_exposure_control=True, max_resolution=(1920, 1080),
            color_controls={"wb": True, "exposure": True, "gain": True, "ae": True, "awb": True},
            gate_defaults={"min_sharpness": 60, "max_exposure": 0.05,
                           "min_object_ratio": 0.40, "max_object_ratio": 0.92},
        )
    def open(self): return self._cam.open()
    def grab(self, *, index, yaw_deg): return self._cam.grab(index=index, yaw_deg=yaw_deg)
    def close(self): return self._cam.close()
    def __enter__(self): self._cam.__enter__(); return self
    def __exit__(self, *exc): return self._cam.__exit__(*exc)
    # 生产 c161ecf 新增：透传实时曝光控制（EV/增益/自动曝光）。手机源不支持 → AndroidUsbDevice 返回 None。
    def exposure_controls(self): return self._cam.exposure_controls()
    def set_exposure_controls(self, *, auto_exposure, exposure=None, gain=None):
        return self._cam.set_exposure_controls(auto_exposure=auto_exposure, exposure=exposure, gain=gain)
    # read_imu / shading_lut_path 由 capture_sku 从 capabilities + camera.json 相机字段取（见 §3.3）
```

> 不动 `camera.py`；仅新增包装，避免破坏现有 import（`run.py`、`webapp.py`、`webui/app.py` 都 import 了 `D435iCamera`）。

### 3.2 `AndroidUsbDevice`（新实现，走 spyglass）

**依赖**：`experiments/android_usb_cam/` 已验证的 `receive.py` 相机 helpers + 修复后的 spyglass（`POST /config` 可控、`/snap` 拉帧、`/status.cameras`）。

```python
class AndroidUsbDevice(CaptureDevice):
    def __init__(self, *, device_id=None, base_url="http://127.0.0.1:4747",
                 camera_id=0, resolution="3264x2448", fps=15, jpeg_quality=95,
                 adb="adb", adb_forward="tcp:4747:4747", **kw):
        self.capabilities = DeviceCapabilities(
            kind="android_usb", model=f"Android cam{camera_id}",
            has_depth=False, has_imu=False, supports_shading=False, supports_exposure_control=False,
            max_resolution=(3072, 3072),
            color_controls={"wb": False, "exposure": False, "gain": False, "ae": True, "awb": True},
            gate_defaults={"min_sharpness": 30, "max_exposure": 0.08,
                           "min_object_ratio": 0.08, "max_object_ratio": 0.97},
        )
    def open(self) -> CameraInfo:
        self._adb_forward()                  # adb forward tcp:4747 tcp:4747
        self._post_config({camera_id, resolution, fps, jpegQuality})  # 修复后生效
        info = self._status()                # /status 拿电池/相机
        return CameraInfo(model=..., color=f"{w}x{h}", depth="disabled", depth_scale=1.0, imu={}, intrinsics=self._fake_intrinsics())
    def grab(self, *, index, yaw_deg) -> FrameBundle:
        jpg = self._snap()                   # GET /snap（读最近帧，≈0.03~0.08s）
        return FrameBundle(color=decode(jpg), yaw_deg=yaw_deg, index=index, depth=None, ts_ns=now_ns())
    def close(self): pass                    # 无独占资源可释放（服务在手机端）
```

**关键设计点**（手机设备与 D435i 的差异）：
- **无 depth**：`FrameBundle.depth=None`、`capture.json` 的 frame `depth=null`、`camera.json` 的 depth 记 `"disabled"`。
- **无 IMU**：`CameraInfo.imu={}`。
- **无平场校正 LUT**：`shading_lut_path()=None`；`capture_sku` 对 `supports_shading=False` 跳过 shading，依赖厂商 ISP 校色（实测 B/R≈0.95 无偏色 ✓）。
- **无曝光控制**：`supports_exposure_control=False`，`exposure_controls()`/`set_exposure_controls()` 返回 `None`；`CameraWorker` 检测到 `false` 时前端 `setExposureSupported(false)` **置灰曝光控件（EV/增益/自动曝光/应用曝光）**，但 `brightnessHint` 亮度提醒仍照常显示（生产新增功能的优雅降级，**非整块隐藏**）。
- **内参**：手机拿不到真实相机内参（spyglass 不曝光），`Intrinsics` 用占位（如 fx=fy=宽，cx=cy=中心）并标记 `approx=true`——深度/尺度估计不需要，标注即可。
- **gate 阈值**：用 `capabilities.gate_defaults`（手机 sharp=30 / obj=0.08~0.97），与 D435i 的默认不同——这正是「新源按实测重标定 gate」的落地。
- **帧率/质量**：`fps=15`（静态图够）、`jpeg_quality=95`（实测更清晰）。

### 3.3 设备特性如何落到 `capture.json` / `camera.json`

设备差异不能漏——`capture_sku` 在写包时按设备能力填字段（手机部分来自 `capabilities` + 实测）：

```python
capture_json["camera"] = {
    "model": cam_info.model,
    "serial": cam_info.serial or device_id,
    "color": cam_info.color,                  # "2448x2448" / "3072x3072"（实际输出）
    "kind": caps.kind,                        # 新增：d435i | android_usb
    "tilt_deg": opts.tilt_deg,
    "color_controls": {...} if caps.supports_color else None,   # 手机 → None
    "shading_lut": str(lut_path) if caps.supports_shading else None,
    "transit": "USB/ADB tunnel" if android else "USB3/RealSense",
    "camera_id": camera_id if android else None,   # 手机镜头 id（0=主摄）
    "resource": {"resolution": "3264x2448", "fps": 15, "jpeg_quality": 95},  # 手机配置
}
```

> `kind` 是新增字段，向后兼容（旧包无它，下游不读）。**下游仍只认 `frames[].color`**，`camera.kind` 仅供溯源。

## 4. 衔接点（实现时的改动清单）

**只动这 3 处，其余零改动**：

### 4.1 `pipeline/capture/run.py::capture_sku`
- 删掉 `D435iCamera(...)` 硬编码，改 `dev = make_capture_device(opts.camera_kind, serial=opts.serial, ...)`。
- `CaptureOptions` 加 `camera_kind: str = "d435i"`（默认保持现状）。
- 设备能力驱动：`if caps.has_depth ... else depth=None`；`if caps.supports_shading ... else skip LUT`；gate 阈值默认取 `caps.gate_defaults`（仍可被 CLI 覆盖）。

### 4.2 `pipeline/capture/webapp.py::CameraWorker`
- `CameraWorker.__init__` 改用 `make_capture_device(opts.camera_kind, ...)` 替代 `with D435iCamera(...)`。
- `WebOptions` 加 `camera_kind: str = "d435i"`、`android: AndroidUsbOptions | None = None`。
- 预览 `/stream` 与 `/capture` 逻辑不变（都走 `dev.grab`）。
- **曝光控制透传与降级（生产 c161ecf 新增）**：`_apply_exposure_request(cam: D435iCamera, ...)` 改泛型 `cam: CaptureDevice`；当 `dev.capabilities.supports_exposure_control == False`（手机源）时，`request_exposure` 返回「该设备不支持曝光控制」，前端置灰曝光控件并保留曝光/亮度提醒（`setExposureSupported(false)`）。`register_sku`/`recalibrate_from_live` 逻辑不变（手机源无 shading，跳过 recalibrate）。

### 4.3 生产 WebUI 启动参数（`pipeline/webui/__main__.py`）
- `--camera` 加选项 `d435i | android_usb`（映射到 `WebOptions.camera_kind`；`both` 规划中）。
- `start_camera` 时用 `make_capture_device`；`both` 时按「D435i 优先、缺则手机」选。

> 附：`capture/cli.py`、`webapp_main.py`、`batch_main.py` 若直接调用 `list_devices()`，也需要感知 `kind`；具体在实现时按调用链逐一核对。

## 5. 下游零改动论证（把 §1 复习成可执行断言）

| 下游模块 | 依赖采集设备吗 | 依据 |
|---|---|---|
| `handoff` | 否 | 只读 `frames[].color`+`ok` |
| `preprocess` | 否 | 只认 `incoming/SKU_0X.jpg` |
| `hunyuan` | 否 | 只认 `api/` 压缩图 |
| `validate` | 否 | 只认 GLB/OBJ |
| `archive` | 否 | 只拷 work/ + 原图 |

因此**实现时只需验证**：手机源 `handoff → preprocess` 跑通（实验已证明），且 `api_bytes ≤8MB`（实测 1.5MB ✓）。

## 6. 待定项 / 下一步（明天实施前需确认）

1. **手机深度缺口的取舍**：`capture.json` frame `depth=null` 即可，还是需要把 D435i 深度与手机 RGB 对齐？（本方案先按「无 depth」处理，纯 RGB 拍照场景足够。）
2. **内参占位标记**：手机 `Intrinsics` 用近似值 + `approx=true`，是否影响下游尺度估计？（validate/archive 用 `meta.json` 的 `size_mm`，不依赖内参 → 应无影响。）
3. **设备发现**：`both` 时如何确认 D435i 是否在线（`list_devices()` 非空）+ 手机是否在线（`adb devices` 出现 `device` 态）。实现时先做 CLI `--list-devices` 扩展。
4. **`experiments/android_usb_cam/` 是否入库**：当前被根 `.gitignore` 排除。若要把这套手机设备实现纳入主项目 `pipeline/capture/`，需把 `AndroidUsbDevice` 放进 `pipeline/`（实验脚本留 experiments），并考虑是否让 `vendor/spyglass` 随仓库或仅作本地构建。

## 7. 验收标准（明天实现后的最小闭环）

1. **保留 D435i**：`python -m pipeline.capture --camera d435i` 行为与现在完全一致（深度/IMU/shading 都在）。
2. **手机可用**：`python -m pipeline.capture --camera android_usb` 能连 spyglass 拉帧、写同构 `capture.json`。
3. **下游无感**：手机源 `handoff → preprocess` `verdict=ok`，`api_bytes ≤8MB`，04/06 不上传。
4. **gate 按设备**：手机源用 `sharp=30/obj=0.08~0.97`，D435i 用 `sharp=60/obj=0.40~0.92`，互不串扰。
5. **生产 WebUI**：`--camera d435i|android_usb` 均能启动对应采集台（`both` 为规划项）。

---

## 附录 A：候选设备后端 scrcpy（探索记录，2026-09-04）

> **结论先行**：scrcpy（v4.1）的 `--video-source=camera` 能**列出相机 / 合法分辨率 / fps / 变焦范围**，这些是其相对 spyglass 的关键优势。但当前**无「按需抓单帧」接口**（只有 `--record` 整段录像 + 镜像窗口），不像 spyglass 那样 `GET /snap` 可单帧取 JPEG。因此本管线**暂不把 scrcpy 作为采集后端**（保留 spyglass 作为 F 级适配器），但把它当作**相机能力探测工具**（`--list-camera-sizes`）很有价值——可作为「新输入源按实测重标定 gate 阈值」时的快速探测手段。若未来要做**视频/多视角连续采集**，scrcpy `--record`（MP4）可作为备选。

### A.1 为什么探索它
主路径手机相机经 spyglass（HTTP GET/POST）+ ADB 隧道拉帧。但 spyglass 对**相机枚举**能力弱（不暴露逐镜头合法尺寸清单），且我们此前网格实验只能靠「逐 id 试 config + 看输出」摸索。scrcpy 用 CameraX 的 Camera2 完整接口枚举，能一次性列出全部信息，正好补上这块盲区。

### A.2 实测环境
- scrcpy `4.1`（WinGet 安装，`Genymobile.scrcpy`），SDL 3.4.12 / libavcodec 62.28.102。
- 设备：`NABDU20512011233`（HUAWEI ANA-AN00，Android 12，USB 连接）。

### A.3 关键能力（help 相关选项）

| 选项 | 作用 | 备注 |
|---|---|---|
| `--list-cameras` | 列出设备相机（id / facing / 最大尺寸 / fps / zoom 范围） | **互补 spyglass 的最大价值点** |
| `--list-camera-sizes` | 列出每个相机的合法尺寸清单 | 同镜头全分辨率，便于选档 |
| `--camera-id=id` | 指定相机 id（配 `--video-source=camera`） | |
| `--camera-fps=value` | 指定采集帧率 | 可选值见 `--list-cameras` 的 `fps={...}` |
| `--camera-size=WxH` | 指定明确采集尺寸 | 必须是 `--list-camera-sizes` 的合法项 |
| `--camera-facing=facing` | 按方向选（front/back/...） | |
| `--camera-ar=value` | 按宽高比选（`sensor` 或比例） | ±10% 容忍 |
| `--camera-zoom=value` / `--camera-torch` | 变焦初值 / 开补光灯 | |
| `--camera-high-speed` | 高速采集模式 | 需设备/镜头支持 |
| `--video-source=source` | `display` 或 `camera` | **camera mirroring 需 Android 12+** |
| `--record=file.mp4` | 录像到文件（配 `--record-format`） | 当前无「单帧抓取」接口 |
| `-r` 别名 | 同上 | |

### A.4 实机相机清单（`--list-cameras`，HUAWEI ANA-AN00）

```
--camera-id=0    (back, 8192x6144, fps={15,20,24,25,30,60}, zoom-range=[1,10])
--camera-id=1    (front, 6528x4896, fps={15,20,24,25,30,60}, zoom-range=[1,6])
--camera-id=2    (back, 8192x6144, fps={15,20,24,25,30}, zoom-range=[1,10])
--camera-id=3    (front, 6528x4896, fps={15,20,24,25,30,60}, zoom-range=[1,6])
--camera-id=4    (back, 4608x3456, fps={15,20,24,25,30}, zoom-range=[1,10])
--camera-id=6    (back, 3264x2448, fps={15,20,24,25,30}, zoom-range=[1,10])
```

**与 spyglass 网格实验对比**：两者都枚举出 6 个逻辑相机，但 `id` 映射不同（scrcpy 用 `0/1/2/3/4/6`，spyglass 报 `0..5`）。**两个工具暴露的相机集合不一致**——不同 app 走不同 CameraX/Camera2 组合，导致逻辑 id 错位。**结论：相机 id 不能跨工具复用**；要采集某镜头，必须在所用工具内部按其枚举结果选。

### A.5 实机合法分辨率（`--camera-id=6` back 3264x2448 的完整清单）

```
4096x3072, 4096x2304, 4096x1888, 3648x2056, 3648x1680, 3648x2056,
3072x3072, 3840x2160, 3840x1760, 3840x1648, 3120x2340, 2560x1080,
3280x2448, 3264x2448, 3264x1840, 3008x2256, 2448x2448, 2336x1080,
2048x1536, 1920x1080, 1552x720, 1440x1080, 1456x1456, 1664x768,
1440x720, 1280x960, 1280x720, 1088x1080, 960x720, 960x540,
720x720, 720x540, 640x480
```

**印证此前结论**：包含 `3072x3072`、`2448x2448`、`1456x1456` 等**方形档**（CameraX 就近映射成方形），也保留 `3264x2448`（视频流默认 4:3）。**注意 `3072x4096`（我们 push 的 3:4 竖屏档）不在其列**——scrcpy 走 CameraX 会映射到 `3072x3072`；这与 spyglass 把 `3264x2448` 映射成 `3072x3072` 的行为一致（**竖屏 3:4 高分档偏不被 CameraX 保真**）。若确实要竖屏 3:4，需自研 App 走 Camera2 `TakePicture`（已在 spec 立项，未并入主流程）。

### A.6 对管线的影响与后续定位

- **不作为采集后端**：spyglass 的 `GET /snap` 单帧抓取 + `POST /config` 动态配置，仍是最贴合「静态图采集台」的后端；scrcpy 无对应单帧接口。
- **作为能力探测工具**：接新机型时，先用 `scrcpy --list-camera-sizes` 一次性拿到「合法尺寸清单 + 最高分辨率 + fps」，据此定 `--camera-id`/`--camera-size` 与 gate 阈值，比逐档试 spyglass config 高效。
- **VideoSource 前瞻**：`capture.json` 若未来扩展 `camera.transit` 或新增 `burst/video` 采集，scrcpy `--record` 可产出 MP4 作为连续采样源；届时需在 `_do_capture` 之外新增「从 MP4 抽帧」路径。
- **前提约束**：camera mirroring 需 **Android 12+**（本机 OK）。若面向低版本机型，scrcpy 不可用，仍需 spyglass。
- **跨相机 id 不可复用**（见 A.4），接多机时应**以各工具内部枚举为准**，勿假设 `cam0` 恒为主摄。

### A.7 一句话给后续开发者
> 想快速知道某台手机相机有哪些镜头/能开多大分辨率 → 用 `scrcpy --list-cameras` / `--list-camera-sizes`；想按需抓单帧走 `spyglass`（`GET /snap`）。两者相机 id 映射不同，不能混用。
