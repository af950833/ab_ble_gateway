from __future__ import annotations
from datetime import datetime, timezone
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.restore_state import RestoreEntity
from .const import DOMAIN
from .mqtt_client import AbBleMqtt

class AbBleEntity(RestoreEntity):
    _attr_should_poll = False

    def __init__(self, client: AbBleMqtt, key: str, name: str):
        self._client = client
        self._key = key
        self._attr_unique_id = f"{DOMAIN}:{key}"
        self._attr_name = name

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(identifiers={(DOMAIN, self._key)}, name=self.name, manufacturer="AB BLE", model="Gateway Beacon")

    def last_seen_seconds(self) -> float:
        ts = self._client.last_seen(self._key)
        if ts is None:
            return 1e9
        return max(0.0, datetime.now(timezone.utc).timestamp() - ts)
