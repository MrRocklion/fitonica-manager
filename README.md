# fitonica-manager

Escucha los comandos MQTT del backend en la Raspberry Pi y acciona los relés de
acceso.

- `pi_control.py` — clase `piControl`, pulsos de 500 ms sobre GPIO 21 (pase
  normal) y GPIO 20 (pase especial). Relés activos en bajo.
- `mqtt_example.py` — cliente suscrito al tópico; al recibir `OPEN_ACCESS`
  genera un pase normal.

## Requisitos

```bash
pip install paho-mqtt python-dotenv gpiozero lgpio
```

Un `.env` junto a los scripts:

```
PI_MQTT_BROKER_URL=mqtt://broker.ejemplo.com:1883
PI_MQTT_TOPIC=fitonica/v1/companies/<tenant-uuid>/devices/<device-uuid>/commands
PI_MQTT_USERNAME=...
PI_MQTT_PASSWORD=...
```

## Servicio systemd

`/etc/systemd/system/fitonica-manager.service`:

```ini
[Unit]
Description=Manager de accesos Fitonica
After=network-online.target
Wants=network-online.target

[Service]
User=admin
WorkingDirectory=/home/admin/fitonica-manager/
ExecStart=/home/admin/env/bin/python3 /home/admin/fitonica-manager/mqtt_example.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

Ajusta `User`, `WorkingDirectory` y la ruta del intérprete a tu instalación.
`WorkingDirectory` importa: desde ahí se resuelve el import de `pi_control`.

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now fitonica-manager
```

## Operación

```bash
systemctl status fitonica-manager
journalctl -u fitonica-manager -f
sudo systemctl restart fitonica-manager
```

## Notas

- `network-online.target` en vez de `network.target`: con el segundo el
  servicio arranca antes de que haya IP y el primer `connect()` falla.
- El usuario del servicio debe pertenecer al grupo `gpio`
  (`sudo usermod -aG gpio admin`).
