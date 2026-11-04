"""Cloud question-bank inventory, verified links, safe XML generation."""
from collections import Counter,defaultdict
from pathlib import Path
import hashlib,io,json,re,tempfile,xml.etree.ElementTree as ET
from bank_import import inventory
from cloud_backend import normalize
from notion_bridge import normalized_type,title,type_name
from workflow import build_one,plain,status


def bank_import(cloud, uploaded, site):
    """uploaded: sequence[(filename, bytes)]. Idempotent; conflicting IDs need human resolution."""
    findings=[]
    current={r['moodle_id']:r for r in cloud.bank_items()}
    for filename,raw in uploaded:
        if len(raw)>25_000_000: raise ValueError(f'{filename}: 25 MB maximum')
        extension=Path(filename).suffix.lower()
        if extension not in ('.xml','.html','.htm'):raise ValueError('Unsupported question-bank export')
        with tempfile.TemporaryDirectory() as tmp:
            f=Path(tmp)/('bank'+extension);f.write_bytes(raw)
            questions=inventory([f])
        sha=hashlib.sha256(raw).hexdigest()
        source=f'original/{sha}{extension}'
        for q in questions:
            mid=q['moodle_export_question_id']
            if not mid or not mid.isdigit():continue
            existing=current.get(mid)
            changed=existing and (existing['name']!=q['name'] or
                 (existing.get('element_hash') and q['full_xml_available']=='yes' and existing['element_hash']!=q['content_sha256']))
            if changed:
                findings.append({'id':mid,'question':q['name'],'status':'conflict — retained previous version'});continue
            if existing and existing.get('xml_storage_path') and extension!='.xml':
                findings.append({'id':mid,'question':q['name'],'status':'existing XML preserved'});continue
            if extension=='.xml':
                tags=tags_for(raw,mid)
                if not existing or not existing.get('xml_storage_path'):
                    # storage upload once per source checksum
                    try:cloud.upload(source,raw)
                    except Exception as e:
                        if '409' not in str(e):raise
            else:tags=[]
            row={'moodle_id':mid,'site':site,'name':q['name'],'question_type':q['question_type'],
                 'category':q['category'],'tags':tags,
                 'xml_storage_path':source if extension=='.xml' else (existing or {}).get('xml_storage_path'),
                 'element_hash':q['content_sha256'] if extension=='.xml' else (existing or {}).get('element_hash'),
                 'notion_page_id':(existing or {}).get('notion_page_id')}
            cloud.upsert('bank_items',row,'moodle_id')
            current[mid]=row
            findings.append({'id':mid,'question':q['name'],'status':'registered'})
    return findings


def xml_element(raw,mid):
    parser=ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))
    root=ET.fromstring(raw,parser=parser)
    previous=None
    for child in root:
        if child.tag is ET.Comment:
            m=re.search(r'question:\s*(\d+)',child.text or '')
            if m:previous=m.group(1)
        elif child.tag=='question' and child.get('type')!='category' and previous==str(mid):
            return child
    raise ValueError(f'Question ID {mid} not found in source XML')


def tags_for(raw,mid):
    el=xml_element(raw,mid)
    return [(t.text or '') for t in el.findall('./tags/tag/text')]


def match_bank(bank,pages):
    by_id=defaultdict(list);by_name=defaultdict(list)
    for p in pages:
        ident=plain(p,'Moodle ID').strip()
        if ident:by_id[ident].append(p)
        by_name[normalize(title(p))].append(p)
    output=[]
    for b in bank:
        matches=by_id[b['moodle_id']]
        if len(matches)==1:target=matches[0];state='confirmed'
        elif len(matches)>1:target=None;state='ambiguous ID'
        else:
            candidates=by_name[normalize(b['name'])]
            if len(candidates)==1:target=candidates[0];state='candidate'
            elif len(candidates)>1:target=None;state='ambiguous name'
            else:target=None;state='unmatched'
        if target and b['question_type'] and type_name(target) and normalized_type(b['question_type'])!=normalized_type(type_name(target)):
            state='type conflict'
        output.append({'moodle_id':b['moodle_id'],'name':b['name'],'status':state,
                       'page_id':target['id'] if target else None,
                       'page_name':title(target) if target else ''})
    claims=Counter(r['page_id'] for r in output if r['page_id'])
    for r in output:
        if r['page_id'] and claims[r['page_id']]>1:r['status']='ambiguous claim'
    return output


