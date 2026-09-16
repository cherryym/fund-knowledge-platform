/** Local User Timing only: no telemetry, payloads, user IDs or external calls. */
export function wikiOpenStart() {
  if(typeof performance.mark==='function') {performance.clearMarks('fundkb-wiki-open');performance.mark('fundkb-wiki-open');}
}
export function wikiOpenTime() {
  return performance.getEntriesByName('fundkb-wiki-open').at(-1)?.startTime ?? 0;
}
export function observeWikiReady(root:HTMLElement, expected:{pages:number;readers:number;nodes:number;mode:'pages'|'graph'},
  start:number,kind:'open'|'switch',done?:()=>void) {
  let scheduled=false,stopped=false,firstFrame=0,secondFrame=0;
  const complete=()=>{
    if(root.querySelector('.loading'))return false;
    if(expected.mode==='graph') return expected.nodes===0 || Number(root.querySelector<HTMLElement>('[data-star-frames]')?.dataset.starFrames)>0;
    return root.querySelectorAll('.wiki-page-index > ul > li').length===expected.pages
      && expected.readers>=expected.pages && (expected.pages===0 || !!root.querySelector('.wiki-connection-grid'));
  };
  const check=()=>{
    if(stopped||scheduled||!complete())return;
    scheduled=true;
    firstFrame=requestAnimationFrame(()=>{secondFrame=requestAnimationFrame(()=>{
      if(stopped)return;
      if(!complete()){scheduled=false;return;}
      const name=`fundkb-wiki-${kind}`;
      performance.clearMeasures(name);
      performance.measure(name,{start,end:performance.now(),detail:{...expected,complete:true}});
      done?.();
      cleanup();
    });});
  };
  const observer=new MutationObserver(check);
  const timer=setTimeout(()=>cleanup(),20000);
  function cleanup(){stopped=true;observer.disconnect();clearTimeout(timer);cancelAnimationFrame(firstFrame);cancelAnimationFrame(secondFrame);}
  observer.observe(root,{subtree:true,childList:true,attributes:true});check();
  return cleanup;
}
