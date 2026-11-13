from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import signal
import threading
from urllib.parse import urlsplit
from docker_api import Error
from watchtower import Tower

STATIC=Path(__file__).resolve().parent/"static"


class Handler(BaseHTTPRequestHandler):
    server_version="Sal0Tower"
    sys_version=""
    def log_message(self,*args):pass

    def send(self,status,value,mime="application/json; charset=utf-8"):
        data=json.dumps(value,ensure_ascii=False).encode() if mime.startswith("application/json") else value
        self.send_response(status)
        for key,val in {"Content-Type":mime,"Content-Length":str(len(data)),"Cache-Control":"no-store","X-Content-Type-Options":"nosniff","Referrer-Policy":"no-referrer","Content-Security-Policy":"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: https: http:; connect-src 'self'; frame-ancestors 'self'; base-uri 'self'; form-action 'self'"}.items():self.send_header(key,val)
        self.end_headers()
        try:self.wfile.write(data)
        except (BrokenPipeError,ConnectionResetError):pass

    def do_GET(self):
        path=urlsplit(self.path).path
        if path=="/healthz":return self.send(200,{"ok":True,"version":"1.2.2"})
        if path=="/api/state":return self.send(200,self.server.tower.state())
        files={"/appearance.css":("appearance.css","text/css; charset=utf-8"),"/appearance.js":("appearance.js","application/javascript; charset=utf-8"),"/":("index.html","text/html; charset=utf-8"),"/app.js":("app.js","application/javascript; charset=utf-8"),"/style.css":("style.css","text/css; charset=utf-8"),"/icon.svg":("icon.svg","image/svg+xml")}
        if path not in files:return self.send(404,{"error":"Página não encontrada."})
        name,mime=files[path];self.send(200,(STATIC/name).read_bytes(),mime)

    def do_POST(self):
        origin=self.headers.get("Origin")
        if self.headers.get("X-Sal0-Request")!="1" or (origin and urlsplit(origin).netloc!=self.headers.get("Host")):
            return self.send(403,{"error":"Abra o painel pelo endereço do servidor para realizar alterações."})
        if self.headers.get_content_type()!="application/json":return self.send(415,{"error":"Pedido inválido."})
        try:
            length=int(self.headers.get("Content-Length",0))
            if not 0<length<=512*1024:return self.send(413,{"error":"Pedido muito grande."})
            self.connection.settimeout(20)
            body=json.loads(self.rfile.read(length))
            self.send(202,self.server.tower.action(urlsplit(self.path).path,body))
        except Error as exc:self.send(exc.status,{"error":str(exc)})
        except (ValueError,UnicodeError):self.send(400,{"error":"JSON inválido."})
        except Exception:self.send(500,{"error":"Não foi possível concluir. Confira os registros e tente novamente."})


def make_server(host="0.0.0.0",port=7860,directory="/data",docker=None,http=None,catalog=None):
    server=ThreadingHTTPServer((host,port),Handler);server.daemon_threads=True
    server.tower=Tower(directory,docker,http,catalog)
    return server


def main():
    server=make_server(os.getenv("SAL0_HOST","0.0.0.0"),int(os.getenv("SAL0_PORT","7860")),os.getenv("SAL0_DATA","/data"))
    def stop(*_):threading.Thread(target=server.shutdown,daemon=True).start()
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    print(f"Sal0 Tower em http://0.0.0.0:{server.server_port}",flush=True)
    try:server.serve_forever(poll_interval=.25)
    finally:server.server_close() # Watchtower remains independent.


if __name__=="__main__":main()
