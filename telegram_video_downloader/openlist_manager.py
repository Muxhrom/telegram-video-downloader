from __future__ import annotations

import hashlib
import json
import os
import secrets
import socket
import subprocess
import time
import zipfile
from pathlib import Path
from typing import Callable

import httpx

from .config import AppConfig
from .credentials import load_openlist_password, save_openlist_password
from .paths import AppPaths


OPENLIST_VERSION = "v4.2.3"
OPENLIST_ASSET = "openlist-windows-amd64.zip"
OPENLIST_RELEASE_API = (
    f"https://api.github.com/repos/OpenListTeam/OpenList/releases/tags/{OPENLIST_VERSION}"
)


class OpenListManager:
    def __init__(self, paths: AppPaths, config: AppConfig) -> None:
        self.paths = paths
        self.config = config
        self.process: subprocess.Popen | None = None

    @property
    def executable(self) -> Path:
        return self.paths.openlist_dir / "openlist.exe"

    @property
    def data_dir(self) -> Path:
        return self.paths.openlist_dir / "data"

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.config.openlist_port}"

    @property
    def webdav_url(self) -> str:
        return self.base_url + "/dav"

    def password(self) -> str:
        value = load_openlist_password()
        if value:
            return value
        value = secrets.token_urlsafe(24)
        save_openlist_password(value)
        return value

    @staticmethod
    def _direct_environment() -> dict[str, str]:
        environment = os.environ.copy()
        for name in (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
        ):
            environment.pop(name, None)
        environment.update({"NO_PROXY": "*", "no_proxy": "*"})
        return environment

    def set_password(self, value: str) -> None:
        password = value.strip()
        if len(password) < 8 or len(password) > 128:
            raise ValueError("OpenList 管理员密码必须为 8–128 个字符。")
        if "\n" in password or "\r" in password:
            raise ValueError("OpenList 管理员密码不能包含换行符。")
        database_file = self.data_dir / "data.db"
        if self.installed() and database_file.is_file():
            completed = subprocess.run(
                [
                    str(self.executable),
                    "--data",
                    str(self.data_dir),
                    "admin",
                    "set",
                    password,
                ],
                cwd=str(self.paths.openlist_dir),
                env=self._direct_environment(),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                check=False,
            )
            if completed.returncode != 0:
                raise RuntimeError(
                    f"更新 OpenList 管理员密码失败，退出码 {completed.returncode}。"
                )
        save_openlist_password(password)

    def installed(self) -> bool:
        return self.executable.is_file()

    def _client(self, proxy: bool = False) -> httpx.Client:
        options = {
            "timeout": httpx.Timeout(30.0, read=120.0),
            "follow_redirects": True,
            "headers": {"User-Agent": "TelegramVideoDownloader/1.0"},
            "trust_env": False,
        }
        if proxy:
            options["proxy"] = (
                f"socks5://{self.config.proxy_host}:{self.config.proxy_port}"
            )
        return httpx.Client(**options)

    def _get(self, url: str) -> httpx.Response:
        error: Exception | None = None
        for use_proxy in (False, True):
            try:
                with self._client(use_proxy) as client:
                    response = client.get(url)
                    response.raise_for_status()
                    return response
            except Exception as exc:
                error = exc
        raise RuntimeError(f"下载 OpenList 失败：{error}")

    def install(self, progress: Callable[[int], None] | None = None) -> Path:
        if self.installed():
            return self.executable
        self.paths.openlist_dir.mkdir(parents=True, exist_ok=True)
        metadata = self._get(OPENLIST_RELEASE_API).json()
        asset = next(
            (item for item in metadata.get("assets", []) if item.get("name") == OPENLIST_ASSET),
            None,
        )
        if not asset:
            raise RuntimeError("OpenList 官方 Release 中没有 Windows AMD64 安装包。")
        expected = str(asset.get("digest") or "")
        if not expected.startswith("sha256:"):
            raise RuntimeError("OpenList Release 未提供 SHA256 摘要，已拒绝安装。")
        download_url = str(asset["browser_download_url"])
        archive = self.paths.openlist_dir / OPENLIST_ASSET
        response = self._get(download_url)
        payload = response.content
        actual = hashlib.sha256(payload).hexdigest()
        if actual.casefold() != expected.split(":", 1)[1].casefold():
            raise RuntimeError("OpenList 安装包 SHA256 校验失败。")
        archive.write_bytes(payload)
        if progress:
            progress(70)
        with zipfile.ZipFile(archive) as bundle:
            root = self.paths.openlist_dir.resolve()
            for member in bundle.infolist():
                target = (root / member.filename).resolve()
                if root not in target.parents and target != root:
                    raise RuntimeError("OpenList 安装包包含不安全路径。")
            bundle.extractall(root)
        archive.unlink(missing_ok=True)
        located = next(self.paths.openlist_dir.rglob("openlist.exe"), None)
        if not located:
            raise RuntimeError("OpenList 安装包中未找到 openlist.exe。")
        if located != self.executable:
            located.replace(self.executable)
        if progress:
            progress(100)
        return self.executable

    @staticmethod
    def port_available(port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.2)
            return probe.connect_ex(("127.0.0.1", port)) != 0

    def choose_port(self) -> int:
        current = int(self.config.openlist_port)
        if self._looks_like_openlist(current) or self.port_available(current):
            return current
        for port in range(5244, 5255):
            if self.port_available(port):
                self.config.openlist_port = port
                self.config.save(self.paths.config_file)
                return port
        raise RuntimeError("5244–5254 均被占用，无法启动 OpenList。")

    @staticmethod
    def _tcp_ready(port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.3)
            return probe.connect_ex(("127.0.0.1", port)) == 0

    def running(self) -> bool:
        if self.process and self.process.poll() is None:
            return True
        return self._looks_like_openlist(int(self.config.openlist_port))

    @staticmethod
    def _looks_like_openlist(port: int) -> bool:
        try:
            with httpx.Client(trust_env=False, timeout=1.0) as client:
                response = client.get(
                    f"http://127.0.0.1:{port}/api/public/settings"
                )
            return response.status_code == 200 and "data" in response.text
        except httpx.HTTPError:
            return False

    def start(self) -> str:
        if not self.installed():
            raise RuntimeError("请先安装 OpenList。")
        if self.running():
            return self.base_url
        port = self.choose_port()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._ensure_local_config(port)
        environment = self._direct_environment()
        environment.update(
            {
                "OPENLIST_ADMIN_PASSWORD": self.password(),
                "OPENLIST_SCHEME_ADDRESS": "127.0.0.1",
                "OPENLIST_SCHEME_HTTP_PORT": str(port),
                "TZ": "Asia/Shanghai",
            }
        )
        log_path = self.paths.openlist_dir / "openlist.log"
        log_file = open(log_path, "a", encoding="utf-8")
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.process = subprocess.Popen(
            [
                str(self.executable),
                "--data",
                str(self.data_dir),
                "server",
            ],
            cwd=str(self.paths.openlist_dir),
            env=environment,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            creationflags=creation_flags,
        )
        for _ in range(40):
            if self.process.poll() is not None:
                raise RuntimeError("OpenList 启动失败，请查看 openlist.log。")
            if self._tcp_ready(port):
                return self.base_url
            time.sleep(0.25)
        raise RuntimeError("OpenList 启动超时。")

    def _ensure_local_config(self, port: int) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        config_path = self.data_dir / "config.json"
        if config_path.is_file():
            try:
                data = json.loads(config_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                data = {}
        else:
            data = {}
        data.setdefault("force", False)
        data.setdefault("site_url", "")
        data.setdefault("cdn", "")
        data.setdefault("jwt_secret", secrets.token_urlsafe(24))
        data.setdefault("token_expires_in", 48)
        database = data.setdefault("database", {})
        database.update(
            {
                "type": "sqlite3",
                "db_file": str(self.data_dir / "data.db"),
            }
        )
        database.setdefault("table_prefix", "x_")
        scheme = data.setdefault("scheme", {})
        scheme.update(
            {
                "address": "127.0.0.1",
                "http_port": int(port),
                "https_port": -1,
                "force_https": False,
                "cert_file": "",
                "key_file": "",
            }
        )
        data.setdefault("temp_dir", str(self.data_dir / "temp"))
        data.setdefault("bleve_dir", str(self.data_dir / "bleve"))
        data.setdefault("dist_dir", "")
        log = data.setdefault("log", {})
        log.update(
            {
                "enable": True,
                "name": str(self.data_dir / "log" / "log.log"),
                "max_size": 20,
                "max_backups": 5,
                "max_age": 14,
                "compress": False,
            }
        )
        data.setdefault("delayed_start", 0)
        data.setdefault("auto_memory_limit", 4)
        data.setdefault("min_free_memory", 0)
        data.setdefault("max_block_limit", 0)
        data.setdefault("max_connections", 0)
        data.setdefault("max_concurrency", 16)
        data.setdefault("tls_insecure_skip_verify", False)
        cors = data.get("cors")
        if not isinstance(cors, dict):
            cors = {}
        for key in ("allow_origins", "allow_methods", "allow_headers"):
            values = cors.get(key)
            if not isinstance(values, list) or not any(str(value).strip() for value in values):
                cors[key] = ["*"]
        data["cors"] = cors
        data.setdefault("s3", {"enable": False, "port": 5246, "ssl": False})
        data.setdefault("ftp", {"enable": False, "listen": "127.0.0.1:5221"})
        data.setdefault("sftp", {"enable": False, "listen": "127.0.0.1:5222"})
        data.setdefault("mcp", {"enable": False})
        data["proxy_address"] = ""
        temporary = config_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(config_path)

    def stop(self) -> None:
        process = self.process
        self.process = None
        if not process or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()

    def state(self) -> dict:
        saved_password = load_openlist_password() or ""
        return {
            "installed": self.installed(),
            "running": self.running(),
            "base_url": self.base_url,
            "webdav_url": self.webdav_url,
            "port": int(self.config.openlist_port),
            "username": "admin",
            "password": saved_password,
            "mount": self.config.openlist_mount,
        }
