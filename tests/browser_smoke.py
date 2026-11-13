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
from test_apps import BrowserDocker,browser_catalog

temp=tempfile.TemporaryDirectory();docker=BrowserDocker();http_calls=[]
catalog=browser_catalog(Path(temp.name)/"apps")
app=server.make_server("127.0.0.1",0,str(Path(temp.name)/"data"),docker,lambda info,images:http_calls.append(images),catalog)
thread=threading.Thread(target=app.serve_forever,daemon=True);thread.start();errors=[]
try:
 with patch("watchtower.time.sleep",return_value=None),sync_playwright() as p:
  browser=p.chromium.launch(headless=True,executable_path=shutil.which("google-chrome"),args=["--no-sandbox"])
  page=browser.new_page(viewport={"width":1280,"height":1000});page.on("pageerror",lambda e:errors.append(str(e)))
  page.goto(f"http://127.0.0.1:{app.server_port}")
  def confirm_and_wait():
   page.locator("#confirm-action").click()
   expect(page.locator("#confirm-dialog")).not_to_be_visible(timeout=15000)
   expect(page.locator("#last-job")).to_have_text("Concluída",timeout=15000)
  expect(page.locator("#connection")).to_have_text("Conectado")
  expect(page.locator("#containers .app-card")).to_have_count(2)
  expect(page.locator("#apps-count")).to_have_text("2 aplicativos · 4 containers")
  expect(page.locator("#containers")).to_contain_text("Cinema Victor")
  expect(page.locator("#catalog-notice")).to_be_hidden()
  cinema=page.locator(".app-card").filter(has=page.get_by_role("heading",name="Cinema Victor",exact=True))
  immich=page.locator(".app-card").filter(has=page.get_by_role("heading",name="Immich da Casa",exact=True))
  expect(cinema.locator(".app-icon")).to_have_class("app-icon loaded")
  assert not immich.locator("details").evaluate("e=>e.open")
  immich.locator("summary").click()
  expect(immich.locator(".container-row")).to_have_count(3)
  expect(immich.locator(".container-status").first).to_have_text("Parcial")
  page.wait_for_timeout(5500)
  assert immich.locator("details").evaluate("e=>e.open")
  expect(page.locator("#update-all")).to_have_text("Ativar controles")
  page.locator("#update-all").click();confirm_and_wait()
  page.locator("#tab-containers").click()
  expect(page.locator("#update-all")).to_have_text("Verificar atualizações",timeout=15000)
  page.locator("#update-all").click();confirm_and_wait()
  expect(page.locator("#last-job")).to_have_text("Concluída",timeout=15000)
  immich.get_by_role("button",name="Verificar stack",exact=True).click();confirm_and_wait()
  expect(page.locator("#last-job")).to_have_text("Concluída",timeout=15000)
  assert http_calls[-1]==["ghcr.io/immich-app/immich-server"],http_calls
  page.locator("#tab-settings").click();page.locator("#interval").fill("1200");page.locator("#interval").focus()
  page.wait_for_timeout(5500)
  expect(page.locator("#interval")).to_have_value("1200")
  assert page.locator("#interval").evaluate("e=>e===document.activeElement")
  page.locator("#save-settings").click();confirm_and_wait()
  expect(page.locator("#last-job")).to_have_text("Concluída",timeout=15000)
  page.locator("#tab-settings").click();expect(page.locator("#interval")).to_have_value("1200",timeout=15000)
  page.locator("#tab-containers").click();page.locator("#search").fill("database")
  expect(page.locator("#containers .app-card")).to_have_count(1)
  expect(page.locator(".app-title")).to_have_text("Immich da Casa")
  assert page.locator(".stack-details").evaluate("e=>e.open")
  page.get_by_role("checkbox",name="Excluir database",exact=True).check()
  expect(page.locator("#exclusions-bar")).to_be_visible()
  page.locator("#apply-exclusions").click();confirm_and_wait()
  expect(page.locator("#last-job")).to_have_text("Concluída",timeout=15000)
  page.locator("#tab-containers").click();page.locator("#search").fill("")
  stack_check=page.get_by_role("checkbox",name="Excluir stack Immich da Casa",exact=True)
  assert stack_check.evaluate("e=>e.indeterminate")
  stack_check.check()
  expect(immich.locator(".container-row input:checked")).to_have_count(3)
  page.locator("#cancel-exclusions").click()
  assert stack_check.evaluate("e=>e.indeterminate")
  page.locator("#app-filter").select_option("stopped")
  expect(page.locator("#containers .app-card")).to_have_count(1)
  expect(page.locator(".app-title")).to_have_text("Immich da Casa")
  page.locator("#app-filter").select_option("all")
  expect(page.locator("#containers .app-card")).to_have_count(2)
  for width in (390,360):
   page.set_viewport_size({"width":width,"height":844})
   for section in ("containers","activity","settings"):
    page.locator("#tab-"+section).click()
    assert page.evaluate("document.documentElement.scrollWidth<=innerWidth"),(width,section)
   page.locator("#tab-containers").click();page.locator("#update-all").click()
   assert page.locator("#confirm-dialog").evaluate("e=>e.getBoundingClientRect().right<=innerWidth")
   page.locator("#cancel-dialog").click()
  page.locator("#theme").click();page.locator("[data-sal0-mode-choice=light]").click();page.locator("[data-sal0-close]").click();expect(page.locator("body")).to_have_class("light")
  page.reload();expect(page.locator("body")).to_have_class("light")
  assert immich.locator("details").evaluate("e=>e.open")
  assert not errors,errors
  browser.close();print("Browser OK: Zima names/icons, collapsed stacks, mixed exclusions, native stack update filters, member search, state persistence, 1280/390/360px, themes and settings focus.")
finally:
 app.shutdown();thread.join();app.server_close();temp.cleanup()
