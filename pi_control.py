"""Control de los relés de acceso conectados a la Raspberry Pi 4."""

from time import sleep

from gpiozero import OutputDevice

# Duración del pulso del relé, en segundos.
#
# El módulo de relés es electromecánico: la bobina necesita algunos
# milisegundos para cerrar el contacto, así que un pulso de 0.5 ms no movería
# nada. Aquí va medio segundo, que es lo que abre una puerta.
PULSE_SECONDS = 0.5


class piControl:
    """
    Dos relés, dos tipos de pase.

    MÓDULO ACTIVO EN BAJO: el relé cierra con 0 V y queda abierto con 3.3 V.
    `active_high=False` invierte el pin, de modo que `on()` lo manda a bajo
    (relé activado) y `off()` lo devuelve a alto. `initial_value=False` deja
    ambos pines en alto al construir el objeto, antes de que nada pueda
    accionarse: sin eso, los relés chasquean al arrancar el programa.
    """

    def __init__(self):
        self.gpio_normal_pass = OutputDevice(21, active_high=False, initial_value=False)
        self.gpio_special_pass = OutputDevice(20, active_high=False, initial_value=False)

    def generate_normal_pass(self):
        """Un pulso del relé del pase normal (GPIO 21)."""
        self._pulse(self.gpio_normal_pass)

    def generate_special_pass(self):
        """Un pulso del relé del pase especial (GPIO 20)."""
        self._pulse(self.gpio_special_pass)

    @staticmethod
    def _pulse(gpio):
        gpio.on()
        sleep(PULSE_SECONDS)
        gpio.off()
