"""带哈希链的只追加流水。

多路读数、融合决策与故障事件各写一条只追加的 JSONL 流水，每条记录携带
``seq``、``prev_hash`` 与自身 ``hash``（规范 JSON 的 SHA256）。任一行被改、
被删或追加到一半，:meth:`HashChain.verify` 都会在断链处停下。三路流水与
落盘快照之间的业务一致性核对在 :mod:`cspplant.field.consistency`。
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from typing import Any

from ..errors import StorageError

GENESIS = "0" * 64


def canonical_hash(payload: dict[str, Any]) -> str:
    """计算一个载荷的规范 SHA256（排序键、无空白），不含链式字段。"""

    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


class ChainBroken(StorageError):
    """哈希链校验失败：行被篡改、缺失或追加不完整。"""


class HashChain:
    """一条 ``<root>/<topic>.jsonl`` 形式的哈希链。"""

    def __init__(self, root: str, topic: str) -> None:
        self._root = os.path.abspath(root)
        self._topic = str(topic)
        self._path = os.path.join(self._root, *self._topic.split("/")) + ".jsonl"
        self._lock = threading.RLock()
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        self._seq, self._head = self._recover_head()

    @property
    def topic(self) -> str:
        return self._topic

    @property
    def path(self) -> str:
        return self._path

    @property
    def head(self) -> str:
        """链尾哈希；空链为全 0。"""

        return self._head

    def _recover_head(self) -> tuple[int, str]:
        if not os.path.isfile(self._path):
            return 0, GENESIS
        seq = 0
        previous = GENESIS
        for line_no, raw in enumerate(self._iter_lines(self._path), start=1):
            record = self._load_line(raw, line_no)
            self._check_link(record, previous, line_no)
            seq = int(record["seq"])
            previous = record["hash"]
        return seq, previous

    def append(self, payload: dict[str, Any]) -> dict[str, Any]:
        """追加一条载荷，自动填 ``seq`` / ``prev_hash`` / ``hash``。"""

        with self._lock:
            record = dict(payload)
            record["seq"] = self._seq + 1
            record["prev_hash"] = self._head
            record["hash"] = canonical_hash(record)
            try:
                with open(self._path, "a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
            except OSError as error:
                raise StorageError(f"追加哈希链 {self._topic} 失败: {error}", component="store") from error
            self._seq += 1
            self._head = record["hash"]
            return record

    def read_all(self) -> list[dict[str, Any]]:
        """读取全部记录（不校验，校验请显式调用 :meth:`verify`）。"""

        if not os.path.isfile(self._path):
            return []
        return [self._load_line(raw, line_no) for line_no, raw in enumerate(self._iter_lines(self._path), 1)]

    def verify(self) -> dict[str, Any]:
        """全链重算哈希。

        通过时返回 ``{"ok": True, "count": n, "head": ...}；发现断链抛
        :class:`ChainBroken`，消息里带 topic、行号与不匹配的字段。
        """

        with self._lock:
            previous = GENESIS
            count = 0
            if os.path.isfile(self._path):
                for line_no, raw in enumerate(self._iter_lines(self._path), start=1):
                    record = self._load_line(raw, line_no)
                    self._check_link(record, previous, line_no)
                    previous = record["hash"]
                    count += 1
            return {"ok": True, "topic": self._topic, "count": count, "head": previous}

    def reset(self) -> None:
        """交付前清链（测试／演示用）。"""

        with self._lock:
            if os.path.isfile(self._path):
                os.remove(self._path)
            self._seq = 0
            self._head = GENESIS

    @staticmethod
    def _iter_lines(path: str):
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    yield line

    def _load_line(self, raw: str, line_no: int) -> dict[str, Any]:
        try:
            record = json.loads(raw)
        except json.JSONDecodeError as error:
            raise ChainBroken(f"{self._topic} 第 {line_no} 行不是合法 JSON", component="store") from error
        if not isinstance(record, dict) or not all(key in record for key in ("seq", "prev_hash", "hash")):
            raise ChainBroken(f"{self._topic} 第 {line_no} 行缺少链式字段", component="store")
        return record

    def _check_link(self, record: dict[str, Any], previous: str, line_no: int) -> None:
        if int(record["seq"]) != line_no:
            raise ChainBroken(
                f"{self._topic} 第 {line_no} 行序号不连续（记录为 {record['seq']}）",
                component="store",
            )
        if record["prev_hash"] != previous:
            raise ChainBroken(
                f"{self._topic} 第 {line_no} 行前向哈希不匹配（记录 {record['prev_hash'][:12]}…）",
                component="store",
            )
        stored = record["hash"]
        payload = {key: value for key, value in record.items() if key != "hash"}
        if canonical_hash(payload) != stored:
            raise ChainBroken(f"{self._topic} 第 {line_no} 行内容哈希不匹配", component="store")
