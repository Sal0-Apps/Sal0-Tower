import copy
import http.client
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"panel"))
from docker_api import Docker, Error
from watchtower import Tower, build_config, eligible, options, parse_args, public_settings, revision, sanitize
import server


def fixture():
    return {"Id":"old-id","Name":"/watchtower","Config":{"Image":"containrrr/watchtower:latest","Env":["WATCHTOWER_POLL_INTERVAL=900","WATCHTOWER_NOTIFICATION_URL=telegram://private-token","WATCHTOWER_HTTP_API_TOKEN=api-secret","UNRELATED=value"],"Cmd":["--cleanup","--notification-title-tag","My server","jellyfin"],"Labels":{"com.docker.compose.project":"old-watchtower"}},"HostConfig":{"NetworkMode":"bridge","RestartPolicy":{"Name":"unless-stopped"},"Binds":["/var/run/docker.sock:/var/run/docker.sock","/private/registry:/config.json:ro"]},"State":{"Running":True},"Mounts":[],"NetworkSettings":{"Networks":{"bridge":{"IPAddress":"172.17.0.2"}}}}


class FakeDocker:
    def __init__(self):
        self.info=fixture();self.calls=[];self.old=None;self.fail_create=False
    def inspect(self,identifier):
        if self.old and identifier==self.old["Id"]:return copy.deepcopy(self.old)
        if identifier in ("watchtower",self.info.get("Id")):return copy.deepcopy(self.info)
        raise Error("Não encontrado",404)
    def containers(self):
        return [{"Id":"jellyfin-id","Names":["/jellyfin"],"Image":"jellyfin/jellyfin:latest","State":"running","Labels":{}},{"Id":"db-id","Names":["/database"],"Image":"postgres:17","State":"running","Labels":{"com.centurylinklabs.watchtower.enable":"false"}}]
    def logs(self,identifier):return ["2026-11-11T12:00:00Z token=api-secret telegram://private-token Session done"]
    def control(self,identifier,action,timeout=30):
        self.calls.append((action,identifier))
        target=self.old if self.old and identifier==self.old["Id"] else self.info
        target["State"]["Running"]=action!="stop"
    def rename(self,identifier,name):
        self.calls.append(("rename",name))
        if identifier=="old-id":self.old=copy.deepcopy(self.info)
        else:self.info["Name"]="/"+name
    def create(self,name,payload):
        self.calls.append(("create",name))
        if self.fail_create:raise Error("Creation failed")
        self.info={"Id":"new-id","Name":"/"+name,"Config":{k:copy.deepcopy(v) for k,v in payload.items() if k not in ("HostConfig","NetworkingConfig")},"HostConfig":copy.deepcopy(payload["HostConfig"]),"State":{"Running":False},"NetworkSettings":{"Networks":{"bridge":{"IPAddress":"172.17.0.3"}}},"Mounts":[]}
        return {"Id":"new-id"}
    def remove_retired(self,identifier):self.calls.append(("remove_retired",identifier));self.old=None
    def remove_failed_replacement(self,identifier):self.calls.append(("remove_failed",identifier))


