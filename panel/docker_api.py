"""Small Docker transport. No shell commands or arbitrary exec endpoint."""
import http.client
import json
import os
import socket
from urllib.parse import quote


class Error(RuntimeError):
    def __init__(self, message, status=503):
        super().__init__(message)
        self.status = status


class UnixHTTP(http.client.HTTPConnection):
    def __init__(self, path, timeout):
        super().__init__("localhost", timeout=timeout)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


class Docker:
    def __init__(self, socket_path=None, transport=None):
        self.socket_path = socket_path or os.getenv("DOCKER_SOCKET", "/var/run/docker.sock")
        self.transport = transport

    def request(self, method, path, body=None, timeout=30, raw=False):
        data = json.dumps(body).encode() if body is not None else None
        if self.transport:
            status, payload = self.transport(method, path, body)
        else:
            conn = UnixHTTP(self.socket_path, timeout)
            try:
                conn.request(method, path, data, {"Content-Type": "application/json"})
                response = conn.getresponse()
                status, payload = response.status, response.read(8*1024*1024)
            except (OSError, http.client.HTTPException) as exc:
                raise Error("Não foi possível acessar o Docker. Confira a montagem do socket no painel.") from exc
            finally:
                conn.close()
        if status not in (200, 201, 204, 304):
            if status == 404:
                raise Error("Container não encontrado. Ele pode estar sendo recriado pelo Watchtower.", 404)
            raise Error(f"O Docker recusou a operação (HTTP {status}).")
        if raw:
            return payload
        try:
            return json.loads(payload) if payload else {}
        except ValueError as exc:
            raise Error("O Docker retornou uma resposta inválida.") from exc

    def inspect(self, identifier):
        return self.request("GET", "/containers/"+quote(identifier, safe="")+"/json")

    def containers(self):
        return self.request("GET", "/containers/json?all=1")

    def control(self, identifier, action, timeout=30):
        if action not in ("start", "stop", "restart"):
            raise Error("Ação inválida.", 400)
        suffix = "?t=-1" if action in ("stop", "restart") else ""
        return self.request("POST", "/containers/"+quote(identifier, safe="")+"/"+action+suffix, timeout=timeout)

    def rename(self, identifier, name):
        return self.request("POST", "/containers/"+quote(identifier,safe="")+"/rename?name="+quote(name,safe=""))

    def create(self, name, payload):
        return self.request("POST", "/containers/create?name="+quote(name,safe=""), payload)

    def remove_failed_replacement(self, identifier):
        return self.request("DELETE", "/containers/"+quote(identifier,safe="")+"?force=1&v=0")

    def remove_retired(self, identifier):
        return self.request("DELETE", "/containers/"+quote(identifier,safe="")+"?v=0")

    def logs(self, identifier):
        data = self.request("GET", "/containers/"+quote(identifier,safe="")+"/logs?stdout=1&stderr=1&timestamps=1&tail=150", raw=True)
        parts, offset = [], 0
        while offset+8 <= len(data) and data[offset] in (0,1,2) and data[offset+1:offset+4] == b"\0\0\0":
            size = int.from_bytes(data[offset+4:offset+8], "big")
            if offset+8+size > len(data): break
            parts.append(data[offset+8:offset+8+size]); offset += 8+size
        return (b"".join(parts) if parts and offset==len(data) else data).decode(errors="replace").splitlines()
