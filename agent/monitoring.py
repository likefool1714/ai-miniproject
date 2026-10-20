"""모니터링 샘플 제공자. 기본 난수 데모와 명시적인 시나리오 재생을 분리한다."""
import json
import random
import re
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from .recovery import utc_now


class MonitoringSource:
    def __init__(self, data_dir: Path, scenario: str = "", clock=utc_now):
        self.data_dir, self.scenario, self.clock = data_dir, scenario, clock
        self.base = json.loads((data_dir / "monitoring.json").read_text())
        self.sequence = {}
        self.positions = {}
        self.epochs = {}
        self.run_id = uuid4().hex[:10]
        if scenario:
            if not re.fullmatch(r"[a-z0-9-]{1,40}", scenario):
                raise ValueError("MONITORING_SCENARIO는 등록된 시나리오 이름이어야 합니다.")
            self.sequence = json.loads((data_dir / "scenarios" / f"{scenario}.json").read_text())
            if not isinstance(self.sequence, dict) or not self.sequence:
                raise ValueError("시나리오는 서비스별 관측 배열이어야 합니다.")
            for system, steps in self.sequence.items():
                if system not in self.base or not isinstance(steps, list) or not steps:
                    raise ValueError("시나리오 대상 또는 관측 배열이 올바르지 않습니다.")

    def get(self, system_name: str, observation_context: str = "") -> dict:
        if system_name not in self.base:
            return {"system_name": system_name, "status": "unknown", "cpu_percent": 0,
                    "memory_percent": 0, "alerts": ["등록되지 않은 시스템"],
                    "last_checked": self.clock().isoformat(), "sample_id": uuid4().hex,
                    "data_source": "unknown-system"}
        if system_name in self.sequence:
            steps = self.sequence[system_name]
            key = (observation_context, system_name)
            epoch = self.epochs.setdefault(key, self.clock())
            position = min(self.positions.get(key, 0), len(steps) - 1)
            self.positions[key] = position + 1
            step = dict(steps[position])
            # 고정 시나리오의 관측 시각은 시작 이전에 배치한다. 빠른 재생도 미래 자료가 되지 않는다.
            age = step.pop("age_seconds", (len(steps) - 1 - position) * 10)
            return {**step, "system_name": system_name,
                    "last_checked": (epoch - timedelta(seconds=age)).isoformat(),
                    "sample_id": f"{self.run_id}:{system_name}:{position}",
                    "data_source": f"scenario:{self.scenario} (synthetic)"}
        source = self.base[system_name]
        jitter = random.uniform(-3, 8)
        return {"system_name": system_name, "status": source["status"],
                "cpu_percent": round(max(0, min(100, source["cpu_base"] + jitter)), 1),
                "memory_percent": round(max(0, min(100, source["memory_base"] + jitter / 2)), 1),
                "alerts": source.get("alerts", []), "last_checked": self.clock().isoformat(),
                "sample_id": uuid4().hex, "data_source": "sample-json-jitter (synthetic)"}
