from __future__ import annotations
from typing import Dict, Any
import re

from homeassistant.core import HomeAssistant, callback
from homeassistant.config_entries import ConfigEntry
from homeassistant.components.device_tracker.const import SourceType
from homeassistant.components.device_tracker.config_entry import TrackerEntity
from homeassistant.const import STATE_HOME, STATE_NOT_HOME
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .const import (
    DOMAIN,
    BLE_PREFIX,
    CONF_AUTO_LEARN,
    CONF_IDLE_TIMEOUT,
    DEFAULT_IDLE_TIMEOUT,
    CONF_PRELOAD_KEYS,
    CONF_PRELOAD_IBEACON,
)
from .mqtt_client import AbBleMqtt, SIGNAL_PACKET, Packet
from .entity import AbBleEntity

KEY_RE = re.compile(r"^(IBC_[0-9A-F]{32}(?:[0-9A-F]{8})?|EDS_[0-9A-F]+|[0-9A-F]{2}(?::[0-9A-F]{2}){5})$", re.I)
HEX_RE = re.compile(r"^[0-9A-F]{1,4}$", re.I)

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities):
    client: AbBleMqtt = hass.data[DOMAIN][entry.entry_id]
    data = entry.data
    opts = entry.options

    auto = opts.get(CONF_AUTO_LEARN, data.get(CONF_AUTO_LEARN, False))
    idle = opts.get(CONF_IDLE_TIMEOUT, data.get(CONF_IDLE_TIMEOUT, DEFAULT_IDLE_TIMEOUT))

    entities: Dict[str, AbBleTracker] = {}

    def _normalize_key(s: str) -> str:
        s = s.strip().upper()
        return s if s and KEY_RE.match(s) else ""

    def _to_hex4(v: str) -> str:
        vv = v.strip().lower()
        if not vv:
            return "0000"
        if vv.startswith("0x"):
            n = int(vv, 16)
        elif vv.isdigit():
            n = int(vv, 10)
        elif HEX_RE.match(vv):
            n = int(vv, 16)
        else:
            raise ValueError(f"invalid value: {v}")
        if not (0 <= n <= 0xFFFF):
            raise ValueError("out of range")
        return f"{n:04X}"

    def _split_rows(text: str) -> list[str]:
        rows: list[str] = []
        if not text:
            return rows
        for chunk in text.split(';'):
            rows.extend(chunk.splitlines())
        return rows

    def _parse_ibeacon_rows(text: str) -> list[dict[str, str]]:
        parsed: list[dict[str, str]] = []
        if not text:
            return parsed
        for line in _split_rows(text):
            line = line.strip()
            if not line:
                continue
            parts = line.replace(',', ' ').split()
            if len(parts) < 1:
                continue
            uuid = parts[0]
            major = parts[1] if len(parts) >= 2 else "0000"
            minor = parts[2] if len(parts) >= 3 else "0000"
            uuid32 = uuid.replace("-", "").strip().upper()
            if not re.fullmatch(r"[0-9A-F]{32}", uuid32):
                continue
            try:
                maj4 = _to_hex4(major)
                min4 = _to_hex4(minor)
            except Exception:
                continue
            parsed.append({
                "key": f"IBC_{uuid32}{maj4}{min4}",
                "uuid": uuid32,
                "major": maj4,
                "minor": min4,
            })
        return parsed

    preload_ibeacon_raw = opts.get(CONF_PRELOAD_IBEACON, data.get(CONF_PRELOAD_IBEACON, ""))
    for beacon in _parse_ibeacon_rows(preload_ibeacon_raw):
        key = beacon["key"]
        if key not in entities:
            ent = AbBleTracker(client, key, f"{BLE_PREFIX}{key}", idle, beacon["uuid"], beacon["major"], beacon["minor"])
            entities[key] = ent

    preload_raw = opts.get(CONF_PRELOAD_KEYS, data.get(CONF_PRELOAD_KEYS, ""))
    if preload_raw:
        for token in re.split(r"[\s,]+", preload_raw):
            k = _normalize_key(token)
            if k and k not in entities:
                ent = AbBleTracker(client, k, f"{BLE_PREFIX}{k.replace(':','')}", idle)
                entities[k] = ent

    if entities:
        async_add_entities(list(entities.values()))

    @callback
    def _on_packet(pkt: Packet | None):
        for ent in list(entities.values()):
            ent.evaluate_idle()
        if pkt is None:
            return
        key = pkt.key
        if key not in entities and auto:
            ent = AbBleTracker(client, key, f"{BLE_PREFIX}{key.replace(':','')}", idle)
            entities[key] = ent
            async_add_entities([ent])
        if key in entities:
            entities[key].update_from_packet(pkt)

    entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_PACKET, _on_packet))

class AbBleTracker(AbBleEntity, TrackerEntity):
    def __init__(self, client: AbBleMqtt, key: str, name: str, idle_timeout: int, uuid: str | None = None, major: str | None = None, minor: str | None = None):
        super().__init__(client, key, name)
        self._idle = idle_timeout
        self._source_type = SourceType.BLUETOOTH_LE
        self._last_rssi: int | None = None
        self._attr_is_connected = False
        self._uuid = uuid
        self._major = major
        self._minor = minor

    @property
    def source_type(self):
        return self._source_type

    # --- explicit state mapping (fix unknown) ---
    @property
    def state(self):
        return STATE_HOME if self._attr_is_connected else STATE_NOT_HOME

    # legacy HA compatibility
    @property
    def is_home(self):
        return self._attr_is_connected

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = {
            "rssi": self._last_rssi,
            "last_seen_seconds": round(self.last_seen_seconds(), 1),
        }
        if self._uuid:
            data["uuid"] = self._uuid
        if self._major:
            data["major"] = self._major
        if self._minor:
            data["minor"] = self._minor
        return data

    def evaluate_idle(self):
        was = bool(self._attr_is_connected)
        self._attr_is_connected = self.last_seen_seconds() <= self._idle
        if self._attr_is_connected != was:
            self.async_write_ha_state()

    @callback
    def update_from_packet(self, pkt: Packet):
        self._last_rssi = pkt.rssi
        self._attr_is_connected = True
        self.async_write_ha_state()
