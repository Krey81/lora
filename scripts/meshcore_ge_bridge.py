#!/usr/bin/env python3
"""
MeshCore BLE -> Google Earth Pro (Live KML).
"""

import asyncio
import math
from contextlib import asynccontextmanager
from lxml import etree

from bleak import BleakScanner
from meshcore import MeshCore

from pyLiveKML import (
    Document, Placemark, Point, LookAt,
    Style, IconStyle, Icon,
    kml_root_tag,
)

from fastapi import FastAPI
from fastapi.responses import Response
import uvicorn


# ================= НАСТРОЙКИ =================
BLE_DEVICE_NAME   = "MeshCore-Krey81-echo"
SERVER_HOST       = "127.0.0.1"
SERVER_PORT       = 8080

POLL_INTERVAL     = 5.0     # секунд между опросами ноды
MOVE_THRESHOLD_M  = 0.05    # мин. сдвиг для отправки в GE (м)
RECONNECT_DELAY   = 5.0     # пауза перед переподключением

CAMERA_RANGE      = 500.0
CAMERA_TILT       = 45.0
CAMERA_HEADING    = 0.0
# =============================================


# ================= СОСТОЯНИЕ =================
_latest    = {"lat": None, "lon": None, "alt": 0.0}
_last_sent = {"lat": None, "lon": None}
_mc: MeshCore | None = None

_point_style = Style(
    icon_style=IconStyle(
        icon=Icon(href="http://maps.google.com/mapfiles/kml/shapes/placemark_circle.png"),
        scale=1.2,
    )
)


# ================= ПОЗИЦИЯ =================

async def fetch_position() -> bool:
    global _latest
    if _mc is None:
        return False
    try:
        result = await _mc.commands.send_appstart()
        info = result.payload if result else None
        if not info:
            return False

        lat = info.get("adv_lat") or info.get("lat") or info.get("latitude")
        lon = info.get("adv_lon") or info.get("lon") or info.get("longitude")
        alt = info.get("adv_alt") or info.get("alt") or info.get("altitude") or 0.0

        if lat is None or lon is None:
            return False

        if abs(lat) > 1000 or abs(lon) > 1000:
            lat /= 1e6
            lon /= 1e6

        if abs(lat) < 0.0001 and abs(lon) < 0.0001:
            return False

        _latest = {"lat": lat, "lon": lon, "alt": alt}
        print(f"Position: {lat:.6f}, {lon:.6f}, alt={alt:.1f}")
        return True
    except Exception as e:
        print(f"fetch_position error: {e}")
        return False


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
    etree.SubElement(doc, "name").text = "MeshCore Live Tracking"

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
    global _mc
    print(f"Сканирование BLE: {BLE_DEVICE_NAME}...")
    try:
        device = await BleakScanner.find_device_by_name(BLE_DEVICE_NAME, timeout=15)
        if device is None:
            print("Устройство не найдено.")
            return False
        print(f"Подключение к {device.address}...")
        _mc = await MeshCore.create_ble(device.address)
        print("Подключено.")
        return True
    except Exception as e:
        print(f"Ошибка подключения: {e}")
        _mc = None
        return False


async def poll_loop():
    global _mc
    while True:
        if _mc is None:
            if not await connect_ble():
                await asyncio.sleep(RECONNECT_DELAY)
                continue
            await fetch_position()

        await asyncio.sleep(POLL_INTERVAL)
        ok = await fetch_position()
        if not ok:
            try:
                connected = _mc is not None and getattr(_mc, "is_connected", lambda: True)()
                if not connected:
                    print("BLE отвалился, переподключение...")
                    _mc = None
            except Exception:
                _mc = None


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
    if _mc is not None:
        try:
            await _mc.disconnect()
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