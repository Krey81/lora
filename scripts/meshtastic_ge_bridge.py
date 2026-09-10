#!/usr/bin/env python3
"""
Meshtastic BLE -> Google Earth Pro (Live KML).
Периодически запрашивает позицию у ноды и отдаёт её в GE через NetworkLink.
"""

import asyncio
import math
import time
from contextlib import asynccontextmanager
from lxml import etree

import meshtastic
import meshtastic.ble_interface

from pyLiveKML import (
    Document, Placemark, Point, LookAt,
    Style, IconStyle, Icon,
    kml_root_tag,
)

from fastapi import FastAPI
from fastapi.responses import Response
import uvicorn

# ================= НАСТРОЙКИ =================
BLE_DEVICE_NAME   = "Meshtastic_2178"   # <-- имя вашей ноды
SERVER_HOST       = "127.0.0.1"
SERVER_PORT       = 8080

POLL_INTERVAL     = 20.0    # секунд между запросами позиции
MOVE_THRESHOLD_M  = 5.0     # мин. сдвиг для отправки в GE (м)
RECONNECT_DELAY   = 5.0

CAMERA_RANGE      = 500.0
CAMERA_TILT       = 45.0
CAMERA_HEADING    = 0.0
# =============================================

_latest    = {"lat": None, "lon": None, "alt": 0.0}
_last_sent = {"lat": None, "lon": None}
_iface: meshtastic.ble_interface.BLEInterface | None = None
_my_node_num: int | None = None

_point_style = Style(
    icon_style=IconStyle(
        icon=Icon(href="http://maps.google.com/mapfiles/kml/shapes/placemark_circle.png"),
        scale=1.2,
    )
)


# ================= ПОЗИЦИЯ =================

def _extract_position(node: dict) -> tuple[float, float, float] | None:
    """Извлекает координаты из node['position'] (поддерживает оба формата)."""
    pos = node.get("position")
    if not pos:
        return None

    lat = pos.get("latitude")
    lon = pos.get("longitude")
    alt = pos.get("altitude", 0) or 0.0

    if lat is None or lon is None:
        lat_i = pos.get("latitudeI") or pos.get("latitude_i")
        lon_i = pos.get("longitudeI") or pos.get("longitude_i")
        if lat_i is None or lon_i is None:
            return None
        lat = lat_i / 1e7
        lon = lon_i / 1e7

    if abs(lat) < 0.0001 and abs(lon) < 0.0001:
        return None

    return (lat, lon, alt)


def fetch_position() -> bool:
    """Запрашивает позицию у ноды и обновляет _latest."""
    global _latest
    if _iface is None or _my_node_num is None:
        return False

    try:
        # Запрос с фиктивными координатами (0,0) — прошивка Meshtastic
        # требует payload, иначе запрос игнорируется.
        _iface.sendPosition(
            destinationId=_my_node_num,
            wantResponse=True,
            latitude=0.0,
            longitude=0.0,
            altitude=0,
        )
        # Ждём ответа (waitForPosition использует внутренний флаг receivedPosition)
        ok = _iface.waitForPosition(timeout=10)
        if not ok:
            print("Position request timed out")
            return False
    except Exception as e:
        print(f"sendPosition error: {e}")
        return False

    node = _iface.nodes.get(_my_node_num)
    if not node:
        return False
    coords = _extract_position(node)
    if not coords:
        return False

    lat, lon, alt = coords
    _latest = {"lat": lat, "lon": lon, "alt": alt}
    print(f"Position: {lat:.6f}, {lon:.6f}, alt={alt:.1f}")
    return True


def position_changed(lat, lon, threshold_m=MOVE_THRESHOLD_M) -> bool:
    if _last_sent["lat"] is None:
        _last_sent["lat"], _last_sent["lon"] = lat, lon
        return True
    dlat = (lat - _last_sent["lat"]) * 111_000
    dlon = (lon - _last_sent["lon"]) * 111_000 * math.cos(math.radians(lat))
    if (dlat * dlat + dlon * dlon) ** 0.5 > threshold_m:
        _last_sent["lat"], _last_sent["lon"] = lat, lon
        return True
    return False


