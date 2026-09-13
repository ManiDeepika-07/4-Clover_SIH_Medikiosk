import json, mimetypes, re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs
from pathlib import Path
from email.parser import BytesParser
from email.policy import default

class _Request:
    def __init__(self): self.method=''; self.headers={}; self.path=''; self._body=b''; self._json=None; self.files={}; self.form={}
    def get_json(self, silent=False):
        try:
            if self._json is None: self._json=json.loads(self._body.decode('utf-8') or '{}')
            return self._json
        except Exception:
            if silent: return None
            raise
    @property
    def files_proxy(self): return self.files
    @property
    def form_proxy(self): return self.form
    def __getattribute__(self, name):
        if name == 'files': return object.__getattribute__(self,'files')
        if name == 'form': return object.__getattribute__(self,'form')
        return object.__getattribute__(self,name)
request = _Request()

class _File:
    def __init__(self, filename, data): self.filename=filename; self.data=data
    def save(self, path): Path(path).write_bytes(self.data)

def jsonify(**kwargs): return Response(json.dumps(kwargs), 200, {'Content-Type':'application/json'})
class Response:
    def __init__(self, body='', status=200, headers=None): self.body=body.encode() if isinstance(body,str) else body; self.status=status; self.headers=headers or {}

def send_file(path):
    path=Path(path); data=path.read_bytes(); return Response(data,200,{'Content-Type':mimetypes.guess_type(str(path))[0] or 'application/octet-stream','Content-Length':str(len(data))})

class Flask:
    def __init__(self, name): self.routes=[]; self.config={}
    def route(self, path, methods=None):
        methods=methods or ['GET']
        def deco(fn): self.routes.append((path,[m.upper() for m in methods],fn)); return fn
        return deco
    def get(self,path): return self.route(path,['GET'])
    def post(self,path): return self.route(path,['POST'])
    def run(self, host='127.0.0.1', port=5000, debug=False):
        app=self
        class Handler(BaseHTTPRequestHandler):
            def _handle(self):
                global request
                req=_Request(); req.method=self.command; req.headers={k:v for k,v in self.headers.items()}; req.path=urlsplit(self.path).path
                n=int(self.headers.get('Content-Length','0') or 0); req._body=self.rfile.read(n) if n else b''
                ct=self.headers.get('Content-Type','')
                if 'application/json' in ct:
                    try:req._json=json.loads(req._body.decode() or '{}')
                    except: req._json=None
                elif ct.startswith('multipart/form-data'):
                    raw = (f'Content-Type: {ct}\r\nMIME-Version: 1.0\r\n\r\n').encode() + req._body
                    msg = BytesParser(policy=default).parsebytes(raw)
                    for part in msg.iter_parts():
                        disp = part.get('Content-Disposition','')
                        m = re.search(r'name=\"([^\"]+)\"', disp)
                        if not m: continue
                        key=m.group(1); payload=part.get_payload(decode=True) or b''
                        fn=part.get_filename()
                        if fn: req.files[key]=_File(fn,payload)
                        else: req.form[key]=payload.decode(part.get_content_charset() or 'utf-8','replace')
                elif 'application/x-www-form-urlencoded' in ct:
                    req.form={k:v[-1] for k,v in parse_qs(req._body.decode()).items()}
                request.__dict__.update(req.__dict__)
                path=req.path
                for pattern,methods,fn in app.routes:
                    if req.method not in methods: continue
                    m=re.fullmatch(re.sub(r'<int:(\w+)>',r'(?P<\1>\\d+)',re.sub(r'<(\w+)>',r'(?P<\1>[^/]+)',pattern)),path)
                    if m:
                        try:
                            kwargs={k:int(v) if v.isdigit() else v for k,v in m.groupdict().items()}
                            result=fn(**kwargs)
                            if isinstance(result,tuple): result,code=result
                            else: code=200
                            if isinstance(result,Response): resp=result; resp.status=code
                            else: resp=jsonify(**result) if isinstance(result,dict) else Response(str(result),code)
                            self.send_response(resp.status)
                            for k,v in resp.headers.items(): self.send_header(k,v)
                            if 'Content-Length' not in resp.headers: self.send_header('Content-Length',str(len(resp.body)))
                            self.end_headers(); self.wfile.write(resp.body); return
                        except Exception as e:
                            import traceback; traceback.print_exc(); self.send_response(500); self.send_header('Content-Type','application/json'); body=json.dumps({'detail':str(e)}).encode(); self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body); return
                self.send_response(404); self.send_header('Content-Type','application/json'); body=b'{"detail":"Not found"}'; self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body)
            def do_GET(self): self._handle()
            def do_POST(self): self._handle()
            def log_message(self,fmt,*args): pass
        print(f'MedHistory running at http://{host}:{port}')
        ThreadingHTTPServer((host,port),Handler).serve_forever()
