"""Lectura mínima del MAX30102 por I2C (smbus2).

Configura el sensor en modo SpO2 (LED rojo + IR) a 50 Hz, sin promediado, con
ADC de 18 bits, y lee las muestras de su FIFO interno (32 muestras, 640 ms a
50 Hz). Las muestras las cronometra el propio sensor, así que el ritmo es
estable aunque el programa lea de forma irregular; basta con vaciar el FIFO
antes de que se llene.

Registros según la hoja de datos de Maxim/Analog Devices (MAX30102, rev. 1).
"""

from smbus2 import SMBus, i2c_msg

ADDRESS = 0x57
PART_ID_EXPECTED = 0x15

REG_INT_STATUS_1 = 0x00
REG_INT_ENABLE_1 = 0x02
REG_INT_ENABLE_2 = 0x03
REG_FIFO_WR_PTR = 0x04
REG_OVF_COUNTER = 0x05
REG_FIFO_RD_PTR = 0x06
REG_FIFO_DATA = 0x07
REG_FIFO_CONFIG = 0x08
REG_MODE_CONFIG = 0x09
REG_SPO2_CONFIG = 0x0A
REG_LED1_PA = 0x0C   # LED rojo
REG_LED2_PA = 0x0D   # LED IR
REG_PART_ID = 0xFF

MODE_RESET = 0x40
MODE_SHUTDOWN = 0x80
MODE_SPO2 = 0x03     # rojo + IR

# FIFO_CONFIG: SMP_AVE=1 (sin promediado), FIFO_ROLLOVER_EN=1, FIFO_A_FULL=15.
FIFO_CONFIG = 0b000_1_1111
# SPO2_CONFIG: ADC_RGE=4096 nA (01), SR=50 muestras/s (000), LED_PW=411 µs / 18 bits (11).
SPO2_CONFIG_50HZ = 0b0_01_000_11
SAMPLE_RATE_HZ = 50

FIFO_DEPTH = 32
BYTES_PER_SAMPLE = 6  # 3 bytes rojo + 3 bytes IR
SAMPLE_MASK = 0x3FFFF  # 18 bits


class MAX30102:
    def __init__(self, bus=1, address=ADDRESS, led_red=0x24, led_ir=0x24):
        self.address = address
        self.bus = SMBus(bus)
        part = self.bus.read_byte_data(address, REG_PART_ID)
        if part != PART_ID_EXPECTED:
            raise RuntimeError(f"PART_ID=0x{part:02x}; se esperaba 0x{PART_ID_EXPECTED:02x} (¿es un MAX30102?)")
        self._write(REG_MODE_CONFIG, MODE_RESET)
        while self.bus.read_byte_data(address, REG_MODE_CONFIG) & MODE_RESET:
            pass
        self._write(REG_INT_ENABLE_1, 0x00)
        self._write(REG_INT_ENABLE_2, 0x00)
        self._write(REG_FIFO_CONFIG, FIFO_CONFIG)
        self._write(REG_SPO2_CONFIG, SPO2_CONFIG_50HZ)
        self._write(REG_LED1_PA, led_red)
        self._write(REG_LED2_PA, led_ir)
        self._write(REG_MODE_CONFIG, MODE_SPO2)
        self.clear_fifo()
        self.overflows = 0

    def _write(self, reg, value):
        self.bus.write_byte_data(self.address, reg, value)

    def clear_fifo(self):
        """Descarta lo que haya en el FIFO (al empezar una captura)."""
        self._write(REG_FIFO_WR_PTR, 0)
        self._write(REG_OVF_COUNTER, 0)
        self._write(REG_FIFO_RD_PTR, 0)

    def read(self):
        """Devuelve la lista de muestras (ir, red) pendientes en el FIFO.

        Si el FIFO llegó a desbordarse se perdieron muestras; se suman en
        self.overflows para que quien lee decida qué hacer.
        """
        ovf = self.bus.read_byte_data(self.address, REG_OVF_COUNTER)
        wr = self.bus.read_byte_data(self.address, REG_FIFO_WR_PTR)
        rd = self.bus.read_byte_data(self.address, REG_FIFO_RD_PTR)
        if ovf:
            self.overflows += ovf
            count = FIFO_DEPTH
        else:
            count = (wr - rd) % FIFO_DEPTH
        if count == 0:
            return []
        # Lectura en bloque: el puntero de FIFO_DATA avanza solo.
        write = i2c_msg.write(self.address, [REG_FIFO_DATA])
        read = i2c_msg.read(self.address, count * BYTES_PER_SAMPLE)
        self.bus.i2c_rdwr(write, read)
        raw = bytes(read)
        samples = []
        for i in range(0, len(raw), BYTES_PER_SAMPLE):
            red = int.from_bytes(raw[i:i + 3], "big") & SAMPLE_MASK
            ir = int.from_bytes(raw[i + 3:i + 6], "big") & SAMPLE_MASK
            samples.append((ir, red))
        return samples

    def close(self):
        try:
            self._write(REG_MODE_CONFIG, MODE_SHUTDOWN)
        finally:
            self.bus.close()
