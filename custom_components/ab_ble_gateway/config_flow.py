from __future__ import annotations
import re
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector
from .const import (
    DOMAIN,
    CONF_MQTT_TOPIC,
    DEFAULT_MQTT_TOPIC,
    CONF_AUTO_LEARN,
    CONF_IDLE_TIMEOUT,
    DEFAULT_IDLE_TIMEOUT,
    CONF_PRELOAD_KEYS,
    CONF_PRELOAD_IBEACON,
)

HEX_RE = re.compile(r"^[0-9A-F]{1,4}$", re.I)

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

def _validate_preload_ibeacon(text: str) -> tuple[bool, str | None]:
    try:
        if not text:
            return True, None
        for idx, raw in enumerate(_split_rows(text), start=1):
            line = raw.strip()
            if not line:
                continue
            parts = line.replace(',', ' ').split()
            if len(parts) < 1:
                return False, f"Line {idx}: empty"
            uuid = parts[0].replace('-', '').strip().upper()
            if not re.fullmatch(r"[0-9A-F]{32}", uuid):
                return False, f"Line {idx}: UUID must be 32 hex (got '{parts[0]}')"
            major = parts[1] if len(parts) >= 2 else "0000"
            minor = parts[2] if len(parts) >= 3 else "0000"
            _ = _to_hex4(major)
            _ = _to_hex4(minor)
        return True, None
    except Exception as e:
        return False, str(e)

class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            ok, msg = _validate_preload_ibeacon(user_input.get(CONF_PRELOAD_IBEACON, ""))
            if not ok:
                errors[CONF_PRELOAD_IBEACON] = msg or "invalid_preload"
            if not errors:
                await self.async_set_unique_id(DOMAIN)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title="AB BLE Gateway", data=user_input)

        schema = vol.Schema({
            vol.Optional(CONF_MQTT_TOPIC, default=DEFAULT_MQTT_TOPIC):
                selector.TextSelector(selector.TextSelectorConfig(multiline=False)),
            vol.Optional(CONF_AUTO_LEARN, default=False):
                selector.BooleanSelector(),
            vol.Optional(CONF_IDLE_TIMEOUT, default=DEFAULT_IDLE_TIMEOUT):
                selector.NumberSelector(selector.NumberSelectorConfig(min=0, mode=selector.NumberSelectorMode.BOX)),
            vol.Optional(CONF_PRELOAD_IBEACON, default=""):
                selector.TextSelector(selector.TextSelectorConfig(multiline=True)),
            vol.Optional(CONF_PRELOAD_KEYS, default=""):
                selector.TextSelector(selector.TextSelectorConfig(multiline=True)),
        })
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return OptionsFlowHandler(config_entry)

class OptionsFlowHandler(config_entries.OptionsFlow):
    def __init__(self, entry):
        self._entry = entry

    async def async_step_init(self, user_input=None):
        errors: dict[str, str] = {}
        data = self._entry.data
        opts = self._entry.options
        def get(k, default):
            return opts.get(k, data.get(k, default))

        if user_input is not None:
            ok, msg = _validate_preload_ibeacon(user_input.get(CONF_PRELOAD_IBEACON, ""))
            if not ok:
                errors[CONF_PRELOAD_IBEACON] = msg or "invalid_preload"
            if not errors:
                return self.async_create_entry(title="Options", data=user_input)

        schema = vol.Schema({
            vol.Optional(CONF_AUTO_LEARN, default=get(CONF_AUTO_LEARN, False)):
                selector.BooleanSelector(),
            vol.Optional(CONF_IDLE_TIMEOUT, default=get(CONF_IDLE_TIMEOUT, DEFAULT_IDLE_TIMEOUT)):
                selector.NumberSelector(selector.NumberSelectorConfig(min=0, mode=selector.NumberSelectorMode.BOX)),
            vol.Optional(CONF_PRELOAD_IBEACON, default=get(CONF_PRELOAD_IBEACON, "")):
                selector.TextSelector(selector.TextSelectorConfig(multiline=True)),
            vol.Optional(CONF_PRELOAD_KEYS, default=get(CONF_PRELOAD_KEYS, "")):
                selector.TextSelector(selector.TextSelectorConfig(multiline=True)),
        })
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
