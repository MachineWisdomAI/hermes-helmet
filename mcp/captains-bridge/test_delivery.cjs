const fs = require('fs'), vm = require('vm'), assert = require('node:assert/strict');
class Element {
  constructor(tag) { this.tagName=tag; this.children=[]; this.textContent=''; this.hidden=false; this.style={setProperty(){}}; }
  append(c){this.children.push(c)} prepend(c){this.children.unshift(c)}
  replaceChildren(...c){this.children=c} focus(){}
}
const roots=Object.fromEntries(['app','error','connect','connection'].map(k=>[k,new Element(k)]));
const document={getElementById:id=>roots[id],createElement:t=>new Element(t),documentElement:new Element('html')};
let listener, responseError=false;
const calls=[];
const portable={threadId:'this-chat-only',walkthrough:{summary:'Prepared work'}};
const payload={view:portable,deck:{title:'Delivery regression',readAt:'2026-10-08T16:00:00Z',turns:[],records:[],walkthrough:{summary:'The walkthrough arrived.',objective:'Show the recorded work.',evidence:[],items:[{id:'change',title:'A recorded change',group:'changed',status:'Recorded',summary:'Visible in the panel.',detail:'Details.',evidence:[],steps:[],links:[]}]}}};
const parent={postMessage(message){
  if(message.method!=='tools/call')return;
  calls.push(message);
  queueMicrotask(()=>listener({source:parent,data:{jsonrpc:'2.0',id:message.id,result:responseError?{isError:true,content:[{type:'text',text:'This connection expired.'}]}:{structuredContent:payload}}}));
}};
const context=vm.createContext({document,parent,window:{scrollY:0,scrollTo(){}},setTimeout,clearTimeout,requestAnimationFrame:f=>f(),addEventListener:(type,fn)=>{if(type==='message')listener=fn;},console});
vm.runInContext(fs.readFileSync('view.html','utf8').match(/<script>([\s\S]*)<\/script>/)[1],context);
const event=(method,params)=>listener({source:parent,data:{jsonrpc:'2.0',method,params}});
const visible=e=>e.textContent+' '+e.children.map(visible).join(' ');
(async()=>{
  // The initial result can survive in the host without its UI-only metadata.
  event('ui/notifications/tool-result',{content:[{type:'text',text:'Prepared'}],structuredContent:{title:'Delivery regression',prepared:true,view:portable}});
  await new Promise(resolve=>setImmediate(resolve));
  assert.match(visible(roots.app),/What changed/, 'Prepared result must replace the waiting screen even when _meta is absent.');
  assert.equal(calls.length,1);
  assert.equal(calls[0].params.name,'get_chat_work_view');
  assert.deepEqual(JSON.parse(JSON.stringify(calls[0].params.arguments)),{view:portable});
  event('ui/notifications/tool-result',{structuredContent:{view:portable,prepared:true}});
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(calls.length,2,'A new result must retrieve the current prepared account.');
  await vm.runInContext('refresh({disabled:false})',context);
  assert.equal(calls[2].params.name,'refresh_observation_deck');
  assert.deepEqual(JSON.parse(JSON.stringify(calls[2].params.arguments)),{view:portable});
  // Captured Codex item_completed shape: the MCP result is JSON in one text block.
  event('ui/notifications/tool-result',{content:[{type:'text',text:JSON.stringify({content:[],structuredContent:payload})}]});
  assert.equal(roots.error.hidden,true,'A Codex text-wrapped result must not be mistaken for a missing update.');
  assert.match(visible(roots.app),/What changed/);
  const requestCount=calls.length;
  event('ui/notifications/tool-result',{content:[{type:'text',text:JSON.stringify({content:[],_meta:payload,structuredContent:{prepared:true}})}]});
  assert.equal(calls.length,requestCount,'Use the delivered deck without rereading records.');
  assert.equal(roots.error.hidden,true);
  event('ui/notifications/tool-result',{content:[{type:'text',text:JSON.stringify({content:[],structuredContent:{view:portable,prepared:true}})}]});
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(calls.length,requestCount+1,'A wrapped portable view can use the app-only fallback.');
  responseError=true;
  event('ui/notifications/tool-result',{structuredContent:{view:portable,prepared:true}});
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(roots.error.hidden,false);
  assert.match(roots.error.textContent,/expired/);
  assert.match(visible(roots.app),/What changed/,'Keep the prior view when loading fails.');
  console.log('PASS: real tool notification path, metadata-free delivery, portable chat reference, stateless refresh, repeated updates, and visible retrieval failure.');
})().catch(e=>{console.error(e.message);process.exitCode=1;});
