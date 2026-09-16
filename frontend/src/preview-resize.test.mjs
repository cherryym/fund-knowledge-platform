import test, {afterEach, after} from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {transform} from 'esbuild';
import {JSDOM} from 'jsdom';
const code = await transform(await readFile(new URL('./previewResize.ts', import.meta.url),'utf8'),{loader:'ts',format:'esm'});
const {attachPreviewResize, previewResizeLimits, clampPreviewWidth} = await import('data:text/javascript;base64,'+Buffer.from(code.code).toString('base64'));
const dom=new JSDOM('<!doctype html><html><body></body></html>',{url:'http://localhost',pretendToBeVisual:true});
globalThis.window=dom.window;globalThis.document=dom.window.document;
Object.defineProperty(window,'innerWidth',{value:1280,writable:true,configurable:true});
let nextFrame=0, frames=new Map(), observers=[], disposers=[];
window.requestAnimationFrame=fn=>{const id=++nextFrame;frames.set(id,fn);return id;};
window.cancelAnimationFrame=id=>frames.delete(id);
globalThis.ResizeObserver=class {constructor(cb){this.cb=cb;this.stopped=false;observers.push(this);} observe(){} disconnect(){this.stopped=true;}};
const flush=()=>{const pending=[...frames.values()];frames.clear();pending.forEach(fn=>fn(0));};
function setup(mode='dock',saved,withWorkspace=false){
  const key=`test-width:${mode}`;
  if(saved!==undefined)window.localStorage.setItem(key,saved);
  const host=document.createElement('section'),handle=document.createElement('div');handle.tabIndex=0;host.append(handle);document.body.append(host);
  const workspace=withWorkspace ? document.createElement('section') : undefined;
  if(workspace){workspace.className='resource-workspace no-overview';host.append(workspace);}
  let available=1100;Object.defineProperty(host,'clientWidth',{get:()=>available});
  const captured=new Set();handle.setPointerCapture=id=>captured.add(id);handle.releasePointerCapture=id=>captured.delete(id);handle.hasPointerCapture=id=>captured.has(id);
  const dispose=attachPreviewResize(handle,host,mode,key);disposers.push(dispose);
  const event=(type,props={})=>{const event=new window.Event(type,{bubbles:true,cancelable:true});Object.assign(event,{pointerId:1,button:0,isPrimary:true,clientX:700,...props});handle.dispatchEvent(event);return event;};
  return {host,handle,workspace,event,key,captured,dispose,available:value=>{available=value;},width:()=>Number(handle.getAttribute('aria-valuenow'))};
}
afterEach(()=>{disposers.forEach(fn=>fn());disposers=[];frames.clear();observers=[];document.body.replaceChildren();window.localStorage.clear();window.innerWidth=1280;});
after(()=>dom.window.close());

