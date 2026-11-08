// Program tests with an explicitly fake DOM/fetch. These are not browser or HTTP QA.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const crypto = require('node:crypto').webcrypto;
const assets = path.join(__dirname, '../ssr_chat/review_assets');
const html = fs.readFileSync(path.join(assets, 'index.html'), 'utf8');
const source = fs.readFileSync(path.join(assets, 'app.js'), 'utf8');
const settle = () => new Promise(resolve => setImmediate(resolve));
const event = {message_id:'review-example',conversation_id:'review-room',author_id:'synthetic-viewer',source:'local-fixture',text:'!ask 星界遠征是什麼遊戲？'};

async function harness(runResponse, flow='fixture') {
  const elements = {}, requests = [];
  class Element {
    constructor(id = '') { this.id=id; this.value=''; this.textContent=''; this.checked=true; this.disabled=false; this.hidden=false; this.children=[]; this.listeners={}; this.attributes={}; }
    set innerHTML(_) { throw new Error('untrusted HTML assignment'); }
    addEventListener(name, callback) { (this.listeners[name] ||= []).push(callback); }
    async emit(name, value={}) { for (const callback of this.listeners[name] || []) await callback(value); }
    setAttribute(key, value) { this.attributes[key]=value; }
    getAttribute(key) { return this.attributes[key]; }
    replaceChildren(...children) { this.children=children; if (this.id==='example') this.value=children[0].value; }
    append(child) { this.children.push(child); }
    focus() { this.focused=true; }
    scrollIntoView() { this.scrolled=true; }
    showModal() { this.open=true; }
  }
  for (const match of html.matchAll(/id="([^"]+)"/g)) elements[match[1]]=new Element(match[1]);
  const tabs=['workbench','standard','evidence'].map(id => { const element=elements['tab-'+id]; element.attributes['aria-controls']=id; return element; });
  const meta={workspace_id:'mock-workspace',csrf_token:'mock-token',version:{head:'541c296d0f63',dirty:true,fingerprint:'mock-fingerprint'}};
  const response = value => ({ok:true,status:200,json:async()=>value});
  const fetch=async(url,options={})=>{
    requests.push({url,options});
    if (url==='/api/meta') return response(meta);
    if (url==='/api/examples') return response([{id:'normal',label:'正常提問',event}]);
    if (url==='/api/native/examples') return response([{id:'prepare',label:'原生 prompt',request:{tool:'channel-prepare',text:'/channel launch'}}]);
    if (url==='/api/criteria') return response({criteria:[],standard:'mock standard'});
    if (url==='/api/evidence') return response({historical:'historical',current_review:null});
    if (url==='/api/reset') return response({workspace_id:'mock-new-workspace',status:'new_synthetic_workspace'});
    if (url==='/api/run') return runResponse ? runResponse(options) : response({operation_id:'mock-operation',replayed:false,result:{status:'answered',answer:'<img src=x onerror=alert(1)>',send_result:{status:'dry_run_not_sent',public_reply:false}},evidence:{mode:'mock'}});
    if (url==='/api/native/run') return runResponse ? runResponse(options) : response({operation_id:'mock-native',execution_mode:'actual_native_pipeline',pipeline_exit_code:3,prompt_status:'prepared_not_sent',model_invoked:false,replayed:false,result:{status:'held',reason_code:'answer_generation_not_enabled'},evidence:{records:[{text:'mock synthetic evidence'}],prompt:{user_text:'launch'},trace:[]}});
    throw new Error('unexpected route');
  };
  const context=vm.createContext({document:{getElementById:id=>elements[id],querySelectorAll:()=>tabs,createElement:()=>new Element()},fetch,crypto,structuredClone,console});
  vm.runInContext(source,context,{filename:'app.js'});
  await settle();
  assert.equal(elements.connection.textContent,'● 本機服務已連線');
  if (flow==='fixture') { elements.flow.value='fixture'; await elements.flow.emit('change'); }
  return {elements,requests,tabs};
}

test('invalid JSON stays local and does not invent a runtime result',async()=>{
  const {elements,requests}=await harness();
  elements['event-input'].value='{';
  await elements.run.emit('click');
  assert.equal(requests.filter(x=>x.url==='/api/run').length,0);
  assert.match(elements['result-json'].textContent,/invalid_json/);
});

test('duplicate keys reach the server unchanged and busy submits do not repeat',async()=>{
  let resolve;
  const waiting=new Promise(r=>resolve=r);
  const {elements,requests}=await harness(()=>waiting);
  elements['event-input'].value='{"source":"local-fixture","text":"first","text":"second"}';
  const pending=elements.run.emit('click');
  assert.equal(elements.run.disabled,true);
  await elements.run.emit('click');
  const operations=requests.filter(x=>x.url==='/api/run');
  assert.equal(operations.length,1);
  assert.match(operations[0].options.body,/"text":"first","text":"second"/);
  resolve({ok:false,status:400,json:async()=>({status:'error',reason_code:'duplicate_json_key'})});
  await pending;
  assert.match(elements['result-json'].textContent,/duplicate_json_key/);
});

