import {useEffect,useRef,useState,useSyncExternalStore} from 'react';
import {freshWikiEntry,loadWikiEntry,readWikiEntry,subscribeWikiCache,wikiCacheGeneration,WIKI_SNAPSHOT_TTL} from './wikiSessionCache';

export function useWikiSnapshot<T>(key:string, token:string, loader:(signal:AbortSignal)=>Promise<T>) {
  const generation=useSyncExternalStore(subscribeWikiCache,wikiCacheGeneration,wikiCacheGeneration);
  const identity=`${generation}:${key}:${token}`;
  const [nonce,setNonce]=useState(0), seenNonce=useRef(0);
  type State={identity:string;data?:T;loading:boolean;refreshing:boolean;error?:Error};
  const initial=():State=>{const entry=readWikiEntry<T>(key), data=entry?.token===token?entry.data:undefined;
    return {identity,data,loading:data===undefined,refreshing:false,error:entry?.token===token?entry.error:undefined};};
  const [state,setState]=useState<State>(initial);
  useEffect(()=>{
    let mounted=true;
    const forced=nonce!==seenNonce.current;seenNonce.current=nonce;
    const read=async(force=false)=>{
      const old=readWikiEntry<T>(key), data=old?.token===token?old.data:undefined;
      if(!force&&freshWikiEntry(old,token)){if(mounted)setState({identity,data,loading:false,refreshing:false});return;}
      if(mounted)setState({identity,data,loading:data===undefined,refreshing:true});
      try{const value=await loadWikiEntry(key,token,loader,force);
        if(mounted)setState({identity,data:value,loading:false,refreshing:false});
      }catch(error){if(mounted)setState({identity,data:readWikiEntry<T>(key)?.data,loading:false,refreshing:false,
        error:error instanceof Error?error:new Error(String(error))});}
    };
    void read(forced);
    const check=()=>{if(document.visibilityState==='visible'&&!freshWikiEntry(readWikiEntry(key),token))void read();};
    const timer=setInterval(check,WIKI_SNAPSHOT_TTL);
    window.addEventListener('focus',check);document.addEventListener('visibilitychange',check);
    return()=>{mounted=false;clearInterval(timer);window.removeEventListener('focus',check);document.removeEventListener('visibilitychange',check);};
    // Route exits retain the already-authorized in-flight read; StrictMode or a
    // quick return joins it instead of causing a second full database traversal.
  },[identity,nonce]);
  return {...(state.identity===identity?state:initial()),reload:()=>setNonce(n=>n+1)};
}
