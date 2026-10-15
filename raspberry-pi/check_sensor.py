"""Prueba del MAX30102 sin red ni servidor.

Lee el sensor durante unos segundos e imprime, cada medio segundo, cuántas
muestras llegaron y la media y amplitud (pico a pico) de los canales IR y rojo.
Sirve para comprobar el cableado y ver qué valores da el sensor con y sin dedo
antes de ajustar los umbrales de pulseppg.env.

Uso (en la Pi):
    /opt/pulseppg/venv/bin/python /opt/pulseppg/check_sensor.py --seconds 20
"""

import argparse
import time

from max30102 import MAX30102


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--bus", type=int, default=1)
    p.add_argument("--address", type=lambda v: int(v, 0), default=0x57)
    p.add_argument("--seconds", type=float, default=15)
    args = p.parse_args()

    sensor = MAX30102(bus=args.bus, address=args.address)
    print("MAX30102 encontrado. Coloca y retira el dedo para ver la diferencia.")
    print(f"{'muestras':>8} {'IR medio':>10} {'IR p-p':>8} {'rojo medio':>10} {'rojo p-p':>8} {'IR/rojo':>7}")
    end = time.monotonic() + args.seconds
    try:
        while time.monotonic() < end:
            time.sleep(0.5)
            batch = sensor.read()
            if not batch:
                print("sin muestras (¿sensor detenido?)")
                continue
            ir = [s[0] for s in batch]
            red = [s[1] for s in batch]
            ir_mean = sum(ir) / len(ir)
            red_mean = sum(red) / len(red)
            ratio = ir_mean / red_mean if red_mean else 0
            print(f"{len(batch):8d} {ir_mean:10.0f} {max(ir) - min(ir):8d} "
                  f"{red_mean:10.0f} {max(red) - min(red):8d} {ratio:7.2f}")
        if sensor.overflows:
            print(f"aviso: el FIFO se desbordó {sensor.overflows} veces")
    finally:
        sensor.close()


if __name__ == "__main__":
    main()
