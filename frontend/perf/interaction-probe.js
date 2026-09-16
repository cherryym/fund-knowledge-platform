// CDP Runtime.evaluate expression. No application state/global variables are changed.
// Call the returned finish() once: every observer/listener/frame is removed.
(() => {
  const surface = document.querySelector('.star-surface');
  if (!surface) throw new Error('Graph is not mounted');
  const isCanvas = surface.tagName.toLowerCase() === 'canvas';
  const view = surface.querySelector(':scope > g:not(.star-dust)');
  // Optional 300-node fixture trace: the dragged node and its four actual neighbors.
  // This samples only five existing transforms, not a full SVG scan every frame.
  const trackedNodes = [250, 233, 249, 251, 267].map(id => surface.querySelector(`[data-star-node="perf-${id}"]`)).filter(Boolean);
  const started = performance.now();
  const frameGaps = [], cameraGaps = [], geometryGaps = [], drawGaps = [], wheelTimes = [], eventTimes = [], longTasks = [];
  let previousDrawTime = null, previousDraw = surface.dataset.starFrames || '0';
  let previousGeometryTime = null, previousGeometry = '', geometryFrames = 0;
  let previousFrame = null, previousCameraTime = null, previousCamera = '', raf = 0;
  let attributeWrites = 0, textWrites = 0, wheelStarted = 0, wheelEvents = 0, cameraFrames = 0;
  const records = entries => { for (const e of entries) e.type === 'attributes' ? attributeWrites++ : textWrites++; };
  const mutation = new MutationObserver(records);
  mutation.observe(surface, { attributes:true, childList:true, characterData:true, subtree:true });
  const wheelStart = () => { wheelStarted = performance.now(); wheelEvents++; };
  const wheelEnd = () => { wheelTimes.push(performance.now() - wheelStarted); };
  surface.addEventListener('wheel', wheelStart, {capture:true,passive:true});
  surface.addEventListener('wheel', wheelEnd, {passive:true});
  const longObserver = new PerformanceObserver(list => {
    for (const e of list.getEntries()) longTasks.push({start:e.startTime-started,duration:e.duration});
  });
  longObserver.observe({type:'longtask', buffered:false});
  let eventObserver;
  try {
    eventObserver = new PerformanceObserver(list => {
      for (const e of list.getEntries()) if (e.target && surface.contains(e.target))
        eventTimes.push({name:e.name,duration:e.duration,processing:e.processingEnd-e.processingStart,interactionId:e.interactionId});
    });
    eventObserver.observe({type:'event',durationThreshold:16,buffered:false});
  } catch { /* Unsupported Event Timing remains unavailable, never reported as zero latency. */ }
  const sample = time => {
    if (previousFrame!==null) frameGaps.push(time-previousFrame);
    previousFrame = time;
    const camera = surface.dataset.starCamera || view?.getAttribute('transform') || '';
    if (camera!==previousCamera) {
      if (previousCameraTime!==null) cameraGaps.push(time-previousCameraTime);
      previousCameraTime=time;previousCamera=camera;cameraFrames++;
    }
    const geometry = isCanvas ? surface.dataset.starGeometry || '' : trackedNodes.map(node => node.getAttribute('transform')).join('|');
    if (geometry && geometry !== previousGeometry) {
      if (previousGeometryTime !== null) geometryGaps.push(time - previousGeometryTime);
      previousGeometryTime = time; previousGeometry = geometry; geometryFrames++;
    }
    const draw = surface.dataset.starFrames || '0';
    if (isCanvas && draw !== previousDraw) {
      if (previousDrawTime !== null) drawGaps.push(time - previousDrawTime);
      previousDrawTime = time; previousDraw = draw;
    }
    raf=requestAnimationFrame(sample);
  };
  raf=requestAnimationFrame(sample);
  const summary = values => {
    const sorted=values.slice().sort((a,b)=>a-b);
    return {count:sorted.length,median:sorted.length?sorted[Math.floor(sorted.length*.5)]:null,
      p95:sorted.length?sorted[Math.floor(sorted.length*.95)]:null,max:sorted.at(-1)??null,
      over33:sorted.filter(x=>x>33.4).length,over50:sorted.filter(x=>x>50).length};
  };
  let finished;
  return { finish() {
    if (finished) return finished;
    cancelAnimationFrame(raf);records(mutation.takeRecords());mutation.disconnect();
    surface.removeEventListener('wheel',wheelStart,true);surface.removeEventListener('wheel',wheelEnd);
    longObserver.disconnect();eventObserver?.disconnect();
    finished={elapsedMs:performance.now()-started,wheelEvents,wheelHandlerMs:summary(wheelTimes),
      frameGapMs:summary(frameGaps),cameraChangeGapMs:summary(cameraGaps),cameraFrames,
      geometryChangeGapMs:summary(geometryGaps),geometryFrames,drawChangeGapMs:summary(drawGaps),
      renderer:surface.dataset.starRenderer || 'svg',physics:surface.dataset.starPhysics || 'main-thread',
      trackedNodeIds:isCanvas ? JSON.parse(surface.dataset.starSamples || '[]').map(n=>n.id) : trackedNodes.map(node => node.dataset.starNode),
      attributeWrites,textWrites,eventTimings:eventTimes,longTasks,
      nodeCount:isCanvas ? Number(surface.dataset.starDrawNodes || 0) : surface.querySelectorAll('[data-star-node]').length,
      edgeCount:isCanvas ? Number(surface.dataset.starDrawEdges || 0) : surface.querySelectorAll('[data-star-edge]').length,
      camera:previousCamera,phase:surface.dataset.starPhase,viewport:[innerWidth,innerHeight]};
    return finished;
  }};
})()
