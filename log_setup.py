"""Registro en archivo para soporte: sobrevive a los reinicios del servicio."""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import time
from pathlib import Path

# Tope de disco. La Pi arranca desde una microSD y un log sin limite la llena
# hasta dejar el servicio inservible: 1 MiB por archivo y 5 historicos son
# 6 MiB en el peor caso, suficiente para varias semanas de operacion normal.
MAX_BYTES = 1024 * 1024
BACKUP_COUNT = 5

# El nombre del logger va en cada linea para distinguir quien habla: `main`,
# `hikvision`, `paho.mqtt`...
LOG_FORMAT = "%(asctime)s %(levelname)-8s [%(name)s] %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def _log_file() -> Path:
    directory = Path(
        os.environ.get("FITONICA_LOG_DIR")
        or Path(__file__).resolve().parent / "logs"
    )
    directory.mkdir(parents=True, exist_ok=True)
    return directory / "fitonica-manager.log"


def setup_logging() -> Path:
    """Configura el logger raiz y devuelve la ruta del archivo .log."""

    level_name = os.environ.get("FITONICA_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    log_file = _log_file()
    formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)

    # `delay=False`: el archivo se crea y se comprueba que es escribible al
    # arrancar, no la primera vez que haya algo que registrar. Si los permisos
    # estan mal, el servicio falla de inmediato y no horas despues.
    file_handler = logging.handlers.RotatingFileHandler(
        log_file,
        maxBytes=MAX_BYTES,
        backupCount=BACKUP_COUNT,
        encoding="utf-8",
        delay=False,
    )
    file_handler.setFormatter(formatter)

    # Segundo destino a stdout para que `journalctl -u fitonica-manager -f`
    # siga sirviendo en vivo. El archivo es el registro duradero; el journal,
    # la vista inmediata.
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(level)
    # Se limpian los handlers previos para que una segunda llamada no duplique
    # cada linea en el archivo.
    for handler in list(root.handlers):
        root.removeHandler(handler)
    root.addHandler(file_handler)
    root.addHandler(stream_handler)

    _install_excepthook()

    logger = logging.getLogger("arranque")
    # Marca de inicio: con `Restart=always` systemd relanza el proceso en
    # silencio, y esta linea es lo que permite contar los reinicios y ubicar
    # donde empieza cada vida del servicio dentro del archivo.
    logger.info("=" * 72)
    logger.info(
        "fitonica-manager arrancando | pid=%s | nivel=%s | zona=%s",
        os.getpid(),
        logging.getLevelName(level),
        time.strftime("%Z %z"),
    )
    logger.info("registro en %s", log_file)
    return log_file


def _install_excepthook() -> None:
    """Deja el traceback de un fallo no capturado dentro del archivo."""

    def handler(exc_type, exc_value, exc_traceback):
        # Ctrl+C no es un fallo: se delega al comportamiento normal para no
        # ensuciar el log con un traceback durante una parada manual.
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_traceback)
            return
        logging.getLogger("arranque").critical(
            "el proceso termina por una excepcion no capturada",
            exc_info=(exc_type, exc_value, exc_traceback),
        )

    sys.excepthook = handler
