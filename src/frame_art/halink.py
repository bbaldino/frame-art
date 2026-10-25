"""The link to Home Assistant, over MQTT. HA stays the only thing talking to the TV.

We publish one MQTT-discovered sensor, ``sensor.frame_art``. Its state is a
sequence number that goes up whenever there is new work for HA (new files to
upload, TV content ids to delete); its attributes say what that work is. The HA
automation triggers on it and reports back on ``frame-art/ack``:

    {"deleted": "MY_F0123"}                 one TV id it deleted
    {"upload": {...art_upload_batch response...}}
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable

import paho.mqtt.client as mqtt

log = logging.getLogger(__name__)

STATE = "frame-art/state"
STATUS = "frame-art/status"
ACK = "frame-art/ack"
DISCOVERY = "homeassistant/sensor/frame_art/state/config"


class HaLink:
    def __init__(
        self, host: str, port: int, on_ack: Callable[[dict], None], version: str
    ):
        self.on_ack = on_ack
        self.version = version
        self._last: dict | None = None
        self._lock = threading.Lock()
        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2, client_id="frame-art", clean_session=True
        )
        self.client.will_set(STATUS, "offline", qos=1, retain=True)
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.reconnect_delay_set(1, 60)
        self.client.connect_async(host, port, keepalive=60)
        self.client.loop_start()

    @property
    def connected(self) -> bool:
        return self.client.is_connected()

    def _on_connect(self, client, userdata, flags, reason, properties) -> None:
        if reason.is_failure:
            log.warning("mqtt: connect failed: %s", reason)
            return
        log.info("mqtt: connected")
        client.publish(DISCOVERY, json.dumps(self._discovery()), qos=1, retain=True)
        client.publish(STATUS, "online", qos=1, retain=True)
        client.subscribe(ACK, qos=1)
        with self._lock:
            if self._last is not None:
                client.publish(STATE, json.dumps(self._last), qos=1, retain=True)

    def _on_message(self, client, userdata, msg) -> None:
        try:
            payload = json.loads(msg.payload)
            if not isinstance(payload, dict):
                raise ValueError("not an object")
        except ValueError as ex:
            log.warning("mqtt: ignoring malformed ack %r: %s", msg.payload[:200], ex)
            return
        try:
            self.on_ack(payload)
        except Exception:
            log.exception("mqtt: handling ack failed")

    def _discovery(self) -> dict:
        return {
            "name": None,  # the entity takes the device's name: "Frame Art"
            "unique_id": "frame_art_state",
            "default_entity_id": "sensor.frame_art",
            "state_topic": STATE,
            "value_template": "{{ value_json.seq }}",
            "json_attributes_topic": STATE,
            "availability_topic": STATUS,
            "icon": "mdi:palette",
            "device": {
                "identifiers": ["frame_art"],
                "name": "Frame Art",
                "manufacturer": "frame-art",
                "sw_version": self.version,
            },
        }

    def publish(self, state: dict) -> None:
        """Publish (retained) what HA should do next."""
        with self._lock:
            self._last = state
        if self.connected:
            self.client.publish(STATE, json.dumps(state), qos=1, retain=True)

    def close(self) -> None:
        self.client.publish(STATUS, "offline", qos=1, retain=True)
        self.client.loop_stop()
        self.client.disconnect()
