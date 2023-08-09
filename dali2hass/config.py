import tomli
import voluptuous as vol


SCHEMA = vol.Schema(
    {
        vol.Required("dali"): {
            vol.Optional("mqtt_prefix", default="dali2hass"): str,
            vol.Required("bus_id"): str,
            vol.Required("bus_name"): str,
            vol.Required("bus"): vol.Schema(
                {
                    vol.Required("driver"): str,
                },
                extra=True,
            ),
            vol.Optional("poll_interval", default=60.0): float,
            vol.Optional("groups", default="off"): vol.Any(
                "off", "min", "average", "max"),
            vol.Optional("gear", default={}): dict,
        },
        vol.Required("homeassistant"): {
            vol.Required("mqtt_hostname"): str,
            vol.Optional("mqtt_port", default=1883): int,
            vol.Optional("mqtt_username"): str,
            vol.Optional("mqtt_password"): str,
            vol.Optional("mqtt_client_id", default="dali2hass"): str,
            vol.Optional("discovery_prefix", default="homeassistant"): str,
        },
    },
    extra=False,
)


class Config:
    def __init__(self, cf):
        self._config = SCHEMA(tomli.load(cf))

    @property
    def homeassistant(self):
        return self._config["homeassistant"]

    @property
    def dali(self):
        return self._config["dali"]
