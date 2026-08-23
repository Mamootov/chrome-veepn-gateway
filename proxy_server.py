import asyncio
import ssl
import base64
import re
from typing import Callable

IR_DOMAINS = ['.ir', '.bank', 'aparat.com', 'shaparak.ir', 'snapp.ir', 'divar.ir', 'digikala.com']

class ProxyStats:
    def __init__(self):
        self.tx_bytes = 0
        self.rx_bytes = 0
        self.active_conns = 0

class VeeBridgeProxy:
    def __init__(self, config, listen_host='127.0.0.1', listen_port=1080, http_port=0,
                 log_callback: Callable[[str], None] = None, stats=ProxyStats(), 
                 debug=False, bypass_iran=False):
        self.config = config
        self.listen_host = listen_host
        self.listen_port = listen_port
        self.http_port = http_port
        self.servers = self.config['servers']
        self.upstream_port = 443
        self.stats = stats
        self.debug = debug
        self.bypass_iran = bypass_iran
        self.log_callback = log_callback or (lambda msg: print(msg))
        
        user_pass = f"{self.config['username']}:{self.config['password']}"
        encoded_creds = base64.b64encode(user_pass.encode()).decode('utf-8')
        self.auth_header = f"Basic {encoded_creds}"

    def log(self, message: str, level="INFO"):
        fmt = f"[{level}] {message}"
        # Skip debug prints if not debug mode
        if level == "DEBUG" and not self.debug:
            return
        self.log_callback(fmt)

    def is_iran_domain(self, host: str):
        if not self.bypass_iran:
            return False
        host_lower = host.lower()
        return any(host_lower.endswith(d) for d in IR_DOMAINS)

    async def _connect_failover(self, target_host: str, target_port: int, direct=False):
        """Returns (reader, writer, server_node_name)"""
        # Feature: Split Tunneling (Direct Connect for IR domains)
        if direct:
            try:
                reader, writer = await asyncio.open_connection(target_host, target_port)
                self.log(f"SPLIT TUNNEL (Direct) -> {target_host}:{target_port}", "WARN")
                return reader, writer, "DIRECT"
            except Exception as e:
                raise ConnectionError(f"Direct connection to {target_host} failed: {e}")

        # Normal VeePN Failover loop
        ssl_context = ssl.create_default_context()
        last_error = None
        for i, (ext_ip, ext_port) in enumerate(self.servers):
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(ext_ip, ext_port, ssl=ssl_context),
                    timeout=5.0
                )
                if i > 0:
                    self.log(f"Failover switched to node {ext_ip}:{ext_port}", "WARN")
                return reader, writer, f"{ext_ip}:{ext_port}"
            except Exception as e:
                last_error = e
                self.log(f"Node {ext_ip}:{ext_port} dropped. Trying next...", "DEBUG")
                
        raise ConnectionError(f"All VeePN nodes failed! Last error: {last_error}")

    async def handle_socks5(self, reader, writer):
        """SOCKS5 Protocol Handler"""
        self.stats.active_conns += 1
        try:
            version = await reader.read(1)
            if not version or version != b'\x05':
                writer.close()
                return

            nmethods = await reader.read(1)
            await reader.read(ord(nmethods))

            writer.write(b'\x05\x00')
            await writer.drain()

            _ = await reader.read(1) # req_ver
            cmd = await reader.read(1)
            _ = await reader.read(1) # rsv
            atyp = await reader.read(1)

            target_host = ""
            if atyp == b'\x01':   
                target_host = ".".join(map(str, await reader.read(4)))
            elif atyp == b'\x03': 
                dom_len = ord(await reader.read(1))
                target_host = (await reader.read(dom_len)).decode('utf-8')
            elif atyp == b'\x04': 
                await reader.read(16)
                writer.close()
                return

            target_port = int.from_bytes(await reader.read(2), 'big')

            if cmd == b'\x01': # CONNECT
                self.log(f"SOCKS5 Bound -> {target_host}:{target_port}", "DEBUG")
                direct = self.is_iran_domain(target_host)
                await self.establish_tunnel(reader, writer, target_host, target_port, direct, proto="SOCKS5")
            else:
                writer.close()

        except asyncio.IncompleteReadError:
             pass
        except Exception as e:
            self.log(f"SOCKS5 Error: {e}", "DEBUG")
        finally:
            self.stats.active_conns -= 1
            writer.close()

    async def handle_http(self, reader, writer):
        """HTTP Proxy (CONNECT) Handler"""
        self.stats.active_conns += 1
        try:
            # Read the HTTP CONNECT string
            request_line = await reader.readuntil(b'\r\n')
            req_str = request_line.decode('utf-8', errors='ignore')
            
            if not req_str.startswith("CONNECT"):
                self.log(f"Unsupported HTTP Method (Only CONNECT is supported): {req_str.strip()}", "DEBUG")
                writer.close()
                return
            
            # e.g., "CONNECT github.com:443 HTTP/1.1"
            parts = req_str.split(" ")
            if len(parts) >= 2:
                host_port = parts[1]
                if ':' in host_port:
                    target_host, target_port = host_port.split(':', 1)
                    target_port = int(target_port)
                else:
                    target_host = host_port
                    target_port = 443

                # Read remaining headers
                while True:
                    line = await reader.readuntil(b'\r\n')
                    if line == b'\r\n':
                        break

                self.log(f"HTTP Bound -> {target_host}:{target_port}", "DEBUG")
                direct = self.is_iran_domain(target_host)
                await self.establish_tunnel(reader, writer, target_host, target_port, direct, proto="HTTP")
            else:
                writer.close()

        except asyncio.IncompleteReadError:
             pass
        except Exception as e:
            self.log(f"HTTP Proxy Error: {e}", "DEBUG")
        finally:
            self.stats.active_conns -= 1
            writer.close()

    async def establish_tunnel(self, local_reader, local_writer, target_host, target_port, direct, proto):
        """Universal Upstream Tunnel Core (Handles both VeePN wrapping and Direct bridging)"""
        try:
            up_reader, up_writer, active_host = await self._connect_failover(target_host, target_port, direct=direct)
        except Exception as e:
            self.log(f"Connection dropped: {e}", "ERROR")
            local_writer.close()
            return

        if not direct:
            # Inject VeePN HTTP CONNECT Auth inside the TLS Context
            connect_req = (
                f"CONNECT {target_host}:{target_port} HTTP/1.1\r\n"
                f"Host: {target_host}:{target_port}\r\n"
                f"Proxy-Authorization: {self.auth_header}\r\n"
                f"Connection: Keep-Alive\r\n"
                f"\r\n"
            )
            up_writer.write(connect_req.encode())
            await up_writer.drain()

            try:
                resp_line = await asyncio.wait_for(up_reader.readuntil(b'\r\n\r\n'), timeout=5.0)
                if b"200" not in resp_line:
                    header_line = resp_line.split(b'\r\n')[0].decode('utf-8', errors='ignore')
                    self.log(f"VeePN rejected auth for {target_host}: {header_line}", "ERROR")
                    local_writer.close()
                    up_writer.close()
                    return
            except asyncio.TimeoutError:
                self.log(f"Timeout waiting for VeePN proxy response", "ERROR")
                up_writer.close()
                local_writer.close()
                return
        
        # Reply to local client
        if proto == "SOCKS5":
            local_writer.write(b'\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00')
        elif proto == "HTTP":
            local_writer.write(b'HTTP/1.1 200 Connection established\r\n\r\n')
        await local_writer.drain()
        
        if not direct:
             self.log(f"Tunnel Active: {target_host}:{target_port} VIA {active_host}", "SUCCESS")

        await asyncio.gather(
            self.pipe_stream(local_reader, up_writer, "tx"),
            self.pipe_stream(up_reader, local_writer, "rx")
        )

    async def pipe_stream(self, reader, writer, direction="tx"):
        try:
            while not reader.at_eof():
                data = await reader.read(8192)
                if not data:
                    break
                
                if direction == "tx":
                    self.stats.tx_bytes += len(data)
                elif direction == "rx":
                    self.stats.rx_bytes += len(data)
                    
                writer.write(data)
                await writer.drain()
        except:
            pass
        finally:
            writer.close()

    async def start(self):
        servers = []
        socks_server = await asyncio.start_server(self.handle_socks5, self.listen_host, self.listen_port)
        servers.append(socks_server)
        self.log(f"SOCKS5 Gateway running at {self.listen_host}:{self.listen_port}", "INFO")

        if self.http_port:
            http_server = await asyncio.start_server(self.handle_http, self.listen_host, self.http_port)
            servers.append(http_server)
            self.log(f"HTTP Proxy running at {self.listen_host}:{self.http_port}", "INFO")

        async with asyncio.TaskGroup() as tg:
            for srv in servers:
                tg.create_task(srv.serve_forever())
