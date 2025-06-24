"""
Enhanced JoyDance with ESP32-C3 Controller Support (Windows-compatible)
Improved architecture with device abstraction and connection monitoring
"""
import asyncio
import json
import logging
import platform
import re
import socket
import time
import struct
from abc import ABC, abstractmethod
from configparser import ConfigParser
from enum import Enum
from typing import Dict, List, Optional, Tuple, Union
from dataclasses import dataclass
import concurrent.futures

import aiohttp
from aiohttp import WSMsgType, web

# HID support for Joy-Con controllers (with graceful fallback)
HID_AVAILABLE = False
try:
    import hid
    HID_AVAILABLE = True
    print("✅ HID support loaded (Joy-Con controllers available)")
except ImportError as e:
    print(f"⚠️  HID support unavailable: {e}")
    print("   Joy-Con controllers will not be available, but ESP32 BLE controllers will work fine.")
    print("   To enable Joy-Con support, install hidapi:")
    print("   - Windows: pip install hidapi")
    print("   - Or try: pip uninstall hid && pip install hid")

# BLE support for ESP32 controllers
BLE_AVAILABLE = False
try:
    from bleak import BleakClient, BleakScanner
    from bleak.backends.characteristic import BleakGATTCharacteristic
    BLE_AVAILABLE = True
    print("✅ BLE support loaded (ESP32-C3 controllers available)")
except ImportError:
    BLE_AVAILABLE = False
    print("⚠️  BLE support unavailable. Install with: pip install bleak")

# JoyDance core imports (with fallback handling)
try:
    from joydance import JoyDance, PairingState
    from joydance.constants import (DEFAULT_CONFIG, JOYDANCE_VERSION,
                                    WsSubprotocolVersion)
    JOYDANCE_CORE_AVAILABLE = True
except ImportError as e:
    print(f"⚠️  JoyDance core modules unavailable: {e}")
    JOYDANCE_CORE_AVAILABLE = False
    # Define fallback constants
    class PairingState:
        IDLE = type('obj', (object,), {'value': 0})()
        CONNECTING = type('obj', (object,), {'value': 3})()
        CONNECTED = type('obj', (object,), {'value': 4})()
        ERROR_CONNECTION = type('obj', (object,), {'value': 102})()
    
    class WsSubprotocolVersion:
        V1 = type('obj', (object,), {'value': 'v1'})()
        V2 = type('obj', (object,), {'value': 'v2'})()
    
    DEFAULT_CONFIG = {
        'pairing_method': 'default',
        'host_ip_addr': '',
        'console_ip_addr': '',
        'pairing_code': '',
        'accel_acquisition_freq_hz': 200,
        'accel_acquisition_latency': 0,
        'accel_max_range': 8,
    }
    JOYDANCE_VERSION = '0.5.2-enhanced'

# Joy-Con specific imports (with fallback)
if HID_AVAILABLE:
    try:
        from pycon import ButtonEventJoyCon, JoyCon
        from pycon.constants import JOYCON_PRODUCT_IDS, JOYCON_VENDOR_ID
        JOYCON_AVAILABLE = True
    except ImportError as e:
        print(f"⚠️  Joy-Con modules unavailable: {e}")
        JOYCON_AVAILABLE = False
        # Define fallback constants
        JOYCON_PRODUCT_IDS = [0x2006, 0x2007]
        JOYCON_VENDOR_ID = 0x057e
else:
    JOYCON_AVAILABLE = False
    JOYCON_PRODUCT_IDS = [0x2006, 0x2007]
    JOYCON_VENDOR_ID = 0x057e

logging.getLogger('asyncio').setLevel(logging.WARNING)


# Sensor data abstraction
@dataclass
class SensorData:
    """Unified sensor data structure"""
    timestamp: float
    acceleration: Tuple[float, float, float]
    gyroscope: Optional[Tuple[float, float, float]] = None
    device_id: str = ""


@dataclass
class ControllerCapabilities:
    """Controller capabilities description"""
    has_buttons: bool = False
    has_accelerometer: bool = False
    has_gyroscope: bool = False
    has_analog_sticks: bool = False
    has_battery_info: bool = False
    connection_type: str = "unknown"


@dataclass
class DeviceInfo:
    """Device information"""
    device_id: str
    name: str
    device_type: str
    vendor_id: int
    product_id: int
    serial: str
    color: str = "#000000"
    battery_level: int = 100
    is_left: bool = True
    capabilities: ControllerCapabilities = None


@dataclass
class ConnectionStatus:
    """Device connection status"""
    is_connected: bool = False
    health_score: float = 1.0  # 0.0 - 1.0
    packet_loss_rate: float = 0.0
    latency_ms: float = 0.0
    last_data_time: float = 0.0
    reconnect_attempts: int = 0


