const fs=require('fs'),vm=require('vm'),assert=require('node:assert/strict');
class Element{
  constructor(tag){this.tagName=tag;this.children=[];this.textContent='';this.hidden=false;this.disabled=false;this.style={setProperty(){}}}
  append(c){this.children.push(c)} prepend(c){this.children.unshift(c)} replaceChildren(...c){this.children=c} focus(){}
}
const roots=Object.fromEntries(['app','error','connect','connection','request-status'].map(k=>[k,new Element(k)]));
const document={getElementById:id=>roots[id],createElement:t=>new Element(t),documentElement:new Element('html')};
let listener;const sent=[];const timers=[];
const parent={postMessage(m){if(m.method)sent.push(m)}};
const context=vm.createContext({document,parent,window:{scrollY:0,scrollTo(){}},
  setTimeout:(fn,ms)=>{timers.push({fn,ms});return timers.length},clearTimeout:id=>{if(timers[id-1])timers[id-1].fn=null},
  requestAnimationFrame:f=>f(),addEventListener:(t,fn)=>{if(t==='message')listener=fn},console});
vm.runInContext(fs.readFileSync('view.html','utf8').match(/<script>([\s\S]*)<\/script>/)[1],context);
const chat='11111111-1111-4111-8111-111111111111';
const item=(title)=>({id:'k',title,group:'changed',status:'Recorded',summary:'s',detail:'d',evidence:['r'],steps:[],links:[]});
const account=(title)=>({objective:'o',summary:'Summary '+title,evidence:['r'],items:[item(title)],explainedAt:'t',fingerprint:'a'.repeat(64)});
const view={threadId:chat,walkthrough:account('Current')};
const deckFor=(title)=>({threadId:chat,title:'Chat',turns:[],records:[],readAt:'now',walkthrough:account(title)});
const run=s=>vm.runInContext(s,context);
const walk=e=>[e,...e.children.flatMap(walk)];
const visible=e=>e.textContent+' '+e.children.map(visible).join(' ');
const button=label=>walk(roots.app).find(e=>e.tagName==='button'&&e.textContent===label);
const tick=()=>new Promise(r=>setImmediate(r));
const request=(id)=>({requestId:id,threadId:chat,snapshot:{fingerprint:'b'.repeat(64),readAt:'then',recordCount:3}});
let capture=0,failCapture=false,failMessages=false,holdAcks=false;const pendingAcks=[];
const answer=(message,result)=>listener({source:parent,data:{jsonrpc:'2.0',id:message.id,result}});
const pump=async()=>{ // answer app tool calls and ui/message acknowledgements
  for(let i=0;i<20;i++){await tick();for(const m of sent.splice(0)){
    if(m.method==='tools/call'&&m.params.name==='request_walkthrough_update'){
      capture++;answer(m,failCapture?{isError:true,content:[{type:'text',text:'Capture failed.'}]}:{structuredContent:{request:request('request-identity-'+String(capture).padStart(4,'0'))}})}
    else if(m.method==='ui/message'){messages.push(m);
      if(holdAcks)pendingAcks.push(m);
      else if(failMessages)listener({source:parent,data:{jsonrpc:'2.0',id:m.id,error:{message:'Host refused'}}});
      else answer(m,{})}
    else if(m.method==='tools/call')answer(m,{structuredContent:{}})}}};
const messages=[];
const deliver=(id,title,outcome='delivered',message)=>listener({source:parent,data:{jsonrpc:'2.0',method:'ui/notifications/tool-result',params:
  outcome==='delivered'?{structuredContent:{preparation:{requestId:id,outcome},view:{threadId:chat,walkthrough:account(title)}},_meta:{deck:deckFor(title),view:{threadId:chat,walkthrough:account(title)},preparation:{requestId:id,outcome}}}
  :{structuredContent:{preparation:{requestId:id,outcome,message}}}}});
