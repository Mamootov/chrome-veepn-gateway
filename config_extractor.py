import os
import re
import json
import tempfile
import shutil
import logging

import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("VeeBridge.Extractor")

VEEPN_EXT_ID = "majdfhpaihoncoakbjgbdhglocklcgno"

def get_chrome_ext_path():
    home = os.path.expanduser("~")
    
    if sys.platform.startswith("linux"):
        base_dirs = [
            os.path.join(home, ".config", "google-chrome"),
            os.path.join(home, ".config", "chromium"),
            os.path.join(home, ".config", "google-chrome-beta"),
            os.path.join(home, ".config", "google-chrome-unstable"),
            os.path.join(home, ".config", "BraveSoftware", "Brave-Browser"),
            os.path.join(home, ".var", "app", "com.google.Chrome", "config", "google-chrome"),
            os.path.join(home, ".var", "app", "org.chromium.Chromium", "config", "chromium"),
            os.path.join(home, "snap", "chromium", "current", ".config", "chromium"),
            os.path.join(home, "snap", "google-chrome", "current", ".config", "google-chrome"),
        ]
        default_fallback = os.path.join(home, ".config", "google-chrome", "Default", "Local Extension Settings", VEEPN_EXT_ID)
    elif sys.platform == "win32":
        base_dirs = [
            os.path.join(home, "AppData", "Local", "Google", "Chrome", "User Data"),
            os.path.join(home, "AppData", "Local", "Chromium", "User Data"),
            os.path.join(home, "AppData", "Local", "BraveSoftware", "Brave-Browser", "User Data"),
            os.path.join(home, "AppData", "Local", "Microsoft", "Edge", "User Data"),
        ]
        default_fallback = os.path.join(home, "AppData", "Local", "Google", "Chrome", "User Data", "Default", "Local Extension Settings", VEEPN_EXT_ID)
    elif sys.platform == "darwin":
        base_dirs = [
            os.path.join(home, "Library", "Application Support", "Google", "Chrome"),
            os.path.join(home, "Library", "Application Support", "Chromium"),
            os.path.join(home, "Library", "Application Support", "BraveSoftware", "Brave-Browser"),
            os.path.join(home, "Library", "Application Support", "Microsoft Edge"),
        ]
        default_fallback = os.path.join(home, "Library", "Application Support", "Google", "Chrome", "Default", "Local Extension Settings", VEEPN_EXT_ID)
    else:
        base_dirs = [
            os.path.join(home, ".config", "google-chrome"),
        ]
        default_fallback = os.path.join(home, ".config", "google-chrome", "Default", "Local Extension Settings", VEEPN_EXT_ID)

    for base_dir in base_dirs:
        if not os.path.exists(base_dir):
            continue
        
        profiles = ["Default"]
        try:
            for entry in os.listdir(base_dir):
                if entry.startswith("Profile ") and os.path.isdir(os.path.join(base_dir, entry)):
                    profiles.append(entry)
        except OSError:
            pass

        for profile in profiles:
            ext_path = os.path.join(base_dir, profile, "Local Extension Settings", VEEPN_EXT_ID)
            if os.path.exists(ext_path):
                return ext_path
        
        direct_path = os.path.join(base_dir, "Local Extension Settings", VEEPN_EXT_ID)
        if os.path.exists(direct_path):
            return direct_path

    return default_fallback

def extract_veepn_config():
    """Reads Chrome's LevelDB to extract username, password, and actual proxy nodes."""
    ext_path = get_chrome_ext_path()
    if not os.path.exists(ext_path):
        logger.error("VeePN extension storage not found.")
        raise FileNotFoundError("VeePN extension storage not found!")

    log_files = [f for f in os.listdir(ext_path) if f.endswith('.log') or f.endswith('.ldb')]
    if not log_files:
        raise FileNotFoundError("LevelDB log file not found!")

    log_files.sort(key=lambda x: os.path.getmtime(os.path.join(ext_path, x)), reverse=True)
    target_log = os.path.join(ext_path, log_files[0])
    
    tmp_file = os.path.join(tempfile.gettempdir(), 'veepn_extract_temp.ldb')
    try:
        shutil.copy2(target_log, tmp_file)
    except PermissionError:
        if len(log_files) > 1:
            shutil.copy2(os.path.join(ext_path, log_files[1]), tmp_file)
        else:
            raise

    with open(tmp_file, 'rb') as f:
        content = f.read()
    os.remove(tmp_file)

    # 1. Extract Credentials
    creds = None
    json_pattern = rb'\{[^{}]*\"password\"[^{}]*\"username\"[^{}]*\}|\{[^{}]*\"username\"[^{}]*\"password\"[^{}]*\}'
    json_matches = re.findall(json_pattern, content)
    for match in reversed(json_matches):
        try:
            parsed = json.loads(match.decode('utf-8', errors='ignore'))
            if 'username' in parsed and 'password' in parsed:
                creds = parsed
                break
        except json.JSONDecodeError:
            continue
            
    # 2. Extract actual Egress Proxy Nodes (serverConfigList)
    proxy_nodes = []
    # Match JSON arrays of proxy configs
    server_list_pattern = rb'\"serverConfigList\"[^\[]*\[(.*?)\]'
    server_matches = re.findall(server_list_pattern, content)
    
    if server_matches:
        # Take the most recent one (last match)
        last_match = b'[' + server_matches[-1] + b']'
        try:
            # Sometime the regex capture might be a little malformed if there are nested brackets, let's fix
            nodes = json.loads(last_match.decode('utf-8', errors='ignore'))
            for node in nodes:
                if 'addresses' in node and 'port' in node:
                    for addr in node['addresses']:
                        proxy_nodes.append((addr, node['port']))
        except Exception as e:
            logger.error(f"Failed to parse serverConfigList JSON: {e}")

    # Fallback: Just look for any string that matches the server config object
    if not proxy_nodes:
        fallback_pattern = rb'\{\"addresses\"\:\[.*?\]\,\"port\"\:.*?\,\"protocol\"\:.*?\}'
        f_matches = re.findall(fallback_pattern, content)
        for fm in reversed(f_matches):
            try:
                node = json.loads(fm.decode('utf-8', errors='ignore'))
                if 'addresses' in node and 'port' in node:
                    for addr in node['addresses']:
                        tup = (addr, node['port'])
                        if tup not in proxy_nodes:
                            proxy_nodes.append(tup)
            except:
                pass

    if not creds:
        raise ValueError("Credentials not found! Please ensure VeePN is logged in and connected in Chrome.")
    if not proxy_nodes:
        raise ValueError("No proxy servers found! Please connect VeePN in Chrome to populate the active server.")

    logger.info(f"Successfully extracted config. Found {len(proxy_nodes)} proxy nodes.")
    return {
        "username": creds.get("username"),
        "password": creds.get("password"),
        "servers": proxy_nodes
    }

if __name__ == "__main__":
    try:
        conf = extract_veepn_config()
        print(f"Extracted User: {conf['username'][:5]}...")
        print(f"Extracted Servers: {conf['servers']}")
    except Exception as e:
        print(f"Extraction failed: {e}")