# Abstract interfaces
class Controller(ABC):
    """Abstract base class for all controllers"""
    
    def __init__(self, device_info: DeviceInfo):
        self.device_info = device_info
        self.connection_status = ConnectionStatus()
        self._sensor_buffer: List[SensorData] = []
        self._last_data_time = 0.0
    
    @abstractmethod
    async def connect(self) -> bool:
        """Connect to device"""
        pass
    
    @abstractmethod
    async def disconnect(self) -> bool:
        """Disconnect from device"""
        pass
    
    @abstractmethod
    def get_accels(self) -> List[Tuple[float, float, float]]:
        """Get accelerometer data (JoyDance compatibility)"""
        pass
    
    @abstractmethod
    def get_capabilities(self) -> ControllerCapabilities:
        """Get controller capabilities"""
        pass
    
    def is_left(self) -> bool:
        """Determine left/right controller"""
        return self.device_info.is_left
    
    def events(self) -> List:
        """Button events (compatibility)"""
        return []
    
    def get_status(self) -> Dict:
        """Get device status (compatibility)"""
        return {
            'battery': {'level': self.device_info.battery_level},
            'analog-sticks': {
                'left': {'horizontal': 0, 'vertical': 0},
                'right': {'horizontal': 0, 'vertical': 0}
            }
        }
    
    def update_connection_health(self, packet_received: bool, latency: float = 0.0):
        """Update connection quality statistics"""
        current_time = time.time()
        
        if packet_received:
            self.connection_status.last_data_time = current_time
            self.connection_status.latency_ms = latency
        
        # Calculate packet loss rate
        time_since_last_data = current_time - self.connection_status.last_data_time
        if time_since_last_data > 1.0:  # No data for more than 1 second
            self.connection_status.packet_loss_rate = min(1.0, time_since_last_data / 5.0)
        else:
            self.connection_status.packet_loss_rate *= 0.95  # Smooth recovery
        
        # Update overall health score
        latency_factor = max(0, 1.0 - (self.connection_status.latency_ms / 100.0))
        packet_factor = 1.0 - self.connection_status.packet_loss_rate
        self.connection_status.health_score = (latency_factor + packet_factor) / 2.0


class JoyConController(Controller):
    """Controller for Joy-Con devices"""
    
    def __init__(self, device_info: DeviceInfo):
        super().__init__(device_info)
        self._joycon = None
        
    async def connect(self) -> bool:
        """Connect to Joy-Con"""
        if not JOYCON_AVAILABLE:
            logging.error("Joy-Con support not available")
            return False
            
        try:
            self._joycon = ButtonEventJoyCon(
                self.device_info.vendor_id,
                self.device_info.product_id,
                self.device_info.serial
            )
            self.connection_status.is_connected = True
            return True
        except Exception as e:
            logging.error(f"Joy-Con connection error {self.device_info.serial}: {e}")
            return False
    
    async def disconnect(self) -> bool:
        """Disconnect from Joy-Con"""
        if self._joycon:
            try:
                self._joycon.__del__()
            except:
                pass
            self._joycon = None
            self.connection_status.is_connected = False
        return True
    
    def get_accels(self) -> List[Tuple[float, float, float]]:
        """Get accelerometer data from Joy-Con"""
        if not self._joycon:
            return []
        
        try:
            accels = self._joycon.get_accels()
            self.update_connection_health(True)
            return accels
        except Exception as e:
            logging.error(f"Joy-Con data read error {self.device_info.serial}: {e}")
            self.update_connection_health(False)
            return []
    
    def get_capabilities(self) -> ControllerCapabilities:
        """Joy-Con capabilities"""
        return ControllerCapabilities(
            has_buttons=True,
            has_accelerometer=True,
            has_gyroscope=True,
            has_analog_sticks=True,
            has_battery_info=True,
            connection_type="HID"
        )
    
    def events(self):
        """Joy-Con button events"""
        if self._joycon:
            try:
                return self._joycon.events()
            except:
                return []
        return []
    
    def get_status(self):
        """Joy-Con status"""
        if self._joycon:
            try:
                return self._joycon.get_status()
            except:
                pass
        return super().get_status()


