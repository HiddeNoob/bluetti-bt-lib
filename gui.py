#!/usr/bin/env python3
"""Minik Bluetti web GUI. Repo kokune koy ve calistir:

    python bluetti_gui.py      ->  http://localhost:8080

CLI'leri cagirmaz; DeviceReader / DeviceWriter / BleakScanner'i dogrudan kullanir.
Tum BLE islemleri tek bir arka plan asyncio dongusunde, tek kilitle sirayla calisir.
"""
import asyncio, json, logging, os, re, sys, threading
from enum import Enum
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)  # repodaki bluetti_bt_lib kullanilsin

from bleak import BleakClient, BleakScanner
from bluetti_bt_lib.bluetooth import DeviceWriter, DeviceWriterConfig
from bluetti_bt_lib.bluetooth.device_reader import DeviceReader, DeviceReaderConfig
from bluetti_bt_lib.fields import FieldName, get_unit
from bluetti_bt_lib.utils.device_builder import build_device
from bluetti_bt_lib.utils.device_info import get_type_by_bt_name

logging.basicConfig(level=logging.DEBUG if os.environ.get("BLUETTI_DEBUG") else logging.WARNING)
PORT = 8080
ADDR_RE = re.compile(r"^(([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}|[0-9A-Fa-f]{8}(-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12})$")  # MAC veya macOS UUID
NAME_RE = re.compile(r"^[A-Za-z0-9_\-]{1,40}$")

# ---- tek asyncio dongusu + sira kilidi -------------------------------------
LOOP = asyncio.new_event_loop()
threading.Thread(target=LOOP.run_forever, daemon=True).start()
_lock = None


def submit(coro_fn, timeout=60):
    async def guarded():
        global _lock
        if _lock is None:
            _lock = asyncio.Lock()
        async with _lock:  # ayni anda tek BLE islemi
            return await asyncio.wait_for(coro_fn(), timeout)
    return asyncio.run_coroutine_threadsafe(guarded(), LOOP).result(timeout + 5)


def check(body):
    mac, typ = body.get("mac", ""), body.get("type", "")
    if not ADDR_RE.match(mac) or not NAME_RE.match(typ):
        raise ValueError("Gecersiz MAC/UUID veya cihaz tipi")
    return mac, typ, bool(body.get("encryption"))


def name_of(x):
    return x.value if isinstance(x, Enum) else str(x)


# ---- API -------------------------------------------------------------------
def api_scan(body):
    t = int(body.get("time", 5))
    if not 1 <= t <= 30:
        raise ValueError("Sure 1-30 sn olmali")

    async def go():
        found = []
        for d in await BleakScanner.discover(timeout=t):
            if not d.name:
                continue
            typ = get_type_by_bt_name(d.name)
            if typ is not None or d.name.startswith("PBOX"):
                found.append({"name": d.name, "type": typ, "mac": d.address})
        return found

    devs = submit(go, t + 30)
    return {"ok": True, "devices": devs, "raw": f"{len(devs)} cihaz bulundu"}


def api_read(body):
    mac, typ, enc = check(body)

    async def go():
        built = build_device(typ + "12345678")
        if built is None:
            raise ValueError("Desteklenmeyen cihaz tipi: " + typ)
        reader = DeviceReader(mac, built, asyncio.Future, DeviceReaderConfig(use_encryption=enc))
        data = await reader.read()
        if data is None:
            raise RuntimeError("Okuma basarisiz (reader.read() None dondu)")
        return data

    data = submit(go)
    fields, units = {}, {}
    for k, v in data.items():
        key = name_of(k)
        fields[key] = v.name if isinstance(v, Enum) else v
        try:
            u = get_unit(FieldName(key))
            if u:
                units[key] = u
        except Exception:
            pass
    return {"ok": True, "fields": fields, "units": units, "raw": "okundu"}


def api_write(body):
    mac, typ, enc = check(body)
    field = body.get("field", "")
    if not NAME_RE.match(field):
        raise ValueError("Gecersiz alan adi")
    raw = str(body.get("value", "")).strip()
    if raw:
        if not NAME_RE.match(raw.lstrip("-")):
            raise ValueError("Gecersiz deger")
        value = int(raw) if re.fullmatch(r"-?\d+", raw) else raw  # sayi -> value, metin -> select/enum
    else:
        value = bool(body.get("state"))  # ctrl_ac / ctrl_dc gibi acma-kapama

    async def go():
        built = build_device(typ + "12345678")
        if built is None:
            raise ValueError("Desteklenmeyen cihaz tipi: " + typ)
        client = BleakClient(mac)
        try:
            writer = DeviceWriter(client, built, DeviceWriterConfig(use_encryption=enc))
            return await writer.write(field, value)
        finally:  # sonraki okuma icin baglanti acik kalmasin
            try:
                if client.is_connected:
                    await client.disconnect()
            except Exception:
                pass

    res = submit(go)
    return {"ok": True, "raw": f"{field} = {value!r}" + ("" if res is None else f"  ({res!r})")}