test('dock preserves 320px list space and drawer can expand nearly to viewport',()=>{
  assert.deepEqual(previewResizeLimits('dock',1100),{min:280,max:780,defaultWidth:320});
  assert.equal(previewResizeLimits('drawer',1280).max,1256);
  assert.equal(previewResizeLimits('drawer',390).max,390);
  assert.equal(clampPreviewWidth(Infinity,previewResizeLimits('dock',1100)),320);
});
test('detail window dimensions belong to the stable detail modal, including mobile and empty states',async()=>{
  const source=await readFile(new URL('./ResourceDetail.tsx',import.meta.url),'utf8');
  const css=await readFile(new URL('./document-canvas.css',import.meta.url),'utf8');
  assert.match(source,/<Modal\s+title=\{resource\.name\}\s+wide\s+className="resource-detail-modal"/);
  assert.match(css,/\.modal\.resource-detail-modal,\s*\.modal:has\(\.document-workbench\)\s*\{\s*width: min\(1480px, calc\(100vw - 28px\)\);\s*height: calc\(100dvh - 28px\)/);
  assert.match(css,/\.modal\.resource-detail-modal \.detail-body\s*\{\s*flex: 1;\s*min-height: 0;\s*max-height: none/);
  assert.match(css,/@media \(max-width: 760px\)\s*\{\s*\.modal\.resource-detail-modal,\s*\.modal:has\(\.document-workbench\)/);
});
test('tiny viewport clamps both bounds rather than overflowing',()=>{
  window.innerWidth=220;const e=setup('drawer');assert.equal(e.width(),220);assert.equal(e.handle.getAttribute('aria-disabled'),'true');
});
test('left drag expands, coalesces 100 moves to one frame, and persists only on release',()=>{
  const e=setup();e.event('pointerdown');
  for(let i=1;i<=100;i++)e.event('pointermove',{clientX:700-i*2});
  assert.equal(frames.size,1);assert.equal(e.width(),320);assert.equal(window.localStorage.getItem(e.key),null);
  flush();assert.equal(e.width(),520);assert.equal(e.host.dataset.previewResizing,'true');
  e.event('pointerup',{clientX:500});assert.equal(e.captured.size,0);assert.equal(e.host.dataset.previewResizing,undefined);
  assert.equal(window.localStorage.getItem(e.key),'520');
});
test('pointerup flushes final position even without the final animation frame',()=>{
  const e=setup();e.event('pointerdown');e.event('pointermove',{clientX:600});e.event('pointerup',{clientX:450});
  assert.equal(e.width(),570);assert.equal(frames.size,0);
});
test('release click retargeted to the modal is swallowed but a later real click is not',()=>{
  const e=setup('drawer');let backdropClicks=0;e.host.addEventListener('click',()=>backdropClicks++);
  e.event('pointerdown');e.event('pointerup',{clientX:460});
  e.host.dispatchEvent(new window.MouseEvent('click',{bubbles:true,cancelable:true}));assert.equal(backdropClicks,0);
  e.host.dispatchEvent(new window.Event('pointerdown',{bubbles:true}));
  e.host.dispatchEvent(new window.MouseEvent('click',{bubbles:true,cancelable:true}));assert.equal(backdropClicks,1);
});
test('pointer IDs and secondary buttons cannot hijack resize',()=>{
  const e=setup();e.event('pointerdown',{button:2});e.event('pointermove',{clientX:200});assert.equal(frames.size,0);
  e.event('pointerdown');e.event('pointermove',{pointerId:9,clientX:200});e.event('pointerup',{pointerId:9,clientX:200});
  assert.equal(e.width(),320);assert.equal(e.captured.size,1);
});
test('right drag shrinks and clamps to minimum; excessive left drag respects list room',()=>{
  const e=setup();e.event('pointerdown');e.event('pointerup',{clientX:2000});assert.equal(e.width(),280);
  e.event('pointerdown');e.event('pointerup',{clientX:-2000});assert.equal(e.width(),780);
});
for(const signal of ['pointercancel','lostpointercapture','blur','Escape'])test(`${signal} cancels gesture without changing saved width`,()=>{
  const e=setup('dock','400');e.event('pointerdown');e.event('pointermove',{clientX:550});flush();assert.equal(e.width(),550);
  if(signal==='blur')window.dispatchEvent(new window.Event('blur'));
  else if(signal==='Escape')assert.equal(e.event('keydown',{key:'Escape'}).defaultPrevented,true);
  else e.event(signal);
  assert.equal(e.width(),400);assert.equal(e.captured.size,0);assert.equal(window.localStorage.getItem(e.key),'400');assert.equal(frames.size,0);
});
test('keyboard adjusts width accessibly and double click restores the mode default',()=>{
  const e=setup();e.event('keydown',{key:'ArrowLeft'});assert.equal(e.width(),344);
  e.event('keydown',{key:'ArrowRight',shiftKey:true});assert.equal(e.width(),280);
  e.event('keydown',{key:'End'});assert.equal(e.width(),780);e.event('keydown',{key:'Home'});assert.equal(e.width(),280);
  e.event('dblclick');assert.equal(e.width(),320);assert.equal(e.handle.getAttribute('aria-valuetext'),'320 像素');
});
test('document remount restores only numeric width preference and uses separate drawer key',()=>{
  const e=setup();e.event('keydown',{key:'ArrowLeft',shiftKey:true});e.dispose();
  const second=setup();assert.equal(second.width(),400);const drawer=setup('drawer');assert.equal(drawer.width(),500);
});
test('container resize clamps without overwriting preferred larger width',()=>{
  const e=setup('dock','700');e.available(800);observers[0].cb();assert.equal(e.width(),480);
  e.available(1200);observers[0].cb();assert.equal(e.width(),700);assert.equal(window.localStorage.getItem(e.key),'700');
});
test('disposed gesture releases capture, cancels frames and cannot revive from callbacks',()=>{
  const e=setup();e.event('pointerdown');e.event('pointermove',{clientX:300});const late=[...frames.values()][0], observer=observers[0];
  e.dispose();assert.equal(e.captured.size,0);assert.equal(frames.size,0);assert.equal(observer.stopped,true);
  assert.equal(e.host.style.getPropertyValue('--document-preview-width'),'');late(0);observer.cb();
  assert.equal(e.host.style.getPropertyValue('--document-preview-width'),'');assert.equal(e.host.dataset.previewResizing,undefined);
});
test('invalid saved preferences use defaults and inaccessible storage is optional',()=>{
  assert.equal(setup('dock','not-a-width').width(),320);const get=window.Storage.prototype.getItem,set=window.Storage.prototype.setItem;
  window.Storage.prototype.getItem=()=>{throw new Error('denied');};window.Storage.prototype.setItem=()=>{throw new Error('denied');};
  try{const e=setup('drawer');e.event('keydown',{key:'ArrowLeft'});assert.equal(e.width(),524);}finally{window.Storage.prototype.getItem=get;window.Storage.prototype.setItem=set;}
});

test('wiki navigation reserves readable main space and has its own bounds',()=>{
  assert.deepEqual(previewResizeLimits('wiki-navigation',1100),{min:188,max:560,defaultWidth:188});
  assert.deepEqual(previewResizeLimits('wiki-navigation',700),{min:188,max:220,defaultWidth:188});
  assert.equal(previewResizeLimits('wiki-navigation',390).max,0);
});
test('wiki divider expands to the right, coalesces moves and stores only its width',()=>{
  const e=setup('wiki-navigation');e.event('pointerdown');
  for(let i=1;i<=100;i++)e.event('pointermove',{clientX:700+i*2});
  assert.equal(frames.size,1);assert.equal(e.width(),188);flush();assert.equal(e.width(),388);
  e.event('pointerup',{clientX:930});assert.equal(e.width(),418);
  assert.equal(e.host.style.getPropertyValue('--wiki-navigation-width'),'418px');
  assert.equal(e.host.style.getPropertyValue('--document-preview-width'),'');
  assert.equal(window.localStorage.getItem(e.key),'418');
  assert.equal(e.captured.size,0);assert.equal(e.host.dataset.previewResizing,undefined);
});
test('wiki text reflow height changes cannot cancel an ongoing drag',()=>{
  const e=setup('wiki-navigation');e.event('pointerdown');e.event('pointermove',{clientX:880});flush();
  observers[0].cb(); // Width unchanged: the resize notification came from height reflow.
  assert.equal(e.captured.size,1);assert.equal(e.host.dataset.previewResizing,'true');
  e.event('pointermove',{clientX:910});flush();e.event('pointerup',{clientX:930});
  assert.equal(e.width(),418);
});
test('wiki width changes clamp without erasing the larger saved preference',()=>{
  const e=setup('wiki-navigation','460');e.available(800);observers[0].cb();
  assert.equal(e.width(),320);assert.equal(window.localStorage.getItem(e.key),'460');
  e.available(1100);observers[0].cb();assert.equal(e.width(),460);
  e.event('keydown',{key:'ArrowLeft'});assert.equal(e.width(),436);
  e.event('keydown',{key:'ArrowRight',shiftKey:true});assert.equal(e.width(),516);
  e.event('dblclick');assert.equal(e.width(),188);
});
test('wiki preference restores independently and escape cancels without persisting',()=>{
  const e=setup('wiki-navigation','400');e.event('pointerdown');e.event('pointermove',{clientX:950});flush();
  e.event('keydown',{key:'Escape'});assert.equal(e.width(),400);assert.equal(window.localStorage.getItem(e.key),'400');
  e.dispose();assert.equal(e.host.style.getPropertyValue('--wiki-navigation-width'),'');
  assert.equal(setup('wiki-navigation').width(),400);assert.equal(setup('dock').width(),320);
});

test('document categories reserve list space and additional room for an inline preview',()=>{
  assert.deepEqual(previewResizeLimits('document-navigation',1100),{min:184,max:520,defaultWidth:184});
  assert.deepEqual(previewResizeLimits('document-navigation',1100,true),{min:184,max:500,defaultWidth:184});
  assert.deepEqual(previewResizeLimits('document-navigation',800),{min:184,max:320,defaultWidth:184});
  assert.equal(previewResizeLimits('document-navigation',390).max,0);
});
test('document category drag expands right with one frame for 100 moves and no preview preference changes',()=>{
  const e=setup('document-navigation');e.event('pointerdown');
  for(let i=1;i<=100;i++)e.event('pointermove',{clientX:700+i*2});
  assert.equal(frames.size,1);assert.equal(e.width(),184);flush();assert.equal(e.width(),384);
  observers[0].cb();assert.equal(e.captured.size,1,'height-only reflow must not cancel dragging');
  e.event('pointerup',{clientX:950});assert.equal(e.width(),434);
  assert.equal(e.host.style.getPropertyValue('--document-category-width'),'434px');
  assert.equal(window.localStorage.getItem(e.key),'434');
  assert.equal(window.localStorage.getItem('test-width:dock'),null);
  assert.equal(e.host.style.getPropertyValue('--document-preview-width'),'');
});
test('document divider supports shrink, keyboard, reset, remount and cancellation',()=>{
  const e=setup('document-navigation','400');e.event('keydown',{key:'ArrowLeft'});assert.equal(e.width(),376);
  e.event('keydown',{key:'ArrowRight',shiftKey:true});assert.equal(e.width(),456);
  e.event('pointerdown');e.event('pointermove',{clientX:600});flush();assert.equal(e.width(),356);
  e.event('keydown',{key:'Escape'});assert.equal(e.width(),456);
  e.dispose();assert.equal(e.host.style.getPropertyValue('--document-category-width'),'');
  const next=setup('document-navigation');assert.equal(next.width(),456);
  next.event('dblclick');assert.equal(next.width(),184);
  assert.equal(window.localStorage.getItem(next.key),'184');
});
test('document divider clamps when the companion opens and restores preference when it closes',async()=>{
  const e=setup('document-navigation','520',true);
  assert.equal(e.width(),520);
  const preview=document.createElement('aside');preview.className='document-inspector';
  e.workspace.append(preview);e.workspace.classList.remove('no-overview');
  await Promise.resolve();assert.equal(e.width(),500);
  assert.equal(window.localStorage.getItem(e.key),'520');
  e.workspace.classList.add('no-overview');await Promise.resolve();assert.equal(e.width(),520);
  e.available(800);observers[0].cb();assert.equal(e.width(),320);
  e.available(1100);observers[0].cb();assert.equal(e.width(),520);
  e.dispose();e.workspace.classList.remove('no-overview');await Promise.resolve();
  assert.equal(e.host.style.getPropertyValue('--document-category-width'),'');
});
