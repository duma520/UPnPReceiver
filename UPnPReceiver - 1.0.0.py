import sys
import socket
import threading
import time
from xml.etree import ElementTree as ET
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs
from PyQt5.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                             QLabel, QPushButton, QListWidget, QTextEdit, QMessageBox)
from PyQt5.QtCore import Qt, pyqtSignal, QObject

class UPnPDevice:
    def __init__(self):
        self.uuid = ""
        self.friendly_name = ""
        self.device_type = ""
        self.service_list = []
        self.location = ""
        self.server = ""
        self.usn = ""

class UPnPMediaRenderer(UPnPDevice):
    def __init__(self):
        super().__init__()
        self.av_transport_control_url = ""
        self.rendering_control_url = ""
        self.connection_manager_url = ""

class UPnPDiscovery(QObject):
    device_discovered = pyqtSignal(UPnPDevice)
    discovery_finished = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.running = False
        self.discovered_devices = []
        self.search_target = "urn:schemas-upnp-org:device:MediaRenderer:1"

    def start_discovery(self):
        self.running = True
        threading.Thread(target=self._discover_devices, daemon=True).start()

    def stop_discovery(self):
        self.running = False

    def _discover_devices(self):
        M_SEARCH = f"""M-SEARCH * HTTP/1.1
HOST: 239.255.255.250:1900
MAN: "ssdp:discover"
MX: 3
ST: {self.search_target}
USER-AGENT: Python DLNA Receiver

"""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 4)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.settimeout(5)

        while self.running:
            try:
                sock.sendto(M_SEARCH.encode(), ("239.255.255.250", 1900))
                time.sleep(1)  # 等待响应

                while True:
                    try:
                        data, addr = sock.recvfrom(1024)
                        response = data.decode().split('\r\n')
                        headers = {}
                        for line in response[1:]:
                            if ':' in line:
                                key, value = line.split(':', 1)
                                headers[key.strip().upper()] = value.strip()

                        if 'LOCATION' in headers:
                            device = self._parse_device_description(headers['LOCATION'])
                            if device and device not in self.discovered_devices:
                                self.discovered_devices.append(device)
                                self.device_discovered.emit(device)
                    except socket.timeout:
                        break
            except Exception as e:
                print(f"发现错误: {e}")

            time.sleep(5)  # 每5秒重新搜索一次

        sock.close()
        self.discovery_finished.emit()

    def _parse_device_description(self, location):
        try:
            parsed_url = urlparse(location)
            host = parsed_url.hostname
            port = parsed_url.port or 80

            # 获取设备描述XML
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(5)
            sock.connect((host, port))
            request = f"GET {parsed_url.path} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n"
            sock.send(request.encode())

            response = b""
            while True:
                data = sock.recv(4096)
                if not data:
                    break
                response += data

            sock.close()

            # 解析XML
            xml_data = response.split(b'\r\n\r\n', 1)[1]
            root = ET.fromstring(xml_data.decode())

            # 创建媒体渲染器设备
            device = UPnPMediaRenderer()
            device.location = location

            # 解析设备信息
            device_info = root.find(".//{urn:schemas-upnp-org:device-1-0}device")
            if device_info is None:
                return None

            device.uuid = device_info.find(".//{urn:schemas-upnp-org:device-1-0}UDN").text
            device.friendly_name = device_info.find(".//{urn:schemas-upnp-org:device-1-0}friendlyName").text
            device.device_type = device_info.find(".//{urn:schemas-upnp-org:device-1-0}deviceType").text

            # 解析服务
            service_list = device_info.find(".//{urn:schemas-upnp-org:device-1-0}serviceList")
            if service_list is not None:
                for service in service_list.findall(".//{urn:schemas-upnp-org:device-1-0}service"):
                    service_type = service.find(".//{urn:schemas-upnp-org:device-1-0}serviceType").text
                    control_url = service.find(".//{urn:schemas-upnp-org:device-1-0}controlURL").text

                    if "AVTransport" in service_type:
                        device.av_transport_control_url = self._resolve_relative_url(control_url, parsed_url)
                    elif "RenderingControl" in service_type:
                        device.rendering_control_url = self._resolve_relative_url(control_url, parsed_url)
                    elif "ConnectionManager" in service_type:
                        device.connection_manager_url = self._resolve_relative_url(control_url, parsed_url)

            return device
        except Exception as e:
            print(f"解析设备描述错误: {e}")
            return None

    def _resolve_relative_url(self, url, base_url):
        if url.startswith("http"):
            return url
        return f"http://{base_url.hostname}:{base_url.port}{url}"

