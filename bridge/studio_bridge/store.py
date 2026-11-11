"""On-disk state: the mesh network (keys, lights, sequence number)."""

from __future__ import annotations

import json
import os
import secrets
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

#: A node drops any PDU whose sequence number it has already seen, so the
#: counter must only ever go up. Each time the bridge starts it resumes from
#: the stored value, which is kept ahead of what was really sent.
SEQUENCE_BLOCK = 256


def write_json_atomic(path: Path, data: Any, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w") as handle:
        json.dump(data, handle, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


@dataclass
class LightRecord:
    id: str
    name: str
    model: str
    ble_address: str
    node_address: int
    num_elements: int = 2
    device_key: str = ""


@dataclass
class MeshStore:
    """The mesh network this bridge owns. Holds secret keys: mode 0600."""

    path: Path
    network_key: str = ""
    app_key: str = ""
    iv_index: int = 0
    provisioner_address: int = 1
    sequence_number: int = 0
    lights: list[LightRecord] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> MeshStore:
        store = cls(path=path)
        if path.exists():
            raw = json.loads(path.read_text())
            store.network_key = raw.get("network_key", "")
            store.app_key = raw.get("app_key", "")
            store.iv_index = int(raw.get("iv_index", 0))
            store.provisioner_address = int(raw.get("provisioner_address", 1))
            store.sequence_number = int(raw.get("sequence_number", 0))
            store.lights = [LightRecord(**item) for item in raw.get("lights", [])]
        return store

    def ensure_keys(self) -> None:
        if not self.network_key:
            self.network_key = secrets.token_hex(16)
        if not self.app_key:
            self.app_key = secrets.token_hex(16)

    def save(self) -> None:
        write_json_atomic(
            self.path,
            {
                "network_key": self.network_key,
                "app_key": self.app_key,
                "iv_index": self.iv_index,
                "provisioner_address": self.provisioner_address,
                "sequence_number": self.sequence_number,
                "lights": [asdict(light) for light in self.lights],
            },
        )

    def get(self, light_id: str) -> LightRecord | None:
        return next((light for light in self.lights if light.id == light_id), None)

    def next_node_address(self) -> int:
        """First unicast address above every node's element range."""

        highest = self.provisioner_address
        for light in self.lights:
            highest = max(highest, light.node_address + max(1, light.num_elements) - 1)
        return highest + 1

    def reserve_sequence(self, used: int) -> None:
        """Keep the stored counter a block ahead of the one in use."""

        if used >= self.sequence_number:
            self.sequence_number = (used // SEQUENCE_BLOCK + 1) * SEQUENCE_BLOCK
            self.save()
