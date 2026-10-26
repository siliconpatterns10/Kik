"""Load persisted transcript boundaries and reject source drift."""
from __future__ import annotations
import hashlib, json
from pathlib import Path
from . import transcripts

def source_hash(bundle):
 t=bundle.get('transcript',{})
 if t.get('segments'):
  ordered=sorted(t['segments'],key=lambda x: int(x.get('sequence') or 0))
  value=[{"sequence":int(x['sequence']) if x.get('sequence') is not None else None,"segmentId":x.get('id'),"text":str(x.get('text') or ''),"speaker":x.get('speakerName'),"startSeconds":float(x['startSeconds']) if x.get('startSeconds') is not None else None,"endSeconds":float(x['endSeconds']) if x.get('endSeconds') is not None else None} for x in ordered]
  payload=json.dumps(value,ensure_ascii=False,separators=(',',':'),sort_keys=True).encode()
 else: payload=str(t.get('fullText') or '').encode()
 return hashlib.sha256(payload).hexdigest()
def plan_from_manifest(bundle,manifest):
 expected=manifest.get('source',{}).get('transcriptContentSha256') or manifest.get('source',{}).get('contentSha256')
 if expected!=source_hash(bundle): raise transcripts.BatchError('saved batch plan source hash does not match current bundle; refusing changed source')
 path=manifest.get('source',{}).get('bundlePath'); digest=manifest.get('source',{}).get('bundleSha256')
 if path and digest and hashlib.sha256(Path(path).read_bytes()).hexdigest()!=digest: raise transcripts.BatchError('saved batch plan bundle hash does not match original bundle; refusing changed source')
 max_chars=manifest.get('batching',{}).get('maxChars')
 plan=transcripts.plan_from_bundle(bundle,max_chars=max_chars)
 rows=manifest.get('batches',[])
 if [x.get('batchId') for x in rows] != [x.batch_id for x in plan.batches]: raise transcripts.BatchError('saved batch boundaries do not match regenerated plan')
 if any('units' not in x for x in rows): raise transcripts.BatchError('saved manifest lacks boundary units')
 return plan