class DLNAReceiverServer(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/description.xml":
            self.send_response(200)
            self.send_header("Content-type", "text/xml")
            self.end_headers()
            self.wfile.write(self.server.description_xml.encode())
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        content_length = int(self.headers['Content-Length'])
        post_data = self.rfile.read(content_length).decode()
        self.server.received_data = post_data
        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):
        pass  # 禁用默认日志输出

class DLNAReceiver(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("DLNA/UPnP投屏接收器")
        self.setGeometry(100, 100, 800, 600)
        
        # 初始化UPnP发现
        self.upnp_discovery = UPnPDiscovery()
        self.upnp_discovery.device_discovered.connect(self.add_device)
        self.upnp_discovery.discovery_finished.connect(self.discovery_finished)
        
        # 初始化HTTP服务器
        self.http_server = None
        self.description_xml = self._generate_description_xml()
        
        # 创建UI
        self.init_ui()
        
        # 开始设备发现
        self.start_discovery()
        
    def init_ui(self):
        main_widget = QWidget()
        layout = QVBoxLayout()
        
        # 标题
        title_label = QLabel("DLNA/UPnP投屏接收器")
        title_label.setAlignment(Qt.AlignCenter)
        title_label.setStyleSheet("font-size: 20px; font-weight: bold;")
        layout.addWidget(title_label)
        
        # 设备列表
        self.device_list_label = QLabel("可用的DLNA设备:")
        layout.addWidget(self.device_list_label)
        
        self.device_list = QListWidget()
        self.device_list.itemDoubleClicked.connect(self.connect_to_device)
        layout.addWidget(self.device_list)
        
        # 控制按钮
        self.refresh_button = QPushButton("刷新设备列表")
        self.refresh_button.clicked.connect(self.refresh_devices)
        layout.addWidget(self.refresh_button)
        
        self.start_server_button = QPushButton("启动接收服务器")
        self.start_server_button.clicked.connect(self.toggle_server)
        layout.addWidget(self.start_server_button)
        
        # 日志区域
        self.log_label = QLabel("操作日志:")
        layout.addWidget(self.log_label)
        
        self.log_area = QTextEdit()
        self.log_area.setReadOnly(True)
        layout.addWidget(self.log_area)
        
        main_widget.setLayout(layout)
        self.setCentralWidget(main_widget)
        
    def _generate_description_xml(self):
        return f"""<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
    <specVersion>
        <major>1</major>
        <minor>0</minor>
    </specVersion>
    <device>
        <deviceType>urn:schemas-upnp-org:device:MediaRenderer:1</deviceType>
        <friendlyName>Python DLNA 接收器</friendlyName>
        <manufacturer>Python DLNA 项目</manufacturer>
        <manufacturerURL>https://github.com</manufacturerURL>
        <modelDescription>Python实现的DLNA接收器</modelDescription>
        <modelName>Python DLNA Receiver</modelName>
        <modelNumber>1.0</modelNumber>
        <modelURL>https://github.com</modelURL>
        <serialNumber>123456789</serialNumber>
        <UDN>uuid:PythonDLNAReceiver-1_0-123456789</UDN>
        <serviceList>
            <service>
                <serviceType>urn:schemas-upnp-org:service:AVTransport:1</serviceType>
                <serviceId>urn:upnp-org:serviceId:AVTransport</serviceId>
                <controlURL>/upnp/control/AVTransport</controlURL>
                <eventSubURL>/upnp/event/AVTransport</eventSubURL>
                <SCPDURL>/AVTransport.xml</SCPDURL>
            </service>
            <service>
                <serviceType>urn:schemas-upnp-org:service:RenderingControl:1</serviceType>
                <serviceId>urn:upnp-org:serviceId:RenderingControl</serviceId>
                <controlURL>/upnp/control/RenderingControl</controlURL>
                <eventSubURL>/upnp/event/RenderingControl</eventSubURL>
                <SCPDURL>/RenderingControl.xml</SCPDURL>
            </service>
            <service>
                <serviceType>urn:schemas-upnp-org:service:ConnectionManager:1</serviceType>
                <serviceId>urn:upnp-org:serviceId:ConnectionManager</serviceId>
                <controlURL>/upnp/control/ConnectionManager</controlURL>
                <eventSubURL>/upnp/event/ConnectionManager</eventSubURL>
                <SCPDURL>/ConnectionManager.xml</SCPDURL>
            </service>
        </serviceList>
    </device>
</root>"""
    
    def start_discovery(self):
        self.log("开始搜索DLNA设备...")
        self.device_list.clear()
        self.upnp_discovery.discovered_devices.clear()
        self.upnp_discovery.start_discovery()
        
    def stop_discovery(self):
        self.upnp_discovery.stop_discovery()
        self.log("停止搜索DLNA设备")
        
    def refresh_devices(self):
        self.log("刷新设备列表...")
        self.start_discovery()
        
    def add_device(self, device):
        self.device_list.addItem(f"{device.friendly_name} ({device.device_type})")
        self.log(f"发现设备: {device.friendly_name}")
        
    def discovery_finished(self):
        self.log(f"设备搜索完成，共发现 {len(self.upnp_discovery.discovered_devices)} 个设备")
        
    def connect_to_device(self, item):
        index = self.device_list.row(item)
        if 0 <= index < len(self.upnp_discovery.discovered_devices):
            device = self.upnp_discovery.discovered_devices[index]
            self.log(f"尝试连接到设备: {device.friendly_name}")
            self.send_play_command(device)
            
    def send_play_command(self, device):
        try:
            # 这里应该发送UPnP命令到设备，但由于我们是接收端，这部分逻辑可能需要调整
            # 实际应用中，设备会主动连接到我们的接收服务器
            self.log(f"准备接收来自 {device.friendly_name} 的投屏")
        except Exception as e:
            self.log(f"连接设备失败: {str(e)}")
            QMessageBox.critical(self, "错误", f"连接设备失败: {str(e)}")
            
    def toggle_server(self):
        if self.http_server is None:
            self.start_server()
        else:
            self.stop_server()
            
    def start_server(self):
        try:
            self.http_server = HTTPServer(('0.0.0.0', 8000), DLNAReceiverServer)
            self.http_server.description_xml = self.description_xml
            self.http_server.received_data = None
            threading.Thread(target=self.http_server.serve_forever, daemon=True).start()
            self.start_server_button.setText("停止接收服务器")
            self.log("DLNA接收服务器已启动，端口: 8000")
        except Exception as e:
            self.log(f"启动服务器失败: {str(e)}")
            QMessageBox.critical(self, "错误", f"启动服务器失败: {str(e)}")
            
    def stop_server(self):
        if self.http_server:
            self.http_server.shutdown()
            self.http_server.server_close()
            self.http_server = None
            self.start_server_button.setText("启动接收服务器")
            self.log("DLNA接收服务器已停止")
            
    def log(self, message):
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        self.log_area.append(f"[{timestamp}] {message}")
        
    def closeEvent(self, event):
        self.stop_discovery()
        self.stop_server()
        event.accept()

if __name__ == "__main__":
    app = QApplication(sys.argv)
    receiver = DLNAReceiver()
    receiver.show()
    sys.exit(app.exec_())