ROUTES = {"/api/scan": api_scan, "/api/read": api_read, "/api/write": api_write}


class H(BaseHTTPRequestHandler):
    def _send(self, code, ctype, payload):
        b = payload.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        self._send(200, "text/html; charset=utf-8", PAGE)

    def do_POST(self):
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            res = ROUTES[self.path](body)
            self._send(200, "application/json", json.dumps(res, default=str))
        except Exception as e:
            msg = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
            print("HATA", self.path, msg)
            self._send(200, "application/json", json.dumps({"ok": False, "raw": msg}))

    def log_message(self, *a):
        pass


PAGE = r"""<!doctype html><html lang="tr"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Bluetti AC2</title>
<style>
:root{--bg:#f6f7f9;--c:#fff;--t:#1b1f24;--m:#6b7280;--a:#2563eb;--ok:#16a34a;--b:#e5e7eb}
@media(prefers-color-scheme:dark){:root{--bg:#111418;--c:#1a1f26;--t:#e6e8eb;--m:#8b93a0;--b:#2a313a}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--t);font:15px system-ui,sans-serif}
main{max-width:640px;margin:0 auto;padding:16px}
.card{background:var(--c);border:1px solid var(--b);border-radius:12px;padding:16px;margin-bottom:14px}
h1{font-size:20px;margin:4px 0 14px}h2{font-size:13px;color:var(--m);text-transform:uppercase;margin:0 0 10px;letter-spacing:.05em}
label{display:block;font-size:12px;color:var(--m);margin:8px 0 3px}
input{width:100%;padding:9px;border:1px solid var(--b);border-radius:8px;background:var(--bg);color:var(--t)}
.row{display:flex;gap:10px}.row>*{flex:1}
button{padding:10px 14px;border:0;border-radius:8px;background:var(--a);color:#fff;font-weight:600;cursor:pointer}
button.sec{background:var(--b);color:var(--t)}button:disabled{opacity:.5}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.stat{padding:10px;border:1px solid var(--b);border-radius:8px}.stat b{display:block;font-size:20px}.stat span{font-size:12px;color:var(--m)}
.sw{display:flex;align-items:center;justify-content:space-between;padding:10px 0;border-bottom:1px solid var(--b)}
.sw:last-child{border:0}.tg{width:46px;height:26px;border-radius:13px;background:var(--b);position:relative;cursor:pointer;flex:none}
.tg::after{content:"";position:absolute;top:3px;left:3px;width:20px;height:20px;border-radius:50%;background:#fff;transition:.15s}
.tg.on{background:var(--ok)}.tg.on::after{left:23px}
.dev{display:flex;justify-content:space-between;padding:10px;margin-top:8px;border:1px solid var(--b);border-radius:8px;cursor:pointer}
.dev:hover{border-color:var(--a)}.dev span{color:var(--m);font-size:13px}
table{width:100%;border-collapse:collapse;font-size:13px}td{padding:4px 6px;border-bottom:1px solid var(--b)}td:last-child{text-align:right}
summary{cursor:pointer;color:var(--m);font-size:13px;margin-top:12px}
pre{background:var(--bg);padding:10px;border-radius:8px;overflow:auto;font-size:12px;max-height:200px;margin:8px 0 0}
#st{font-size:13px;color:var(--m);margin-left:8px}
</style><main>
<h1>&#9889; Bluetti AC2 Kontrol</h1>

<div class="card"><h2>Cihaz tara</h2>
<div class="row" style="align-items:center"><button id="sb" onclick="scan()" style="flex:none">Tara</button>
<label style="margin:0;flex:none">Sure (sn)</label><input id="stime" type="number" min="1" max="30" value="5" style="max-width:80px">
<span id="ss" style="color:var(--m);font-size:13px"></span></div>
<div id="devs"></div></div>

<div class="card"><h2>Baglanti</h2>
<div class="row"><div><label>MAC / UUID</label><input id="mac" placeholder="MAC veya macOS UUID"></div>
<div><label>Cihaz tipi</label><input id="type" value="AC2P"></div></div>
<label><input type="checkbox" id="enc" style="width:auto" checked> Sifreleme (-e)</label>
<div style="margin-top:10px"><button id="rb" onclick="readAll()">Oku</button>
<label style="display:inline-block;margin-left:10px"><input type="checkbox" id="auto" style="width:auto" onchange="autoToggle()"> 15 sn'de bir</label>
<span id="st"></span></div></div>

<div class="card"><h2>Durum</h2><div class="grid" id="stats"></div>
<details><summary>Tum alanlar</summary><table id="all"></table></details></div>

<div class="card"><h2>Kontroller</h2><div id="ctl"></div>
<div style="margin-top:12px"><label>Ozel alan yaz (sayi = -v, metin = select/enum)</label>
<div class="row"><input id="cf" placeholder="alan, orn: ctrl_led_mode"><input id="cv" placeholder="deger, orn: 1 veya low">
<button class="sec" onclick="custom()" style="flex:none">Yaz</button></div></div></div>

<div class="card"><h2>Cikti</h2><pre id="raw">-</pre></div>
</main>
<script>
const CONTROLS=[["ctrl_ac","AC cikisi"],["ctrl_dc","DC cikisi"]];
const STATS=[["total_battery_percent","Batarya"],["ac_input_power","AC giris"],["dc_input_power","DC giris"],["ac_output_power","AC cikis"],["dc_output_power","DC cikis"]];
let last={},units={},DEVS=[],timer=null,busy=false;
const $=id=>document.getElementById(id);
const base=()=>({mac:$("mac").value.trim(),type:$("type").value.trim(),encryption:$("enc").checked});
if(localStorage.mac)$("mac").value=localStorage.mac;
if(localStorage.type)$("type").value=localStorage.type;
if(localStorage.enc!==undefined)$("enc").checked=localStorage.enc==="1";
async function post(p,b){const r=await fetch(p,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(b)});return r.json()}
const val=k=>last[k]===undefined?"-":last[k]+(units[k]||"");
function render(){
 $("stats").innerHTML=STATS.map(([k,l])=>`<div class="stat"><span>${l}</span><b>${val(k)}</b></div>`).join("");
 $("ctl").innerHTML=CONTROLS.map(([k,l])=>`<div class="sw"><div>${l}<br><small style="color:var(--m)">${k}</small></div><div class="tg ${last[k]===true?"on":""}" onclick="toggle('${k}',${last[k]!==true})"></div></div>`).join("");
 $("all").innerHTML=Object.keys(last).map(k=>`<tr><td>${k}</td><td>${val(k)}</td></tr>`).join("");
}
async function readAll(){
 if(busy)return;busy=true;$("rb").disabled=true;$("st").textContent="okunuyor...";
 localStorage.mac=$("mac").value;localStorage.type=$("type").value;localStorage.enc=$("enc").checked?"1":"0";
 try{const r=await post("/api/read",base());
  if(r.ok){last=r.fields;units=r.units||{};render();$("st").textContent="guncellendi "+new Date().toLocaleTimeString()}
  else{$("st").textContent="hata";$("raw").textContent=r.raw}}
 catch(e){$("st").textContent="sunucu hatasi"}
 busy=false;$("rb").disabled=false}
async function write(field,extra,optimistic){
 busy=true;$("st").textContent="yaziliyor...";
 if(optimistic){last[field]=extra.state;render()}
 try{const r=await post("/api/write",{...base(),field,...extra});$("raw").textContent=r.raw;
  busy=false;await readAll()}
 catch(e){busy=false;$("st").textContent="yazma hatasi"}}
const toggle=(f,s)=>write(f,{state:s},true);
const custom=()=>{const f=$("cf").value.trim(),v=$("cv").value.trim();f&&v!==""&&write(f,{value:v})};
async function scan(){
 $("sb").disabled=true;$("ss").textContent="taraniyor...";$("devs").innerHTML="";
 try{const r=await post("/api/scan",{time:$("stime").value||5});
  if(!r.ok){$("ss").textContent="hata";$("raw").textContent=r.raw}
  else{DEVS=r.devices;$("ss").textContent=DEVS.length?DEVS.length+" cihaz bulundu":"cihaz bulunamadi";
   $("devs").innerHTML=DEVS.map((d,i)=>`<div class="dev" onclick="pick(${i})"><b>${d.name} ${d.type?"("+d.type+")":""}</b><span>${d.mac}</span></div>`).join("")}}
 catch(e){$("ss").textContent="tarama hatasi"}
 $("sb").disabled=false}
function pick(i){const d=DEVS[i];
 $("mac").value=d.mac;$("type").value=d.type||d.name.replace(/\d{10,}$/,"");
 $("st").textContent="secildi: "+d.name;readAll()}
function autoToggle(){clearInterval(timer);if($("auto").checked)timer=setInterval(readAll,15000)}
render();
</script></html>"""

if __name__ == "__main__":
    print(f"http://localhost:{PORT}")
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()