class BleEsp32Controller(Controller):
    """Controller for ESP32-C3 BLE devices"""
    
    # UUIDs for BLE service and characteristic (configurable)
    SERVICE_UUID = "12345678-1234-1234-1234-123456789abc"
    CHAR_UUID = "87654321-4321-4321-4321-cba987654321"
    
    def __init__(self, device_info: DeviceInfo, ble_address: str):
        super().__init__(device_info)
        self.ble_address = ble_address
        self._client: Optional[BleakClient] = None
        self._characteristic = None
        self._connection_task: Optional[asyncio.Task] = None
        self._accel_buffer: List[Tuple[float, float, float]] = []
        self._max_reconnect_attempts = 5
        
    async def connect(self) -> bool:
        """Connect to ESP32 via BLE"""
        if not BLE_AVAILABLE:
            logging.error("BLE support unavailable")
            return False
            
        try:
            self._client = BleakClient(self.ble_address)
            await self._client.connect()
            
            # Search for IMU data characteristic
            services = await self._client.get_services()
            for service in services:
                if service.uuid.lower() == self.SERVICE_UUID.lower():
                    for char in service.characteristics:
                        if char.uuid.lower() == self.CHAR_UUID.lower():
                            self._characteristic = char
                            break
            
            if not self._characteristic:
                # Try to use first available characteristic with NOTIFY
                for service in services:
                    for char in service.characteristics:
                        if "notify" in char.properties:
                            self._characteristic = char
                            logging.warning(f"Using characteristic {char.uuid} instead of expected")
                            break
                    if self._characteristic:
                        break
            
            if not self._characteristic:
                raise Exception("No suitable characteristic found for IMU data")
            
            # Subscribe to notifications
            await self._client.start_notify(self._characteristic, self._on_data_received)
            
            self.connection_status.is_connected = True
            self.connection_status.reconnect_attempts = 0
            
            logging.info(f"ESP32 {self.device_info.serial} connected")
            return True
            
        except Exception as e:
            logging.error(f"ESP32 connection error {self.device_info.serial}: {e}")
            await self._cleanup_connection()
            return False
    
    async def disconnect(self) -> bool:
        """Disconnect from ESP32"""
        await self._cleanup_connection()
        return True
    
    async def _cleanup_connection(self):
        """Cleanup connection"""
        if self._connection_task:
            self._connection_task.cancel()
            self._connection_task = None
            
        if self._client and self._client.is_connected:
            try:
                if self._characteristic:
                    await self._client.stop_notify(self._characteristic)
                await self._client.disconnect()
            except Exception as e:
                logging.error(f"Disconnect error: {e}")
        
        self._client = None
        self._characteristic = None
        self.connection_status.is_connected = False
    
    async def _on_data_received(self, sender, data: bytearray):
        """Handle BLE data reception"""
        try:
            current_time = time.time()
            
            # Parse data depending on format
            if len(data) >= 12:  # 6 int16 values (ax, ay, az, gx, gy, gz)
                values = struct.unpack('<6h', data[:12])
                
                # Convert to physical units (configurable)
                accel_scale = 8.0 / 32768.0  # ±8g range
                ax = values[0] * accel_scale
                ay = values[1] * accel_scale
                az = values[2] * accel_scale
                
                # Add to buffer
                self._accel_buffer.append((ax, ay, az))
                
                # Limit buffer size
                if len(self._accel_buffer) > 100:
                    self._accel_buffer = self._accel_buffer[-50:]
                
                self.update_connection_health(True, 0.0)
                
            elif len(data) > 0:
                # Try parsing JSON format
                try:
                    json_str = data.decode('utf-8')
                    json_data = json.loads(json_str)
                    
                    if all(key in json_data for key in ['ax', 'ay', 'az']):
                        ax = float(json_data['ax'])
                        ay = float(json_data['ay'])
                        az = float(json_data['az'])
                        
                        self._accel_buffer.append((ax, ay, az))
                        
                        if len(self._accel_buffer) > 100:
                            self._accel_buffer = self._accel_buffer[-50:]
                            
                        self.update_connection_health(True, 0.0)
                        
                except (UnicodeDecodeError, json.JSONDecodeError):
                    logging.warning(f"Unknown data format from ESP32: {data}")
            
        except Exception as e:
            logging.error(f"ESP32 data processing error: {e}")
            self.update_connection_health(False)
    
    def get_accels(self) -> List[Tuple[float, float, float]]:
        """Get accelerometer data from ESP32"""
        if not self._accel_buffer:
            return []
        
        # Return all accumulated data and clear buffer
        result = self._accel_buffer.copy()
        self._accel_buffer.clear()
        return result
    
    def get_capabilities(self) -> ControllerCapabilities:
        """ESP32 controller capabilities"""
        return ControllerCapabilities(
            has_buttons=False,
            has_accelerometer=True,
            has_gyroscope=True,
            has_analog_sticks=False,
            has_battery_info=False,
            connection_type="BLE"
        )
    
    async def reconnect_if_needed(self):
        """Automatic reconnection on connection loss"""
        if (self.connection_status.is_connected and 
            self.connection_status.health_score < 0.3 and
            self.connection_status.reconnect_attempts < self._max_reconnect_attempts):
            
            logging.info(f"Attempting ESP32 reconnection {self.device_info.serial}")
            self.connection_status.reconnect_attempts += 1
            
            await self.disconnect()
            await asyncio.sleep(2.0)  # Delay before reconnection
            
            if await self.connect():
                logging.info(f"ESP32 {self.device_info.serial} reconnected successfully")
            else:
                logging.error(f"Failed to reconnect ESP32 {self.device_info.serial}")
    @property
    def serial(self):
        return self.device_info.serial

    def __del__(self):
        # BLE cleanup if needed
        pass

# Это удовлетворит JoyDance,
# что серийный номер есть и метод __del__() присутствует.



