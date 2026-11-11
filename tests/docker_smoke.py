"""Disposable, real Watchtower integration. No monitored user containers."""
import json
from pathlib import Path
import socket
import subprocess
import tempfile
import time
from urllib.request import Request,urlopen

WATCH="sal0-tower-ci-watchtower"
PANEL="sal0-tower-ci-panel"
IMAGE="containrrr/watchtower:latest"

def docker(*args):return subprocess.check_output(["docker",*args],text=True).strip()
def inspect(name):return json.loads(docker("inspect",name))[0]

try:
 docker("pull",IMAGE)
 with tempfile.TemporaryDirectory() as temp:
  api_version=docker("version","--format","{{.Server.APIVersion}}")
  sentinel=Path(temp)/"sentinel";sentinel.write_text("preserve-this-bind")
  docker("run","-d","--name",WATCH,"--restart","unless-stopped","-e","DOCKER_API_VERSION="+api_version,
         "-e","WATCHTOWER_POLL_INTERVAL=3600","-e","WATCHTOWER_MONITOR_ONLY=true",
         "-e","WATCHTOWER_SCOPE=sal0-tower-ci-unique","-e","WATCHTOWER_NOTIFICATION_URL=logger://",
         "-e","SAL0_SENTINEL=private-fixture-value","--label","com.centurylinklabs.watchtower.enable=false",
         "-v","/var/run/docker.sock:/var/run/docker.sock","-v",str(sentinel)+":/sentinel:ro",IMAGE)
  original=inspect(WATCH)
  sock=socket.socket();sock.bind(("127.0.0.1",0));port=sock.getsockname()[1];sock.close()
  docker("run","-d","--name",PANEL,"--network","host","-e",f"SAL0_PORT={port}","-e","SAL0_HOST_NETWORK=true",
         "-e","WATCHTOWER_CONTAINER="+WATCH,"-v",temp+"/panel:/data","-v","/var/run/docker.sock:/var/run/docker.sock:ro","sal0-tower:ci")
  base=f"http://127.0.0.1:{port}"
  def request(path="/api/state",body=None):
   req=Request(base+path,data=json.dumps(body).encode() if body is not None else None,headers={"Content-Type":"application/json","X-Sal0-Request":"1"})
   with urlopen(req,timeout=30) as response:return json.load(response)
  def ready():
   for _ in range(80):
    try:return request()
    except OSError:time.sleep(.25)
   raise AssertionError("Panel did not start")
  def finish():
   for _ in range(160):
    s=request()
    if s["job"]["state"]!="running":
     assert s["job"]["state"]=="done",s["job"]
     return s
    time.sleep(.25)
   raise AssertionError("Operation timed out")
  state=ready();assert state["status"]=="running",state
  assert "private-fixture-value" not in json.dumps(state)
  assert not state["settings"]["http_api_update"]
  request("/api/settings",{"revision":state["revision"],"settings":{"http_api_update":True}})
  state=finish();new=inspect(WATCH)
  assert new["Id"]!=original["Id"]
  assert state["settings"]["http_api_periodic_polls"]
  assert "WATCHTOWER_NOTIFICATION_URL=logger://" in new["Config"]["Env"]
  assert "SAL0_SENTINEL=private-fixture-value" in new["Config"]["Env"]
  assert any(m["Destination"]=="/sentinel" and m["Source"]==str(sentinel) for m in new["Mounts"])
  request("/api/update",{"images":["sal0/never-match-any-container"]});finish()
  logs=subprocess.check_output(["docker","logs",WATCH],stderr=subprocess.STDOUT,text=True)
  assert "Updates triggered by HTTP API request" in logs,logs
  state=request();request("/api/settings",{"revision":state["revision"],"settings":{"interval":1200,"schedule":""}})
  state=finish();assert state["settings"]["interval"]==1200
  request("/api/restore",{"revision":state["revision"]});state=finish();assert state["settings"]["interval"]==3600
  before=inspect(WATCH)["State"]["StartedAt"]
  docker("stop",PANEL);assert inspect(WATCH)["State"]["Running"]
  docker("start",PANEL);ready();assert inspect(WATCH)["State"]["StartedAt"]==before
  print("Docker OK: native API activation, periodic polls, preserved credentials/mounts, native update, settings restore and panel independence.")
finally:
 for name in (PANEL,WATCH):subprocess.run(["docker","rm","-f",name],capture_output=True)
 # Remove only stopped, disposable Watchtower rollback fixtures after a failed test.
 try:
  for name in docker("ps","-a","--format","{{.Names}}").splitlines():
   if name.startswith(WATCH+"-sal0-backup-"):subprocess.run(["docker","rm","-f",name],capture_output=True)
 except Exception:pass
