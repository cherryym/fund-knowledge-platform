import { useLayoutEffect, useRef } from "react";
import { DotsSixVertical } from "@phosphor-icons/react";
import { attachPreviewResize } from "./previewResize";

export function WikiNavigationResizeHandle({ownerId,spaceId,panelId}:{ownerId:string;spaceId:string;panelId:string}) {
  const ref = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    const handle = ref.current, host = handle?.closest<HTMLElement>(".wiki-workbench");
    if (!handle || !host) return;
    return attachPreviewResize(handle,host,"wiki-navigation",`fkb:wiki-navigation-width:v1:${ownerId}:${spaceId}`);
  },[ownerId,spaceId]);
  return <div ref={ref} className="wiki-navigation-resize" role="separator" tabIndex={0}
    aria-label="调整分类目录宽度" aria-orientation="vertical" aria-controls={panelId}
    aria-valuemin={188} aria-valuemax={560} aria-valuenow={188}
    title="向右拖动加宽目录；双击恢复默认；左右方向键微调"><DotsSixVertical aria-hidden="true"/></div>;
}