test('answers are plain text; return keeps input; resubmit keeps the event ID',async()=>{
  const {elements,requests}=await harness();
  const before=elements['event-input'].value;
  await elements.run.emit('click');
  assert.equal(elements.answer.textContent,'<img src=x onerror=alert(1)>');
  await elements['back-input'].emit('click');
  assert.equal(elements['event-input'].value,before);
  assert.equal(elements['event-input'].focused,true);
  await elements.resubmit.emit('click');
  const runs=requests.filter(x=>x.url==='/api/run').map(x=>JSON.parse(x.options.body));
  assert.equal(runs.length,2);
  assert.equal(runs[0].event.message_id,runs[1].event.message_id);
  assert.notEqual(runs[0].request_id,runs[1].request_id);
});

test('cancel keeps state and reopening a previously confirmed dialog resets its return value',async()=>{
  const {elements,requests}=await harness();
  const before=elements['event-input'].value;
  await elements['new-workspace'].emit('click');
  assert.equal(elements['reset-dialog'].returnValue,'cancel');
  await elements['reset-dialog'].emit('close');
  assert.equal(requests.filter(x=>x.url==='/api/reset').length,0);
  assert.equal(elements['event-input'].value,before);
  elements['reset-dialog'].returnValue='confirm';
  await elements['reset-dialog'].emit('close');
  assert.equal(requests.filter(x=>x.url==='/api/reset').length,1);
  await elements['new-workspace'].emit('click');
  assert.equal(elements['reset-dialog'].returnValue,'cancel');
  await elements['reset-dialog'].emit('close');
  assert.equal(requests.filter(x=>x.url==='/api/reset').length,1);
});

test('keyboard tabs change selection without combining panels',async()=>{
  const {elements,tabs}=await harness();
  let prevented=false;
  await tabs[0].emit('keydown',{key:'ArrowRight',preventDefault:()=>{prevented=true;}});
  assert.equal(prevented,true);
  assert.equal(elements.workbench.hidden,true);
  assert.equal(elements.standard.hidden,false);
  assert.equal(tabs[1].attributes['aria-selected'],'true');
  assert.equal(tabs[1].focused,true);
});

test('native default sends only registered request and labels prepared prompt as not generated',async()=>{
  const {elements,requests}=await harness(undefined,'native');
  assert.equal(elements.flow.value,'native');
  await elements.run.emit('click');
  const request=JSON.parse(requests.find(x=>x.url==='/api/native/run').options.body);
  assert.equal(request.request.tool,'channel-prepare');
  assert.equal(request.request.text,'/channel launch');
  assert.equal(Object.hasOwn(request,'event'),false);
  assert.match(elements['result-badge'].textContent,/未生成/);
  assert.match(elements.answer.textContent,/未生成回答/);
  assert.match(elements['trace-json'].textContent,/user_text/);
  assert.match(elements['native-records-json'].textContent,/mock synthetic evidence/);
});

test('native purpose denial is displayed without inventing an answer or fixture fallback',async()=>{
  const {elements,requests}=await harness(async()=>({ok:true,status:200,json:async()=>({operation_id:'mock-denied',execution_mode:'actual_native_pipeline',pipeline_exit_code:1,prompt_status:'not_prepared',result:{status:'held',reason_code:'ai_input_not_allowed'},evidence:{records:[],prompt:null,trace:[]}})}),'native');
  await elements.run.emit('click');
  assert.match(elements.answer.textContent,/用途政策拒絕/);
  assert.equal(requests.filter(x=>x.url==='/api/run').length,0);
  assert.equal(elements['result-status'].textContent,'held / ai_input_not_allowed');
});

test('source drift is displayed as execution failure even if the entry produced evidence',async()=>{
  const {elements}=await harness(async()=>({ok:true,status:200,json:async()=>({operation_id:'mock-drift',execution_mode:'actual_native_pipeline',execution_status:'error',execution_error_code:'source_changed_during_execution',source_unchanged:false,pipeline_exit_code:2,prompt_status:'not_prepared',result:{status:'evidence_ready'},evidence:{records:[],prompt:null,trace:[]}})}),'native');
  await elements.run.emit('click');
  assert.match(elements['result-badge'].textContent,/驗證失敗/);
  assert.match(elements.answer.textContent,/source_changed_during_execution/);
  assert.match(elements.answer.textContent,/不代表同版成功/);
});