class ControllerManager:
    """Controller manager with factory pattern"""
    
    def __init__(self):
        self.controllers: Dict[str, Controller] = {}
        self._monitor_task: Optional[asyncio.Task] = None
        
    async def discover_devices(self) -> List[DeviceInfo]:
        """Discover all available devices"""
        devices = []
        
        # Search for Joy-Con devices
        if JOYCON_AVAILABLE:
            try:
                joycon_devices = await self._discover_joycon_devices()
                devices.extend(joycon_devices)
            except Exception as e:
                logging.error(f"Joy-Con device discovery error: {e}")
        
        # Search for BLE ESP32 devices
        if BLE_AVAILABLE:
            try:
                esp32_devices = await self._discover_esp32_devices()
                devices.extend(esp32_devices)
            except Exception as e:
                logging.error(f"ESP32 device discovery error: {e}")
        
        return devices
    
    async def _discover_joycon_devices(self) -> List[DeviceInfo]:
        """Discover Joy-Con devices"""
        devices = []
        
        if not HID_AVAILABLE:
            return devices
            
        try:
            hid_devices = hid.enumerate(JOYCON_VENDOR_ID, 0)
            
            for device in hid_devices:
                vendor_id = device['vendor_id']
                product_id = device['product_id']
                product_string = device['product_string']
                serial = device.get('serial') or device.get('serial_number')
                
                if product_id not in JOYCON_PRODUCT_IDS or not product_string:
                    continue
                
                # Get additional information
                try:
                    temp_joycon = JoyCon(vendor_id, product_id, serial)
                    time.sleep(0.05)  # Wait for initialization
                    
                    battery_level = temp_joycon.get_battery_level()
                    color = '#%02x%02x%02x' % temp_joycon.color_body
                    is_left = temp_joycon.is_left()
                    
                    if platform.system() != 'Windows':
                        temp_joycon.__del__()
                    
                    device_info = DeviceInfo(
                        device_id=f"joycon_{serial}",
                        name=product_string,
                        device_type="joycon",
                        vendor_id=vendor_id,
                        product_id=product_id,
                        serial=serial,
                        color=color,
                        battery_level=battery_level,
                        is_left=is_left,
                        capabilities=ControllerCapabilities(
                            has_buttons=True,
                            has_accelerometer=True,
                            has_gyroscope=True,
                            has_analog_sticks=True,
                            has_battery_info=True,
                            connection_type="HID"
                        )
                    )
                    devices.append(device_info)
                    
                except Exception as e:
                    logging.error(f"Joy-Con info retrieval error {serial}: {e}")
        except Exception as e:
            logging.error(f"HID enumeration error: {e}")
        
        return devices
    
    async def _discover_esp32_devices(self) -> List[DeviceInfo]:
        """Discover ESP32 BLE devices"""
        devices = []
        
        try:
            # Scan BLE devices
            ble_devices = await BleakScanner.discover(timeout=5.0)

            # 🔍 ОТЛАДОЧНЫЙ ВЫВОД:
            print("📡 BLE devices found:")
            for dev in ble_devices:
                print(f"  - Name: {dev.name}, Address: {dev.address}, RSSI: {dev.rssi}")

            
            for device in ble_devices:
                # Filter by device name
                if (device.name and 
                    ("joydance" in device.name.lower() or 
                     "esp32" in device.name.lower() or
                     device.name.lower().startswith("jd_"))):
                    
                    device_info = DeviceInfo(
                        device_id=f"esp32_{device.address}",
                        name=device.name or f"ESP32 {device.address[-5:]}",
                        device_type="esp32_ble",
                        vendor_id=0xFFFF,  # Virtual vendor_id for BLE
                        product_id=0x0001,  # Virtual product_id for ESP32
                        serial=device.address,
                        color="#0066CC",  # Blue color for ESP32
                        battery_level=100,  # ESP32 usually powered from mains
                        is_left=True,  # Default left
                        capabilities=ControllerCapabilities(
                            has_buttons=False,
                            has_accelerometer=True,
                            has_gyroscope=True,
                            has_analog_sticks=False,
                            has_battery_info=False,
                            connection_type="BLE"
                        )
                    )
                    devices.append(device_info)
                    
        except Exception as e:
            logging.error(f"BLE device scan error: {e}")
        
        return devices
    
    async def connect_device(self, device_info: DeviceInfo) -> Optional[Controller]:
        """Connect to device"""
        controller = None
        
        try:
            if device_info.device_type == "joycon":
                if not JOYCON_AVAILABLE:
                    logging.error("Joy-Con support not available")
                    return None
                controller = JoyConController(device_info)
            elif device_info.device_type == "esp32_ble":
                if not BLE_AVAILABLE:
                    logging.error("BLE support not available")
                    return None
                controller = BleEsp32Controller(device_info, device_info.serial)
            else:
                logging.error(f"Unknown device type: {device_info.device_type}")
                return None
            
            if await controller.connect():
                self.controllers[device_info.device_id] = controller
                
                # Start connection monitoring if not already running
                if not self._monitor_task:
                    self._monitor_task = asyncio.create_task(self._monitor_connections())
                
                return controller
            else:
                return None
                
        except Exception as e:
            logging.error(f"Device connection error {device_info.device_id}: {e}")
            return None
    
    async def disconnect_device(self, device_id: str) -> bool:
        """Disconnect device"""
        if device_id in self.controllers:
            controller = self.controllers[device_id]
            success = await controller.disconnect()
            del self.controllers[device_id]
            
            # Stop monitoring if no active connections
            if not self.controllers and self._monitor_task:
                self._monitor_task.cancel()
                self._monitor_task = None
            
            return success
        return False
    
    async def _monitor_connections(self):
        """Monitor connection quality"""
        while True:
            try:
                for controller in list(self.controllers.values()):
                    # Check ESP32 controllers for reconnection needs
                    if isinstance(controller, BleEsp32Controller):
                        await controller.reconnect_if_needed()
                    
                    # Log statistics (optional)
                    if controller.connection_status.health_score < 0.5:
                        logging.warning(
                            f"Low connection quality with {controller.device_info.device_id}: "
                            f"health={controller.connection_status.health_score:.2f}, "
                            f"packet_loss={controller.connection_status.packet_loss_rate:.2f}"
                        )
                
                await asyncio.sleep(5.0)  # Check every 5 seconds
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logging.error(f"Connection monitoring error: {e}")
                await asyncio.sleep(1.0)


