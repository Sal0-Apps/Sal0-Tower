from __future__ import annotations
from collections import deque
import copy
from datetime import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import re
import secrets
import tarfile
import tempfile
import threading
import time
from urllib.parse import urlencode
from urllib.request import Request, build_opener, ProxyHandler

from docker_api import Docker, Error
from apps import AppCatalog

ROOT = Path(__file__).resolve().parent
SPEC = json.loads((ROOT / "flag_spec.json").read_text())
ALIASES = {v["alias"]: k for k,v in SPEC.items() if v["alias"]}
BOOLS = {k for k,v in SPEC.items() if v["type"]=="bool"}
EDITABLE = {"cleanup", "label-enable", "include-stopped", "revive-stopped", "include-restarting", "monitor-only", "rolling-restart", "http-api-update", "http-api-periodic-polls", "disable-containers"}
CONFIG_KEYS = {"Hostname", "Domainname", "User", "AttachStdin", "AttachStdout", "AttachStderr", "ExposedPorts", "Tty", "OpenStdin", "StdinOnce", "Env", "Cmd", "Healthcheck", "Image", "Volumes", "WorkingDir", "Entrypoint", "NetworkDisabled", "MacAddress", "OnBuild", "Labels", "StopSignal", "StopTimeout", "Shell"}
PREFIX = "com.centurylinklabs.watchtower."


def atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".sal0-", dir=str(path.parent))
    try:
        with os.fdopen(fd,"w") as out:
            json.dump(value,out,ensure_ascii=False,indent=2);out.flush();os.fsync(out.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)


def boolean(value):
    return str(value).lower() in ("true","1","yes","on")


def parse_args(args):
    """Parse native flag forms, retaining spans so unrelated args stay intact."""
    values, names, spans, unknown = {}, [], [], []
    i=0
    while i<len(args):
        token=args[i]
        if token=="--": names.extend(args[i+1:]);break
        if not token.startswith("-"): names.append(token);i+=1;continue
        start=i; attached=None
        if token.startswith("--"):
            key,eq,attached=token[2:].partition("=");attached=attached if eq else None
        else:
            short=token[1:2];key=ALIASES.get(short)
            if len(token)>2 and key in BOOLS:
                keys=[ALIASES.get(x) for x in token[1:]]
                if all(x in BOOLS for x in keys):
                    for x in keys: values[x]="true"
                    spans.append((start,start+1,tuple(keys)));i+=1;continue
            attached=token[2:] or None
            if attached and attached.startswith("="):attached=attached[1:]
        if key not in SPEC:
            unknown.append(token);i+=1;continue
        if key in BOOLS:
            values[key]="true" if attached is None else attached
        else:
            if attached is None:
                if i+1>=len(args):unknown.append(token);i+=1;continue
                i+=1;attached=args[i]
            values[key]=attached
        spans.append((start,i+1,(key,)));i+=1
    return values,names,spans,unknown


def options(info):
    env=dict(x.split("=",1) for x in info.get("Config",{}).get("Env",[]) if "=" in x)
    args=info.get("Config",{}).get("Cmd") or []
    flags,names,_,unknown=parse_args(args)
    result={k:env.get(v["env"],"") for k,v in SPEC.items()}
    result.update(flags)
    return result,names,unknown


def public_settings(info):
    opts,names,unknown=options(info)
    values={k.replace("-","_"):boolean(opts[k]) for k in EDITABLE if k in BOOLS}
    try: interval=int(opts.get("interval") or 86400)
    except ValueError:interval=86400
    values.update(interval=interval,schedule=opts.get("schedule", ""),scope=opts.get("scope", ""),
                  disabled=split_names(opts.get("disable-containers", "")),names=[] if unknown else names,
                  no_pull=boolean(opts.get("no-pull")),no_restart=boolean(opts.get("no-restart")),
                  host_network=info.get("HostConfig",{}).get("NetworkMode")=="host",
                  unknown_flags=bool(unknown),notification_configured=any(opts.get(k) for k in SPEC if k.startswith("notification") and ("url" in k or "hook" in k or k=="notifications")))
    return values


def split_names(value):return [x for x in re.split(r"[,\s]+",value or "") if x]


