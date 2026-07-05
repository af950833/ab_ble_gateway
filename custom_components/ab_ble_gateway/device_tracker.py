from __future__ import annotations
from datetime import datetime, timezone
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
    CONF_PRELOAD_IRK,
)
from .mqtt_client import AbBleMqtt, SIGNAL_PACKET, Packet, parse_irk_value
from .entity import AbBleEntity

KEY_RE = re.compile(r"^(IRK_[0-9A-F]{32}|IBC_[0-9A-F]{32}(?:[0-9A-F]{8})?|EDS_[0-9A-F]+|[0-9A-F]{2}(?::[0-9A-F]{2}){5})$", re.I)
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

    def _parse_irk_rows(text: str) -> list[dict[str, str]]:
        parsed: list[dict[str, str]] = []
        if not text:
            return parsed
        for line in _split_rows(text):
            line = line.strip()
            if not line:
                continue
            parts = line.replace(',', ' ').split()
            irk_bytes: bytes | None = None
            irk_token = ""
            min_rssi: int | None = None
            labels: list[str] = []
            for part in parts:
                parsed_irk = parse_irk_value(part)
                if parsed_irk is not None and irk_bytes is None:
                    irk_bytes = parsed_irk
                    irk_token = part
                elif re.fullmatch(r"-?\d+", part) and min_rssi is None:
                    rssi = int(part)
                    if -100 <= rssi <= 0:
                        min_rssi = rssi
                    else:
                        labels.append(part)
                else:
                    labels.append(part)
            if irk_bytes is None:
                continue
            irk_hex = irk_bytes.hex().upper()
            label = "_".join(labels) if labels else irk_token[:8]
            label = re.sub(r"[^0-9A-Za-z_-]+", "_", label).strip("_") or irk_hex[:8]
            parsed.append({
                "key": f"IRK_{irk_hex}",
                "irk": irk_hex,
                "irk_bytes": irk_bytes,
                "label": label,
                "min_rssi": min_rssi,
            })
        return parsed

    irks: dict[str, bytes] = {}
    preload_irk_raw = opts.get(CONF_PRELOAD_IRK, data.get(CONF_PRELOAD_IRK, ""))
    for irk in _parse_irk_rows(preload_irk_raw):
        key = irk["key"]
        irks[key] = irk["irk_bytes"]
        if key not in entities:
            ent = AbBleTracker(client, key, f"{BLE_PREFIX}{irk['label']}", idle, irk=irk["irk"], min_rssi=irk["min_rssi"])
            entities[key] = ent
    client.set_irks(irks)

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
    def __init__(self, client: AbBleMqtt, key: str, name: str, idle_timeout: int, uuid: str | None = None, major: str | None = None, minor: str | None = None, irk: str | None = None, min_rssi: int | None = None):
        super().__init__(client, key, name)
        self._idle = idle_timeout
        self._source_type = SourceType.BLUETOOTH_LE
        self._last_rssi: int | None = None
        self._last_mac: str | None = None
        self._last_seen_ts: float | None = None
        self._last_weak_rssi: int | None = None
        self._attr_is_connected = False
        self._uuid = uuid
        self._major = major
        self._minor = minor
        self._irk = irk
        self._min_rssi = min_rssi if irk else None

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
        if self._irk:
            data["irk"] = self._irk
        if self._min_rssi is not None:
            data["min_rssi"] = self._min_rssi
        if self._last_mac:
            data["current_address"] = self._last_mac
        if self._last_weak_rssi is not None:
            data["last_weak_rssi"] = self._last_weak_rssi
        return data

    def last_seen_seconds(self) -> float:
        if self._last_seen_ts is None:
            return 1e9
        return max(0.0, datetime.now(timezone.utc).timestamp() - self._last_seen_ts)

    def evaluate_idle(self):
        was = bool(self._attr_is_connected)
        self._attr_is_connected = self.last_seen_seconds() <= self._idle
        if self._attr_is_connected != was:
            self.async_write_ha_state()

    @callback
    def update_from_packet(self, pkt: Packet):
        self._last_rssi = pkt.rssi
        self._last_mac = pkt.mac
        if self._irk and self._min_rssi is not None and (pkt.rssi is None or pkt.rssi < self._min_rssi):
            self._last_weak_rssi = pkt.rssi
            self.evaluate_idle()
            self.async_write_ha_state()
            return
        self._last_seen_ts = pkt.ts
        self._last_weak_rssi = None
        self._attr_is_connected = True
        self.async_write_ha_state()