# ================= KML =================

def serialize(doc) -> str:
    root = kml_root_tag()
    doc.build_kml(root)
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8").decode("utf-8")


def build_loader_kml() -> str:
    kml = etree.Element("kml", xmlns="http://www.opengis.net/kml/2.2")
    doc = etree.SubElement(kml, "Document")
    etree.SubElement(doc, "name").text = "Meshtastic Live Tracking"

    nl = etree.SubElement(doc, "NetworkLink")
    etree.SubElement(nl, "name").text = "Live Updates"
    etree.SubElement(nl, "flyToView").text = "1"

    link = etree.SubElement(nl, "Link")
    etree.SubElement(link, "href").text = f"http://{SERVER_HOST}:{SERVER_PORT}/update.kml"
    etree.SubElement(link, "refreshMode").text = "onInterval"
    etree.SubElement(link, "refreshInterval").text = "2"
    etree.SubElement(link, "viewRefreshMode").text = "onStop"
    etree.SubElement(link, "viewRefreshTime").text = "0.1"

    return etree.tostring(kml, xml_declaration=True, encoding="UTF-8").decode("utf-8")


def build_update_kml(lat, lon, alt) -> str:
    lookat = LookAt(
        lon=lon, lat=lat, alt=alt,
        heading=CAMERA_HEADING,
        tilt=CAMERA_TILT,
        range=CAMERA_RANGE,
    )
    pm = Placemark(
        geometry=Point(coordinates=(lon, lat, alt)),
        name="Me",
        inline_style=_point_style,
    )
    doc = Document(name="Live", features=[pm], abstract_view=lookat)
    return serialize(doc)


# ================= BLE + ОПРОС =================

async def connect_ble() -> bool:
    global _iface, _my_node_num
    print(f"Сканирование BLE: {BLE_DEVICE_NAME}...")
    try:
        _iface = meshtastic.ble_interface.BLEInterface(BLE_DEVICE_NAME)
        info = _iface.getMyNodeInfo()
        if info:
            _my_node_num = info.get("num")
        print(f"Подключено. my_node_num={_my_node_num}")
        return True
    except Exception as e:
        print(f"Ошибка подключения: {e}")
        _iface = None
        return False


async def poll_loop():
    global _iface
    while True:
        if _iface is None:
            if not await connect_ble():
                await asyncio.sleep(RECONNECT_DELAY)
                continue
            fetch_position()

        await asyncio.sleep(POLL_INTERVAL)
        ok = fetch_position()
        if not ok:
            # проверка соединения: если sendPosition кидает ошибку — переподключение
            try:
                _iface.sendPosition(destinationId=_my_node_num, wantResponse=False)
            except Exception:
                print("BLE отвалился, переподключение...")
                _iface = None


# ================= FASTAPI =================

@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(poll_loop())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    if _iface is not None:
        try:
            _iface.close()
        except Exception:
            pass


app = FastAPI(lifespan=lifespan)

_EMPTY = '<?xml version="1.0" encoding="UTF-8"?><kml xmlns="http://www.opengis.net/kml/2.2"><Document/></kml>'
_KML_MIME = "application/vnd.google-earth.kml+xml"


@app.get("/loader.kml")
async def loader():
    return Response(content=build_loader_kml(), media_type=_KML_MIME)


@app.get("/update.kml")
async def update():
    if _latest["lat"] is None:
        return Response(content=_EMPTY, media_type=_KML_MIME)
    lat, lon, alt = _latest["lat"], _latest["lon"], _latest["alt"]
    if not position_changed(lat, lon):
        return Response(content=_EMPTY, media_type=_KML_MIME)
    return Response(content=build_update_kml(lat, lon, alt), media_type=_KML_MIME)


def main():
    print(f"Сервер:   http://{SERVER_HOST}:{SERVER_PORT}")
    print(f"Загрузчик: http://{SERVER_HOST}:{SERVER_PORT}/loader.kml")
    uvicorn.run(app, host=SERVER_HOST, port=SERVER_PORT, log_level="warning")


if __name__ == "__main__":
    main()