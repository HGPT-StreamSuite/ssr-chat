"""Pure packet-validation tests using saved synthetic provider outputs, not E2E."""
from copy import deepcopy
from pathlib import Path
import unittest

from ssr_chat.channel_current_binding import (BindingError, PROFILE, digest, project_current,
                                            read, reader_config, validate_query)

BASE=Path(__file__).resolve().parents[2]/'components/hgpt-channel-store/tests/snapshot-evidence/20261106T005929328403Z-native'

def fixture(case='S01-history', layer='raw'):
    packet=read(BASE/(case+'.stdout.json'));request=read(BASE/(case+'.request.json'))
    grant=read(BASE/(case+'.grant.json'));context=read(BASE/'trusted-context.json')
    query={'schema_version':'1.0.1','request_id':'synthetic-current-query','text':'','intent':'channel_lookup',
           'scope':deepcopy(request['scope']),'actor_account_id':None,'person_id':None,'time_from':None,'time_to':None,
           'exclude_event_ids':[],'limit':20,'layer':layer,'conditions':{},'access_scope':deepcopy(request['access_scope'])}
    return packet,query,request,grant,context

def rehash(packet):
    body=packet['snapshot'];body['snapshot_id']=digest({k:v for k,v in body.items() if k!='snapshot_id'})

class ChannelCurrentBindingTests(unittest.TestCase):
    def test_applicability_keeps_exact_null_scope_fields(self):
        _,query,request,_,context=fixture()
        config=reader_config(request['scope'],context['principal_account_id'],'raw',[])
        self.assertEqual(set(config['applicable_scope_fields']),{'domain','platform','channel_id'})
        self.assertEqual(config['grants'][0]['scope'],query['scope'])
        self.assertEqual(len(config['grants'][0]['scope']),6)
        self.assertIsNone(config['grants'][0]['scope']['content_version'])

    def test_saved_active_packet_each_layer_preserves_record(self):
        for layer in ('raw','draft','published'):
            args=fixture(layer=layer);result=project_current(*args)
            self.assertEqual(len(result),1)
            self.assertIn(result[0],args[0]['snapshot']['records'])
            self.assertEqual(args[1]['conditions'],{})

    def test_saved_direct_and_ancestor_withdrawals_have_no_current_records(self):
        for layer in ('raw','draft','published'):
            self.assertEqual(project_current(*fixture('S07-withdrawn-history',layer)),[])

    def test_history_exact_and_withdrawn_queries_rejected(self):
        for conditions in ({'revision_mode':'all'},{'revision_mode':'exact','revision':1},
                           {'revision_view':'history'},{'include_withdrawn':True},{'revision':None}):
            args=fixture();args[1]['conditions']=conditions
            with self.assertRaisesRegex(BindingError,'current_view_only'):project_current(*args)

    def test_wrong_query_scope_or_source_rejected(self):
        args=fixture();args[1]['scope']['channel_id']='different'
        with self.assertRaises(BindingError):project_current(*args)
        args=fixture();args[1]['conditions']={'source_ids':['unknown']}
        with self.assertRaisesRegex(BindingError,'query_source_mismatch'):project_current(*args)

    def test_packet_hash_tamper_rejected(self):
        args=fixture();args[0]['snapshot']['records'][0]['text']='tampered'
        with self.assertRaisesRegex(BindingError,'snapshot_hash_invalid'):project_current(*args)

    def test_rehashed_record_tamper_rejected_by_payload_hash(self):
        args=fixture();args[0]['snapshot']['records'][0]['text']='tampered';rehash(args[0])
        with self.assertRaisesRegex(BindingError,'record_payload_mismatch'):project_current(*args)

    def test_source_policy_cannot_be_replaced_with_caller_acl(self):
        args=fixture();args[0]['snapshot']['record_metadata'][0]['source_policy_current']['policy_ref']='guessed';rehash(args[0])
        with self.assertRaisesRegex(BindingError,'source_policy_mismatch'):project_current(*args)

    def test_first_source_acl_remains_independent(self):
        args=fixture();args[0]['snapshot']['record_metadata'][0]['source_access_scope_snapshot']['read_scopes']=[];rehash(args[0])
        with self.assertRaisesRegex(BindingError,'source_acl_denied'):project_current(*args)

    def test_current_index_cannot_hide_a_latest_revision(self):
        args=fixture();args[0]['snapshot']['current'].pop();rehash(args[0])
        with self.assertRaisesRegex(BindingError,'current_index_invalid'):project_current(*args)

    def test_missing_lineage_refuses_current_projection(self):
        args=fixture();parent=args[0]['snapshot']['record_metadata'][1]['lineage_parent']
        self.assertIsNotNone(parent);parent['record_id']='missing';rehash(args[0])
        with self.assertRaisesRegex(BindingError,'lineage_missing'):project_current(*args)

    def test_rehashed_ancestor_resurrection_rejected(self):
        args=fixture('S07-withdrawn-history')
        args[0]['snapshot']['record_metadata'][1]['current_eligible']=True
        args[0]['snapshot']['current'][1]['state']='active';rehash(args[0])
        with self.assertRaisesRegex(BindingError,'current_eligibility_invalid'):project_current(*args)

    def test_withdrawal_fact_cannot_disappear(self):
        args=fixture('S07-withdrawn-history');args[0]['snapshot']['withdrawals']=[];rehash(args[0])
        with self.assertRaisesRegex(BindingError,'withdrawal_facts_mismatch'):project_current(*args)

    def test_withdrawal_provenance_required(self):
        args=fixture('S07-withdrawn-history');del args[0]['snapshot']['withdrawals'][0]['principal_account_id'];rehash(args[0])
        with self.assertRaisesRegex(BindingError,'withdrawal_provenance_invalid'):project_current(*args)

    def test_incomplete_export_and_missing_metadata_rejected(self):
        args=fixture();args[0]['snapshot']['completeness']='selected_revisions_only';rehash(args[0])
        with self.assertRaisesRegex(BindingError,'complete_history_packet_required'):project_current(*args)
        args=fixture();args[0]['snapshot']['record_metadata'].pop();rehash(args[0])
        with self.assertRaisesRegex(BindingError,'record_metadata_missing'):project_current(*args)

    def test_ai_purpose_never_inferred(self):
        args=fixture();args[0]['snapshot']['authorized_purpose']['downstream_ai_uses_granted']=True;rehash(args[0])
        with self.assertRaisesRegex(BindingError,'snapshot_purpose_mismatch'):project_current(*args)

    def test_wrong_kind_version_and_mixed_source_fail_closed(self):
        for field,value in (('snapshot_version','2.0.0'),('kind','Evidence')):
            args=fixture();args[0][field]=value
            with self.assertRaises(BindingError):project_current(*args)
        args=fixture();args[0]['snapshot']['source_bindings'][0]['source_kind']='real';rehash(args[0])
        with self.assertRaises(BindingError):project_current(*args)

    def test_coverage_and_duplicate_records_are_checked(self):
        args=fixture();args[0]['snapshot']['source_coverage']=[];rehash(args[0])
        with self.assertRaisesRegex(BindingError,'source_coverage_invalid'):project_current(*args)
        args=fixture();args[0]['snapshot']['records'].append(deepcopy(args[0]['snapshot']['records'][0]));rehash(args[0])
        with self.assertRaisesRegex(BindingError,'duplicate_record'):project_current(*args)


if __name__=='__main__':unittest.main()
