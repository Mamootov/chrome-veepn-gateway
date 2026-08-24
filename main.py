import argparse
import asyncio
import sys
import os
if sys.platform == "win32":
    import msvcrt
else:
    import select

from config_extractor import extract_veepn_config
from proxy_server import VeeBridgeProxy, ProxyStats

BANNER = r"""
  _   _           ____       _     _            
 | | | | ___  ___| __ ) _ __(_) __| |__ _  ___  
 | | | |/ _ \/ _ \  _ \| '__| |/ _` / _` |/ _ \ 
 | |_| |  __/  __/ |_) | |  | | (_| |(_| |  __/ 
  \___/ \___|\___|____/|_|  |_|\__,_|\__, |\___|
                                     |___/      
     * Universal Multiplexing VeePN Gateway *
"""

def print_banner():
    print(f"\033[96m{BANNER}\033[0m")

def log_callback(msg: str):
    # Old school terminal color coding (ANSI)
    if "[ERROR]" in msg:
        print(msg.replace("[ERROR]", "\033[91m[!]\033[0m"))
    elif "[WARN]" in msg:
        print(msg.replace("[WARN]", "\033[93m[*]\033[0m"))
    elif "[SUCCESS]" in msg:
        print(msg.replace("[SUCCESS]", "\033[92m[+]\033[0m"))
    elif "[DEBUG]" in msg:
        print(msg.replace("[DEBUG]", "\033[90m[~]\033[0m"))
    elif "[INFO]" in msg:
        print(msg.replace("[INFO]", "\033[94m[*]\033[0m"))
    else:
        print(msg)

def check_keypress() -> bool:
    if sys.platform == "win32":
        if msvcrt.kbhit():
            key = msvcrt.getch()
            return key in (b'\r', b'\n', b' ')
        return False
    else:
        if not sys.stdin.isatty():
            return False
        rlist, _, _ = select.select([sys.stdin], [], [], 0)
        if rlist:
            try:
                data = os.read(sys.stdin.fileno(), 1024)
                return any(c in data for c in (b'\r', b'\n', b' '))
            except Exception:
                return False
        return False

async def stats_listener(stats: ProxyStats):
    print("\033[94m[*]\033[0m Keypress Monitor Active: Press 'Enter' anytime to view Live Usage Stats.")
    while True:
        if check_keypress():
            print(f"\n\033[95m--- [ LIVE STATS SUMMARY ] ---\033[0m")
            print(f"    Active Connections: {stats.active_conns}")
            print(f"    Total Upload (TX):  {stats.tx_bytes/1024/1024:.2f} MB")
            print(f"    Total Download (RX):{stats.rx_bytes/1024/1024:.2f} MB")
            print(f"\033[95m------------------------------\033[0m\n")
        await asyncio.sleep(0.1)

async def start_gateway(args):
    print("\033[94m[*]\033[0m Probing Chrome LevelDB for VeePN configurations...")
    try:
        config = extract_veepn_config()
        masked_user = config['username'][:5] + "***" + config['username'][-4:]
        print(f"\033[92m[+]\033[0m Auth OK -> User: {masked_user}")
        print(f"\033[92m[+]\033[0m Discovered {len(config['servers'])} active CDN egress nodes.")
    except Exception as e:
        print(f"\033[91m[!]\033[0m Extraction Error: {e}")
        sys.exit(1)
        
    stats = ProxyStats()
    proxy = VeeBridgeProxy(
        config=config,
        listen_host='127.0.0.1',
        listen_port=args.port,
        http_port=args.http_port,
        log_callback=log_callback,
        stats=stats,
        debug=args.debug,
        bypass_iran=args.bypass_iran
    )

    # Launch background stats listener
    asyncio.create_task(stats_listener(stats))

    print(f"\n\033[92m[+]\033[0m Routing Engine Engaged! Press Ctrl+C to terminate.\n")
    try:
        await proxy.start()
    except asyncio.CancelledError:
        print("\n\033[93m[*]\033[0m Terminating connection streams...")

def main():
    # Fix console colors to support ANSI on Windows
    if sys.platform == "win32":
        os.system('color') 

    parser = argparse.ArgumentParser(
        description="VeeBridge: A local generic SOCKS5/HTTP multiplexing gateway for VeePN extensions.",
        usage="python main.py [-h] [--run] [--port PORT] [--http-port PORT] [--bypass-iran] [--debug] [--status]"
    )
    parser.add_argument("-r", "--run", action="store_true", help="Launch the routing engine.")
    parser.add_argument("-p", "--port", type=int, default=1080, help="Local SOCKS5 port to bind (default: 1080)")
    parser.add_argument("--http-port", type=int, default=0, help="Optional local HTTP Proxy port to bind (default: disabled)")
    parser.add_argument("-b", "--bypass-iran", action="store_true", help="Split-Tunneling: Bypass VeePN for Iranian (.ir) domains")
    parser.add_argument("-d", "--debug", action="store_true", help="Enable verbose and RAW debug logging output")
    parser.add_argument("-s", "--status", action="store_true", help="Inspect and print configured VeePN settings without launching.")

    args = parser.parse_args()
    print_banner()

    if args.status:
        try:
            conf = extract_veepn_config()
            print("\033[92m[+]\033[0m VeePN is Active and configured in Chrome!")
            print(f"  User Auth : {conf['username'][:10]}...")
            print(f"  Egress IPs: {len(conf['servers'])} static nodes available.")
            sys.exit(0)
        except Exception as e:
            print(f"\033[91m[!]\033[0m Status Check Failed: {e}")
            sys.exit(1)

    if args.run:
        try:
            asyncio.run(start_gateway(args))
        except KeyboardInterrupt:
            print("\n\033[93m[*]\033[0m Interrupted by User (Ctrl+C). Cleaning up...")
    else:
        print("\033[93m[*]\033[0m Engine standby. Pass -r or --run flag to initialize.")
        print("    Run `python main.py -h` for full manual.\n")

if __name__ == "__main__":
    main()