def revision(info):
    return hashlib.sha256(json.dumps({k:info.get(k) for k in ("Id","Config","HostConfig")},sort_keys=True).encode()).hexdigest()


def check_image(info):
    image=info.get("Config",{}).get("Image","").split("@")[0].rsplit(":",1)[0]
    if image not in ("containrrr/watchtower", "ghcr.io/containrrr/watchtower", "nickfedor/watchtower", "ghcr.io/nickfedor/watchtower"):
        raise Error("O container selecionado não usa uma imagem Watchtower reconhecida. Nenhuma alteração foi executada.",400)


def sanitize(line, info):
    opts,_,_=options(info)
    for key,value in opts.items():
        if value and any(x in key for x in ("token","password","hook","notification-url","email-to","email-from")):
            line=line.replace(value,"[oculto]")
    for entry in info.get("Config",{}).get("Env",[]):
        key,_,value=entry.partition("=")
        if value and re.search(r"token|pass|secret|credential|hook|api.?key",key,re.I):line=line.replace(value,"[oculto]")
    line=re.sub(r"[a-z][a-z0-9+.-]*://[^\s\"'<>]+", "[endereço oculto]", line, flags=re.I)
    return re.sub(r"(?i)(bearer\s+|(?:token|password|passwd|authorization)[=: ]+)[^\s,\"']+",r"\1[oculto]",line)[:3000]


def eligible(container, info):
    opts,names,unknown=options(info);name=(container.get("Names") or [""])[0].lstrip("/");labels=container.get("Labels") or {}
    if labels.get(PREFIX+"enable", "").lower() in ("false","0"):return False,"Desativado por etiqueta"
    if name in split_names(opts.get("disable-containers")):return False,"Excluído no Watchtower"
    if boolean(opts.get("label-enable")) and not boolean(labels.get(PREFIX+"enable")):return False,"Sem etiqueta de autorização"
    scope=opts.get("scope")
    if scope and (labels.get(PREFIX+"scope") or "none")!=scope:return False,"Fora do escopo"
    if names:
        if unknown:return None,"Filtro nativo adicional"
        try:
            if not any(n in (name,"/"+name) or (lambda m:bool(m) and m.start()<=1 and m.end()>=len(name))(re.search(n,"/"+name)) for n in names):return False,"Fora da lista de nomes"
        except re.error:return None,"Filtro nativo de nomes"
    state=container.get("State","")
    if state=="restarting" and not boolean(opts.get("include-restarting")):return False,"Reiniciando; filtro desativado"
    if state not in ("running","restarting") and not boolean(opts.get("include-stopped")):return False,"Parado; filtro desativado"
    if unknown:return None,"Filtro nativo adicional"
    monitor=boolean(opts.get("monitor-only"))
    if PREFIX+"monitor-only" in labels and (not monitor or boolean(opts.get("label-take-precedence"))):monitor=boolean(labels[PREFIX+"monitor-only"])
    return True,"Somente monitoramento" if monitor else "Monitorado"


