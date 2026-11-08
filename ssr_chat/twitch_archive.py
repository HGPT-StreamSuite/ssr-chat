"""Synthetic replay -> explicit fixture receipt -> existing task-20/task-7 pipeline."""
from copy import deepcopy
from datetime import datetime,timezone
from pathlib import Path
import re
import threading
from uuid import UUID

from .chat_ingest import ChatIngestPipeline,digest,file_sha,local
from .modular_api import RetrievalInputError
from .modular_retrieval import PipelineError,load_object
from .twitch_replay import token,validate_fixture_notification


class TwitchArchiveGateway:
    def __init__(self,config,pipeline,*,clock=None,watched=None):
        fields={"schema_version","mode","source_binding","storage_binding","storage_pipeline","storage_state_dir","fixture_receipts"}
        if not isinstance(config,dict) or set(config)!=fields or config["schema_version"]!="1.0.1" or config["mode"]!="synthetic_twitch_archive_only":
            raise PipelineError("twitch_archive_configuration_invalid")
        binding=config["source_binding"]
        if not isinstance(binding,dict) or set(binding)!={"subscription_id","broadcaster_user_id","reader_user_id","chatter_user_id"} or not token(binding["subscription_id"]):
            raise PipelineError("twitch_archive_source_binding_invalid")
        for field in ("broadcaster_user_id","reader_user_id","chatter_user_id"):
            if not isinstance(binding[field],str) or re.fullmatch(r"[1-9][0-9]{0,19}",binding[field]) is None:
                raise PipelineError("twitch_archive_source_binding_invalid")
        if binding["chatter_user_id"]==binding["reader_user_id"]:
            raise PipelineError("twitch_archive_self_binding_invalid")
        expected_source="synthetic-twitch-"+binding["broadcaster_user_id"]+"-"+binding["chatter_user_id"]
        mapped=config["storage_binding"]
        if (not isinstance(mapped,dict) or mapped!=pipeline.binding or mapped.get("source_id")!=expected_source
                or mapped.get("scope",{}).get("platform")!="synthetic-twitch" or mapped["scope"].get("channel_id")!=binding["broadcaster_user_id"]):
            raise PipelineError("twitch_archive_storage_mapping_mismatch")
        receipts=config["fixture_receipts"]
        if not isinstance(receipts,dict) or not 1<=len(receipts)<=500:raise PipelineError("twitch_archive_receipts_invalid")
        for identifier,receipt in receipts.items():
            try:
                if str(UUID(identifier))!=identifier or not isinstance(receipt,dict) or set(receipt)!={"event_time","received_at","semantics_sha256"}:raise ValueError()
                if not isinstance(receipt["semantics_sha256"],str) or re.fullmatch(r"[0-9a-f]{64}",receipt["semantics_sha256"]) is None:raise ValueError()
                event,received=(datetime.fromisoformat(receipt[k]) for k in ("event_time","received_at"))
                if event.tzinfo is None or received.tzinfo is None or received<event:raise ValueError()
            except (ValueError,TypeError,AttributeError):raise PipelineError("twitch_archive_receipts_invalid") from None
        self.binding,self.mapped,self.receipts=deepcopy(binding),deepcopy(mapped),deepcopy(receipts)
        self.pipeline=pipeline;self._clock=clock or (lambda:datetime.now(timezone.utc));self._session=None;self._lock=threading.RLock()
        self.watched=dict(watched or {})

    @classmethod
    def read(cls,path):
        selected=Path(path).resolve();config=load_object(selected.read_bytes())
        if not {"storage_pipeline","storage_state_dir"}<=set(config):raise PipelineError("twitch_archive_configuration_invalid")
        pipeline=ChatIngestPipeline(local(config["storage_pipeline"],selected.parent),local(config["storage_state_dir"],selected.parent))
        return cls(config,pipeline,watched={str(selected):file_sha(selected)})

    @property
    def profile(self):
        return {"configured":True,"mode":"synthetic_twitch_archive_only","transport_authenticated":False,"live_connection_enabled":False,
                "storage":"isolated_synthetic_task20_task7","event_time_basis":"host_authored_synthetic_receipt","clock_receipt_required":True,
                "deduplication":"task7_persistent_event_revision","query_enabled":False,"model_enabled":False,"public_output_enabled":False}

    def archive(self,payload):
        if not isinstance(payload,dict) or not isinstance(payload.get("operation"),str) or payload["operation"] not in {"connect","disconnect","notification"}:
            raise RetrievalInputError("synthetic_archive_operation_required")
        operation=payload["operation"]
        fields={"operation","session_id","synthetic"}|({"envelope"} if operation=="notification" else set())
        if set(payload)!=fields or not token(payload.get("session_id")):raise RetrievalInputError("synthetic_archive_fields_invalid")
        result={"status":"held","mode":"synthetic_twitch_archive_only","transport_authenticated":False,"query_invoked":False,"model_invoked":False,
                "answer":None,"public_output":"not_attempted","send_result":{"status":"not_requested","attempted":False,"public_reply":False}}
        def held(code):return {**result,"reason_code":code}
        if payload["synthetic"] is not True:return held("synthetic_source_required")
        with self._lock:
            if any(not Path(p).is_file() or file_sha(p)!=sha for p,sha in self.watched.items()):return held("twitch_archive_configuration_changed")
            session=payload["session_id"]
            if operation=="connect":
                if self._session not in (None,session):return held("session_already_connected")
                self._session=session;return {**result,"status":"connected"}
            if self._session!=session:return held("session_not_connected")
            if operation=="disconnect":self._session=None;return {**result,"status":"disconnected"}
            try:normalized,reason=validate_fixture_notification(payload["envelope"],session,binding=self.binding,clock=self._clock)
            except (ValueError,UnicodeError):return held("synthetic_notification_invalid")
            if reason:return held(reason)
            identifier=normalized["message_id"];receipt=self.receipts.get(identifier)
            if receipt is None:return held("synthetic_clock_receipt_required")
            semantics={"broadcaster_user_id":self.binding["broadcaster_user_id"],"chatter_user_id":normalized["author_id"],"message_id":identifier,"text":normalized["text"]}
            if digest(semantics)!=receipt["semantics_sha256"]:return held("synthetic_receipt_content_mismatch")
            request_id="tw-archive-"+digest([self.mapped["source_id"],identifier])
            result.update(request_id=request_id,event_time_basis="host_authored_synthetic_receipt",receipt_sha256=digest(receipt))
            record_id="synthetic-twitch-"+identifier
            reference="synthetic://twitch-replay/"+self.binding["broadcaster_user_id"]+"/"+identifier+"/1"
            record={"schema_version":"1.0.1","source_id":self.mapped["source_id"],"source_event_id":identifier,"record_id":record_id,"revision":1,
                    "event_time":receipt["event_time"],"received_at":receipt["received_at"],"actor_account_id":self.mapped["actor_account_id"],"text":normalized["text"],
                    "record_kind":"raw_event","quality_flags":["synthetic"],"scope":deepcopy(self.mapped["scope"]),
                    "access_scope":deepcopy(self.pipeline.context["sources"][self.mapped["source_id"]]["access_scope"]),
                    "source_refs":[{"source_id":self.mapped["source_id"],"record_id":record_id,"revision":1,"locator":reference,"transformation_version":None}]}
            envelope={"synthetic":True,"source":self.mapped["source_id"],"raw_reference":reference,"record":record}
            try:
                stored=self.pipeline.ingest({"request_id":request_id,"synthetic":True,"events":[envelope]})
                if (not isinstance(stored,dict) or stored.get("request_id")!=request_id or stored.get("mode")!="synthetic_chat_ingest_only"
                        or stored.get("public_output")!="not_attempted" or stored.get("model_invoked") is not False
                        or not isinstance(stored.get("items"),list) or len(stored["items"])!=1):raise ValueError()
                item=stored["items"][0]
                if (not isinstance(item,dict) or item.get("status") not in {"stored","duplicate","held","uncertain"}
                        or item.get("index")!=1 or item.get("stage") not in {"input","normalizer","store"}
                        or item.get("storage_outcome") not in {"not_attempted","unknown","confirmed"}):raise ValueError()
                if "record_sha256" in item and (not isinstance(item["record_sha256"],str) or re.fullmatch(r"[0-9a-f]{64}",item["record_sha256"]) is None):raise ValueError()
                for name in ("normalizer_status","store_status"):
                    if name in item and item[name] not in {"ok","skipped","denied","error","missing_input","not_configured","no_match"}:raise ValueError()
                for name in ("normalizer_exit","store_exit"):
                    if name in item and (type(item[name]) is not int or item[name] not in {0,2,3}):raise ValueError()
                if item["status"] in {"stored","duplicate"} and (item["stage"]!="store" or item["storage_outcome"]!="confirmed"):raise ValueError()
                # Do not return normalized text, account IDs, raw envelopes or backend additions.
                summary={key:item[key] for key in ("index","status","stage","storage_outcome","record_sha256","normalizer_status","normalizer_exit","store_status","store_exit") if key in item}
                reason=item.get("reason_code")
                if reason is not None and (not isinstance(reason,str) or re.fullmatch(r"[a-z][a-z0-9_]{0,99}",reason) is None):raise ValueError()
                summary["reason_code"]=reason
                result.update(status=item["status"],storage=summary)
                if item["status"] in {"held","uncertain"}:result["reason_code"]=reason or "archive_storage_not_confirmed"
                return result
            except Exception:
                result.update(status="uncertain",reason_code="archive_storage_outcome_uncertain")
                return result
