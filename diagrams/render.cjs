const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const {pathToFileURL} = require('node:url');
const playwrightPath = process.env.PLAYWRIGHT_PATH || path.join(os.homedir(), '.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const {chromium} = require(playwrightPath);
const out = process.argv[2] || __dirname;
if (!out) throw new Error('Usage: node render.cjs <artifact-directory>');
const bundle = process.env.DIAGRAM_BUNDLE || path.join(os.homedir(), '.codex/skills/gstack/lib/diagram-render/dist/diagram-render.html');
const chrome = process.env.CHROME_PATH || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const manifest = JSON.parse(fs.readFileSync(path.join(out, 'manifest.json'), 'utf8'));
(async () => {
 const browser = await chromium.launch({executablePath:chrome,headless:true});
 try {
  const page = await browser.newPage();
  await page.route(/^https?:/, route => route.abort());
  await page.goto(pathToFileURL(bundle).href);
  await page.waitForSelector('#done');
  const inventory=[];
  for (let i=0;i<manifest.diagrams.length;i++) {
   const d=manifest.diagrams[i];
   if (i) { await page.reload(); await page.waitForSelector('#done'); }
   const source=fs.readFileSync(path.join(out,d.slug+'.mmd'),'utf8');
   const raw=await page.evaluate(({id,text})=>window.__renderMermaid(id,text),{id:'helmet-diagram-'+i,text:source});
   const scene=await page.evaluate(async text=>{
    // Mermaid prefixes cluster IDs; the converter still looks up the bare ID.
    // Resolve that exact ID within the converter's temporary container only.
    const query=Element.prototype.querySelector;
    Element.prototype.querySelector=function(selector) {
     const found=query.call(this,selector);
     if (found || !/^mermaid-to-excalidraw-\d+-container$/.test(this.id)) return found;
     const match=/^\[id='([\w-]+)'\]$/.exec(selector);
     return match ? query.call(this,`[id="${this.id.replace(/-container$/, '')}-${match[1]}"]`) : null;
    };
    // The converter treats HTML line breaks as literal text; Mermaid accepts newlines.
    try { return await window.__mermaidToExcalidraw(text.replace(/<br\s*\/?\s*>/gi, '\n')); }
    finally { Element.prototype.querySelector=query; }
   },source);
   const built=await page.evaluate(({raw,d})=>{
    const dom=new DOMParser().parseFromString(raw,'image/svg+xml');
    const graph=dom.documentElement;
    const viewBox=graph.getAttribute('viewBox').split(/[ ,]+/).map(Number);
    const width=Math.ceil(viewBox[2]), height=Math.ceil(viewBox[3]);
    graph.removeAttribute('style');
    graph.setAttribute('width',String(width));
    graph.setAttribute('height',String(height));
    graph.setAttribute('role','img');
    graph.setAttribute('aria-labelledby','title desc');
    for (const [tag,id,text] of [['title','title',d.title],['desc','desc',d.alt]]) {
     const element=document.createElementNS('http://www.w3.org/2000/svg',tag);
     element.id=id; element.textContent=text; graph.prepend(element);
    }
    return {svg:new XMLSerializer().serializeToString(graph),width,height};
   },{raw,d});
   // The bundle rasterizer paints white; keep the canvas clear for transparent PNGs.
   const png=await page.evaluate(async svg=>{
    const url=URL.createObjectURL(new Blob([svg],{type:'image/svg+xml;charset=utf-8'}));
    try {
     const image=new Image(); image.src=url; await image.decode();
     const canvas=document.createElement('canvas');
     canvas.width=2400; canvas.height=Math.round(2400*image.naturalHeight/image.naturalWidth);
     canvas.getContext('2d').drawImage(image,0,0,canvas.width,canvas.height);
     return canvas.toDataURL('image/png');
    } finally {URL.revokeObjectURL(url);}
   },built.svg);
   const editable=JSON.parse(scene);
   if (editable.elements.some(e=>e.type==='image')) throw new Error(`${d.slug}: converter flattened the diagram`);
   editable.appState.viewBackgroundColor='transparent';
   editable.appState.exportBackground=false;
   fs.writeFileSync(path.join(out,d.slug+'.svg'),built.svg);
   fs.writeFileSync(path.join(out,d.slug+'.png'),Buffer.from(png.split(',')[1],'base64'));
   fs.writeFileSync(path.join(out,d.slug+'.excalidraw'),JSON.stringify(editable)+'\n');
   const pngBytes=Buffer.from(png.split(',')[1],'base64');
   inventory.push({slug:d.slug,width:pngBytes.readUInt32BE(16),height:pngBytes.readUInt32BE(20),svgWidth:built.width,svgHeight:built.height,editableElements:editable.elements.length});
   console.log(JSON.stringify(inventory.at(-1)));
  }
  fs.writeFileSync(path.join(out,'render-inventory.json'),JSON.stringify({bundle:await page.evaluate(()=>window.__bundleInfo),diagrams:inventory},null,2)+'\n');
 } finally {await browser.close();}
})().catch(e=>{console.error(e.stack);process.exit(1)});
