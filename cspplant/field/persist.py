"""镜场持久化。

镜场有两类需要跨重启保留的状态：多路辐照投票快照（追日必须按它来，不能用内存
里可能更新却没落盘的值）与清洗／检修窗口（重启后仍在窗口里的镜子要继续避让）。
两者都走 :class:`~cspplant.store.jsonfile.JsonStore`，文档路径固定，读取失败
按"文件不存在"处理。辐照快照带摘要，读回时重新计算，确保多路读数、投票结论与
落盘总辐照没有被拆开改写。
"""

from __future__ import annotations

import hashlib
import json

from ..store.jsonfile import JsonStore

IRRADIANCE_DOC = "field/irradiance"
WINDOW_DOC = "field/window"
IRRADIANCE_SCHEMA_VERSION = 3


def digest_snapshot(snapshot: dict) -> str:
    """计算辐照快照摘要；摘要字段本身不参与摘要。"""

    canonical = {key: value for key, value in snapshot.items() if key != "digest"}
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class FieldPersistence:
    """镜场状态的读写入口。"""

    def __init__(self, store: JsonStore) -> None:
        self._store = store

    @property
    def store(self) -> JsonStore:
        return self._store

    def write_irradiance(self, snapshot: dict) -> str:
        """落盘一次多路辐照投票快照，返回文件路径。"""

        stored = dict(snapshot)
        stored["schema_version"] = IRRADIANCE_SCHEMA_VERSION
        stored["digest"] = digest_snapshot(stored)
        return self._store.write(IRRADIANCE_DOC, stored)

    def read_irradiance(self) -> dict | None:
        """读回并校验辐照快照，旧版单表或校验失败都返回 ``None``。"""

        raw = self._store.read(IRRADIANCE_DOC)
        if not isinstance(raw, dict):
            return None
        version = raw.get("schema_version", 1)
        if version == 1:
            dni = raw.get("dni_w_m2")
            ts = raw.get("ts", 0.0)
            if not isinstance(dni, (int, float)) or isinstance(dni, bool) or not isinstance(ts, (int, float)):
                return None
            return {
                "schema_version": 1,
                "aggregate": {"dni_w_m2": float(dni), "ts": float(ts)},
                "readings": [],
                "verdict": {},
                "digest": "",
            }
        if version != IRRADIANCE_SCHEMA_VERSION:
            return None
        aggregate = raw.get("aggregate")
        readings = raw.get("readings")
        verdict = raw.get("verdict")
        if not self._valid_v3_payload(aggregate, readings, verdict):
            return None
        digest = str(raw.get("digest", ""))
        if not digest or digest != digest_snapshot(raw):
            return None
        return {
            "schema_version": IRRADIANCE_SCHEMA_VERSION,
            "aggregate": {
                "dni_w_m2": float(aggregate["dni_w_m2"]),
                "ts": float(aggregate["ts"]),
            },
            "readings": readings,
            "verdict": verdict,
            "digest": digest,
        }

    def _valid_v3_payload(self, aggregate: object, readings: object, verdict: object) -> bool:
        """校验 v3 快照的关键字段类型，避免坏文档进入控制链。"""

        if not isinstance(aggregate, dict) or not isinstance(readings, list) or not isinstance(verdict, dict):
            return False
        dni = aggregate.get("dni_w_m2")
        ts = aggregate.get("ts")
        if not isinstance(dni, (int, float)) or isinstance(dni, bool) or not isinstance(ts, (int, float)):
            return False
        for item in readings:
            if not isinstance(item, dict) or not isinstance(item.get("meter_id"), str):
                return False
            value = item.get("dni_w_m2")
            if value is not None and (not isinstance(value, (int, float)) or isinstance(value, bool)):
                return False
            if not isinstance(item.get("ts"), (int, float)) or not isinstance(item.get("valid"), bool):
                return False
            if not isinstance(item.get("reason", ""), str):
                return False
        list_fields = ("trusted_meters", "rejected_meters")
        for key in list_fields:
            value = verdict.get(key)
            if not isinstance(value, list) or not all(isinstance(part, str) for part in value):
                return False
        return isinstance(verdict.get("accepted"), bool) and isinstance(
            verdict.get("confidence"), (int, float)
        )

    def write_window(self, mirrors: list[str], zone: str, opened_at: float) -> str:
        """落盘当前清洗／检修窗口。"""

        return self._store.write(
            WINDOW_DOC,
            {
                "mirrors": list(mirrors),
                "zone": zone,
                "opened_at": round(float(opened_at), 3),
            },
        )

    def read_window(self) -> dict | None:
        """读回清洗／检修窗口。"""

        raw = self._store.read(WINDOW_DOC)
        if not isinstance(raw, dict):
            return None
        mirrors = raw.get("mirrors")
        if not isinstance(mirrors, list):
            return None
        return {
            "mirrors": [str(item) for item in mirrors],
            "zone": str(raw.get("zone", "")),
            "opened_at": float(raw.get("opened_at", 0.0)),
        }

    def clear_window(self) -> bool:
        """删除窗口文档，返回是否真的删掉了。"""

        return self._store.remove(WINDOW_DOC)

    def documents(self) -> list[str]:
        """返回镜场已经落盘的文档列表。"""

        return [
            doc
            for doc in self._store.list_ids()
            if doc in (IRRADIANCE_DOC, WINDOW_DOC)
        ]