# Global controller manager
controller_manager = ControllerManager()


# Mock JoyDance class for fallback
class MockJoyDance:
    """Mock JoyDance class when core is unavailable"""
    def __init__(self, controller, **kwargs):
        self.controller = controller
        self.serial = controller.device_info.serial if controller else "unknown"
        
    async def pair(self):
        print(f"Mock pairing for {self.serial}")
        
    async def disconnect(self):
        print(f"Mock disconnect for {self.serial}")


# Updated enums
class WsCommand(Enum):
    GET_JOYCON_LIST = 'get_joycon_list'
    CONNECT_JOYCON = 'connect_joycon'
    DISCONNECT_JOYCON = 'disconnect_joycon'
    UPDATE_JOYCON_STATE = 'update_joycon_state'
    GET_CONNECTION_STATS = 'get_connection_stats'
    GET_SYSTEM_STATUS = 'get_system_status'  # New command


class PairingMethod(Enum):
    DEFAULT = 'default'
    FAST = 'fast'
    STADIA = 'stadia'
    OLD = 'old'


# Regular expressions
REGEX_PAIRING_CODE = re.compile(r'^\d{6}$')
REGEX_LOCAL_IP_ADDRESS = re.compile(r'^(192\.168|10.(\d{1,2}|1\d\d|2[0-4]\d|25[0-5]))\.((\d{1,2}|1\d\d|2[0-4]\d|25[0-5])\.)(\d{1,2}|1\d\d|2[0-4]\d|25[0-5])$')


# Main functions (updated)
async def get_device_ids():
    """Get list of all available devices"""
    try:
        device_infos = await controller_manager.discover_devices()
        
        devices = []
        for device_info in device_infos:
            devices.append({
                'vendor_id': device_info.vendor_id,
                'product_id': device_info.product_id,
                'serial': device_info.serial,
                'product_string': device_info.name,
                'device_type': device_info.device_type,
                'device_id': device_info.device_id,
            })
        
        return devices
    except Exception as e:
        logging.error(f"Device list retrieval error: {e}")
        return []


async def get_joycon_list(app):
    """Get controller list with enhanced information"""
    controllers = []
    devices = await get_device_ids()
    
    for dev in devices:
        device_id = dev['device_id']
        serial = dev['serial']
        
        if serial in app['joycons_info']:
            info = app['joycons_info'][serial]
        else:
            # Determine color based on device type
            if dev['device_type'] == 'esp32_ble':
                color = '#0066CC'  # Blue for ESP32
                battery_level = 100
                is_left = True
            else:
                # Get real data for Joy-Con
                try:
                    temp_devices = await controller_manager.discover_devices()
                    device_info = next((d for d in temp_devices if d.serial == serial), None)
                    
                    if device_info:
                        color = device_info.color
                        battery_level = device_info.battery_level
                        is_left = device_info.is_left
                    else:
                        color = '#000000'
                        battery_level = 0
                        is_left = True
                        
                except Exception as e:
                    logging.error(f"Device info retrieval error {serial}: {e}")
                    color = '#000000'
                    battery_level = 0
                    is_left = True
            
            info = {
                'vendor_id': dev['vendor_id'],
                'product_id': dev['product_id'],
                'serial': serial,
                'device_id': device_id,
                'device_type': dev['device_type'],
                'name': dev['product_string'],
                'color': color,
                'battery_level': battery_level,
                'is_left': is_left,
                'state': PairingState.IDLE.value,
                'pairing_code': '',
                'connection_health': 1.0,
            }
            
            app['joycons_info'][serial] = info
        
        # Update connection statistics if device is connected
        if device_id in controller_manager.controllers:
            controller = controller_manager.controllers[device_id]
            info['connection_health'] = controller.connection_status.health_score
            info['packet_loss'] = controller.connection_status.packet_loss_rate
            info['latency_ms'] = controller.connection_status.latency_ms
        
        controllers.append(info)
    
    return sorted(controllers, key=lambda x: (x['device_type'], x['name'], x['color'], x['serial']))


