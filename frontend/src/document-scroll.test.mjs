import test, {afterEach, after} from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {transform} from 'esbuild';
import {JSDOM} from 'jsdom';

const source = await readFile(new URL('./documentScroll.ts', import.meta.url), 'utf8');
const compiled = await transform(source, {loader:'ts',format:'esm'});
const {attachDocumentProgress} = await import('data:text/javascript;base64,'+Buffer.from(compiled.code).toString('base64'));
const dom = new JSDOM('<!doctype html><html><head></head><body></body></html>',{pretendToBeVisual:true});
const {document} = dom.window;
let next=1, frames=new Map(), observers=[], disposers=[];
globalThis.requestAnimationFrame=fn=>{const id=next++;frames.set(id,fn);return id;};
globalThis.cancelAnimationFrame=id=>frames.delete(id);
globalThis.ResizeObserver=class {
  constructor(callback){this.callback=callback;this.targets=[];this.disconnected=false;observers.push(this);}
  observe(target){this.targets.push(target);}
  disconnect(){this.disconnected=true;}
};
const flush=()=>{const pending=[...frames.values()];frames.clear();pending.forEach(fn=>fn());};
function mount(){
  const viewport=document.createElement('div'),indicator=document.createElement('div'),paper=document.createElement('div');
  viewport.append(indicator,paper);document.body.append(viewport);
  let height=35000,client=450,reads=0;
  Object.defineProperty(viewport,'scrollHeight',{get(){reads++;return height;}});
  Object.defineProperty(viewport,'clientHeight',{get(){reads++;return client;}});
  const dispose=attachDocumentProgress(viewport,indicator,paper);disposers.push(dispose);
  return {viewport,indicator,paper,dispose,reads:()=>reads,resize:(h,c)=>{height=h;client=c;},
    scroll:(top)=>{viewport.scrollTop=top;viewport.dispatchEvent(new dom.window.Event('scroll'));}};
}
afterEach(()=>{disposers.forEach(fn=>fn());disposers=[];observers=[];frames.clear();document.head.replaceChildren();document.body.replaceChildren();});
after(()=>dom.window.close());

test('559-block scroll progress batches 100 scroll events into one geometry read pass',()=>{
  const h=mount();flush();const initial=h.reads();
  for(let i=1;i<=100;i++)h.scroll(i*100);
  assert.equal(frames.size,1);
  assert.equal(h.reads(),initial);
  flush();assert.equal(h.reads()-initial,2);
  assert.equal(h.indicator.style.transform,`scaleX(${10000/34550})`);
  assert.equal(h.indicator.style.width,'');
  assert.equal(h.viewport.scrollTop,10000,'progress code must never drive native scrolling');
});
test('progress clamps overscroll and handles no overflow or edited paper height',()=>{
  const h=mount();h.scroll(-10);flush();assert.equal(h.indicator.style.transform,'scaleX(0)');
  h.scroll(40000);flush();assert.equal(h.indicator.style.transform,'scaleX(1)');
  h.resize(450,450);observers[0].callback();flush();assert.equal(h.indicator.style.transform,'scaleX(1)');
  h.resize(2000,400);h.scroll(800);flush();assert.equal(h.indicator.style.transform,'scaleX(0.5)');
  assert.deepEqual(observers[0].targets,[h.viewport,h.paper]);
});
test('unmount cancels pending work, disconnects observers and ignores late callbacks',()=>{
  const h=mount();assert.equal(frames.size,1);h.dispose();assert.equal(frames.size,0);
  assert.equal(observers[0].disconnected,true);
  h.scroll(200);observers[0].callback();assert.equal(frames.size,0);
});
test('document canvas grid cannot inherit the document-list align-start rule',async()=>{
  const canvas=await readFile(new URL('./document-canvas.css',import.meta.url),'utf8');
  const management=await readFile(new URL('./document-management.css',import.meta.url),'utf8');
  const component=await readFile(new URL('./DocumentCanvas.tsx',import.meta.url),'utf8');
  const sheet=document.createElement('style');sheet.textContent=canvas+'\n'+management;document.head.append(sheet);
  const grid=document.createElement('div');grid.className='document-canvas-layout';document.body.append(grid);
  const css=dom.window.getComputedStyle(grid);
  assert.equal(css.alignItems,'stretch');
  assert.equal(css.gridTemplateRows,'minmax(0, 1fr)');
  assert.equal(css.minHeight,'0');
  assert.equal(css.overflow,'hidden');
  assert.match(component,/className=\{`document-canvas-layout/);
  assert.doesNotMatch(component,/className=\{`document-layout/);
  assert.match(component,/aria-label="文稿正文"\s+tabIndex=\{0\}/);
  assert.doesNotMatch(component,/matches\.some|progress\.current\.style\.width/);
  // JSDOM asserts declarations only; native scrollbar geometry is verified in-browser.
});
