import assert from "node:assert/strict";
import {test, after} from "node:test";
import {createRequire, Module} from "node:module";
import {dirname, resolve} from "node:path";
import {fileURLToPath} from "node:url";
import {build, stop} from "esbuild";
const require=createRequire(import.meta.url);
const React=require("react");
const {renderToStaticMarkup}=require("react-dom/server");
const sourceDir=dirname(fileURLToPath(import.meta.url));
const built=await build({entryPoints:[resolve(sourceDir,"EvidenceReview.tsx")],bundle:true,
  platform:"node",format:"cjs",write:false,logLevel:"silent",external:["react","react/jsx-runtime"]});
const runtime=new Module(resolve(sourceDir,"evidence-review-runtime.cjs"));
runtime.filename=resolve(sourceDir,"evidence-review-runtime.cjs");runtime.paths=Module._nodeModulePaths(sourceDir);
runtime._compile(built.outputFiles[0].text,runtime.filename);
after(()=>stop());
const draw=report=>renderToStaticMarkup(React.createElement(runtime.exports.EvidenceReviewNotice,{report}));
const report={version:"evidence_review_v1",status:"REVIEW_REQUIRED",semantic_entailment:"NOT_EVALUATED",
  issues:[{code:"ANSWER_EVENT_DAY_CONFLICT",message:"原文日期与答复不同",severity:"critical",evidence_ids:["E2","E3"]}]};
test("critical structural findings appear without a collapsed disclosure",()=>{
  const html=draw(report);assert.match(html,/勿直接执行/);assert.match(html,/原文日期与答复不同/);
  assert.match(html,/E2、E3/);assert.doesNotMatch(html,/<details/);
});
test("no findings never produce a professional PASS badge",()=>{
  assert.equal(draw({...report,issues:[],status:"NO_STRUCTURAL_FINDING"}),"");assert.equal(draw(undefined),"");
});
test("a current read projection is clearly not a regenerated answer",()=>{
  assert.match(draw({...report,observation_origin:"current_authorized_read_projection"}),/未重写历史答案或重新调用模型/);
});
test("source derived labels remain inert text",()=>{
  const html=draw({...report,issues:[{...report.issues[0],message:'<img src=x onerror="bad()">'}]});
  assert.doesNotMatch(html,/<img/);assert.match(html,/&lt;img/);
});
test("ordinary source comparisons are not styled as certified conflicts",()=>{
  assert.match(draw({...report,issues:[{...report.issues[0],severity:"warning"}]}),/来源适用边界仍需核对/);
});
