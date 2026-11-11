import json
from pathlib import Path
import sys
import tempfile
import unittest

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "panel"))
from apps import AppCatalog
from test_tower import FakeDocker

PIXEL = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAYAAABytg0kAAAAFUlEQVR4nGP0XPf+PwMDAwMTAxQAACviAuk7tqE1AAAAAElFTkSuQmCC"


def write_app(root, folder, name, members, main, app_icon=PIXEL, project=None):
    path = Path(root) / folder
    path.mkdir(parents=True, exist_ok=True)
    document = {"name": project or folder, "services": {service: {"container_name": container, "environment": {"PRIVATE_TOKEN": "must-never-reach-browser"}} for service, container in members.items()},
                "x-casaos": {"main": main, "title": {"en_us": "English fallback", "pt_br": "Nome em português", "custom": name}, "icon": app_icon}}
    (path / "docker-compose.yml").write_text(yaml.safe_dump(document, allow_unicode=True))


class BrowserDocker(FakeDocker):
    def __init__(self):
        super().__init__()
        self.info["Config"]["Cmd"] = ["--cleanup"]

    def containers(self):
        result = super().containers()
        result[0]["Labels"].update({"com.docker.compose.project": "jellyfin", "com.docker.compose.service": "server"})
        result[1]["Labels"].update({"com.docker.compose.project": "immich", "com.docker.compose.service": "database"})
        result.extend([
            {"Id": "immich-main", "Names": ["/immich-server"], "Image": "ghcr.io/immich-app/immich-server:release", "State": "running", "Labels": {"com.docker.compose.project": "immich", "com.docker.compose.service": "server"}},
            {"Id": "redis-id", "Names": ["/immich-redis"], "Image": "redis:7", "State": "exited", "Labels": {"com.docker.compose.project": "immich", "com.docker.compose.service": "redis", "icon": "data:image/png;base64,broken"}}
        ])
        return result


def browser_catalog(root):
    write_app(root, "jellyfin", "Cinema Victor", {"server": "jellyfin"}, "server")
    write_app(root, "immich", "Immich da Casa", {"server": "immich-server", "database": "database", "redis": "immich-redis"}, "server")
    return AppCatalog(root, ttl=0)


def public(raw):
    return [dict(id=c["Id"], name=c["Names"][0].lstrip("/"), image=c["Image"], state=c["State"]) for c in raw]


class CatalogTests(unittest.TestCase):
    def test_zima_custom_names_icons_and_stack_membership_without_secrets(self):
        with tempfile.TemporaryDirectory() as root:
            catalog = browser_catalog(root)
            raw = BrowserDocker().containers()
            containers = public(raw)
            groups, status = catalog.decorate(raw, containers)
            self.assertTrue(status["available"])
            self.assertEqual([g["title"] for g in groups], ["Cinema Victor", "Immich da Casa"])
            self.assertEqual(groups[1]["main_container"], "immich-main")
            self.assertEqual(len(groups[1]["containers"]), 3)
            self.assertEqual(groups[1]["icon"], PIXEL)
            self.assertNotIn("must-never-reach-browser", json.dumps([groups, containers]))

    def test_same_image_does_not_make_unrelated_containers_a_stack(self):
        with tempfile.TemporaryDirectory() as root:
            raw = [dict(Id=str(i), Names=["/standalone-"+str(i)], Image="postgres:17", State="running", Labels={"name": "Banco "+str(i), "icon": "https://example.org/postgres.png"}) for i in range(2)]
            groups, _ = AppCatalog(root).decorate(raw, public(raw))
            self.assertEqual(len(groups), 2)
            self.assertFalse(any(g["is_stack"] for g in groups))
            self.assertEqual(groups[0]["title"], "Banco 0")

    def test_generic_project_names_use_config_directory_to_keep_apps_separate(self):
        with tempfile.TemporaryDirectory() as root:
            for folder in ("one", "two"):
                write_app(root, folder, "App "+folder, {"app": folder}, "app", project="yourprojectname")
            raw = [dict(Id=folder, Names=["/"+folder], Image="app:latest", State="running", Labels={"com.docker.compose.project": "yourprojectname", "com.docker.compose.service": "app", "com.docker.compose.project.working_dir": "/var/lib/casaos/apps/"+folder}) for folder in ("one", "two")]
            groups, _ = AppCatalog(root).decorate(raw, public(raw))
            self.assertEqual([g["title"] for g in groups], ["App one", "App two"])
            missing = AppCatalog(Path(root)/"missing")
            fallback, status = missing.decorate(raw, public(raw))
            self.assertEqual(len(fallback), 2)
            self.assertFalse(status["available"])

    def test_compose_stack_groups_without_zima_mount_and_ignores_invalid_icons(self):
        with tempfile.TemporaryDirectory() as root:
            raw = [dict(Id=str(i), Names=["/svc-"+str(i)], Image="app:latest", State="running", Labels={"com.docker.compose.project": "my-stack", "icon": "javascript:alert(1)"}) for i in range(2)]
            groups, _ = AppCatalog(root).decorate(raw, public(raw))
            self.assertEqual(len(groups), 1)
            self.assertTrue(groups[0]["is_stack"])
            self.assertEqual(groups[0]["icon"], "")

    def test_invalid_file_is_skipped_and_custom_title_changes_are_refreshed(self):
        with tempfile.TemporaryDirectory() as root:
            catalog = browser_catalog(root)
            bad = Path(root)/"bad";bad.mkdir()
            (bad/"docker-compose.yml").write_text("!!python/object/apply:os.system [never-run]")
            self.assertEqual(len(catalog.snapshot()[0]), 2)
            write_app(root, "jellyfin", "Novo cinema", {"server": "jellyfin"}, "server")
            self.assertIn("Novo cinema", [r["title"] for r in catalog.snapshot()[0]])


if __name__ == "__main__":
    unittest.main()
