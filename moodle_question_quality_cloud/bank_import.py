"""Read-only Moodle bank inventory for XML and XHTML quiz exports.

Usage: python bank_import.py file1.xml file2.html --output data/bank_inventory.csv
No content is changed and no Notion data is written.
"""
from __future__ import annotations
import argparse,csv,re,hashlib,xml.etree.ElementTree as ET
from pathlib import Path
from html.parser import HTMLParser

COLUMNS=['source_file','format','moodle_export_question_id','name','question_type','category','content_sha256','full_xml_available','notes']

def from_xml(path):
    parser=ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))
    root=ET.parse(path,parser=parser).getroot()
    if root.tag!='quiz':raise ValueError(f'{path}: not a Moodle XML quiz')
    category='';last_id=''
    for el in root:
        if el.tag is ET.Comment:
            m=re.search(r'question:\s*(\d+)',el.text or '')
            if m:last_id=m.group(1)
            continue
        if el.tag!='question':continue
        if el.get('type')=='category':
            category=el.findtext('./category/text','')
            continue
        raw=ET.tostring(el,encoding='utf-8')
        yield {'source_file':path.name,'format':'xml','moodle_export_question_id':last_id,'name':el.findtext('./name/text','').strip(),
               'question_type':el.get('type',''),'category':category,'content_sha256':hashlib.sha256(raw).hexdigest(),
               'full_xml_available':'yes','notes':'Original XML available; export comment ID must be verified before treating as permanent version identity'}

def from_html(path):
    s=path.read_text(encoding='utf-8-sig')
    for m in re.finditer(r'<!--\s*question:\s*(\d+)\s+name:\s*(.*?)\s*-->',s,re.I|re.S):
        yield {'source_file':path.name,'format':'html','moodle_export_question_id':m.group(1),'name':m.group(2).strip(),
               'question_type':'','category':'','content_sha256':'','full_xml_available':'no',
               'notes':'HTML does not preserve correct-answer grades, embedded files, or full question settings; re-export as Moodle XML'}

def inventory(paths):
    rows=[]
    for p in map(Path,paths):
        if p.suffix.lower()=='.xml':rows.extend(from_xml(p))
        elif p.suffix.lower() in ('.html','.htm'):rows.extend(from_html(p))
        else:raise ValueError(f'Unsupported extension: {p}')
    seen={}
    for r in rows:
        k=(r['source_file'],r['moodle_export_question_id'])
        if k in seen:raise ValueError(f'Duplicate export ID in file: {k}')
        seen[k]=1
    return rows

def save(rows,dest):
    dest=Path(dest);dest.parent.mkdir(parents=True,exist_ok=True)
    with dest.open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=COLUMNS);w.writeheader();w.writerows(rows)

if __name__=='__main__':
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('files',nargs='+');ap.add_argument('--output',default='data/bank_inventory.csv')
    a=ap.parse_args();rows=inventory(a.files);save(rows,a.output)
    print(f'Inventoried {len(rows)} questions, {sum(r["full_xml_available"]=="yes" for r in rows)} with full XML; saved {a.output}')
