# frame-art

Public-domain art for a Samsung Frame TV. frame-art crawls museum open-access
collections (Art Institute of Chicago, Cleveland, The Met, National Gallery of
Art, Rijksmuseum, SMK), picks well-known paintings, renders them at 3840x2160
(cropped to fill the screen, or on a blurred background), and keeps a rotating
pool of them in a folder. Home Assistant uploads that folder to the TV with the
[SamsungTV Smart](https://github.com/TheFab21/ha-samsungtv-smart) integration;
frame-art itself never talks to the TV.

## Running

```sh
docker run -p 8740:8740 \
  -v ./data:/data -v /path/to/ha/config/frame_art_pool:/out \
  -e TZ=America/Los_Angeles \
  -e FRAME_ART_MQTT_HOST=mqtt.home \
  -e FRAME_ART_HA_FOLDER=/config/frame_art_pool \
  ghcr.io/bbaldino/frame-art:<version>
```

| Variable | Meaning |
|---|---|
| `FRAME_ART_DATA` | State: catalogue, settings, caches (default `/data`) |
| `FRAME_ART_DEST` | Where rendered works are written (default `/out`) |
| `FRAME_ART_HA_FOLDER` | The same folder as Home Assistant sees it |
| `FRAME_ART_MQTT_HOST`, `FRAME_ART_MQTT_PORT` | Broker Home Assistant listens on |
| `FRAME_ART_PORT` | Web UI port (default 8740) |

The web UI (no authentication; keep it on the LAN) shows the pool and the
catalogue, lets you ban or pin works, and holds the settings: crop tolerance,
background style, kinds, museums, nudes filter, pool size, nightly run time.

Home Assistant learns what to do from an MQTT-discovered `sensor.frame_art` and
acknowledges on `frame-art/ack`; see `docs/design.md` for the automation.

## Development

```sh
uv sync
uv run pytest
uv run frame-art --help
```
