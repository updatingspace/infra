import unittest
from dns import changes
class DNSTests(unittest.TestCase):
 def test_scope_idempotency_and_ambiguous_record_guard(self):
  desired=[{'name':'grafana.updspace.com','type':'CNAME','content':'updspacedd.tplinkdns.com','proxied':True,'ttl':1}]
  self.assertEqual(changes(desired,[]),[('POST',None,desired[0])])
  record=dict(desired[0],id='known')
  self.assertEqual(changes(desired,[record,{'name':'id.updspace.com','type':'CNAME'}]),[])
  self.assertEqual(changes(desired,[dict(record,proxied=False)])[0][:2],('PATCH','known'))
  with self.assertRaises(AssertionError):changes(desired,[record,record])
  with self.assertRaises(AssertionError):changes(desired,[dict(record,type='A')])
if __name__=='__main__':unittest.main()
