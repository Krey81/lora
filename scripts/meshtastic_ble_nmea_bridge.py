#!/usr/bin/env python3
"""
Трансляция GPS с Meshtastic (по BLE) в Google Earth.
Запуск: python3 meshtastic_ble_nmea_bridge.py
"""

import time
import serial
import meshtastic.ble_interface

# ================= НАСТРОЙКИ =================
# Имя BLE-устройства (можно узнать командой: meshtastic --ble-scan)
# Например: "Meshtastic_abcd" или MAC-адрес
BLE_DEVICE_NAME = "Meshtastic_2178"  # <-- ЗАМЕНИТЕ НА ВАШЕ ИМЯ

# Порт, в который socat пишет (мы будем в него писать)
VIRTUAL_PORT = "/tmp/meshtastic_tx"

# Скорость порта (Google Earth требует 4800 бод)
BAUD_RATE = 4800

# Интервал обновления (секунд)
UPDATE_INTERVAL = 1
# =============================================


def decimal_to_nmea(degrees, is_lat):
    """Преобразует десятичные градусы в формат NMEA (ddmm.mmm)."""
    d = int(abs(degrees))
    m = (abs(degrees) - d) * 60
    if is_lat:
        return f"{d:02d}{m:06.3f}"
    else:
        return f"{d:03d}{m:06.3f}"


def build_gga(lat, lon, alt=0.0):
    """Формирует NMEA-строку $GPGGA."""
    now = time.gmtime()
    time_str = f"{now.tm_hour:02d}{now.tm_min:02d}{now.tm_sec:02d}.00"

    lat_str = decimal_to_nmea(lat, True)
    lon_str = decimal_to_nmea(lon, False)

    lat_hem = 'N' if lat >= 0 else 'S'
    lon_hem = 'E' if lon >= 0 else 'W'

    # quality=1 (GPS fix), sats=08, hdop=0.9, alt=alt, geoid=0.0
    body = (f"GPGGA,{time_str},{lat_str},{lat_hem},{lon_str},{lon_hem},"
            f"1,08,0.9,{alt:.1f},M,0.0,M,,")

    # Контрольная сумма
    checksum = 0
    for ch in body:
        checksum ^= ord(ch)

    return f"${body}*{checksum:02X}\r\n"


def main():
    print("🔍 Подключение к Meshtastic по BLE...")
    print(f"   Устройство: {BLE_DEVICE_NAME}")

    try:
        iface = meshtastic.ble_interface.BLEInterface(BLE_DEVICE_NAME)
        print("✅ Подключено!")
    except Exception as e:
        print(f"❌ Ошибка подключения по BLE: {e}")
        print("   Убедитесь, что имя устройства верное (meshtastic --ble-scan)")
        return

    # Открываем виртуальный порт для записи
    try:
        ser = serial.Serial(VIRTUAL_PORT, BAUD_RATE, timeout=1)
        print(f"✅ Виртуальный порт {VIRTUAL_PORT} открыт для записи.")
    except Exception as e:
        print(f"❌ Не удалось открыть порт {VIRTUAL_PORT}.")
        print("   Запустите socat в другом окне Терминала:")
        print(f"   socat -d -d pty,raw,echo=0,link={VIRTUAL_PORT} pty,raw,echo=0,link=/dev/cu.meshtastic_rx")
        iface.close()
        return

    print("🔄 Трансляция NMEA в Google Earth...")
    print("   (Google Earth: Tools → GPS → Realtime → NMEA → /dev/cu.meshtastic_rx)")

    try:
        while True:
            my_info = iface.getMyNodeInfo()
            pos = my_info.get('position') if my_info else None

            if pos and pos.get('latitude') is not None:
                lat = pos['latitude']
                lon = pos['longitude']
                alt = pos.get('altitude', 0.0) or 0.0

                nmea = build_gga(lat, lon, alt)
                ser.write(nmea.encode('ascii'))
                print(f"📡 {nmea.strip()}")
            else:
                print("⏳ Ожидание GPS-фикса...")

            time.sleep(UPDATE_INTERVAL)

    except KeyboardInterrupt:
        print("\n⏹️ Остановка...")
    finally:
        ser.close()
        iface.close()
        print("👋 Скрипт завершён.")


if __name__ == "__main__":
    main()
