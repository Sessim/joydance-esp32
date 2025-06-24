import json
import asyncio
from bleak import BleakClient

class BleJoyCon:
    def __init__(self, address, name):
        self.address = address
        self.name = name
        self.buffer = []
        self.client = None
        self.connected = False

    async def connect(self):
        self.client = BleakClient(self.address)
        await self.client.connect()
        self.connected = True
        await self.client.start_notify(
            "87654321-4321-4321-4321-ba0987654321",  # UUID характеристики
            self._notification_handler
        )

    def _notification_handler(self, sender, data: bytearray):
        try:
            json_str = data.decode('utf-8')
            sensor = json.loads(json_str)
            ax, ay, az = sensor["ax"], sensor["ay"], sensor["az"]
            self.buffer.append((ax / 9.8, ay / 9.8, az / 9.8))  # Конвертация m/s² → g
        except Exception as e:
            print(f"BLE Data Error: {e}")

    def is_left(self):
        return True  # Все контроллеры считаем "левыми"

    def get_accels(self):
        current = self.buffer.copy()
        self.buffer.clear()
        return current

    def events(self):
        return []  # Нет кнопок

    def get_status(self):
        return {
            "battery": {"charging": 0, "level": 4},
            "buttons": {"right": {...}, "shared": {...}, "left": {...}},
            "analog-sticks": {...},
            "accel": [(0,0,0)] * 3
        }

    async def disconnect(self):
        if self.client and self.connected:
            await self.client.disconnect()