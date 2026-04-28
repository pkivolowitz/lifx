# Systemd unit templates

`install.sh` renders these `.template` files into real `.service` files at
install time, substituting `${VAR}` placeholders against the current
environment.

## Placeholder vocabulary (Phase 2b)

| Placeholder          | Meaning                                  | Default          |
| -------------------- | ---------------------------------------- | ---------------- |
| `${SERVICE_USER}`    | User the unit runs as                    | `id -un`         |
| `${SERVICE_GROUP}`   | Group the unit runs as                   | `id -gn`         |
| `${INSTALL_ROOT}`    | Repo checkout root                       | `$REPO_ROOT`     |
| `${VENV}`            | Python venv root                         | `${INSTALL_ROOT}/venv` |
| `${SITE_CONFIG_DIR}` | Site config dir                          | `/etc/glowup`    |

Subsystem-specific roots:

| Placeholder           | Subsystem                | Default                  |
| --------------------- | ------------------------ | ------------------------ |
| `${AGENT_VENV}`       | `distributed/`           | `$HOME/aeye_env`         |
| `${CLOCK_ROOT}`       | `tools/clock/`           | `$HOME/clock`            |
| `${SDR_ROOT}`         | `sdr/`                   | `/opt/glowup-sdr`        |
| `${SENSORS_ROOT}`     | `contrib/sensors/`       | `/opt/glowup-sensors`    |
| `${REMOTE_HID_ROOT}`  | `tools/remote_hid/`      | `/opt/glowup-remote-hid` |
| `${ZIGBEE_ROOT}`      | `zigbee_service/`        | `/opt/glowup-zigbee`     |
| `${ZIGBEE2MQTT_ROOT}` | Z2M (npm)                | `/opt/zigbee2mqtt`       |
| `${ERNIE_ROOT}`       | `services/ble-sniffer`   | `/opt/ernie`             |

Subsystem venvs are referenced as `${SUBSYSTEM_ROOT}/venv/bin/python` —
no separate placeholders, the venv lives under the root by convention.

`User=root` is left literal in templates whose service intrinsically
needs root for hardware capabilities (`glowup-maritime` for SDR access,
`ble-sniffer` for HCI raw sockets).  Templates that can run as the
install user use `${SERVICE_USER}`.

## Adding a template

1. Copy the existing `.service` file into `installer/systemd/<name>.template`.
2. Replace household-specific values with `${VAR}` placeholders from the
   table above. If a new placeholder is needed, add it here and to
   `install_systemd_units` in `install.sh`.
3. Add the filename to `SYSTEMD_TEMPLATES` in `install.sh`.
4. Run `./install.sh` on the test VM (`ubuntu-conway`, 10.0.0.244) and
   verify the rendered output in `site-settings/rendered-units/`.

## Failure mode

The renderer fails loud (`exit 1`) if any `${VAR}` in a template is unset
in the environment. A unit shipped to `/etc/systemd/system/` containing
literal `${FOO}` text would only surface as a confusing systemd start
error later — better to catch it at render time.
