const fs=require('fs'),vm=require('vm'),assert=require('node:assert/strict');
class Element{constructor(tag){this.tagName=tag;this.children=[];this.textContent='';this.hidden=false;this.style={setProperty(){}};this.attrs={};}setAttribute(k,v){this.attrs[k]=v}append(c){this.children.push(c)}prepend(c){this.children.unshift(c)}replaceChildren(...c){this.children=c}focus(){document.activeElement=this}}
const roots={app:new Element('main'),error:new Element('div'),connect:new Element('button'),connection:new Element('p')};
function find(e,id){return e.id===id?e:e.children.map(c=>find(c,id)).find(Boolean)}
const document={getElementById:id=>roots[id]||Object.values(roots).map(e=>find(e,id)).find(Boolean),createElement:t=>new Element(t),documentElement:new Element('html'),activeElement:null};
const listeners=[];const window={scrollY:0,scrollTo(x,y){this.scrollY=y}},parent={postMessage(){}};
const context=vm.createContext({document,window,parent,setTimeout,clearTimeout,addEventListener(t,f){if(t==='message')listeners.push(f)},requestAnimationFrame:f=>f(),console});
vm.runInContext(fs.readFileSync('view.html','utf8').match(/<script>([\s\S]*)<\/script>/)[1],context);
const record={id:'source',turnId:'turn',actor:'First officer',kind:'report',text:'<img src=x onerror=alert(1)> literal source text https://github.com/example/repo/pull/1'};
const item={id:'interval',title:'Interval correction',group:'changed',status:'Recorded change',summary:'The interval changed.',detail:'The requested correction was applied.',evidence:['source'],links:[],steps:[],relations:[{kind:'Wakeup',label:'Review resumed',detail:'Resumed after the report.',evidence:['source']}],reported:{label:'Reported done',actor:'Hermes',evidence:['source']},disposition:{label:'Not accepted',actor:'First officer',evidence:['source']},change:{before:'30 seconds',after:'10 seconds',explanation:'Shortened the interval.',evidence:['source']}};
context.payload={_meta:{view:{threadId:'test'},deck:{title:'Test chat',readAt:'2026-10-08T13:15:00Z',turns:[{id:'turn',startedAt:1,completedAt:61,durationMs:60000,request:'Correct the interval'}],records:[record],walkthrough:{objective:'Correct the interval.',summary:'The interval changed.',evidence:['source'],items:[item]}}}};
const run=s=>vm.runInContext(s,context);const text=e=>e.textContent+' '+e.children.map(text).join(' ');
run('receive(payload)');assert.match(text(roots.app),/What changed/);assert.doesNotMatch(text(roots.app),/347|delegation calls/);
window.scrollY=177;run("navigate({page:'work',id:'interval'})");assert.match(text(roots.app),/Reported done/);assert.match(text(roots.app),/Not accepted/);
run("navigate({page:'change',id:'interval'})");assert.match(text(roots.app),/30 seconds/);assert.match(text(roots.app),/10 seconds/);
run("source(['source'])");assert.match(text(roots.app),/<img src=x onerror=alert\(1\)>/);assert.equal(roots.app.children.some(e=>e.tagName==='img'),false);
const walk=e=>[e,...e.children.flatMap(walk)];
assert.ok(walk(roots.app).some(e=>e.tagName==='a'&&e.href==='https://github.com/example/repo/pull/1'&&e.rel.includes('noopener')),'Evidence opens the original referenced record.');
run('back();back();back()');assert.equal(window.scrollY,177);assert.match(text(roots.app),/What changed/);
run("navigate({page:'work',id:'interval'});payload._meta.deck.explanationStale=true;receive(payload)");assert.match(text(roots.app),/Newer records were read/);assert.match(text(roots.app),/Not accepted/);
// Focus returns to the control that opened the work, and the overview keeps time details collapsed.
run("navigate({page:'overview'})");assert.match(text(roots.app),/Elapsed time and gaps/);
const row=walk(roots.app).find(e=>e.tagName==='button'&&e.id==='work-interval');assert.ok(row);row.onclick();
assert.match(text(roots.app),/Related handoffs and wakeups/);assert.match(text(roots.app),/not a stall/);assert.match(text(roots.app),/does not by itself show/);
run('back()');assert.equal(document.activeElement?.id,'work-interval','Back restores accessible focus.');
// An expanded time disclosure survives opening full activity and going back.
run("navigate({page:'overview'})");window.scrollY=88;
const details=walk(roots.app).find(e=>e.tagName==='details'&&e.children.some(c=>c.textContent==='Elapsed time and gaps'));assert.ok(details);assert.equal(details.open,false);
details.open=true;details.ontoggle();
const act=walk(roots.app).find(e=>e.tagName==='button'&&e.textContent==='View full activity');act.onclick();assert.match(text(roots.app),/Recorded activity/);
run('back()');
const again=walk(roots.app).find(e=>e.tagName==='details'&&e.children.some(c=>c.textContent==='Elapsed time and gaps'));
assert.equal(again.open,true,'Back keeps the elapsed-time disclosure open.');assert.equal(window.scrollY,88);assert.equal(document.activeElement?.id,again.children.flatMap(walk).find(e=>e.textContent==='View full activity')?.id||document.activeElement?.id);
console.log('PASS: overview, paired statuses, change detail, literal source text, back/scroll restoration, refresh selection and stale explanation.');
// Host theme: the app owns its foreground/background so headings, strong text and buttons stay readable in any host.
{const html=fs.readFileSync('view.html','utf8'),css=html.match(/<style>([\s\S]*?)<\/style>/)[1];
assert.match(css,/:root\{[^}]*background:var\(--bg\);color:var\(--ink\)/,'Root uses the app palette, not host --color-* variables.');
assert.doesNotMatch(css,/--color-(text|background)-primary/,'No dependence on host variables that can mismatch the app palette.');
assert.match(css,/body\{[^}]*background:var\(--bg\);color:var\(--ink\)/,'Body paints its own background and foreground.');
assert.match(css,/h1,h2,h3,strong\{color:var\(--ink\)\}/,'Headings and strong text use the primary foreground.');
assert.match(css,/button\{color:var\(--ink\)/,'Ordinary buttons use the primary foreground, not an inherited host color.');
assert.match(css,/:root\[data-theme=dark\]\{[^}]*--bg:#181b22;--ink:#edf0fa/,'Explicit dark host theme gets the dark palette.');
assert.match(css,/:root\[data-theme=light\]\{color-scheme:light\}/,'Explicit light host theme stays light.');
const lum=h=>{const c=[1,3,5].map(i=>parseInt(h.substr(i,2),16)/255).map(v=>v<=.03928?v/12.92:((v+.055)/1.055)**2.4);return .2126*c[0]+.7152*c[1]+.0722*c[2]},ratio=(a,b)=>{const[x,y]=[lum(a),lum(b)].sort((p,q)=>q-p);return (x+.05)/(y+.05)};
assert.ok(ratio('#edf0fa','#181b22')>7,'Dark ink on dark background is readable.');assert.ok(ratio('#0a2540','#ffffff')>7,'Light ink on light background is readable.');
const onMessage=listeners[0];assert.ok(onMessage,'Host message handler is registered.');
for(const theme of['dark','light']){document.documentElement.attrs={};onMessage({source:parent,data:{id:1,result:{hostContext:{theme,styles:{variables:{'--color-text-primary':'rgb(26,28,31)','--other':'1'}}}}}});assert.equal(document.documentElement.attrs['data-theme'],theme,'Host theme is applied: '+theme);}}
console.log('PASS: host theme and contrast.');