async def connect_joycon(app, ws, data):
    """Connect controller with enhanced handling"""
    async def on_joydance_state_changed(serial, state):
        print(f"State {serial}: {state}")
        if serial in app['joycons_info']:
            app['joycons_info'][serial]['state'] = state.value
            try:
                await ws_send_response(ws, WsCommand.UPDATE_JOYCON_STATE, app['joycons_info'][serial])
            except Exception as e:
                logging.error(f"State update send error: {e}")
    
    try:
        print(f"Connection data: {data}")
        
        serial = data['joycon_serial']
        device_info = None
        
        # Search for device information
        if serial in app['joycons_info']:
            joycon_info = app['joycons_info'][serial]
            device_id = joycon_info.get('device_id', f"unknown_{serial}")
            
            # Get full device information
            devices = await controller_manager.discover_devices()
            device_info = next((d for d in devices if d.serial == serial), None)
        
        if not device_info:
            logging.error(f"Device {serial} not found")
            return
        
        # Connect via controller manager
        controller = await controller_manager.connect_device(device_info)
        if not controller:
            logging.error(f"Failed to connect device {serial}")
            return
        
        # Setup connection parameters
        pairing_method = data['pairing_method']
        host_ip_addr = data['host_ip_addr']
        console_ip_addr = data['console_ip_addr']
        pairing_code = data['pairing_code']
        
        # Create JoyDance connection (use mock if core unavailable)
        if JOYDANCE_CORE_AVAILABLE:
            # Determine protocol version
            protocol_version = WsSubprotocolVersion.V1 if pairing_method == PairingMethod.OLD.value else WsSubprotocolVersion.V2
            
            joydance = JoyDance(
                controller,
                protocol_version=protocol_version,
                pairing_code=pairing_code,
                host_ip_addr=host_ip_addr,
                console_ip_addr=console_ip_addr,
                on_state_changed=on_joydance_state_changed,
            )
        else:
            joydance = MockJoyDance(controller)
        
        app['joydance_connections'][serial] = joydance
        
        # Start connection process
        asyncio.create_task(joydance.pair())
        
        logging.info(f"Device {serial} ({device_info.device_type}) connected")
        
    except Exception as e:
        logging.error(f"Device connection error: {e}")
        if 'serial' in locals() and serial in app.get('joycons_info', {}):
            app['joycons_info'][serial]['state'] = PairingState.ERROR_CONNECTION.value


async def disconnect_joycon(app, ws, data):
    """Disconnect controller with enhanced handling"""
    try:
        print(f"Disconnect data: {data}")
        serial = data['joycon_serial']
        
        # Disconnect JoyDance connection
        if serial in app['joydance_connections']:
            joydance = app['joydance_connections'][serial]
            await joydance.disconnect()
            del app['joydance_connections'][serial]
        
        # Disconnect controller via manager
        if serial in app['joycons_info']:
            device_id = app['joycons_info'][serial].get('device_id')
            if device_id:
                await controller_manager.disconnect_device(device_id)
            
            app['joycons_info'][serial]['state'] = PairingState.IDLE.value
        
        # Send status update
        try:
            await ws_send_response(ws, WsCommand.UPDATE_JOYCON_STATE, {
                'joycon_serial': serial,
                'state': PairingState.IDLE.value,
            })
        except Exception:
            pass
        
        logging.info(f"Device {serial} disconnected")
        
    except Exception as e:
        logging.error(f"Device disconnect error: {e}")


async def get_connection_stats(app):
    """Get connection statistics"""
    stats = {}
    
    for device_id, controller in controller_manager.controllers.items():
        stats[device_id] = {
            'is_connected': controller.connection_status.is_connected,
            'health_score': controller.connection_status.health_score,
            'packet_loss_rate': controller.connection_status.packet_loss_rate,
            'latency_ms': controller.connection_status.latency_ms,
            'reconnect_attempts': controller.connection_status.reconnect_attempts,
            'device_type': controller.device_info.device_type,
            'capabilities': {
                'has_buttons': controller.get_capabilities().has_buttons,
                'has_accelerometer': controller.get_capabilities().has_accelerometer,
                'connection_type': controller.get_capabilities().connection_type,
            }
        }
    
    return stats


