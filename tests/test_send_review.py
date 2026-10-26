import hashlib, json, os, tempfile
from pathlib import Path
from types import SimpleNamespace
from lib import send_review as sr

def make_manifest(root):
 root=Path(root); (root/'a.html').write_text('<p>Hello</p>');(root/'a.txt').write_text('Hello')
 validation={'valid':True,'errors':[],'warnings':[],'ownershipAndQualityCertified':False}
 binding={'result':validation,'source':{'bundleSha256':'abc'},'meeting':{'id':'meet-1'},'editorialMode':'PRESERVE_PROVIDER'}
 r={'name':'Fakhrul','userId':'user-1','intendedRecipient':'fakhrul@siliconpatterns.com','permittedActualTestRecipient':sr.ACTUAL,'subject':'Review','htmlFile':'a.html','textFile':'a.txt','htmlSha256':sr.digest((root/'a.html').read_bytes()),'textSha256':sr.digest((root/'a.txt').read_bytes())}
 m={'schemaVersion':1,'runId':'20261020T090000Z-abc123','templateVersion':'kik-py-1','meeting':{'id':'meet-1','title':'Daily meeting','date':'2026-10-19'},'editorialMode':'PRESERVE_PROVIDER','deliveryMode':'TEST_REVIEW','validation':validation,'validationBinding':binding,'validationSha256':sr.digest(sr.canonical(binding)),'recipients':[r]}
 f=root/'preview-manifest.json';f.write_text(json.dumps(m));return f

def test_manifest_approval_policy_and_tamper():
 with tempfile.TemporaryDirectory() as d:
  f=make_manifest(d); review=sr.inspect_manifest(f,' FAKHRUL@siliconpatterns.com ')
  assert review['actual']==sr.ACTUAL and review['runId'].endswith('abc123')
  assert review['hash']==sr.inspect_manifest(f,'Fakhrul')['hash']
  m=json.loads(f.read_text());m['validationSha256']='0'*64;f.write_text(json.dumps(m))
  try:sr.inspect_manifest(f,'Fakhrul')
  except sr.SendError:pass
  else:raise AssertionError('bad validation hash accepted')

def test_artifact_path_symlink_utf8_size_and_hash():
 with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as other:
  f=make_manifest(d);m=json.loads(f.read_text());r=m['recipients'][0]
  r['htmlFile']='../outside';f.write_text(json.dumps(m))
  try:sr.inspect_manifest(f,'Fakhrul')
  except sr.SendError:pass
  else:raise AssertionError('traversal accepted')
  (Path(d)/'escape').symlink_to(Path(other)/'outside')
  (Path(other)/'outside').write_text('secret');r['htmlFile']='escape';f.write_text(json.dumps(m))
  try:sr.inspect_manifest(f,'Fakhrul')
  except sr.SendError:pass
  else:raise AssertionError('symlink accepted')

def test_ledger_permissions_corruption_and_duplicate():
 with tempfile.TemporaryDirectory() as d:
  l=sr.Ledger(d);assert (l.root.stat().st_mode&0o777)==0o700
  row={'attemptId':'a','approvalHash':'h','state':'ACCEPTED'};l.write([row]);assert (l.file.stat().st_mode&0o777)==0o600
  assert l.blocking('h')['attemptId']=='a'
  l.file.write_text('{broken')
  try:l.history()
  except sr.SendError:pass
  else:raise AssertionError('corrupt ledger treated as empty')

def test_token_failure_is_failed_before_submission():
 with tempfile.TemporaryDirectory() as d:
  f=make_manifest(d);r=sr.inspect_manifest(f,'Fakhrul');l=sr.Ledger(d)
  cfg=SimpleNamespace(env=SimpleNamespace(require=lambda name: (_ for _ in ()).throw(sr.SendError('missing config'))))
  out=sr.execute(r,cfg,l,r['hash'],transport=lambda *a: (_ for _ in ()).throw(AssertionError('network')))
  assert out['state']=='FAILED'
  assert l.history()[-1]['state']=='FAILED'

def test_mocked_acceptance_and_duplicate_block():
 with tempfile.TemporaryDirectory() as d:
  f=make_manifest(d);r=sr.inspect_manifest(f,'Fakhrul');l=sr.Ledger(d)
  class Env:
   def require(self,n):return {'DIRECTORY_TENANT_ID':'tenant','APPLICATION_CLIENT_ID':'client','CLIENT_SECRET':'secret'}[n]
  calls=[]
  def transport(kind,*args):
   calls.append(kind)
   if kind=='token':return {'access_token':'mock-token'}
   return 202,{'request-id':'mock-id'}
  out=sr.execute(r,SimpleNamespace(env=Env()),l,r['hash'],transport=transport,manifest_path=str(f))
  assert out['state']=='ACCEPTED' and calls==['token','send']
  try:sr.execute(r,SimpleNamespace(env=Env()),l,r['hash'],transport=transport,manifest_path=str(f))
  except sr.SendError:pass
  else:raise AssertionError('duplicate executed')
  assert calls==['token','send']

def test_changes_after_inspection_block_execution():
 with tempfile.TemporaryDirectory() as d:
  f=make_manifest(d);r=sr.inspect_manifest(f,'Fakhrul');(Path(d)/'a.html').write_text('changed')
  class Env:
   def require(self,n):return 'x'
  out=sr.execute(r,SimpleNamespace(env=Env()),sr.Ledger(d),r['hash'],transport=lambda *a: (_ for _ in ()).throw(AssertionError('network')),manifest_path=str(f))
  assert out['state']=='FAILED'
  assert sr.Ledger(d).history()[-1]['state']=='FAILED'
