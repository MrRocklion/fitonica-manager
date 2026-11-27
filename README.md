# fitonica-manager

Escucha los comandos MQTT del backend en la Raspberry Pi y acciona los relés de
acceso.

- `pi_control.py` — clase `piControl`, pulsos de 500 ms sobre GPIO 21 (pase
  normal) y GPIO 20 (pase especial). Relés activos en bajo.
- `main.py` — cliente suscrito al tópico; `NORMAL_PASS` genera un pase normal,
  `ESPECIAL_PASS` uno especial y `UPDATE_FACE_ID` descarga la imagen privada
  del usuario en `users/`. Los comandos se distinguen por el campo `type`.

## Requisitos

```bash
pip install paho-mqtt python-dotenv gpiozero lgpio
```

Un `.env` junto a los scripts:

```
PI_MQTT_BROKER_URL=mqtts://broker.ejemplo.com:8883
PI_MQTT_TOPIC=fitonica/v1/companies/<tenant-uuid>/devices/<device-uuid>/commands
PI_MQTT_USERNAME=...
PI_MQTT_PASSWORD=...
FITONICA_API_URL=https://api.ejemplo.com
FITONICA_DEVICE_UID=<device-uuid>
FITONICA_DEVICE_API_KEY=<device-api-key>
```

El esquema determina la seguridad y el puerto predeterminado: `mqtts://` activa
TLS con validacion de certificado y usa el puerto 8883; `mqtt://` usa una
conexion sin cifrar por el puerto 1883. Para conexiones por Internet se debe
usar `mqtts://`.

## API de usuarios y Face ID

`fitonica.py` usa la credencial propia del dispositivo para consultar solamente
usuarios de su establecimiento y descargar su Face ID privado:

```python
from dotenv import load_dotenv
from fitonica import Fitonica

load_dotenv()
fitonica = Fitonica.from_env()
user = fitonica.get_user(1394)
image_path = fitonica.download_face_id(1394)
print(user, image_path)
```

Las imágenes se guardan en `users/` con el formato
`<uuid>_<cedula>.<extension>`. La API key nunca se envía a la URL firmada de R2.

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
ExecStart=/home/admin/env/bin/python3 /home/admin/fitonica-manager/main.py
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