async def get_system_status(app):
    """Get system status"""
    return {
        'hid_available': HID_AVAILABLE,
        'ble_available': BLE_AVAILABLE,
        'joycon_available': JOYCON_AVAILABLE,
        'joydance_core_available': JOYDANCE_CORE_AVAILABLE,
        'active_controllers': len(controller_manager.controllers),
        'supported_devices': [
            'esp32_ble' if BLE_AVAILABLE else None,
            'joycon' if JOYCON_AVAILABLE else None
        ]
    }


# Configuration functions
def parse_config():
    """Parse configuration"""
    parser = ConfigParser()
    parser.read('config.cfg')
    
    if 'joydance' not in parser:
        parser['joydance'] = DEFAULT_CONFIG
    else:
        tmp_config = DEFAULT_CONFIG.copy()
        for key in tmp_config:
            if key in parser['joydance']:
                val = parser['joydance'][key]
                if key == 'pairing_method':
                    if not is_valid_pairing_method(val):
                        val = PairingMethod.DEFAULT.value
                elif key in ['host_ip_addr', 'console_ip_addr']:
                    if not is_valid_ip_address(val):
                        val = ''
                elif key == 'pairing_code':
                    if not is_valid_pairing_code(val):
                        val = ''
                elif key.startswith('accel_'):
                    try:
                        val = int(val)
                    except Exception:
                        val = DEFAULT_CONFIG[key]
                
                tmp_config[key] = val
        
        parser['joydance'] = tmp_config
    
    if not parser['joydance']['host_ip_addr']:
        host_ip_addr = get_host_ip()
        if host_ip_addr:
            parser['joydance']['host_ip_addr'] = host_ip_addr
    
    save_config(parser)
    return parser


def is_valid_pairing_code(val):
    """Validate pairing code"""
    return re.match(REGEX_PAIRING_CODE, val) is not None


def is_valid_ip_address(val):
    """Validate IP address"""
    return re.match(REGEX_LOCAL_IP_ADDRESS, val) is not None


def is_valid_pairing_method(val):
    """Validate pairing method"""
    return val in [
        PairingMethod.DEFAULT.value,
        PairingMethod.FAST.value,
        PairingMethod.STADIA.value,
        PairingMethod.OLD.value,
    ]


def get_host_ip():
    """Get host IP address"""
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if ip.startswith('192.168') or ip.startswith('10.'):
                return ip
    except Exception:
        pass
    return None


def save_config(parser):
    """Save configuration"""
    with open('config.cfg', 'w') as fp:
        parser.write(fp)


async def on_startup(app):
    """Application initialization"""
    print('''
     ░░  ░░░░░░  ░░    ░░ ░░░░░░   ░░░░░  ░░░    ░░  ░░░░░░ ░░░░░░░
     ▒▒ ▒▒    ▒▒  ▒▒  ▒▒  ▒▒   ▒▒ ▒▒   ▒▒ ▒▒▒▒   ▒▒ ▒▒      ▒▒
     ▒▒ ▒▒    ▒▒   ▒▒▒▒   ▒▒   ▒▒ ▒▒▒▒▒▒▒ ▒▒ ▒▒  ▒▒ ▒▒      ▒▒▒▒▒
▓▓   ▓▓ ▓▓    ▓▓    ▓▓    ▓▓   ▓▓ ▓▓   ▓▓ ▓▓  ▓▓ ▓▓ ▓▓      ▓▓
 █████   ██████     ██    ██████  ██   ██ ██   ████  ██████ ███████

Enhanced JoyDance with ESP32-C3 BLE Support (Windows Compatible)
Open http://localhost:32623 in your browser.''')
    
    # System status
    print("\n🔧 System Status:")
    if HID_AVAILABLE and JOYCON_AVAILABLE:
        print("✅ Joy-Con support: Fully available")
    elif HID_AVAILABLE:
        print("⚠️  Joy-Con support: HID available but Joy-Con modules missing")
    else:
        print("❌ Joy-Con support: HID library not available")
    
    if BLE_AVAILABLE:
        print("✅ ESP32-C3 BLE support: Available")
    else:
        print("❌ ESP32-C3 BLE support: Not available")
    
    if JOYDANCE_CORE_AVAILABLE:
        print("✅ JoyDance core: Available")
    else:
        print("⚠️  JoyDance core: Using fallback mode")
    
    print("\n📖 Installation Help:")
    if not HID_AVAILABLE:
        print("   For Joy-Con support install: pip install hidapi")
    if not BLE_AVAILABLE:
        print("   For ESP32 support install: pip install bleak")
    
    # Check for updates (if core available)
    if JOYDANCE_CORE_AVAILABLE:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get('https://api.github.com/repos/redphx/joydance/releases/latest', ssl=False) as resp:
                    json_body = await resp.json()
                    latest_version = json_body['tag_name'][1:]
                    print(f'\n📦 Current version: {JOYDANCE_VERSION}')
                    if JOYDANCE_VERSION != latest_version:
                        print(f'🔄 Version {latest_version} available: https://github.com/redphx/joydance')
        except Exception as e:
            logging.warning(f"Could not check for updates: {e}")