def confirm_links(cloud,notion,links):
    """Only writes confirmed/approved unique matches; no auto match on rename."""
    qlist=cloud.questions(); bank={b['moodle_id']:b for b in cloud.bank_items()}
    total=0;errors=[]
    for row in links:
        if row['status'] not in ('candidate','confirmed'):continue
        try:
            b=bank[row['moodle_id']]; page_id=row['page_id']
            if row['status']=='candidate':
                notion.patch(page_id,{'Moodle ID':{'rich_text':[{'type':'text','text':{'content':row['moodle_id']}}]}})
            cloud.update('bank_items','moodle_id=eq.'+row['moodle_id'],{'notion_page_id':page_id})
            candidates=[q for q in qlist if normalize(q['name'])==normalize(b['name']) and normalized_type(q['question_type'])==normalized_type(b['question_type'])]
            if len(candidates)==1:
                q=candidates[0]
                if q.get('moodle_bank_id') and q['moodle_bank_id']!=b['moodle_id']:
                    raise ValueError('Existing local question has another Moodle ID')
                cloud.update('questions','id=eq.'+q['id'],{'moodle_bank_id':b['moodle_id'],'notion_page_id':page_id})
            total+=1
        except Exception as e:errors.append({'id':row['moodle_id'],'error':str(e)})
    return total,errors


def build_batch(cloud,pages):
    by_id=defaultdict(list)
    for p in pages:
        mid=plain(p,'Moodle ID').strip()
        if mid:by_id[mid].append(p)
    root=ET.Element('quiz');success=[];errors=[]
    for b in cloud.bank_items():
        mid=b['moodle_id']; candidates=by_id.get(mid,[])
        if not candidates:continue
        if len(candidates)>1:
            errors.append({'id':mid,'error':'Duplicate Moodle ID in Notion'});continue
        p=candidates[0]
        if status(p)!='Ready':continue
        try:
            if not b.get('xml_storage_path'):raise ValueError('Original Moodle XML is required (HTML is insufficient)')
            source=cloud.download(b['xml_storage_path'])
            question=build_one(xml_element(source,mid),p)
            category=ET.SubElement(root,'question',{'type':'category'})
            ET.SubElement(ET.SubElement(category,'category'),'text').text=b['category']
            root.append(question)
            content_hash=hashlib.sha256(ET.tostring(question,encoding='utf-8')).hexdigest()
            success.append({'moodle_id':mid,'notion_page_id':p['id'],'content_hash':content_hash,'name':b['name']})
        except Exception as e:errors.append({'id':mid,'name':b['name'],'error':str(e)})
    if not success:return None,[],errors
    return ET.tostring(root,encoding='utf-8',xml_declaration=True),success,errors


def save_batch(cloud,raw,entries):
    digest=hashlib.sha256(raw).hexdigest()
    path=f'approved/{digest}.xml'
    try:cloud.upload(path,raw)
    except Exception as e:
        if '409' not in str(e):raise
    cloud.upsert('publication_batches',{'batch_id':digest,'xml_storage_path':path},'batch_id')
    cloud.upsert('publication_items',[{'batch_id':digest,**{k:v for k,v in r.items() if k in ('moodle_id','notion_page_id','content_hash')}} for r in entries],
                 'batch_id,moodle_id')
    return digest


def confirm_published(cloud,notion,batch_id,observed_pages):
    """Confirm after teacher checks actual Moodle import. Never infer successful publishing from downloading."""
    batch=cloud.select('publication_batches','select=*&batch_id=eq.'+batch_id)
    if not batch:raise ValueError('Unknown publication batch')
    items=cloud.select('publication_items','select=*&batch_id=eq.'+batch_id)
    pages={p['id']:p for p in observed_pages}
    for item in items:
        p=pages.get(item['notion_page_id'])
        if p is None or status(p)!='Awaiting import':
            raise ValueError('Some Notion items are no longer Awaiting import: refresh and inspect')
    count=0
    for item in items:
        notion.patch(item['notion_page_id'],{'Workflow':{'select':{'name':'Published'}}})
        count+=1
    cloud.update('publication_batches','batch_id=eq.'+batch_id,{'confirmed_at':__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat()})
    return count
