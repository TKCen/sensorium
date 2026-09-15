#!/usr/bin/env python3
"""Explicit detached study administration; never touches canonical JSONL."""
from __future__ import annotations
import argparse,json,sys
from datetime import datetime,timedelta,timezone
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from agent_sensorium.prospective_evidence import ProspectiveEvidenceCapture,STUDY_NAME

def read(path):
    try:return json.loads(path.read_text())
    except (OSError,json.JSONDecodeError):return {}
def atomic(path,value):
    from agent_sensorium.prospective_evidence import _atomic_json
    _atomic_json(path,value)
def now():return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00","Z")
def main():
 p=argparse.ArgumentParser();p.add_argument("operation",choices=("status","activate","revoke","freeze","purge","maintenance"));p.add_argument("--profile-root",required=True);p.add_argument("--config",required=True);p.add_argument("--source-receipt",default="");p.add_argument("--start-at",default="");a=p.parse_args(); path=Path(a.config); raw=read(path); control=dict(raw.get("prospective_evidence_capture") or {})
 if a.operation=="activate":
  start=a.start_at or now()
  try:
   s=datetime.fromisoformat(start.replace("Z","+00:00")); assert s.tzinfo
  except (ValueError,AssertionError):print(json.dumps({"error":"invalid_start_at"}));return 2
  control={"enabled":True,"start_at":start,"expires_at":(s.astimezone(timezone.utc)+timedelta(days=14)).isoformat(timespec="seconds").replace("+00:00","Z")}; cap=ProspectiveEvidenceCapture(a.profile_root,control)
  if not cap.activate():print(json.dumps({"error":"activation_nonrenewable"}));return 2
  raw["prospective_evidence_capture"]=control;atomic(path,raw);print(json.dumps({"activated":True,**control},sort_keys=True));return 0
 cap=ProspectiveEvidenceCapture(a.profile_root,control)
 if a.operation=="status":print(json.dumps({"study":STUDY_NAME,"study_exists":cap.study_root.exists(),"active":cap._enabled_window(datetime.now(timezone.utc))},sort_keys=True));return 0
 if a.operation=="revoke":
  if not a.source_receipt:raise ValueError("--source-receipt required")
  print(json.dumps(cap.revoke(a.source_receipt),sort_keys=True));return 0
 if a.operation=="freeze":print(json.dumps(cap.closeout(),sort_keys=True));return 0
 if a.operation=="maintenance":print(json.dumps(cap.maintenance(),sort_keys=True));return 0
 # Purge does not remove the durable consumed latch; config disable cannot renew.
 import shutil;shutil.rmtree(cap.study_root,ignore_errors=True);raw["prospective_evidence_capture"]={"enabled":False,"start_at":"","expires_at":""};atomic(path,raw);print(json.dumps({"purged":True},sort_keys=True));return 0
if __name__=="__main__":raise SystemExit(main())