process.on('exit',()=>{if(!globalThis.__done){console.error('STALLED at step',globalThis.__step);process.exitCode=1}});
(async()=>{
  run('receive({_meta:{deck:'+JSON.stringify(deckFor('Current'))+',view:'+JSON.stringify(view)+'}})');
  sent.splice(0);
  assert.equal(sent.length+messages.length,0,'Opening the Bridge must not start work.');
  assert.match(visible(roots.app),/Current/);

  globalThis.__step='request';
  // Request, then duplicate submission is ignored.
  const first=run('explain()');run('explain()');await pump();await first;
  assert.equal(capture,1,'Duplicate submission must capture one request.');
  assert.equal(messages.length,1,'One dispatch message per request.');
  assert.match(messages[0].params.content[0].text,/exactly one read-only subagent/);
  assert.match(messages[0].params.content[0].text,/Do not wait or poll/);
  assert.match(messages[0].params.content[0].text,new RegExp(chat));
  assert.match(visible(roots.app),/in the background/);
  assert.match(visible(roots.app),/Current/,'The current walkthrough stays visible during preparation.');
  assert.ok(button('Cancel update'));

  // Delivery for a different request is dropped and does not replace the view.
  deliver('request-identity-9999','Foreign');await pump();
  assert.doesNotMatch(visible(roots.app),/Foreign/);
  assert.match(visible(roots.app),/in the background/,'Another request must not settle the active one.');

  globalThis.__step='cancel';
  // Cancel invalidates delivery; a late completion preserves the last useful view.
  const cancelled=run('cancelPrep()');await pump();await cancelled;
  assert.equal(messages.length,2);
  assert.match(messages[1].params.content[0].text,/Cancel .*request-identity-0001/);
  assert.match(messages[1].params.content[0].text,/Do not start another/);
  assert.equal(button('Cancel update'),undefined);
  deliver('request-identity-0001','Late');await pump();
  assert.doesNotMatch(visible(roots.app),/Late/,'Cancelled delivery must be discarded.');
  assert.match(visible(roots.app),/Current/);

  globalThis.__step='again';
  // A new request can be made after cancellation, and delivery for it replaces the view.
  const second=run('explain()');await pump();await second;
  assert.equal(capture,2);
  deliver('request-identity-0002','Fresh');await pump();
  assert.match(visible(roots.app),/Fresh/);
  assert.equal(button('Cancel update'),undefined,'Delivery ends the request.');
  deliver('request-identity-0002','Duplicate delivery');await pump();
  assert.doesNotMatch(visible(roots.app),/Duplicate delivery/,'A settled request cannot deliver twice.');

  // Failure keeps the last view and shows the reason; it never retries.
  const messageCount=messages.length;
  const third=run('explain()');await pump();await third;
  deliver('request-identity-0003','',"failed",'Could not read');await pump();
  assert.match(roots.error.textContent,/Could not read/);
  assert.match(visible(roots.app),/Fresh/);
  assert.equal(messages.length,messageCount+1,'Failure must not create another request.');
  assert.equal(capture,3);

  // Supersession reported for the active request drops it.
  const fourth=run('explain()');await pump();await fourth;
  deliver('request-identity-0004','',"superseded");await pump();
  assert.match(roots.error.textContent,/New instructions/);
  assert.equal(button('Cancel update'),undefined);

  // Timeout ends the request and preserves the view; later completion is dropped.
  const fifth=run('explain()');await pump();await fifth;
  const timer=timers.filter(t=>t.fn&&t.ms===600000).pop();
  assert.ok(timer,'A bounded attempt needs a timeout.');
  const beforeTimeout=messages.length;
  const timedOut=timer.fn();
  assert.equal(button('Cancel update'),undefined,'Timeout invalidates the request at once.');
  deliver('request-identity-0005','Too late');await pump();await timedOut;
  assert.match(roots.error.textContent,/took too long/);
  assert.equal(messages.length,beforeTimeout+1,'Timeout must hand a stop request to the first officer.');
  assert.match(messages.at(-1).params.content[0].text,/Cancel .*request-identity-0005.*timed out/);
  assert.match(messages.at(-1).params.content[0].text,/Interrupt its read-only subagent/);
  assert.match(roots.error.textContent,/asked to stop it; the stop is not confirmed/,'A requested stop is not reported as confirmed.');
  assert.doesNotMatch(visible(roots.app),/Too late/);
  assert.match(visible(roots.app),/Fresh/);

  // A failed stop handoff after timeout is reported honestly and still discards the work.
  const sixthA=run('explain()');await pump();await sixthA;
  const timerB=timers.filter(t=>t.fn&&t.ms===600000).pop();
  assert.ok(timerB);
  failMessages=true;
  const timedOutB=timerB.fn();await pump();await timedOutB;failMessages=false;
  assert.match(roots.error.textContent,/could not be told to stop it/);
  assert.match(roots.error.textContent,/may still be running/);
  deliver('request-identity-0006','Too late again');await pump();
  assert.doesNotMatch(visible(roots.app),/Too late again/);

  // Late acknowledgement failure for cancelled request A must not clear newer request B.
  failMessages=false;holdAcks=true;
  const A=run('explain()');await pump();
  assert.ok(pendingAcks.length>=1,'A dispatch acknowledgement is unresolved.');
  const ackA=pendingAcks.shift();
  const cancelA=run('cancelPrep()');await pump();
  const B=run('explain()');await pump();
  holdAcks=false;
  for(const a of pendingAcks.splice(0))answer(a,{});
  await pump();
  assert.ok(button('Cancel update'),'B is active before the stale acknowledgement fails.');
  listener({source:parent,data:{jsonrpc:'2.0',id:ackA.id,error:{message:'Late A failure'}}});
  await pump();await A;await cancelA;await B;
  assert.ok(button('Cancel update'),'A late failure of A must not clear B or its cancel control.');
  assert.doesNotMatch(roots.error.textContent,/Late A failure/);
  const idB=run('prep.request.requestId');
  deliver(idB,'From B');await pump();
  assert.match(visible(roots.app),/From B/,'B still settles after A fails late.');

  // Capture failure sends nothing to the first officer.
  failCapture=true;const before=messages.length;
  const sixth=run('explain()');await pump();await sixth;
  assert.equal(messages.length,before);
  assert.match(roots.error.textContent,/Capture failed/);
  assert.equal(button('Cancel update'),undefined);
  globalThis.__done=true;console.log('PASS: request capture, single dispatch, duplicate submission, cancel, late and foreign delivery, failure, supersession, timeout and capture failure.');
})().catch(e=>{console.error(e);process.exitCode=1});
