from pathlib import Path
import os
import shutil
import sys
import tempfile
import threading
from unittest.mock import patch
from playwright.sync_api import sync_playwright,expect
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"panel"))
import server
from test_tower import FakeDocker

temp=tempfile.TemporaryDirectory();docker=FakeDocker()
app=server.make_server("127.0.0.1",0,temp.name,docker,lambda info,images:None)
thread=threading.Thread(target=app.serve_forever,daemon=True);thread.start();errors=[]
try:
 with patch("watchtower.time.sleep",return_value=None),sync_playwright() as p:
  browser=p.chromium.launch(headless=True,executable_path=shutil.which("google-chrome"),args=["--no-sandbox"])
  page=browser.new_page(viewport={"width":1280,"height":1000});page.on("pageerror",lambda e:errors.append(str(e)))
  page.goto(f"http://127.0.0.1:{app.server_port}")
  expect(page.locator("#connection")).to_have_text("Conectado")
  expect(page.locator("#containers")).to_contain_text("jellyfin")
  expect(page.locator("#update-all")).to_have_text("Ativar controles")
  page.locator("#update-all").click();page.locator("#confirm-action").click()
  page.locator("#tab-containers").click()
  expect(page.locator("#update-all")).to_have_text("Verificar atualizações",timeout=15000)
  page.locator("#update-all").click();page.locator("#confirm-action").click()
  expect(page.locator("#last-job")).to_have_text("Concluída",timeout=15000)
  page.locator("#tab-settings").click();page.locator("#interval").fill("1200");page.locator("#interval").focus()
  page.wait_for_timeout(5500)
  expect(page.locator("#interval")).to_have_value("1200")
  assert page.locator("#interval").evaluate("e=>e===document.activeElement")
  page.locator("#save-settings").click();page.locator("#confirm-action").click()
  expect(page.locator("#last-job")).to_have_text("Concluída",timeout=15000)
  page.locator("#tab-settings").click();expect(page.locator("#interval")).to_have_value("1200",timeout=15000)
  page.locator("#tab-containers").click();page.locator("#search").fill("database")
  expect(page.locator("#containers article")).to_have_count(1)
  page.get_by_role("checkbox",name="Excluir database",exact=True).check()
  expect(page.locator("#exclusions-bar")).to_be_visible()
  page.locator("#apply-exclusions").click();page.locator("#confirm-action").click()
  expect(page.locator("#last-job")).to_have_text("Concluída",timeout=15000)
  page.locator("#tab-containers").click();page.locator("#search").fill("")
  for width in (390,360):
   page.set_viewport_size({"width":width,"height":844})
   for section in ("containers","activity","settings"):
    page.locator("#tab-"+section).click()
    assert page.evaluate("document.documentElement.scrollWidth<=innerWidth"),(width,section)
   page.locator("#tab-containers").click();page.locator("#update-all").click()
   assert page.locator("#confirm-dialog").evaluate("e=>e.getBoundingClientRect().right<=innerWidth")
   page.locator("#cancel-dialog").click()
  page.locator("#theme").click();expect(page.locator("body")).to_have_class("light")
  page.reload();expect(page.locator("body")).to_have_class("light")
  assert not errors,errors
  browser.close();print("Browser OK: activation, native update, settings, search, exclusions, 1280/390/360px, themes and preserved input focus.")
finally:
 app.shutdown();thread.join();app.server_close();temp.cleanup()