def build_config(before, patch):
    """Change only known flags; preserve image, mounts, notifications and network."""
    check_image(before)
    host=copy.deepcopy(before.get("HostConfig",{}));mode=host.get("NetworkMode", "bridge")
    if host.get("AutoRemove") or mode.startswith("container:"):
        raise Error("Essa instalação usa opções de ciclo de vida que exigem alteração pelo compose original.",400)
    networks=before.get("NetworkSettings",{}).get("Networks",{})
    if any((v.get("IPAMConfig") or {}).get(k) for v in networks.values() for k in ("IPv4Address","IPv6Address")):
        raise Error("O Watchtower usa IP fixo no Docker. Para preservar esse IP, aplique os ajustes pelo compose original.",400)
    cfg={k:copy.deepcopy(v) for k,v in before.get("Config",{}).items() if k in CONFIG_KEYS}
    env=dict(x.split("=",1) for x in cfg.get("Env",[]) if "=" in x);args=cfg.get("Cmd") or []
    opts,_,_=options(before);changes={}
    if boolean(opts.get("run-once")):
        raise Error("Essa instalação está configurada para execução única. Ajuste o compose original para usar um Watchtower contínuo antes de aplicar pela interface.",400)
    for key in EDITABLE:
        ui=key.replace("-","_")
        if key in BOOLS and ui in patch:
            if type(patch[ui]) is not bool:raise Error("Opção inválida: "+ui,400)
            changes[key]="true" if patch[ui] else "false"
    if "disabled" in patch:
        names=patch["disabled"]
        if not isinstance(names,list) or len(names)>2000 or any(not isinstance(x,str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}",x) for x in names):raise Error("Lista de exclusões inválida.",400)
        changes["disable-containers"]=",".join(dict.fromkeys(names))
    if "interval" in patch or "schedule" in patch:
        schedule=patch.get("schedule","").strip()
        if schedule:
            if len(schedule)>150 or not (len(schedule.split())==6 or re.fullmatch(r"@(?:hourly|daily|weekly|monthly|yearly|annually|midnight)",schedule) or re.fullmatch(r"@every [0-9hms.]+",schedule)):
                raise Error("Informe um cron de 6 campos ou uma expressão como @every 15m.",400)
            changes.update(schedule=schedule,interval=None)
        else:
            interval=patch.get("interval",86400)
            if type(interval) is not int or not 60<=interval<=31536000:raise Error("O intervalo precisa estar entre 60 segundos e 365 dias.",400)
            changes.update(interval=str(interval),schedule=None)
    if changes.get("http-api-update")=="true" and not boolean(opts.get("http-api-update")):
        if mode=="host" and patch.get("confirm_host_api") is not True:
            raise Error("Na rede host, ativar o controle abre a API autenticada na porta 8080 do servidor. Confirme essa opção antes de aplicar.",400)
        changes.setdefault("http-api-periodic-polls","true")
        if not opts.get("http-api-token"):
            changes["http-api-token"]=secrets.token_urlsafe(48)
    values,_,spans,unknown=parse_args(args)
    remove=set(changes)
    new_args=[];position=0
    for start,end,keys in spans:
        new_args.extend(args[position:start])
        keep=[k for k in keys if k not in remove]
        if len(keep)==len(keys):new_args.extend(args[start:end])
        elif keep:new_args.extend("--"+k+"="+values[k] for k in keep)
        position=end
    new_args.extend(args[position:])
    for key,value in changes.items():
        envkey=SPEC[key]["env"]
        if not envkey:raise Error("Opção nativa sem mapeamento: "+key,400)
        if value is None:env.pop(envkey,None)
        else:env[envkey]=value
    cfg["Env"]=[k+"="+v for k,v in env.items()];cfg["Cmd"]=new_args
    # Reattach anonymous volumes by their existing name, never allocate replacements.
    mounts=host.get("Mounts") or [];bound={m.get("Target") for m in mounts}
    bound.update(x.split(":")[1] for x in host.get("Binds") or [] if ":" in x)
    for mount in before.get("Mounts",[]):
        if mount.get("Type")=="volume" and mount.get("Destination") not in bound:
            mounts.append(dict(Type="volume",Source=mount["Name"],Target=mount["Destination"],ReadOnly=not mount.get("RW",True)))
    if mounts:host["Mounts"]=mounts
    result={**cfg,"HostConfig":host}
    if mode not in ("host","none"):
        endpoints={n:{"Aliases":v.get("Aliases") or []} for n,v in networks.items() if n!="bridge"}
        if endpoints:result["NetworkingConfig"]={"EndpointsConfig":endpoints}
    return result


class Tower:
    def __init__(self, directory, docker=None, http=None, catalog=None):
        self.directory=Path(directory);self.directory.mkdir(parents=True,exist_ok=True);self.directory.chmod(0o700)
        self.docker=docker or Docker();self.http=http or self.http_update
        self.catalog=catalog or AppCatalog()
        self.name=os.getenv("WATCHTOWER_CONTAINER","watchtower");self.lock=threading.Lock();self.events=deque(maxlen=150)
        self.job={"state":"idle"};self.job_file=self.directory/"last-job.json"
        if self.job_file.exists():
            try:self.job=json.loads(self.job_file.read_text())
            except ValueError:pass
            if self.job.get("state")=="running":self.job.update(state="interrupted",message="O painel reiniciou durante uma operação. Confira o Watchtower e os registros antes de repetir.")

    def inspect(self):
        info=self.docker.inspect(self.name);check_image(info);return info

    def log(self, message):
        self.events.append(dict(time=time.time(),message=message,source="painel"))

    def state(self):
        job=copy.deepcopy(self.job)
        # Completion is visible only after persistence and lock release, so the
        # next action can start immediately when the UI reports it as finished.
        if self.lock.locked():job["state"]="running"
        result=dict(version="1.1.0",name=self.name,status="unavailable",settings={},containers=[],apps=[],catalog={"available":False,"apps":0},logs=list(self.events),job=job,error=None)
        try:
            info=self.inspect();settings=public_settings(info)
            result.update(status="running" if info["State"].get("Running") else "paused",settings=settings,revision=revision(info),image=info["Config"]["Image"])
            raw=self.docker.containers()
            for c in raw:
                allowed,reason=eligible(c,info)
                result["containers"].append(dict(id=c["Id"],name=(c.get("Names")or["?"])[0].lstrip("/"),image=c.get("Image",""),state=c.get("State",""),eligible=allowed,reason=reason,excluded=(c.get("Names")or[""])[0].lstrip("/") in settings["disabled"]))
            result["apps"],result["catalog"]=self.catalog.decorate(raw,result["containers"])
            for line in self.docker.logs(info["Id"]):
                stamp,_,msg=line.partition(" ")
                try:when=datetime.fromisoformat(stamp.replace("Z","+00:00")).timestamp()
                except ValueError:when=0;msg=line
                result["logs"].append(dict(time=when,message=sanitize(msg,info),source="watchtower"))
            result["logs"]=sorted(result["logs"],key=lambda x:x["time"])[-150:]
        except Error as exc:result["error"]=str(exc)
        return result

    def submit(self, kind, operation):
        if not self.lock.acquire(blocking=False):raise Error("Já existe uma operação em andamento. Aguarde a conclusão.",409)
        self.job=dict(state="running",kind=kind,started=time.time(),message="Operação em andamento; acompanhe os registros.")
        try:atomic(self.job_file,self.job)
        except OSError as exc:
            self.job.update(state="error",finished=time.time(),message="Não foi possível iniciar a operação: a pasta de dados não está disponível.")
            self.lock.release()
            raise Error("Não foi possível gravar os dados do painel. Confira a pasta de dados antes de tentar novamente.") from exc
        def run():
            try:
                operation();self.job.update(state="done",finished=time.time(),message="Solicitação concluída. Confira o resultado nos registros do Watchtower.")
            except Exception as exc:
                message=str(exc) if isinstance(exc,Error) else "Não foi possível concluir a operação. Verifique o Watchtower antes de tentar novamente."
                self.job.update(state="error",finished=time.time(),message=message);self.log(message)
            finally:
                try:atomic(self.job_file,self.job)
                except OSError:self.log("A operação terminou, mas não foi possível salvar o histórico na pasta de dados.")
                finally:self.lock.release()
        threading.Thread(target=run,daemon=True).start()
        return self.state()

    def token(self, info, value):
        if value.startswith("/"):
            data=self.docker.request("GET","/containers/"+info["Id"]+"/archive?"+urlencode({"path":value}),raw=True)
            with tarfile.open(fileobj=io.BytesIO(data)) as archive:
                member=next((m for m in archive.getmembers() if m.isfile()),None)
                if not member or member.size>16384:raise Error("Arquivo de token inválido.")
                return archive.extractfile(member).read().decode().strip()
        return value

    def http_update(self, info, images):
        opts,_,_=options(info)
        if not boolean(opts.get("http-api-update")):raise Error("Ative os controles pela interface antes de solicitar uma atualização.",400)
        token=self.token(info,opts.get("http-api-token", ""))
        if not token:raise Error("A API do Watchtower não possui token configurado.",400)
        if info.get("HostConfig",{}).get("NetworkMode")=="host":host="127.0.0.1" if os.getenv("SAL0_HOST_NETWORK")=="true" else "host.docker.internal"
        else:
            networks=info.get("NetworkSettings",{}).get("Networks",{})
            host=next((v.get("IPAddress") for v in networks.values() if v.get("IPAddress")),None)
        if not host:raise Error("O Watchtower não tem um endereço de rede acessível ao painel.")
        url=f"http://{host}:8080/v1/update"+("?"+urlencode([("image",x) for x in images]) if images else "")
        req=Request(url,headers={"Authorization":"Bearer "+token})
        try:
            with build_opener(ProxyHandler({})).open(req,timeout=1800) as response:response.read(8192)
        except Exception as exc:raise Error("A conexão com a API foi interrompida ou expirou. A atualização pode continuar no Watchtower; confira os registros antes de repetir.") from exc

    def reconfigure(self, patch, expected, restore=None):
        before=self.inspect()
        if revision(before)!=expected:raise Error("A configuração mudou em outra aba. Recarregue antes de aplicar.",409)
        payload=build_config(restore or before,patch);backup=self.directory/("backup-"+str(time.time_ns())+".json")
        atomic(backup,before);old_id=before["Id"];old_name=self.name;archive=old_name+"-sal0-backup-"+str(time.time_ns())
        running=before["State"].get("Running",False);renamed=False;created=None
        # A stopped rollback container must never be revived by include-stopped.
        pinfo={"Config":payload};poptions,_,_=options(pinfo)
        blocked=[x for x in split_names(poptions.get("disable-containers")) if not x.startswith(old_name+"-sal0-backup-")]+[archive]
        payload=build_config({**before,"Config":{k:v for k,v in payload.items() if k in CONFIG_KEYS},"HostConfig":payload["HostConfig"]},{"disabled":blocked})
        self.job["message"]="Aguardando o Watchtower encerrar operações e aplicar a configuração."
        try:
            # Native Watchtower waits for its update lock on SIGTERM; never force kill.
            if running:self.docker.control(old_id,"stop",timeout=None)
            self.docker.rename(old_id,archive);renamed=True
            created=self.docker.create(old_name,payload)["Id"]
            if running:self.docker.control(created,"start")
            if running:
                for _ in range(12):
                    current=self.docker.inspect(created)
                    if not current["State"].get("Running") or current["State"].get("Restarting"):
                        raise Error("O Watchtower recusou a nova configuração. A configuração anterior será restaurada.")
                    time.sleep(.5)
            self.log("Configuração aplicada. Agendamento, credenciais e notificações preservados.")
            # Snapshot includes the old create options. Keep volumes, retire only
            # the replaced container after the new process passed its checks.
            self.docker.remove_retired(old_id)
            for previous in sorted(self.directory.glob("backup-*.json"))[:-10]:
                try:previous.unlink()
                except OSError:pass
        except Exception:
            try:self.docker.inspect(old_id);previous_exists=True
            except Error:previous_exists=False
            if previous_exists:
                if created:self.docker.remove_failed_replacement(created)
                if renamed:self.docker.rename(old_id,old_name)
                if running:self.docker.control(old_id,"start")
            raise

    def action(self, path, body):
        if not isinstance(body,dict):raise Error("Pedido inválido.",400)
        if path=="/api/update":
            images=body.get("images",[])
            if not isinstance(images,list) or len(images)>50 or any(not isinstance(x,str) or len(x)>300 or not re.fullmatch(r"[\w./:-]+",x) for x in images):raise Error("Seleção de imagens inválida.",400)
            info=self.inspect();opts,_,_=options(info)
            if not boolean(opts.get("http-api-update")):raise Error("Ative os controles pela interface em Ajustes.",400)
            return self.submit("update",lambda:self.http(self.inspect(),images))
        if path=="/api/settings":
            patch=body.get("settings")
            if not isinstance(patch,dict):raise Error("Configuração inválida.",400)
            info=self.inspect()
            if revision(info)!=body.get("revision"):raise Error("A configuração mudou. Recarregue antes de aplicar.",409)
            build_config(info,patch) # Validate before accepting the asynchronous job.
            return self.submit("settings",lambda:self.reconfigure(patch,body["revision"]))
        if path=="/api/service":
            action=body.get("action")
            if action not in ("start","stop","restart"):raise Error("Ação inválida.",400)
            return self.submit("service",lambda:self.docker.control(self.inspect()["Id"],action,timeout=None))
        if path=="/api/restore":
            backups=sorted(self.directory.glob("backup-*.json"))
            if not backups:raise Error("Não existe configuração anterior para restaurar.",400)
            previous=json.loads(backups[-1].read_text())
            expected=body.get("revision")
            return self.submit("restore",lambda:self.reconfigure({},expected,restore=previous))
        raise Error("Caminho não encontrado.",404)
