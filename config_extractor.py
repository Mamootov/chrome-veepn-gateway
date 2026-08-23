import os
import re
import json
import tempfile
import shutil
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("VeeBridge.Extractor")

def get_chrome_ext_path():
    path = os.path.expanduser(r'~\AppData\Local\Google\Chrome\User Data\Default\Local Extension Settings\majdfhpaihoncoakbjgbdhglocklcgno')
    return path

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
