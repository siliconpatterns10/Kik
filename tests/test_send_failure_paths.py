"""Offline failure-path and process synchronization tests for Phase 3."""
import json, tempfile, time, subprocess, sys, os
from pathlib import Path
from types import SimpleNamespace
from lib import send_review as sr
from test_send_review import make_manifest

class Env:
 def require(self,n):return {'DIRECTORY_TENANT_ID':'t','APPLICATION_CLIENT_ID':'c','CLIENT_SECRET':'s'}[n]
def test_graph_response_classes_and_ledger_states():
 for code,state in [(202,'ACCEPTED'),(400,'FAILED'),(401,'FAILED'),(403,'FAILED'),(429,'UNKNOWN'),(500,'UNKNOWN')]:
  with tempfile.TemporaryDirectory() as d:
   f=make_manifest(d);r=sr.inspect_manifest(f,'Fakhrul');l=sr.Ledger(d)
   def tx(kind,*args): return {'access_token':'t'} if kind=='token' else (code,{'request-id':'rid'})
   out=sr.execute(r,SimpleNamespace(env=Env()),l,r['hash'],transport=tx,manifest_path=str(f))
   assert out['state']==state and l.history()[-1]['state']==state
   assert out['graphRequestId']=='rid'
def test_timeout_unknown_blocks_retry():
 with tempfile.TemporaryDirectory() as d:
  f=make_manifest(d);r=sr.inspect_manifest(f,'Fakhrul');l=sr.Ledger(d);calls=[]
  def tx(kind,*a):
   calls.append(kind)
   if kind=='token':return {'access_token':'x'}
   raise TimeoutError()
  out=sr.execute(r,SimpleNamespace(env=Env()),l,r['hash'],transport=tx,manifest_path=str(f))
  assert out['state']=='UNKNOWN' and calls==['token','send'] and l.blocking(r['hash'])
def test_subprocess_locking_and_cross_run_duplicate():
 with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as d2:
  f=make_manifest(d);f2=make_manifest(d2);r=sr.inspect_manifest(f,'Fakhrul');r2=sr.inspect_manifest(f2,'Fakhrul');assert r['hash']==r2['hash']
  worker="""import sys,time,json\nfrom types import SimpleNamespace\nfrom lib import send_review as s\nr=s.inspect_manifest(sys.argv[1],'Fakhrul');l=s.Ledger(sys.argv[2])\nclass E:\n def require(self,n):return 'mock'\ndef tx(k,*a):\n if k=='token':return {'access_token':'mock'}\n open(sys.argv[3],'a').write('send\\n');time.sleep(.25);return 202,{}\ntry: print(s.execute(r,SimpleNamespace(env=E()),l,r['hash'],transport=tx,manifest_path=sys.argv[1])['state'])\nexcept Exception: print('BLOCKED')\n"""
  counter=Path(d)/'sends';env=dict(os.environ,PYTHONPATH='/root/Kik')
  a=subprocess.Popen([sys.executable,'-c',worker,str(f),d,str(counter)],env=env,stdout=subprocess.PIPE,text=True)
  time.sleep(.05)
  b=subprocess.Popen([sys.executable,'-c',worker,str(f2),d,str(counter)],env=env,stdout=subprocess.PIPE,text=True)
  assert a.communicate(timeout=5)[0].strip() in ('ACCEPTED','BLOCKED')
  assert b.communicate(timeout=5)[0].strip() in ('ACCEPTED','BLOCKED')
  assert counter.read_text().splitlines()==['send'] and len(sr.Ledger(d).history())==1
def test_redirect_refusal_without_network():
 import urllib.error
 class FakeOpener:
  def open(self,*a,**k): raise urllib.error.HTTPError('https://safe.invalid',302,'redirect',{'Location':'https://evil.invalid'},None)
 old=sr._no_redirect;sr._no_redirect=lambda:FakeOpener()
 try:
  try:sr.graph_post('https://safe.invalid',b'x',{'Authorization':'Bearer mock'})
  except sr.SendError as e: assert 'redirect refused' in str(e)
  else:raise AssertionError('redirect followed')
 finally:sr._no_redirect=old
