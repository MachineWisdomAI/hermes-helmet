const fs = require('fs'), vm = require('vm'), assert = require('node:assert/strict');
class Element {
  constructor(tag) { this.tagName=tag; this.children=[]; this.textContent=''; this.hidden=false; this.disabled=false; this.style={setProperty(){}}; }
  append(c){this.children.push(c)} prepend(c){this.children.unshift(c)} replaceChildren(...c){this.children=c} focus(){}
}
const roots=Object.fromEntries(['app','error','connect','connection','request-status'].map(k=>[k,new Element(k)]));
const document={getElementById:id=>roots[id],createElement:t=>new Element(t),documentElement:new Element('html')};
let listener; const calls=[], held=[], messages=[];
const parent={postMessage(m){
  if(m.method==='ui/message'){messages.push(m);return}
  if(m.method!=='tools/call')return;
  calls.push(m);
  held.push(answer=>listener({source:parent,data:{jsonrpc:'2.0',id:m.id,result:answer}}));
}};
const context=vm.createContext({document,parent,window:{scrollY:0,scrollTo(){}},setTimeout,clearTimeout,requestAnimationFrame:f=>f(),addEventListener:(t,fn)=>{if(t==='message')listener=fn},console});
vm.runInContext(fs.readFileSync('view.html','utf8').match(/<script>([\s\S]*)<\/script>/)[1],context);
const visible=e=>e.textContent+' '+e.children.map(visible).join(' ');
const view={threadId:'this-chat-only',walkthrough:{summary:'Prepared'}};
const deck=(readAt,extra={})=>({deck:{title:'Chat',threadId:'this-chat-only',readAt,turns:[],records:[],explanationStale:false,
  walkthrough:{summary:'Original conclusion.',objective:'Obj',explainedAt:'2026-10-09T10:00:00Z',evidence:[],items:[{id:'a',title:'Work',group:'changed',status:'Recorded',summary:'S',detail:'D',evidence:[],steps:[],links:[]}]},...extra},view});
const ok=d=>({structuredContent:d});
const tick=()=>new Promise(r=>setImmediate(r));
const btn=()=>({disabled:false});
(async()=>{
  vm.runInContext('selected="a"',context);
  context.receive(ok(deck('2026-10-09T10:30:00Z')));
  assert.match(visible(roots.app),/Original conclusion/);
  // Overlapping: a second click while one is in flight is not started.
  const b1=btn(), b2=btn();
  const first=vm.runInContext('refresh',context)(b1);
  const second=vm.runInContext('refresh',context)(b2);
  assert.equal(calls.length,1,'one refresh request at a time');
  assert.equal(calls[0].params.name,'refresh_observation_deck');
  assert.equal(messages.length,0,'refresh sends no agent message');
  const stale=deck('2026-10-09T11:00:00Z',{explanationStale:true});
  held.shift()(ok(stale)); await first; await second;
  const text=visible(roots.app);
  assert.match(text,/older than the chat|older than the records/);
  assert.match(text,/Original conclusion/,'conclusions unchanged');
  assert.match(text,/Explanation prepared from records read/);
  assert.equal(vm.runInContext('selected',context),'a','selection kept');
  assert.equal(b1.disabled,false);
  // Out-of-order: an older read arriving later is ignored.
  const older=vm.runInContext('refresh',context)(btn());
  held.shift()(ok(deck('2026-10-09T10:45:00Z')));
  await older;
  assert.equal(vm.runInContext('deck.readAt',context),'2026-10-09T11:00:00Z');
  assert.match(roots.error.textContent,/not refreshed/);
  assert.equal(roots.error.hidden,false);
  assert.match(visible(roots.app),/Original conclusion/);
  // Foreign chat answer is ignored.
  const foreign=vm.runInContext('refresh',context)(btn());
  const other=deck('2026-10-09T12:00:00Z'); other.deck.threadId='someone-else';
  held.shift()(ok(other)); await foreign;
  assert.match(roots.error.textContent,/different chat/);
  assert.equal(vm.runInContext('deck.readAt',context),'2026-10-09T11:00:00Z');
  // Read error keeps view and reports the limitation.
  const failed=vm.runInContext('refresh',context)(btn());
  held.shift()({isError:true,content:[{type:'text',text:'Codex’s saved chat index could not be read in read-only mode.'}]}); await failed;
  assert.match(roots.error.textContent,/not refreshed.*read-only mode.*previous view is kept/);
  assert.match(visible(roots.app),/Original conclusion/);
  // Timeout (host never answers within the rpc limit) uses the same path.
  const timeoutCtx=vm.runInContext('(()=>{const f=refresh;return f})()',context);
  const pendingRefresh=timeoutCtx(btn());
  held.shift()(ok(deck('2026-10-09T13:00:00Z',{partialAppend:true}))); await pendingRefresh;
  assert.match(visible(roots.app),/still being written/);
  // A successful refresh clears the error.
  assert.equal(roots.error.hidden,true);
  console.log('PASS: direct refresh freshness, retained state, failures, foreign and out-of-order responses, overlapping requests.');
})().catch(e=>{console.error(e);process.exitCode=1});
