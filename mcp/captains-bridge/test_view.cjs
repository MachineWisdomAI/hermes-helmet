const fs=require('fs'),vm=require('vm'),assert=require('node:assert/strict');
class Element{constructor(tag){this.tagName=tag;this.children=[];this.textContent='';this.hidden=false;this.style={setProperty(){}};}append(c){this.children.push(c)}prepend(c){this.children.unshift(c)}replaceChildren(...c){this.children=c}focus(){document.activeElement=this}}
const roots={app:new Element('main'),error:new Element('div'),connect:new Element('button'),connection:new Element('p')};
function find(e,id){return e.id===id?e:e.children.map(c=>find(c,id)).find(Boolean)}
const document={getElementById:id=>roots[id]||Object.values(roots).map(e=>find(e,id)).find(Boolean),createElement:t=>new Element(t),documentElement:new Element('html'),activeElement:null};
const window={scrollY:0,scrollTo(x,y){this.scrollY=y}},parent={postMessage(){}};
const context=vm.createContext({document,window,parent,setTimeout,clearTimeout,addEventListener(){},requestAnimationFrame:f=>f(),console});
vm.runInContext(fs.readFileSync('view.html','utf8').match(/<script>([\s\S]*)<\/script>/)[1],context);
const record={id:'source',turnId:'turn',actor:'First officer',kind:'report',text:'<img src=x onerror=alert(1)> literal source text'};
const item={id:'interval',title:'Interval correction',group:'changed',status:'Recorded change',summary:'The interval changed.',detail:'The requested correction was applied.',evidence:['source'],links:[],steps:[],reported:{label:'Reported done',actor:'Hermes',evidence:['source']},disposition:{label:'Not accepted',actor:'First officer',evidence:['source']},change:{before:'30 seconds',after:'10 seconds',explanation:'Shortened the interval.',evidence:['source']}};
context.payload={_meta:{view:{threadId:'test'},deck:{title:'Test chat',readAt:'2026-10-08T13:15:00Z',turns:[{id:'turn',startedAt:1,completedAt:61,durationMs:60000,request:'Correct the interval'}],records:[record],walkthrough:{objective:'Correct the interval.',summary:'The interval changed.',evidence:['source'],items:[item]}}}};
const run=s=>vm.runInContext(s,context);const text=e=>e.textContent+' '+e.children.map(text).join(' ');
run('receive(payload)');assert.match(text(roots.app),/What changed/);assert.doesNotMatch(text(roots.app),/347|delegation calls/);
window.scrollY=177;run("navigate({page:'work',id:'interval'})");assert.match(text(roots.app),/Reported done/);assert.match(text(roots.app),/Not accepted/);
run("navigate({page:'change',id:'interval'})");assert.match(text(roots.app),/30 seconds/);assert.match(text(roots.app),/10 seconds/);
run("source(['source'])");assert.match(text(roots.app),/<img src=x onerror=alert\(1\)>/);assert.equal(roots.app.children.some(e=>e.tagName==='img'),false);
run('back();back();back()');assert.equal(window.scrollY,177);assert.match(text(roots.app),/What changed/);
run("navigate({page:'work',id:'interval'});payload._meta.deck.explanationStale=true;receive(payload)");assert.match(text(roots.app),/Records changed/);assert.match(text(roots.app),/Not accepted/);
console.log('PASS: overview, paired statuses, change detail, literal source text, back/scroll restoration, refresh selection and stale explanation.');
