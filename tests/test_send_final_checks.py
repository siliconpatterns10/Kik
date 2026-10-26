"""Focused offline persistence, malformed-response, redirect, and CLI checks."""
import contextlib, io, json, tempfile, threading, subprocess, sys, os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from lib import send_review as sr
from test_send_review import make_manifest
from test_send_failure_paths import Env

def test_ledger_write_failure_before_submit_blocks_transport():
 with tempfile.TemporaryDirectory() as d:
  f=make_manifest(d);review=sr.inspect_manifest(f,'Fakhrul');ledger=sr.Ledger(d);calls=[]
  ledger.write=lambda rows: (_ for _ in ()).throw(OSError('disk full'))
  out=sr.execute(review,SimpleNamespace(env=Env()),ledger,review['hash'],transport=lambda *a:calls.append(a),manifest_path=str(f))
  assert out['state']=='FAILED' and 'no submission' in out['outcome'] and calls==[]

def test_acceptance_then_final_ledger_failure_reports_acceptance_and_blocks():
 with tempfile.TemporaryDirectory() as d:
  f=make_manifest(d);review=sr.inspect_manifest(f,'Fakhrul');ledger=sr.Ledger(d);orig=ledger.write;calls=[];n=[0]
  def flaky(rows):
   n[0]+=1
   if n[0]==3: raise OSError('disk full')
   orig(rows)
  ledger.write=flaky
  def transport(kind,*a):
   calls.append(kind)
   return {'access_token':'mock'} if kind=='token' else (202,{'request-id':'id'})
  out=sr.execute(review,SimpleNamespace(env=Env()),ledger,review['hash'],transport=transport,manifest_path=str(f))
  assert out['state']=='ACCEPTED' and 'acceptance observed' in out['outcome'].lower() and 'persistence failed' in out['outcome'].lower()
  assert calls==['token','send'] and ledger.blocking(review['hash'])['state']=='SENDING'

def test_interrupted_sending_restart_blocks():
 with tempfile.TemporaryDirectory() as d:
  l=sr.Ledger(d);l.write([{'attemptId':'prior','approvalHash':'same','state':'SENDING'}])
  assert sr.Ledger(d).blocking('same')['attemptId']=='prior'

def test_malformed_token_and_unexpected_graph_responses():
 for token_response,state,calls_expected in [({'error':'denied'},'FAILED',['token']),('invalid-json','FAILED',['token'])]:
  with tempfile.TemporaryDirectory() as d:
   f=make_manifest(d);r=sr.inspect_manifest(f,'Fakhrul');l=sr.Ledger(d);calls=[]
   def tx(kind,*a):
    calls.append(kind)
    if kind=='token':
     if token_response=='invalid-json': raise json.JSONDecodeError('bad','',0)
     return token_response
   out=sr.execute(r,SimpleNamespace(env=Env()),l,r['hash'],transport=tx,manifest_path=str(f))
   assert out['state']==state and calls==calls_expected and l.history()[-1]['state']==state
 with tempfile.TemporaryDirectory() as d:
  f=make_manifest(d);r=sr.inspect_manifest(f,'Fakhrul');l=sr.Ledger(d);calls=[]
  def unexpected(kind,*a):
   calls.append(kind);return {'access_token':'x'} if kind=='token' else (None,{})
  out=sr.execute(r,SimpleNamespace(env=Env()),l,r['hash'],transport=unexpected,manifest_path=str(f))
  assert out['state']=='UNKNOWN' and calls==['token','send'] and l.blocking(r['hash'])

def test_redirect_real_urllib_helper_local_only():
 seen=[]
 class Handler(BaseHTTPRequestHandler):
  def do_POST(self):
   if self.path=='/start':
    self.send_response(307);self.send_header('Location','/dest');self.end_headers()
   else:
    seen.append((self.path,self.headers.get('Authorization'),self.rfile.read(int(self.headers.get('Content-Length','0')))))
    self.send_response(202);self.end_headers()
  def log_message(self,*a):pass
 srv=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=srv.serve_forever,daemon=True);thread.start()
 try:
  url='http://127.0.0.1:%s/start'%srv.server_port
  try:sr.graph_post(url,b'body-secret',{'Authorization':'Bearer secret'})
  except sr.SendError:pass
  else:raise AssertionError('redirect followed')
  assert seen==[]
 finally:srv.shutdown();srv.server_close();thread.join(timeout=2)

def test_cli_history_conflict_and_dryrun_offline():
 with tempfile.TemporaryDirectory() as d:
  cfg=Path(d)/'c.toml';cfg.write_text('env_file="/missing/no-read"\ntimezone="UTC"\nruns_directory='+json.dumps(d)+'\n')
  env=dict(os.environ,PYTHONPATH='/root/Kik')
  base=[sys.executable,'/root/Kik/kik.py','--config',str(cfg)]
  h=subprocess.run(base+['send-review','--history'],capture_output=True,text=True,env=env,timeout=5)
  assert h.returncode==0 and 'history' in h.stdout, (h.returncode,h.stdout,h.stderr)
  c=subprocess.run(base+['send-review','--history','--dry-run'],capture_output=True,text=True,env=env,timeout=5)
  assert c.returncode!=0
  assert '--acknowledge-duplicate-risk' not in subprocess.run(base+['send-review','--help'],capture_output=True,text=True,env=env).stdout