class ConfigTests(unittest.TestCase):
    def test_native_cli_aliases_and_unrelated_arguments(self):
        vals,names,_,unknown=parse_args(["-ce","-i900","--scope=test","--notification-title-tag","A B","jellyfin"])
        self.assertEqual(vals["interval"],"900");self.assertEqual(vals["cleanup"],"true");self.assertEqual(vals["label-enable"],"true")
        self.assertEqual(names,["jellyfin"]);self.assertFalse(unknown)
    def test_activation_preserves_schedule_notifications_and_mounts(self):
        before=fixture();after=build_config(before,{"http_api_update":True})
        self.assertEqual(after["HostConfig"],before["HostConfig"])
        self.assertIn("WATCHTOWER_NOTIFICATION_URL=telegram://private-token",after["Env"])
        self.assertIn("UNRELATED=value",after["Env"])
        self.assertIn("WATCHTOWER_HTTP_API_PERIODIC_POLLS=true",after["Env"])
        self.assertEqual(after["Cmd"],before["Config"]["Cmd"])
    def test_schedule_replaces_interval_and_cli_precedence(self):
        before=fixture();before["Config"]["Cmd"]=["-i900","--notification-title-tag=keep","jellyfin"]
        after=build_config(before,{"schedule":"0 */15 * * * *"})
        self.assertNotIn("-i900",after["Cmd"]);self.assertIn("--notification-title-tag=keep",after["Cmd"])
        self.assertFalse(any(x.startswith("WATCHTOWER_POLL_INTERVAL=") for x in after["Env"]))
        self.assertIn("WATCHTOWER_SCHEDULE=0 */15 * * * *",after["Env"])
    def test_booleans_and_bad_values_rejected(self):
        for patch_ in ({"interval":-1},{"cleanup":"true"},{"schedule":"bad"},{"disabled":["../bad"]}):
            with self.assertRaises(Error):build_config(fixture(),patch_)
    def test_host_api_requires_explicit_acknowledgement(self):
        before=fixture();before["HostConfig"]["NetworkMode"]="host"
        with self.assertRaises(Error):build_config(before,{"http_api_update":True})
        self.assertIn("WATCHTOWER_HTTP_API_UPDATE=true",build_config(before,{"http_api_update":True,"confirm_host_api":True})["Env"])
    def test_native_filters_are_preserved(self):
        before=fixture();c={"Names":["/database"],"Labels":{"com.centurylinklabs.watchtower.enable":"false"},"State":"running"}
        self.assertEqual(eligible(c,before),(False,"Desativado por etiqueta"))
        c.update(Names=["/jellyfin"],Labels={});self.assertTrue(eligible(c,before)[0])
        c["Names"]=["/other"];self.assertFalse(eligible(c,before)[0])
    def test_wrong_image_and_static_ip_rejected(self):
        before=fixture();before["Config"]["Image"]="postgres:latest"
        with self.assertRaises(Error):build_config(before,{"cleanup":True})
        before=fixture();before["NetworkSettings"]["Networks"]["bridge"]["IPAMConfig"]={"IPv4Address":"172.17.0.9"}
        with self.assertRaises(Error):build_config(before,{"cleanup":True})
    def test_redaction(self):
        text=sanitize("private api-secret token=another telegram://private-token",fixture())
        self.assertNotIn("api-secret",text);self.assertNotIn("private-token",text);self.assertNotIn("another",text)


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.docker=FakeDocker();self.http_calls=[]
        self.server=server.make_server("127.0.0.1",0,self.temp.name,self.docker,lambda info,images:self.http_calls.append(images))
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
    def tearDown(self):self.server.shutdown();self.thread.join();self.server.server_close();self.temp.cleanup()
    def request(self,path="/api/state",body=None,headers=None):
        conn=http.client.HTTPConnection("127.0.0.1",self.server.server_port,timeout=10)
        h={"Content-Type":"application/json","X-Sal0-Request":"1"} if headers is None else headers
        conn.request("POST" if body is not None else "GET",path,json.dumps(body) if body is not None else None,h)
        res=conn.getresponse();data=res.read();status=res.status;mime=res.getheader("Content-Type");conn.close()
        return status,json.loads(data) if mime.startswith("application/json") else data
    def wait(self):
        for _ in range(80):
            s=self.request()[1]
            if s["job"]["state"]!="running":return s
            time.sleep(.01)
        self.fail("Job did not finish")
    def test_status_and_shutdown_never_control_watchtower(self):
        current=self.request()[1];self.assertEqual(current["status"],"running");self.assertEqual(self.docker.calls,[])
        self.assertNotIn("api-secret",json.dumps(current));self.assertNotIn("private-token",json.dumps(current))
    def test_public_inventory_includes_zima_names_without_compose_secrets(self):
        from test_apps import browser_catalog,BrowserDocker
        self.server.tower.catalog=browser_catalog(Path(self.temp.name)/"apps")
        self.docker.containers=BrowserDocker().containers
        status,current=self.request()
        self.assertEqual(status,200)
        self.assertEqual([a["title"] for a in current["apps"]],["Cinema Victor","Immich da Casa"])
        self.assertEqual(len(current["apps"][1]["containers"]),3)
        self.assertNotIn("must-never-reach-browser",json.dumps(current))
    def test_api_activation_and_manual_native_update(self):
        s=self.request()[1]
        self.assertEqual(self.request("/api/update",{"images":[]})[0],400)
        with patch("watchtower.time.sleep",return_value=None):
            status,_=self.request("/api/settings",{"revision":s["revision"],"settings":{"http_api_update":True}})
            self.assertEqual(status,202);s=self.wait()
        self.assertEqual(s["job"]["state"],"done",s["job"])
        self.assertTrue(s["settings"]["http_api_update"])
        self.assertEqual(self.request("/api/update",{"images":["jellyfin/jellyfin"]})[0],202);self.wait()
        self.assertEqual(self.http_calls,[["jellyfin/jellyfin"]])
        self.assertTrue(any(x[0]=="remove_retired" for x in self.docker.calls))
    def test_failed_recreation_restores_existing_container(self):
        s=self.request()[1];self.docker.fail_create=True
        self.request("/api/settings",{"revision":s["revision"],"settings":{"cleanup":False}})
        current=self.wait();self.assertEqual(current["job"]["state"],"error")
        self.assertTrue(self.docker.old["State"]["Running"])
        self.assertTrue(any(c[0]=="rename" and c[1]=="watchtower" for c in self.docker.calls))
    def test_concurrent_operation_and_revision_conflict(self):
        self.assertEqual(self.request("/api/settings",{"revision":"stale","settings":{"cleanup":True}})[0],409)
        lock=self.server.tower.lock;lock.acquire()
        try:self.assertEqual(self.request("/api/service",{"action":"stop"})[0],409)
        finally:lock.release()
    def test_csrf_and_static(self):
        self.assertEqual(self.request("/api/service",{"action":"stop"},headers={"Content-Type":"application/json"})[0],403)
        for path in ("/","/app.js","/style.css","/icon.svg","/healthz"):self.assertEqual(self.request(path)[0],200)
        self.assertEqual(self.request("/../watchtower.py")[0],404)


if __name__=="__main__":unittest.main(verbosity=2)
