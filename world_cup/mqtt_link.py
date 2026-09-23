"""
MQTT connection (paho-mqtt 2.x).

paho runs its own network thread (loop_start), so publishing never blocks and
incoming messages arrive on that thread. It also reconnects by itself if the
Wi-Fi drops; every (re)connect re-subscribes and calls on_connect again.
"""

import json
import uuid

import paho.mqtt.client as mqtt

import config


class MqttLink:
    def __init__(self, topics, on_message, status, on_connect=None, name="robot",
                 host=config.BROKER_HOST, port=config.BROKER_PORT):
        """
        topics      list of topics to subscribe to
        on_message  callback(topic: str, payload: str)
        on_connect  callback() after every successful (re)connect
        name        short tag for the client id, e.g. "robot" or "drive"
        """
        self.topics = topics
        self.on_message = on_message
        self.on_connect_cb = on_connect
        self.status = status
        self.host, self.port = host, port
        self.connected = False
        # Client ids must be unique on the broker, so add a random suffix.
        client_id = f"me193-{config.MATCH_PREFIX}-{name}-{uuid.uuid4().hex[:6]}"
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        self.client.reconnect_delay_set(min_delay=1, max_delay=10)

    def start(self):
        self.status.set(mqtt=f"connecting to {self.host}:{self.port}...")
        self.client.connect_async(self.host, self.port, keepalive=30)
        self.client.loop_start()

    def stop(self):
        self.client.loop_stop()
        self.client.disconnect()

    def publish(self, topic, payload, qos=1, retain=False):
        """Publish a string, or a dict as JSON. Safe to call from any thread."""
        if isinstance(payload, dict):
            payload = json.dumps(payload, separators=(",", ":"))
        self.client.publish(topic, payload, qos=qos, retain=retain)

    # -- paho callbacks (run on paho's thread) ---------------------------------
    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            self.status.set(mqtt=f"refused: {reason_code}")
            return
        self.connected = True
        for t in self.topics:
            client.subscribe(t, qos=1)
        self.status.set(mqtt=f"connected {self.host}:{self.port}")
        self.status.log(f"MQTT connected, subscribed to {', '.join(self.topics)}")
        if self.on_connect_cb:
            self.on_connect_cb()

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        self.connected = False
        self.status.set(mqtt="disconnected - retrying")
        self.status.log(f"MQTT disconnected ({reason_code})")

    def _on_message(self, client, userdata, msg):
        try:
            payload = msg.payload.decode("utf-8", errors="replace")
            self.on_message(msg.topic, payload)
        except Exception as e:           # a bad message must not kill the MQTT thread
            self.status.log(f"error handling MQTT message: {e!r}")
