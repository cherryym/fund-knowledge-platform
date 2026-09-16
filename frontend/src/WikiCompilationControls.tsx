import { get } from "./api";
import { ErrorBox, Field, Loading, useLoad } from "./ui";
import type { WikiCompilationType } from "./knowledgeTypes";
import "./wiki-maintenance.css";

export const compilationLabels: Record<WikiCompilationType, string> = {
  topic: "专题综述", atomic_rule: "原子规则", scenario: "场景方案", sop: "标准作业程序（SOP）",
};
type Spec = { compilation_type: WikiCompilationType; purpose: string;
  sections: { key: string; label: string; requirement: string }[] };

export function WikiCompilationControls({ value, onChange, disabled }: {
  value: WikiCompilationType; onChange: (type: WikiCompilationType) => void; disabled?: boolean;
}) {
  const specs = useLoad(signal => get<{spec_version: string; types: Spec[]}>("/wiki/compilation-specs", signal), []);
  const spec = specs.data?.types?.find(item => item.compilation_type === value);
  return <div className="wiki-compilation-controls">
    <Field label="知识编译类型" hint="按完整业务结构编译，不统一限制为短卡片；缺少依据的部分会保留为待核验事项。">
      <select aria-label="知识编译类型" value={value} disabled={disabled}
        onChange={event => onChange(event.target.value as WikiCompilationType)}>
        {Object.entries(compilationLabels).map(([key, label]) => <option key={key} value={key}>{label}</option>)}
      </select>
    </Field>
    {specs.loading && <Loading label="读取编译规范…" />}
    <ErrorBox error={specs.error} retry={specs.reload} />
    {spec && <details className="wiki-compilation-spec"><summary>查看本类型的完整结构要求</summary>
      <p>{spec.purpose}</p><ol>{spec.sections.map(section => <li key={section.key}>
        <strong>{section.label}</strong>：{section.requirement}</li>)}</ol>
      <small>规范版本 {specs.data!.spec_version} · 结构检查不代表专业核验</small>
    </details>}
  </div>;
}
