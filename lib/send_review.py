"""Offline-verifiable review manifests, fixed-policy transport, and durable ledger.

Artifact policy: open with O_NOFOLLOW where available, fstat the descriptor as a
regular file, enforce a size ceiling, and hash/use the bytes read from that fd.
This avoids symlink following and pathname check/open races on supported hosts.
"""
from __future__ import annotations
import base64, errno, fcntl, hashlib, json, os, re, stat, urllib.error, urllib.parse, urllib.request, uuid
from datetime import datetime, timezone
from email.message import EmailMessage
from email.policy import SMTP
from pathlib import Path

SENDER = 'vlsi_delivery@siliconpatterns.com'
ACTUAL = 'yaswanth.ampolu@siliconpatterns.com'
SCHEMA = 1
MAX_ARTIFACT = 5 * 1024 * 1024
STATES = {'PREPARED','SENDING','ACCEPTED','FAILED','UNKNOWN'}
class SendError(ValueError): pass

def norm(value): return str(value or '').strip().casefold()
def digest(data): return hashlib.sha256(data).hexdigest()
def canonical(value): return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()
def utc(): return datetime.now(timezone.utc).isoformat()
def validation_hash(validation): return digest(canonical(validation))
def _email(s): return isinstance(s,str) and bool(re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',s.strip()))
def _read_artifact(root, name):
 if not isinstance(name,str) or not name or Path(name).is_absolute(): raise SendError('invalid artifact path')
 p=root/name
 try:
  if not p.resolve(strict=True).is_relative_to(root): raise SendError('artifact path escapes preview directory')
  fd=os.open(p,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
  try:
   st=os.fstat(fd)
   if not stat.S_ISREG(st.st_mode): raise SendError('artifact is not a regular file')
   if st.st_size>MAX_ARTIFACT: raise SendError('artifact exceeds size limit')
   chunks=[]; total=0
   while True:
    b=os.read(fd,min(65536,MAX_ARTIFACT+1-total))
    if not b: break
    chunks.append(b);total+=len(b)
    if total>MAX_ARTIFACT: raise SendError('artifact exceeds size limit')
   data=b''.join(chunks);data.decode('utf-8')
   return data
  finally: os.close(fd)
 except SendError: raise
 except (OSError,UnicodeError,ValueError): raise SendError('artifact cannot be safely read') from None

def approval_material(m,r,htmlb,textb):
 return {'schemaVersion':SCHEMA,'templateVersion':m['templateVersion'],'meeting':m['meeting'],'intendedRecipient':{'userId':r['userId'],'name':r['name'],'email':norm(r['intendedRecipient'])},'actualRecipient':ACTUAL,'sender':SENDER,'editorialMode':m['editorialMode'],'deliveryMode':'TEST_REVIEW','subject':r['subject'],'htmlSha256':digest(htmlb),'textSha256':digest(textb),'validationBinding':m['validationSha256']}
def approval_hash(m,r,htmlb,textb): return digest(canonical(approval_material(m,r,htmlb,textb)))
def inspect_manifest(path,person):
 try:
  mp=Path(path).resolve(strict=True); root=mp.parent
  raw=mp.read_bytes(); m=json.loads(raw.decode('utf-8'))
 except Exception: raise SendError('preview manifest unreadable') from None
 if not isinstance(m,dict) or m.get('schemaVersion')!=SCHEMA: raise SendError('unsupported preview manifest version')
 run=m.get('runId'); meeting=m.get('meeting')
 if not isinstance(run,str) or not re.fullmatch(r'[0-9]{8}T[0-9]{6}Z-[a-z0-9]{6}',run): raise SendError('invalid or missing run identity')
 if not isinstance(meeting,dict) or not all(isinstance(meeting.get(k),str) and meeting[k].strip() for k in ('id','title','date')): raise SendError('missing meeting identity')
 if not isinstance(m.get('templateVersion'),str) or not m['templateVersion']: raise SendError('missing template version')
 if m.get('editorialMode') not in ('PRESERVE_PROVIDER','AGENT_AUTHORED'): raise SendError('unsupported editorial mode')
 if m.get('deliveryMode')!='TEST_REVIEW': raise SendError('unsupported delivery mode')
 v=m.get('validation')
 binding=m.get('validationBinding')
 if not isinstance(v,dict) or v.get('valid') is not True or v.get('errors')!=[] or not isinstance(binding,dict) or binding.get('result')!=v or m.get('validationSha256')!=digest(canonical(binding)): raise SendError('validation binding invalid')
 rows=m.get('recipients')
 if not isinstance(rows,list): raise SendError('invalid recipients list')
 matches=[r for r in rows if isinstance(r,dict) and (norm(r.get('intendedRecipient'))==norm(person) or norm(r.get('name'))==norm(person))]
 if len(matches)!=1: raise SendError('intended person is missing or ambiguous')
 r=matches[0]
 if not isinstance(r.get('userId'),str) or not r['userId'].strip() or not isinstance(r.get('name'),str) or not _email(r.get('intendedRecipient')): raise SendError('intended recipient identity incomplete')
 if norm(r.get('permittedActualTestRecipient'))!=ACTUAL or norm(r.get('actualRecipient',ACTUAL))!=ACTUAL: raise SendError('routing policy violation')
 if r.get('cc') or r.get('bcc'): raise SendError('CC/BCC prohibited')
 subject=r.get('subject')
 if not isinstance(subject,str) or not subject.strip() or any(c in subject for c in '\r\n') or len(subject)>998: raise SendError('unsafe subject header')
 hb=_read_artifact(root,r.get('htmlFile'));tb=_read_artifact(root,r.get('textFile'))
 if r.get('htmlSha256')!=digest(hb) or r.get('textSha256')!=digest(tb): raise SendError('artifact hash mismatch')
 return {'manifest':m,'row':r,'subject':subject,'html':hb,'text':tb,'hash':approval_hash(m,r,hb,tb),'sender':SENDER,'intended':norm(r['intendedRecipient']),'actual':ACTUAL,'runId':run,'meetingId':meeting['id'],'manifestPath':str(mp)}

class Ledger:
 def __init__(self,root):
  self.root=Path(root)/'delivery-ledger';self.root.mkdir(parents=True,exist_ok=True,mode=0o700);os.chmod(self.root,0o700);self.file=self.root/'ledger.json';self.lock=self.root/'ledger.lock'
 def locked(self):
  fd=os.open(self.lock,os.O_RDWR|os.O_CREAT,0o600);os.chmod(self.lock,0o600);return os.fdopen(fd,'a+')
 def read(self):
  if not self.file.exists():return []
  try: rows=json.loads(self.file.read_text(encoding='utf8'))
  except Exception: raise SendError('delivery ledger is corrupt; sending blocked') from None
  if not isinstance(rows,list) or any(not isinstance(x,dict) or x.get('state') not in STATES or not x.get('attemptId') or not x.get('approvalHash') for x in rows): raise SendError('delivery ledger structure invalid; sending blocked')
  return rows
 def write(self,rows):
  self.root.mkdir(parents=True,exist_ok=True,mode=0o700);tmp=self.root/('.ledger-'+uuid.uuid4().hex+'.tmp')
  fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
  with os.fdopen(fd,'wb') as f:f.write(canonical(rows));f.flush();os.fsync(f.fileno())
  os.replace(tmp,self.file);os.chmod(self.file,0o600)
  d=os.open(self.root,os.O_RDONLY|getattr(os,'O_DIRECTORY',0))
  try:os.fsync(d)
  finally:os.close(d)
 def blocking(self,approval):
  with self.locked() as l:
   fcntl.flock(l,fcntl.LOCK_SH); rows=self.read()
  found=[x for x in rows if x['approvalHash']==approval]
  return found[-1] if found and found[-1]['state'] in ('ACCEPTED','UNKNOWN','SENDING','PREPARED') else None
 def history(self):
  with self.locked() as l:fcntl.flock(l,fcntl.LOCK_SH);return self.read()

def _no_redirect():
 class Reject(urllib.request.HTTPRedirectHandler):
  def redirect_request(self,*args,**kwargs): return None
 return urllib.request.build_opener(urllib.request.ProxyHandler({}),Reject())
def graph_post(url,data,headers):
 req=urllib.request.Request(url,data=data,headers=headers,method='POST')
 try:return _no_redirect().open(req,timeout=20)
 except urllib.error.HTTPError as e:
  if e.code in (301,302,303,307,308): raise SendError('HTTP redirect refused') from None
  return e

def _mime(review):
 """Build explicit UTF-8 base64 MIME parts, then Graph's outer MIME base64."""
 msg=EmailMessage(policy=SMTP);msg['MIME-Version']='1.0';msg['Subject']=review['subject'];msg['From']=SENDER;msg['To']=ACTUAL
 text=review['text'].decode('utf8') if isinstance(review['text'],bytes) else review['text']
 html=review['html'].decode('utf8') if isinstance(review['html'],bytes) else review['html']
 msg.set_content(text,subtype='plain',charset='utf-8',cte='base64')
 msg.add_alternative(html,subtype='html',charset='utf-8',cte='base64')
 raw=msg.as_bytes(policy=SMTP)
 if not raw.endswith(bytes((13, 10))) or any(len(line)>998 for line in raw.split(bytes((13, 10)))): raise SendError('invalid MIME serialization')
 return base64.b64encode(raw)
def _store(ledger,rows): ledger.write(rows)
def history(ledger): return ledger.history()

def execute(review,cfg,ledger,approved_hash,transport=None,manifest_path=None):
 if approved_hash!=review['hash']:raise SendError('approval hash mismatch')
 lock=ledger.locked()
 with lock:
  fcntl.flock(lock,fcntl.LOCK_EX);rows=ledger.read()
  prior=[x for x in rows if x['approvalHash']==review['hash']]
  if prior: raise SendError('duplicate or ambiguous prior attempt blocks sending')
  attempt={'runId':review['runId'],'attemptId':str(uuid.uuid4()),'meetingId':review['meetingId'],'intendedRecipient':review['intended'],'actualRecipient':ACTUAL,'sender':SENDER,'deliveryMode':'TEST_REVIEW','templateVersion':review['manifest']['templateVersion'],'approvalHash':review['hash'],'state':'PREPARED','createdAt':utc(),'updatedAt':utc(),'clientRequestId':str(uuid.uuid4()),'graphRequestId':None,'outcome':'prepared'}
  rows.append(attempt)
  try:
   _store(ledger,rows)
  except Exception:
   return {'state':'FAILED','outcome':'required pre-submission ledger persistence failed; no submission attempted','attemptId':attempt['attemptId'],'approvalHash':review['hash']}
  # Re-read and revalidate current snapshot before credentials or submission.
  try:
   current=inspect_manifest(manifest_path or review['manifestPath'],review['intended'])
   if current['hash']!=approved_hash: raise SendError('approved material changed; new dry run required')
   tenant=cfg.env.require('DIRECTORY_TENANT_ID');client=cfg.env.require('APPLICATION_CLIENT_ID');secret=cfg.env.require('CLIENT_SECRET')
   form=urllib.parse.urlencode({'grant_type':'client_credentials','client_id':client,'client_secret':secret,'scope':'https://graph.microsoft.com/.default'}).encode()
   if transport: tok=transport('token',tenant,form,{})
   else:
    tr=graph_post('https://login.microsoftonline.com/'+urllib.parse.quote(tenant,safe='')+'/oauth2/v2.0/token',form,{'Content-Type':'application/x-www-form-urlencoded'})
    try: tok=json.loads(tr.read())
    finally: tr.close()
   if not isinstance(tok,dict) or not tok.get('access_token'): raise SendError('token exchange failed')
   token=tok['access_token'];mime=_mime(current)
  except Exception:
   attempt.update(state='FAILED',outcome='authentication or preparation failed before submission',updatedAt=utc());_store(ledger,rows);return attempt
  attempt.update(state='SENDING',updatedAt=utc());_store(ledger,rows)
  try:
   if transport: status,headers=transport('send',token,mime,{'Content-Type':'text/plain','client-request-id':attempt['clientRequestId']})
   else:
    url='https://graph.microsoft.com/v1.0/users/'+urllib.parse.quote(SENDER,safe='')+'/sendMail'
    resp=graph_post(url,mime,{'Content-Type':'text/plain','Authorization':'Bearer '+token,'client-request-id':attempt['clientRequestId']})
    try:status,headers=resp.status,resp.headers
    finally:resp.close()
   attempt['graphRequestId']=headers.get('request-id') if headers else None
   if status==202: attempt.update(state='ACCEPTED',outcome='Graph accepted request; delivery not confirmed')
   elif status in (400,401,403,404,413):attempt.update(state='FAILED',outcome='Graph definitive rejection HTTP '+str(status))
   else:attempt.update(state='UNKNOWN',outcome='ambiguous Graph response HTTP '+str(status))
  except Exception:
   attempt.update(state='UNKNOWN',outcome='submission may have begun; acceptance unknown')
  attempt['updatedAt']=utc()
  try:_store(ledger,rows)
  except Exception:
   # Existing durable SENDING entry remains a blocker; do not re-submit.
   attempt['outcome']='Graph acceptance observed; final ledger persistence failed; do not retry'
  return attempt