async def html_handler(request):
    """Main page handler"""
    config = dict((parse_config()).items('joydance'))
    try:
        with open('static/index.html', 'r', encoding='utf-8') as f:
            html = f.read()
            html = html.replace('[[CONFIG]]', json.dumps(config))
            html = html.replace('[[VERSION]]', JOYDANCE_VERSION)
            return web.Response(text=html, content_type='text/html')
    except FileNotFoundError:
        # Fallback HTML if static files not found
        fallback_html = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>JoyDance Enhanced</title>
            <style>
                body {{ font-family: Arial, sans-serif; margin: 40px; }}
                .status {{ padding: 20px; background: #f0f0f0; border-radius: 5px; }}
                .available {{ color: green; }}
                .unavailable {{ color: red; }}
            </style>
        </head>
        <body>
            <h1>🎮 JoyDance Enhanced</h1>
            <div class="status">
                <h2>System Status</h2>
                <p class="{'available' if HID_AVAILABLE else 'unavailable'}">
                    Joy-Con Support: {'✅ Available' if HID_AVAILABLE else '❌ Unavailable'}
                </p>
                <p class="{'available' if BLE_AVAILABLE else 'unavailable'}">
                    ESP32 BLE Support: {'✅ Available' if BLE_AVAILABLE else '❌ Unavailable'}
                </p>
                <p class="{'available' if JOYDANCE_CORE_AVAILABLE else 'unavailable'}">
                    JoyDance Core: {'✅ Available' if JOYDANCE_CORE_AVAILABLE else '⚠️ Fallback Mode'}
                </p>
            </div>
            <h2>Installation Help</h2>
            <ul>
                {'<li>✅ Joy-Con support is ready</li>' if HID_AVAILABLE else '<li>❌ Install Joy-Con support: <code>pip install hidapi</code></li>'}
                {'<li>✅ ESP32 BLE support is ready</li>' if BLE_AVAILABLE else '<li>❌ Install ESP32 support: <code>pip install bleak</code></li>'}
            </ul>
            <p><strong>Note:</strong> The original static files are missing. Please copy them from the original JoyDance installation.</p>
        </body>
        </html>
        """
        return web.Response(text=fallback_html, content_type='text/html')


async def ws_send_response(ws, cmd, data):
    """Send response via WebSocket"""
    resp = {
        'cmd': 'resp_' + cmd.value,
        'data': data,
    }
    await ws.send_json(resp)


async def websocket_handler(request):
    """WebSocket connection handler"""
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    
    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    msg_data = msg.json()
                    cmd = WsCommand(msg_data['cmd'])
                except (ValueError, KeyError, json.JSONDecodeError) as e:
                    logging.error(f'Invalid command: {msg_data if "msg_data" in locals() else msg.data}')
                    continue
                
                # Handle commands
                try:
                    if cmd == WsCommand.GET_JOYCON_LIST:
                        joycon_list = await get_joycon_list(request.app)
                        await ws_send_response(ws, cmd, joycon_list)
                    elif cmd == WsCommand.CONNECT_JOYCON:
                        await connect_joycon(request.app, ws, msg_data['data'])
                        await ws_send_response(ws, cmd, {})
                    elif cmd == WsCommand.DISCONNECT_JOYCON:
                        await disconnect_joycon(request.app, ws, msg_data['data'])
                        await ws_send_response(ws, cmd, {})
                    elif cmd == WsCommand.GET_CONNECTION_STATS:
                        stats = await get_connection_stats(request.app)
                        await ws_send_response(ws, cmd, stats)
                    elif cmd == WsCommand.GET_SYSTEM_STATUS:
                        status = await get_system_status(request.app)
                        await ws_send_response(ws, cmd, status)
                except Exception as e:
                    logging.error(f'Command handling error {cmd}: {e}')
                    await ws_send_response(ws, cmd, {'error': str(e)})
                    
            elif msg.type == WSMsgType.ERROR:
                logging.error(f'WebSocket error: {ws.exception()}')
    except Exception as e:
        logging.error(f'WebSocket connection error: {e}')
    
    return ws


def favicon_handler(request):
    """Favicon handler"""
    try:
        return web.FileResponse('static/favicon.png')
    except FileNotFoundError:
        return web.Response(status=404)


# Application initialization
app = web.Application()
app['joydance_connections'] = {}
app['joycons_info'] = {}

app.on_startup.append(on_startup)
app.add_routes([
    web.get('/', html_handler),
    web.get('/favicon.png', favicon_handler),
    web.get('/ws', websocket_handler),
    web.static('/css', 'static/css'),
    web.static('/js', 'static/js'),
])

if __name__ == '__main__':
    # Setup logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )
    
    print("🚀 Starting Enhanced JoyDance...")
    print("   If you encounter issues, check the system status above.")
    
    try:
        web.run_app(app, port=32623)
    except KeyboardInterrupt:
        print("\n👋 Shutting down...")
    except Exception as e:
        logging.error(f"Critical error: {e}")
