"""Read display metadata only; never expose compose environment or credentials."""
from collections import defaultdict
import hashlib
import os
from pathlib import Path, PurePosixPath
import threading
import time
from urllib.parse import urlsplit

import yaml


def text(value, limit=250):
    return value.strip()[:limit] if isinstance(value, str) else ""


def title(value):
    if isinstance(value, str):
        return text(value)
    if isinstance(value, dict):
        for key in ("custom", "pt_br", "pt_BR", "pt-BR", "en_us", "en_US", "en"):
            if text(value.get(key)):
                return text(value[key])
        return next((text(v) for v in value.values() if text(v)), "")
    return ""


def icon(value):
    value = text(value, 300000)
    if value.startswith(("data:image/png;base64,", "data:image/jpeg;base64,", "data:image/webp;base64,")):
        return value
    try:
        url = urlsplit(value)
        if url.scheme in ("http", "https") and url.hostname and not url.username and not url.password and len(value) <= 4096:
            return value
    except ValueError:
        pass
    return ""


def labels(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        return dict(x.split("=", 1) for x in value if isinstance(x, str) and "=" in x)
    return {}


class AppCatalog:
    def __init__(self, directory=None, ttl=15):
        self.directory = Path(directory or os.getenv("SAL0_ZIMA_APPS", "/zima-apps"))
        self.host_directory = os.getenv("SAL0_ZIMA_HOST_APPS", "/var/lib/casaos/apps")
        self.ttl = ttl
        self.next_refresh = 0
        self.lock = threading.Lock()
        self.records = []
        self.available = False

    def snapshot(self):
        with self.lock:
            if time.monotonic() < self.next_refresh:
                return self.records, self.available
            self.next_refresh = time.monotonic() + self.ttl
            records = []
            try:
                root = self.directory.resolve()
                directories = sorted(p for p in root.iterdir() if p.is_dir())[:2000]
                self.available = True
                for folder in directories:
                    for filename in ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"):
                        path = folder / filename
                        if not path.is_file():
                            continue
                        try:
                            if not path.resolve().is_relative_to(root) or path.stat().st_size > 2 * 1024 * 1024:
                                break
                            document = yaml.load(path.read_text(), Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader))
                            record = self.record(document, folder.name)
                            if record:
                                records.append(record)
                        except (OSError, UnicodeError, ValueError, yaml.YAMLError):
                            pass
                        break
            except OSError:
                self.available = False
            self.records = records
            return records, self.available

    @staticmethod
    def record(document, folder):
        if not isinstance(document, dict) or not isinstance(document.get("services"), dict):
            return None
        meta = document.get("x-casaos")
        meta = meta if isinstance(meta, dict) else {}
        services = {}
        for key, value in document["services"].items():
            if not isinstance(key, str) or not isinstance(value, dict):
                continue
            service_labels = labels(value.get("labels"))
            services[key] = dict(name=text(value.get("container_name")), icon=icon(service_labels.get("icon")), title=title(service_labels.get("name")))
        main = text(meta.get("main")) or next(iter(services), "")
        main_service = services.get(main, {})
        return dict(folder=folder, project=text(document.get("name")) or folder,
                    title=title(meta.get("title")), icon=icon(meta.get("icon")) or main_service.get("icon", ""),
                    main=main, services=services)

    def decorate(self, raw, containers):
        records, available = self.snapshot()
        folders = {r["folder"]: r for r in records}
        named, projects = defaultdict(list), defaultdict(list)
        for record in records:
            for service in record["services"].values():
                if service["name"]:
                    named[service["name"]].append(record)
            for key in {record["folder"], record["project"]}:
                projects[key].append(record)
        groups = {}
        by_id = {c["Id"]: c for c in raw}
        for public in containers:
            container = by_id[public["id"]]
            tags = container.get("Labels") or {}
            project = text(tags.get("com.docker.compose.project"))
            service_name = text(tags.get("com.docker.compose.service"))
            working = text(tags.get("com.docker.compose.project.working_dir"), 4096)
            files = text(tags.get("com.docker.compose.project.config_files"), 8192)
            record = None
            for host_path in [working] + [str(PurePosixPath(p).parent) for p in files.split(",") if p]:
                try:
                    relative = PurePosixPath(host_path).relative_to(self.host_directory)
                    if relative.parts and relative.parts[0] in folders:
                        record = folders[relative.parts[0]]
                        break
                except ValueError:
                    pass
            if record is None and len(named[public["name"]]) == 1:
                record = named[public["name"]][0]
            if record is None and project and len(projects[project]) == 1:
                candidate = projects[project][0]
                if not service_name or service_name in candidate["services"]:
                    record = candidate
            if record:
                group_id = "zima:" + record["folder"]
            elif project:
                # Some ZimaOS versions reuse a generic project name. Distinct
                # config directories must not accidentally become one stack.
                location = working or files
                group_id = "compose:" + project + ":" + hashlib.sha256(location.encode()).hexdigest()[:12]
            else:
                group_id = "container:" + public["id"]
            own_icon = icon(tags.get("icon"))
            own_title = title(tags.get("name"))
            service = (record["services"].get(service_name) or next((v for v in record["services"].values() if v["name"] == public["name"]), {})) if record else {}
            public.update(app_id=group_id, service=service_name,
                          display_name=own_title or service.get("title") or public["name"],
                          icon=own_icon or service.get("icon") or (record["icon"] if record else ""))
            if group_id not in groups:
                groups[group_id] = dict(id=group_id, title=(record["title"] if record else "") or own_title or project or public["name"],
                                        icon=(record["icon"] if record else "") or own_icon,
                                        project=project, source="zima" if record else "compose" if project else "container",
                                        containers=[], main_container=None)
            group = groups[group_id]
            group["containers"].append(public["id"])
            if record and (service_name == record["main"] or (not service_name and service == record["services"].get(record["main"]))):
                group["main_container"] = public["id"]
            if not group["icon"] and public["icon"]:
                group["icon"] = public["icon"]
        result = sorted(groups.values(), key=lambda g: g["title"].casefold())
        for group in result:
            group["is_stack"] = len(group["containers"]) > 1
            if group["is_stack"] and group["source"] == "compose":
                # A database's own display label is not the title of its stack.
                names = {c["display_name"] for c in containers if c["id"] in group["containers"]}
                if len(names) != 1:
                    group["title"] = group["project"]
        result.sort(key=lambda g: g["title"].casefold())
        return result, dict(available=available, apps=len(records))
