"""镜场持久化。

镜场有两类需要跨重启保留的状态：辐照度快照（追日必须按它来，不能用内存里
可能更新却没落盘的值）与清洗／检修窗口（重启后仍在窗口里的镜子要继续避让）。
两者都走 :class:`~cspplant.store.jsonfile.JsonStore`，文档路径固定，读取失败
按"文件不存在"处理。

多路辐照计表决另外维护四样东西：**已提交的融合快照**（原子文档，镜场只
认这一份）、**原始读数哈希链**、**融合决策哈希链**与**故障事件台账哈希链**。
可信度不足时不写死值，因此快照里没有 ``dni_w_m2`` 字段，只有
``status: "held"``；多路链与这份快照之间的核对见
:mod:`cspplant.field.consistency`。
"""

from __future__ import annotations

from typing import Any

from ..store.jsonfile import JsonStore
from .chain import HashChain

IRRADIANCE_DOC = "field/irradiance"
WINDOW_DOC = "field/window"

FUSION_DOC = "field/irradiance-fusion"
RAW_CHAIN = "field/irradiance-raw"
DECISION_CHAIN = "field/irradiance-decisions"
INCIDENT_CHAIN = "field/irradiance-incidents"


class FieldPersistence:
    """镜场状态的读写入口。"""

    def __init__(self, store: JsonStore) -> None:
        self._store = store
        self._raw_chain = HashChain(store.root, RAW_CHAIN)
        self._decision_chain = HashChain(store.root, DECISION_CHAIN)
        self._incident_chain = HashChain(store.root, INCIDENT_CHAIN)

    @property
    def store(self) -> JsonStore:
        return self._store

    def write_irradiance(self, dni_w_m2: float, ts: float) -> str:
        """落盘一次辐照度快照，返回文件路径。"""

        return self._store.write(
            IRRADIANCE_DOC,
            {"dni_w_m2": round(float(dni_w_m2), 3), "ts": round(float(ts), 3)},
        )

    def read_irradiance(self) -> dict | None:
        """读回落盘的辐照度快照，没有就返回 ``None``。"""

        raw = self._store.read(IRRADIANCE_DOC)
        if not isinstance(raw, dict):
            return None
        dni = raw.get("dni_w_m2")
        if not isinstance(dni, (int, float)):
            return None
        return {"dni_w_m2": float(dni), "ts": float(raw.get("ts", 0.0))}

    # ---- 多路融合：快照与哈希链 --------------------------------------

    def write_fusion(self, payload: dict[str, Any]) -> str:
        """原子落盘一份融合快照（提交或挂起）。"""

        return self._store.write(FUSION_DOC, payload)

    def read_fusion(self) -> dict | None:
        """读回融合快照，没有就返回 ``None``。"""

        raw = self._store.read(FUSION_DOC)
        return raw if isinstance(raw, dict) else None

    def append_raw_readings(self, payload: dict[str, Any]) -> dict:
        """往原始读数哈希链追加一行。"""

        return self._raw_chain.append(payload)

    def append_decision(self, payload: dict[str, Any]) -> dict:
        """往融合决策哈希链追加一行。"""

        return self._decision_chain.append(payload)

    def append_incident(self, payload: dict[str, Any]) -> dict:
        """往故障事件台账哈希链追加一行。"""

        return self._incident_chain.append(payload)

    def fusion_chains(self) -> dict[str, HashChain]:
        """返回三路哈希链（供一致性核对与控制台展示）。"""

        return {
            "raw": self._raw_chain,
            "decisions": self._decision_chain,
            "incidents": self._incident_chain,
        }

    def reset_fusion(self) -> None:
        """清掉融合快照与三路哈希链（交付前重新标定用）。"""

        self._store.remove(FUSION_DOC)
        for chain in self.fusion_chains().values():
            chain.reset()

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
