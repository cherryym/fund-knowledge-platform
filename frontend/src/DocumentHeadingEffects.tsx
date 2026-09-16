import { useEffect, useRef, useState, type RefObject } from "react";
import { createPortal } from "react-dom";
import { Sparkle } from "@phosphor-icons/react";
import { useGSAP } from "@gsap/react";
import gsap from "gsap";
import { useMotionPreferences, type MotionLevel } from "./motionPreferences";
import "./document-heading-effects.css";

gsap.registerPlugin(useGSAP);
export function DocumentHeadingEffects({headingRef}: {headingRef: RefObject<HTMLElement | null>}) {
  const layerRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [heading, setHeading] = useState<HTMLElement | null>(null);
  const [controlsHost, setControlsHost] = useState<HTMLElement | null>(null);
  const preferences = useMotionPreferences();
  useEffect(() => {
    setHeading(headingRef.current);
    setControlsHost(headingRef.current?.closest(".app-shell")?.querySelector<HTMLElement>(".topbar-right") ?? null);
  }, [headingRef]);
  useGSAP(() => {
    const layer = layerRef.current, canvas = canvasRef.current;
    if (!heading || !layer || !canvas) return;
    layer.dataset.running = "false";
    if (preferences.effective === "quiet") { canvas.width = canvas.height = 1; return; }
    const title = heading.querySelector<HTMLElement>("h1");
    if (!title) return;
    const oldMarker = title.getAttribute("data-ambient-title-active");
    title.setAttribute("data-ambient-title-active", "");
    const timeline = gsap.timeline({paused:true,repeat:-1,yoyo:true,defaults:{ease:"sine.inOut"}});
    timeline.addLabel("heading-light",0);
    timeline.fromTo(title,{"--ambient-title-position":"0%"},{"--ambient-title-position":"100%",duration:10},"heading-light");
    timeline.fromTo(layer.querySelector(".heading-aurora-blue"),{xPercent:-8,yPercent:-8,scale:.96},{xPercent:10,yPercent:8,scale:1.08,duration:14},"heading-light");
    timeline.fromTo(layer.querySelector(".heading-aurora-violet"),{xPercent:7,yPercent:8,scale:1.04},{xPercent:-10,yPercent:-6,scale:.98,duration:16},"heading-light");
    let context: CanvasRenderingContext2D | null = null;
    try { context = canvas.getContext("2d",{alpha:true}); } catch { /* Light/title still work without canvas. */ }
    const count = preferences.effective === "rich" ? 16 : 10;
    const points = Array.from({length:count},(_,i)=>({x:((i*71)%count+.5)/count,y:((i*7)%11+1)/13,phase:i*1.9}));
    let width=1,height=1,elapsed=0,last=0,running=false,visible=true,disposed=false,frames=0;
    const draw = () => {
      if (!context) return;
      context.clearRect(0,0,width,height);
      const positions=points.map(point=>({x:point.x*width+Math.sin(elapsed*.14+point.phase)*10,y:point.y*height+Math.cos(elapsed*.2+point.phase)*8}));
      let lines=0;
      context.lineWidth=.65;
      for(let i=0;i<positions.length;i++){
        const a=positions[i];
        for(let j=i+1;j<positions.length && lines<24;j++){
          const b=positions[j],distance=Math.hypot(a.x-b.x,a.y-b.y);
          if(distance<180){context.strokeStyle=`rgba(86,123,184,${.16*(1-distance/180)})`;context.beginPath();context.moveTo(a.x,a.y);context.lineTo(b.x,b.y);context.stroke();lines++;}
        }
        context.fillStyle="rgba(66,113,186,.3)";context.beginPath();context.arc(a.x,a.y,1.7,0,Math.PI*2);context.fill();
      }
      layer.dataset.frames=String(++frames);
    };
    const resize = () => {
      if(disposed)return;
      width=Math.max(1,heading.clientWidth);height=Math.max(1,heading.clientHeight);
      const ratio=Math.min(1.5,window.devicePixelRatio||1,Math.sqrt(600000/(width*height)));
      canvas.width=Math.round(width*ratio);canvas.height=Math.round(height*ratio);context?.setTransform(ratio,0,0,ratio,0,0);draw();
    };
    const tick = () => {
      if(disposed||!running)return;
      const now=performance.now();if(now-last<1000/24)return;
      elapsed+=Math.min(.08,(now-last)/1000);last=now;draw();
    };
    const update = () => {
      if(disposed)return;
      const next=document.visibilityState!=="hidden"&&document.hasFocus()&&visible&&heading.isConnected;
      if(next===running)return;
      running=next;layer.dataset.running=String(next);
      if(next){last=performance.now();timeline.play();gsap.ticker.add(tick);}else{timeline.pause();gsap.ticker.remove(tick);}
    };
    const observer=typeof window.IntersectionObserver==='undefined'?null:new window.IntersectionObserver(entries=>{if(disposed)return;for(const entry of entries)if(entry.target===heading)visible=entry.isIntersecting;update();});
    observer?.observe(heading);
    const resizeObserver=typeof window.ResizeObserver==='undefined'?null:new window.ResizeObserver(resize);resizeObserver?.observe(heading);
    window.addEventListener('resize',resize);window.addEventListener('focus',update);window.addEventListener('blur',update);document.addEventListener('visibilitychange',update);
    resize();update();
    return () => {
      disposed=true;running=false;gsap.ticker.remove(tick);timeline.kill();observer?.disconnect();resizeObserver?.disconnect();
      window.removeEventListener('resize',resize);window.removeEventListener('focus',update);window.removeEventListener('blur',update);document.removeEventListener('visibilitychange',update);
      context?.clearRect(0,0,width,height);layer.dataset.running='false';
      if(oldMarker===null)title.removeAttribute('data-ambient-title-active');else title.setAttribute('data-ambient-title-active',oldMarker);
    };
  },{scope:layerRef,dependencies:[heading,preferences.effective],revertOnUpdate:true});
  return <><div ref={layerRef} className="document-heading-effects" data-motion-level={preferences.effective} aria-hidden="true">
    <div className="heading-aurora heading-aurora-blue"/><div className="heading-aurora heading-aurora-violet"/><canvas ref={canvasRef}/>
  </div>{controlsHost&&createPortal(<div className="ambient-controls" role="group" aria-label="界面动效档位" title="装饰仅限标题区，不影响文档内容">
    <Sparkle size={17} aria-hidden="true"/><label><span className="ambient-controls-label">动效</span><select aria-label="界面动效档位" value={preferences.level} onChange={event=>preferences.setLevel(event.target.value as MotionLevel)}><option value="rich">丰富</option><option value="balanced">均衡</option><option value="quiet">静谧</option></select></label>{preferences.reduced&&<small role="status">系统静谧</small>}
  </div>,controlsHost)}</>;
}
