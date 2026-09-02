from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

Verdict = Literal["ok", "warn", "fail"]


@dataclass
class Check:
    id: str
    ok: bool
    detail: str = ""
    label: str | None = None


@dataclass
class ModuleReport:
    module: str
    verdict: Verdict
    input: str
    output: str | None = None
    labels: list[str] = field(default_factory=list)
    checks: list[Check] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def add(self, check: Check) -> None:
        self.checks.append(check)
        if not check.ok and check.label and check.label not in self.labels:
            self.labels.append(check.label)

    def finalize(self) -> None:
        if any(not c.ok and c.label in {"download_error", "geometry_error", "texture_error"} for c in self.checks):
            self.verdict = "fail"
        elif any(not c.ok for c in self.checks):
            self.verdict = "warn"
        else:
            self.verdict = "ok"

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        return payload

    def write(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        return path
