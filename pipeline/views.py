from __future__ import annotations

from dataclasses import dataclass

# Manual yaw marks: index increases counterclockwise from the front (see spec-hunyuan-api.md).
# 04 / 06 have no Hunyuan slot and must not be uploaded.


@dataclass(frozen=True)
class ViewSlot:
    index: str
    degrees: int
    hunyuan_field: str | None
    required_for_submit: bool = False
    pose_name: str = ""


SLOTS: tuple[ViewSlot, ...] = (
    ViewSlot("01", 0, "ImageUrl", required_for_submit=True, pose_name="正面"),
    ViewSlot("02", 45, "left_front", pose_name="45°左前"),
    ViewSlot("03", 90, "left", pose_name="90°左"),
    ViewSlot("04", 135, None, pose_name="135°"),
    ViewSlot("05", 180, "back", pose_name="180°背"),
    ViewSlot("06", 225, None, pose_name="225°"),
    ViewSlot("07", 270, "right", pose_name="270°右"),
    ViewSlot("08", 315, "right_front", pose_name="315°右前"),
    ViewSlot("09", -1, "top", pose_name="顶视"),
    ViewSlot("10", -2, "bottom", pose_name="底视"),
)

SLOT_BY_INDEX = {s.index: s for s in SLOTS}
UPLOAD_SLOTS = tuple(s for s in SLOTS if s.hunyuan_field is not None)
