import unittest
from pathlib import Path
from cloud_backend import parse_csv, quality,row_summary,Cloud
from cloud_workflow import xml_element,build_batch
from workflow import build_one

BASE=Path(__file__).resolve().parents[1]

class TestCloud(unittest.TestCase):
    def test_quality(self):
        self.assertEqual(quality(8,100,0,0),'More data')
        self.assertEqual(quality(20,65,1,0),'At risk')
        self.assertEqual(quality(20,95,0,0),'Review')
        self.assertEqual(quality(20,70,0,0),'Keep candidate')

    def test_complete_and_basic_same_fingerprint(self):
        directory=Path('/mnt/data')
        a=directory/'Füüsika kutsekoolile-Arvestus (mitmekordne test)-completestats.csv'
        b=directory/'Füüsika kutsekoolile-Arvestus (mitmekordne test)-questionstats(1).csv'
        if a.exists() and b.exists():
            aa,ha,_=parse_csv(a.read_bytes());bb,hb,_=parse_csv(b.read_bytes())
            self.assertEqual(len(aa),238);self.assertEqual(len(bb),238)
            self.assertEqual(ha,hb)

    def test_new_secret_key_not_bearer(self):
        cloud=Cloud('https://project.supabase.co','sb_secret_test')
        self.assertEqual(cloud.key,'sb_secret_test')

    def test_xml_extraction(self):
        f=next(Path('/mnt/data').glob('questions-*Default*.xml'),None)
        if f:
            el=xml_element(f.read_bytes(),'25404')
            self.assertEqual(el.findtext('./name/text'),'pildil valemid')

if __name__=='__main__':unittest.main()

class TestSafety(unittest.TestCase):
    def test_moodle_xml_numerical_revision_preserves_grading(self):
        import xml.etree.ElementTree as ET
        el=ET.fromstring('''<question type="numerical"><name><text>Test</text></name>
            <questiontext format="html"><text>Old body</text></questiontext>
            <answer fraction="100"><text>3</text><tolerance>0.1</tolerance></answer>
            <tags><tag><text>kinematics</text></tag></tags></question>''')
        def field(value):return {'rich_text':[{'plain_text':value}]}
        page={'properties':{'Sisu':field('New problem'), 'Vastus':field('4')}}
        revised=build_one(el,page)
        self.assertEqual(revised.findtext('./answer/text'),'4')
        self.assertEqual(revised.findtext('./answer/tolerance'),'0.1')
        self.assertEqual(revised.findtext('./tags/tag/text'),'kinematics')
        self.assertEqual(el.findtext('./answer/text'),'3')

    def test_reject_media_question_rewrite(self):
        import xml.etree.ElementTree as ET
        el=ET.fromstring('''<question type="multichoice"><questiontext><text>&lt;img src="@@PLUGINFILE@@/x.png"&gt;</text></questiontext></question>''')
        page={'properties':{'Sisu':{'rich_text':[{'plain_text':'Change'}]}}}
        with self.assertRaises(ValueError):build_one(el,page)
