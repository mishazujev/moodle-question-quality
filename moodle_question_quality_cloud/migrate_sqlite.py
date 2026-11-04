"""One-time migration of assessments from a previous local quality.sqlite database.

Usage (on your computer, never in GitHub):
  set SUPABASE_URL=https://PROJECT.supabase.co
  set SUPABASE_SECRET_KEY=sb_secret_...
  python migrate_sqlite.py "C:\\path\\to\\quality.sqlite"

The script uploads aggregate question statistics ONLY. No student personal data.
"""
import argparse
import hashlib
import json
import os
import sqlite3
from cloud_backend import Cloud,normalize


def migrate(sqlite_path,cloud):
    db=sqlite3.connect(sqlite_path);db.row_factory=sqlite3.Row
    old=[dict(x) for x in db.execute('select * from assessments order by imported_at')]
    outcomes=[]
    for a in old:
        entries=[]
        for r in db.execute('select * from observations where assessment_id=?',(a['id'],)):
            entries.append({'item_key':r['item_key'],'position':r['q_position'],
                'name':r['name'],'question_type':r['question_type'],
                'signature':normalize(r['name'])+'|'+normalize(r['question_type']),
                'attempts':r['attempts'],'facility':r['facility'],'discrimination':r['discrimination']})
        if not entries:continue
        canonical=sorted((r['item_key'],r['signature'],r['attempts'],r['facility'],r['discrimination']) for r in entries)
        fingerprint=hashlib.sha256(json.dumps(canonical,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
        outcome=cloud.rpc('import_assessment',{'p_assessment_key':a['assessment_key'],
            'p_quiz':a['quiz'],'p_cohort':a['cohort'],'p_academic_year':a['academic_year'],
            'p_stats_hash':fingerprint,'p_rows':entries})
        outcomes.append((a['assessment_key'],outcome))
    # Restore explicit question links where the old provisional signature is unique.
    remote={r['signature']:r for r in cloud.questions()}
    for q in db.execute('select * from questions'):
        target=remote.get(q['signature'])
        if target and (q['notion_page_id'] or q['moodle_bank_id']):
            cloud.update('questions','id=eq.'+target['id'],{
                'moodle_bank_id':q['moodle_bank_id'], 'notion_page_id':q['notion_page_id']})
    db.close()
    return outcomes

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('sqlite_path');args=p.parse_args()
    url=os.environ['SUPABASE_URL'];key=os.environ['SUPABASE_SECRET_KEY']
    result=migrate(args.sqlite_path,Cloud(url,key))
    for key,res in result:print(key,res)
