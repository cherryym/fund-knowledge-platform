import assert from "node:assert/strict";
import {test, after} from "node:test";
import {createRequire, Module} from "node:module";
import {dirname, resolve} from "node:path";
import {fileURLToPath} from "node:url";
import {build, stop} from "esbuild";
const require = createRequire(import.meta.url);
const React = require("react");
const {renderToStaticMarkup} = require("react-dom/server");
const sourceDir = dirname(fileURLToPath(import.meta.url));
const built = await build({entryPoints:[resolve(sourceDir,"RetrievalDiagnostics.tsx")],bundle:true,
  platform:"node",format:"cjs",write:false,logLevel:"silent",external:["react","react/jsx-runtime"]});
const runtime = new Module(resolve(sourceDir,"retrieval-diagnostics-runtime.cjs"));
runtime.filename=resolve(sourceDir,"retrieval-diagnostics-runtime.cjs");
runtime.paths=Module._nodeModulePaths(sourceDir);
runtime._compile(built.outputFiles[0].text,runtime.filename);
const {RetrievalDiagnostics,AnswerTimingDetails}=runtime.exports;
after(()=>stop());
const draw=(component,props)=>renderToStaticMarkup(React.createElement(component,props));
const trace={version:"candidate_lineage_v1",candidate_count:160,reranked_count:160,unscored_count:0,
  candidate_policy:"complete_pool",channel_counts:{vector:80,bm25:80},phases_ms:{reranking_ms:2500},
  timing_scope:"shared_batch"};
test("full-pool observation is not presented as business accuracy",()=>{
  const html=draw(RetrievalDiagnostics,{trace});
  assert.match(html,/完整融合候选池重排/);assert.match(html,/未评分：0/);
  assert.match(html,/不是全库召回率或专业准确率/);assert.match(html,/不能逐问题相加/);
});
test("unscored tail and absent channel are visible",()=>{
  const html=draw(RetrievalDiagnostics,{trace:{...trace,candidate_policy:"ranked_prefix",reranked_count:80,unscored_count:80}});
  assert.match(html,/未评分：80/);assert.match(html,/目录加权：未记录/);
});
test("missing or invalid times never become measured zero",()=>{
  const html=draw(RetrievalDiagnostics,{trace:{...trace,phases_ms:{reranking_ms:NaN}}});
  assert.match(html,/未测量/);assert.doesNotMatch(html,/NaN|0\.000 秒/);
});
test("arbitrary raw question and source props are not rendered",()=>{
  const html=draw(RetrievalDiagnostics,{trace:{...trace,query:"PRIVATE_QUERY",units:[{text:"PRIVATE_BODY"}]}});
  assert.doesNotMatch(html,/PRIVATE/);
});
test("end-to-end timing distinguishes unobserved first visible answer",()=>{
  const html=draw(AnswerTimingDetails,{timing:{version:"answer_timing_v1",execution_elapsed_ms:12345,
    phases:{model_planning:{elapsed_ms:10000,calls:1}},first_visible_answer_ms:null,first_visible_answer_status:"NOT_MEASURED"}});
  assert.match(html,/12\.345 秒/);assert.match(html,/模型问题研判/);
  assert.match(html,/未测量，不能用作完整答复耗时/);assert.match(html,/网络、服务商排队/);
});
